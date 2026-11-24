"""LinkedIn sending (beta): off by default, sent by the member's own extension,
at most once per draft, under daily caps."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services import linkedin_sender as li

USER = "8f7c2b5e-6a0d-4f7e-9d6c-0b1a2c3d4e5f"
URL = "https://www.linkedin.com/in/priya/"


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    async def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    async def decr(self, key):
        self.data[key] = int(self.data.get(key, 0)) - 1
        return self.data[key]

    async def get(self, key):
        return self.data.get(key)

    async def exists(self, key):
        return int(key in self.data)

    async def expire(self, key, seconds):
        return True


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch.object(li.redis_client, "get_redis", return_value=fake):
        yield fake


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_OUTREACH_ENABLED", True)
    monkeypatch.setattr(settings, "LINKEDIN_INVITES_PER_DAY", 10)
    monkeypatch.setattr(settings, "LINKEDIN_MESSAGES_PER_DAY", 25)


def test_off_by_default():
    assert settings.LINKEDIN_OUTREACH_ENABLED is False
    assert not li.enabled()


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        (
            "https://www.linkedin.com/in/priya-sharma-123",
            "https://www.linkedin.com/in/priya-sharma-123/",
        ),
        ("https://in.linkedin.com/in/rahul_v/?trk=x", "https://www.linkedin.com/in/rahul_v/"),
        ("http://www.linkedin.com/in/priya", None),
        ("https://www.linkedin.com/company/acme", None),
        ("https://evil.example/in/priya", None),
        (None, None),
    ],
)
def test_profile_url(url, canonical):
    assert li.profile_url(url) == canonical


# ── One send through the extension ──────────────────────────────


def commands(*answers):
    """Stand-in for the device command channel; records each command body."""
    sent = []

    async def command(device, body):
        sent.append(body)
        answer = answers[len(sent) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return patch.object(li, "_command", command), sent


SECOND = {"degree": "2nd", "can_connect": True, "can_message": False, "pending": False}
FIRST = {"degree": "1st", "can_connect": False, "can_message": True, "pending": False}


@pytest.mark.asyncio
async def test_first_degree_gets_a_message(on, redis):
    patcher, sent = commands(FIRST, {"delivery": "messaged"})
    with patcher:
        assert await li.deliver(USER, "dev1", URL, "Hi Priya") == ("messaged", None)
    assert sent[0] == {"op": "probe", "profile_url": URL}
    assert sent[1] == {"op": "send", "as": "message", "profile_url": URL, "message": "Hi Priya"}


@pytest.mark.asyncio
async def test_others_get_an_invite_with_the_note(on, redis):
    patcher, sent = commands(SECOND, {"delivery": "invited"})
    with patcher:
        assert await li.deliver(USER, "dev1", URL, "x" * 300) == ("invited", None)
    assert sent[1]["as"] == "invite" and sent[1]["message"] == "x" * 300


@pytest.mark.asyncio
async def test_a_note_over_the_limit_is_refused_never_cut(on, redis):
    patcher, sent = commands(SECOND)
    with patcher:
        delivery, error = await li.deliver(USER, "dev1", URL, "x" * 301)
    assert delivery == "failed" and "300" in error
    assert len(sent) == 1  # probe only: no send, no cap used
    assert not any(k.startswith("li:invite:") for k in redis.data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe", "error"),
    [
        ({"signed_out": True}, "Sign in to LinkedIn"),
        ({"not_found": True}, "could not be opened"),
        ({**SECOND, "pending": True}, "already pending"),
        ({"degree": "3rd", "can_connect": False, "can_message": False}, "neither"),
    ],
)
async def test_probe_refusals_send_nothing(on, redis, probe, error):
    patcher, sent = commands(probe)
    with patcher:
        delivery, message = await li.deliver(USER, "dev1", URL, "Hi")
    assert delivery == "failed" and error in message
    assert len(sent) == 1  # probe only
    assert redis.data == {}  # no cap used


@pytest.mark.asyncio
async def test_unreachable_browser_on_probe_is_a_plain_failure(on, redis):
    patcher, sent = commands(HTTPException(504, "Your browser didn't respond in time."))
    with patcher:
        assert (await li.deliver(USER, "dev1", URL, "Hi"))[0] == "failed"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_daily_cap_stops_before_the_send(on, redis, monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_INVITES_PER_DAY", 0)
    patcher, sent = commands(SECOND)
    with patcher:
        assert (await li.deliver(USER, "dev1", URL, "Hi"))[0] == "limit_reached"
    assert [c["op"] for c in sent] == ["probe"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "delivery"),
    [
        ({"reason": "limit"}, "limit_reached"),
        ({"reason": "unconfirmed"}, "outcome_unknown"),
        ({"error": "LinkedIn did not offer a note; nothing was sent."}, "failed"),
        (HTTPException(504, "Your browser didn't respond in time."), "outcome_unknown"),
        (
            HTTPException(502, "The browser could not complete the LinkedIn step."),
            "outcome_unknown",
        ),
        (HTTPException(422, "Invalid message"), "failed"),
    ],
)
async def test_send_outcomes(on, redis, answer, delivery):
    patcher, _ = commands(SECOND, answer)
    with patcher:
        assert (await li.deliver(USER, "dev1", URL, "Hi"))[0] == delivery


# ── At most once per draft ──────────────────────────────────────


def queue_db(item):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = item
    db.get = AsyncMock(return_value=item)
    db.commit = AsyncMock()
    return db


def item(delivery=None, url="https://www.linkedin.com/in/priya"):
    return SimpleNamespace(
        message="Hi Priya",
        contact_linkedin_url=url,
        delivery=delivery,
        delivery_error=None,
        status="pending_approval",
        approved_at=None,
        delivery_started_at=None,
        sent_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["dev1", None])
@pytest.mark.parametrize("delivery", ["sending", "invited", "messaged", "outcome_unknown"])
async def test_sent_or_maybe_sent_items_are_never_sent_again(delivery, device):
    row = item(delivery)
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock()) as deliver,
    ):
        assert await li.send_item(USER, device, uuid.uuid4()) == delivery
    assert row.delivery == delivery
    deliver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", [None, "failed", "limit_reached"])
async def test_unsent_items_can_be_sent(delivery):
    row = item(delivery)
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock(return_value=("invited", None))) as deliver,
    ):
        assert await li.send_item(USER, "dev1", uuid.uuid4()) == "invited"
    deliver.assert_awaited_once_with(USER, "dev1", URL, "Hi Priya")
    assert row.status == "sent" and row.sent_at is not None
    # The send clock is separate: approved_at stays the member's approval time.
    assert row.approved_at is None and row.delivery_started_at is not None


@pytest.mark.asyncio
async def test_contact_without_profile_link_is_not_sent():
    row = item(url="https://www.linkedin.com/company/acme")
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock()) as deliver,
    ):
        assert await li.send_item(USER, "dev1", uuid.uuid4()) == "failed"
    deliver.assert_not_called()


# ── A batch ─────────────────────────────────────────────────────


def run_session(run):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get = AsyncMock(return_value=run)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_batch_records_each_result_and_clears_the_flag(redis):
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    run = SimpleNamespace(output={"queue_ids": ids, "sending": True, "deliveries": {}})
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=run_session(run)),
        patch.object(li, "device_for", AsyncMock(side_effect=["dev1", None])),
        patch.object(li, "send_item", AsyncMock(side_effect=["invited", "failed"])) as send,
    ):
        await li.send_run(USER, uuid.uuid4())
    send.assert_awaited_with(USER, None, uuid.UUID(ids[1]))
    assert run.output["deliveries"] == {ids[0]: "invited", ids[1]: "failed"}
    assert run.output["sending"] is False
    assert redis.data == {}  # lock released


@pytest.mark.asyncio
async def test_one_batch_at_a_time(redis):
    run_id = uuid.uuid4()
    redis.data[f"li:sending:{run_id}"] = "1"
    with patch.object(li, "send_item", AsyncMock()) as send:
        await li.send_run(USER, run_id)
    send.assert_not_called()


# ── API ─────────────────────────────────────────────────────────


def locked_run(status="awaiting_approval", output=None):
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        output=output if output is not None else {"queue_ids": [str(uuid.uuid4())]},
        completed_at=None,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = run
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return run, db


@pytest.mark.asyncio
async def test_approval_hands_the_batch_to_the_extension(on):
    from app.api.v1.linkedin import OutreachApproveRequest, approve_outreach

    run, db = locked_run()
    with (
        patch.object(li, "device_for", AsyncMock(return_value="dev1")),
        patch.object(li, "start_run") as start,
    ):
        result = await approve_outreach(
            uuid.uuid4(),
            OutreachApproveRequest(approved=True),
            db=db,
            current_user=SimpleNamespace(id=USER),
        )
    assert result["sending"] is True
    assert run.status == "completed" and run.output["sending"] is True
    start.assert_called_once_with(USER, run.id)


@pytest.mark.asyncio
async def test_approval_without_extension_keeps_the_manual_handoff(on):
    from app.api.v1.linkedin import OutreachApproveRequest, approve_outreach

    _, db = locked_run()
    with (
        patch.object(li, "device_for", AsyncMock(return_value=None)),
        patch.object(li, "start_run") as start,
    ):
        with pytest.raises(HTTPException) as exc:
            await approve_outreach(
                uuid.uuid4(),
                OutreachApproveRequest(approved=True),
                db=db,
                current_user=SimpleNamespace(id=USER),
            )
    assert exc.value.status_code == 409 and "Copy the draft" in exc.value.detail
    start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "output"),
    [
        ("awaiting_approval", {"queue_ids": ["x"]}),
        # A batch still running (started "now"; far-future so it never goes stale).
        (
            "completed",
            {
                "queue_ids": ["x"],
                "sending": True,
                "sending_started_at": "9999-01-01T00:00:00+00:00",
            },
        ),
        ("completed", {}),
    ],
)
async def test_resend_only_for_idle_approved_runs(on, redis, status, output):
    from app.api.v1.linkedin import resend_outreach

    _, db = locked_run(status, output)
    with (
        patch.object(li, "device_for", AsyncMock(return_value="dev1")),
        patch.object(li, "start_run") as start,
    ):
        with pytest.raises(HTTPException) as exc:
            # Past the rate limiter (it needs a real request); the checks are inside.
            await resend_outreach.__wrapped__(
                request=MagicMock(),
                run_id=uuid.uuid4(),
                db=db,
                current_user=SimpleNamespace(id=USER),
            )
    assert exc.value.status_code == 409
    start.assert_not_called()


@pytest.mark.asyncio
async def test_status_reports_whether_the_extension_is_online(on):
    from app.api.v1.linkedin import linkedin_sending_status

    with patch.object(li, "device_for", AsyncMock(return_value="dev1")):
        status = await linkedin_sending_status(current_user=SimpleNamespace(id=USER))
    assert status["enabled"] and status["ready"]
    assert status["invites_per_day"] == 10


@pytest.mark.asyncio
async def test_status_when_off():
    from app.api.v1.linkedin import linkedin_sending_status

    assert await linkedin_sending_status(current_user=SimpleNamespace(id=USER)) == {
        "enabled": False,
        "ready": False,
    }


# ── Interrupted sends (restart mid-batch) ───────────────────────


def test_sending_flag_goes_stale_after_the_lock_ttl():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert not li.sending_stale({"sending": False})
    assert not li.sending_stale({"sending": True, "sending_started_at": now.isoformat()})
    old = (now - li.STALE_AFTER - timedelta(minutes=1)).isoformat()
    assert li.sending_stale({"sending": True, "sending_started_at": old})
    assert li.sending_stale({"sending": True})  # flag from before the start time was kept


@pytest.mark.asyncio
async def test_item_stuck_in_sending_becomes_unknown_not_resent():
    from datetime import UTC, datetime, timedelta

    row = item("sending")
    row.delivery_started_at = datetime.now(UTC) - li.STALE_AFTER - timedelta(minutes=1)
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock()) as deliver,
    ):
        assert await li.send_item(USER, "dev1", uuid.uuid4()) == "outcome_unknown"
    deliver.assert_not_called()
    assert "interrupted" in row.delivery_error


@pytest.mark.asyncio
async def test_resend_allowed_after_an_interrupted_batch(on, redis):
    from datetime import UTC, datetime, timedelta

    from app.api.v1.linkedin import resend_outreach

    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    run, db = locked_run(
        "completed", {"queue_ids": ["x"], "sending": True, "sending_started_at": old}
    )
    with (
        patch.object(li, "device_for", AsyncMock(return_value="dev1")),
        patch.object(li, "start_run") as start,
    ):
        result = await resend_outreach.__wrapped__(
            request=MagicMock(),
            run_id=uuid.uuid4(),
            db=db,
            current_user=SimpleNamespace(id=USER),
        )
    assert result["sending"] is True
    assert run.output["sending_started_at"] != old
    start.assert_called_once()


# ── Review fixes ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        {"error": "LinkedIn's invitation dialog did not open; nothing was sent."},
        {"reason": "limit"},
    ],
)
async def test_confirmed_non_sends_give_the_daily_slot_back(on, redis, answer):
    patcher, _ = commands(SECOND, answer)
    with patcher:
        await li.deliver(USER, "dev1", URL, "Hi")
    [(key, used)] = [(k, v) for k, v in redis.data.items() if k.startswith("li:invite:")]
    assert used == 0


@pytest.mark.asyncio
async def test_maybe_sent_keeps_the_daily_slot(on, redis):
    patcher, _ = commands(SECOND, {"reason": "unconfirmed"})
    with patcher:
        assert (await li.deliver(USER, "dev1", URL, "Hi"))[0] == "outcome_unknown"
    assert [v for k, v in redis.data.items() if k.startswith("li:invite:")] == [1]


@pytest.mark.asyncio
async def test_a_cap_check_error_is_a_plain_failure(on, redis):
    async def broken(key):
        raise ConnectionError("redis down")

    redis.incr = broken
    patcher, sent = commands(SECOND)
    with patcher:
        delivery, error = await li.deliver(USER, "dev1", URL, "Hi")
    assert delivery == "failed" and "nothing was sent" in error
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_an_unexpected_send_error_is_maybe_sent(on, redis):
    patcher, _ = commands(SECOND, RuntimeError("socket closed"))
    with patcher:
        assert (await li.deliver(USER, "dev1", URL, "Hi"))[0] == "outcome_unknown"


@pytest.mark.asyncio
async def test_device_lookup_errors_mean_offline():
    from app.services import computer_service

    with patch.object(
        computer_service, "computer_backend", AsyncMock(side_effect=RuntimeError("registry"))
    ):
        assert await li.device_for(USER) is None


@pytest.mark.asyncio
async def test_one_item_error_does_not_stop_the_batch(redis):
    run = SimpleNamespace(output={"queue_ids": [str(uuid.uuid4()), str(uuid.uuid4())]})
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=run_session(run)),
        patch.object(li, "device_for", AsyncMock(return_value="dev1")),
        patch.object(
            li, "send_item", AsyncMock(side_effect=[RuntimeError("db"), "invited"])
        ) as send,
    ):
        await li.send_run(USER, uuid.uuid4())
    assert send.await_count == 2
    assert sorted(run.output["deliveries"].values()) == ["invited", "outcome_unknown"]


@pytest.mark.asyncio
async def test_an_old_batch_never_releases_a_newer_batchs_lock_or_flag(redis):
    run_id = uuid.uuid4()
    run = SimpleNamespace(
        output={"queue_ids": [str(uuid.uuid4())], "sending": True, "sending_started_at": "t1"}
    )

    async def newer_batch_takes_over(user_id, device, item_id):
        # Simulates a resend after this batch's lock expired.
        redis.data[li._lock_key(run_id)] = "newer"
        run.output = {**run.output, "sending_started_at": "t2"}
        return "invited"

    with (
        patch("app.core.database.AsyncSessionLocal", return_value=run_session(run)),
        patch.object(li, "device_for", AsyncMock(return_value="dev1")),
        patch.object(li, "send_item", newer_batch_takes_over),
    ):
        await li.send_run(USER, run_id)
    assert redis.data[li._lock_key(run_id)] == "newer"
    assert run.output["sending"] is True


@pytest.mark.asyncio
async def test_resend_refused_while_a_batch_holds_the_lock(on, redis):
    from fastapi import HTTPException

    from app.api.v1.linkedin import resend_outreach

    run, db = locked_run("completed", {"queue_ids": ["x"]})
    redis.data[li._lock_key(run.id)] = "token"
    with (
        patch.object(li, "device_for", AsyncMock(return_value="dev1")),
        patch.object(li, "start_run") as start,
    ):
        with pytest.raises(HTTPException) as exc:
            await resend_outreach.__wrapped__(
                request=MagicMock(), run_id=run.id, db=db, current_user=SimpleNamespace(id=USER)
            )
    assert exc.value.status_code == 409
    start.assert_not_called()


@pytest.mark.asyncio
async def test_linkedin_commands_get_a_longer_wait_within_the_socket_timeout():
    from app.core.redis_client import BLOCKING_SOCKET_TIMEOUT
    from app.services import computer_service

    with patch.object(computer_service, "send_command", AsyncMock(return_value={})) as send:
        await li._command("dev1", {"op": "probe", "profile_url": URL})
    assert send.await_args.kwargs["timeout"] == li.COMMAND_TIMEOUT
    assert computer_service.COMMAND_TIMEOUT < li.COMMAND_TIMEOUT < BLOCKING_SOCKET_TIMEOUT


@pytest.mark.asyncio
async def test_retry_only_visits_items_not_already_reported_sent_or_unknown(redis):
    states = ["invited", "messaged", "outcome_unknown", "failed", "limit_reached"]
    ids = [str(uuid.uuid4()) for _ in states]
    run = SimpleNamespace(output={"queue_ids": ids, "deliveries": dict(zip(ids, states))})
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=run_session(run)),
        patch.object(li, "device_for", AsyncMock(side_effect=["dev1", None])),
        patch.object(li, "send_item", AsyncMock(side_effect=["invited", "failed"])) as send,
    ):
        await li.send_run(USER, uuid.uuid4())
    assert [call.args for call in send.await_args_list] == [
        (USER, "dev1", uuid.UUID(ids[3])),
        (USER, None, uuid.UUID(ids[4])),
    ]
    assert list(run.output["deliveries"].values()) == [*states[:3], "invited", "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "bad_reply", "expected", "used"),
    [
        ("probe", "{", "failed", 0),
        ("probe", "[]", "failed", 0),
        ("probe", '{"ok":true,"result":[]}', "failed", 0),
        ("enqueue", None, "failed", 0),
        ("send", "{", "outcome_unknown", 1),
        ("send", "[]", "outcome_unknown", 1),
        ("send", '{"ok":true,"result":[]}', "outcome_unknown", 1),
        ("send", '{"ok":true,"result":{"error":"No result from the page"}}', "outcome_unknown", 1),
        ("send", HTTPException(422, "Error reading result"), "outcome_unknown", 1),
        ("send", '{"ok":false,"status":422,"error":"Invalid message"}', "failed", 0),
    ],
)
async def test_command_failures_persist_the_outcome(on, redis, stage, bad_reply, expected, used):
    row = item()
    command_redis = AsyncMock()
    if stage == "enqueue":
        command_redis.set.side_effect = [True, ConnectionError("redis down")]
    probe = ("probe", json.dumps({"ok": True, "result": SECOND}))
    bad = bad_reply if isinstance(bad_reply, Exception) else ("reply", bad_reply)
    blocking = SimpleNamespace(
        blpop=AsyncMock(side_effect=[bad] if stage == "probe" else [probe, bad])
    )
    # The first get_redis is for the probe command, then the daily cap;
    # the third is for enqueueing the send, followed by a possible cap release.
    connections = [command_redis, redis, command_redis, redis]
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li.redis_client, "get_redis", side_effect=connections),
        patch.object(li.redis_client, "get_blocking_redis", return_value=blocking),
    ):
        assert await li.send_item(USER, "dev1", uuid.uuid4()) == expected
    assert row.delivery == expected and row.delivery_error
    assert command_redis.rpush.await_count == (1 if stage in {"probe", "enqueue"} else 2)
    assert redis.data.get(li._cap_key(USER, "invite"), 0) == used
