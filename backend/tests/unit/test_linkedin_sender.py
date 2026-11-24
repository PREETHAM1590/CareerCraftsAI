"""LinkedIn sending (beta): off by default, hosted login, at-most-once sends."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.services import linkedin_sender as li

USER = "8f7c2b5e-6a0d-4f7e-9d6c-0b1a2c3d4e5f"
REAL_CLIENT = httpx.AsyncClient


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def getdel(self, key):
        return self.data.pop(key, None)

    async def incr(self, key):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

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
    monkeypatch.setattr(settings, "UNIPILE_DSN", "https://api8.unipile.com:13851")
    monkeypatch.setattr(settings, "UNIPILE_API_KEY", "key")
    monkeypatch.setattr(settings, "PUBLIC_API_URL", "https://api.careercraftsai.me")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://careercraftsai.me")
    monkeypatch.setattr(settings, "LINKEDIN_INVITES_PER_DAY", 10)
    monkeypatch.setattr(settings, "LINKEDIN_MESSAGES_PER_DAY", 25)


def unipile(handler):
    """Route every httpx call in linkedin_sender to `handler`."""
    calls = []

    def recorder(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(recorder)
    return patch.object(
        li.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=transport, **kw)
    ), calls


def test_off_by_default():
    assert settings.LINKEDIN_OUTREACH_ENABLED is False
    assert not li.enabled()


@pytest.mark.parametrize(
    ("url", "ident"),
    [
        ("https://www.linkedin.com/in/priya-sharma-123/", "priya-sharma-123"),
        ("https://in.linkedin.com/in/rahul_v?trk=x", "rahul_v"),
        ("http://www.linkedin.com/in/priya", None),
        ("https://www.linkedin.com/company/acme", None),
        ("https://evil.example/in/priya", None),
        (None, None),
    ],
)
def test_public_identifier(url, ident):
    assert li.public_identifier(url) == ident


# ── Connecting ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_needs_the_flag(redis):
    with pytest.raises(li.LinkedInUnavailable):
        await li.link_url(USER)


@pytest.mark.asyncio
async def test_link_uses_unilogin_and_a_one_time_state(on, redis):
    client, calls = unipile(
        lambda r: httpx.Response(201, json={"url": "https://account.unipile.com/x"})
    )
    with client:
        assert await li.link_url(USER) == "https://account.unipile.com/x"
    body = json.loads(calls[0].content)
    assert calls[0].url.path == "/api/v1/hosted/accounts/link"
    assert calls[0].headers["X-API-KEY"] == "key"
    assert body["providers"] == ["LINKEDIN"] and "unilogin" in body
    assert body["name"] == USER
    state = body["notify_url"].split("state=")[1]
    assert body["notify_url"].startswith("https://api.careercraftsai.me/api/v1/inbound/unipile?")
    assert redis.data[f"li:link:{state}"] == USER


def account_db(existing=None):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get = AsyncMock(return_value=existing)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_callback_connects_only_with_the_matching_state(redis):
    state = "s" * 24
    redis.data[f"li:link:{state}"] = USER
    db = account_db()
    with patch("app.core.database.AsyncSessionLocal", return_value=db):
        result = await li.handle_notify(
            state, {"status": "CREATION_SUCCESS", "account_id": "e54m8LR22bA7", "name": USER}
        )
    assert result == {"status": "connected"}
    row = db.add.call_args.args[0]
    assert str(row.user_id) == USER and row.unipile_account_id == "e54m8LR22bA7"
    assert f"li:link:{state}" not in redis.data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "payload", "status"),
    [
        ("bad state!", {}, "ignored"),
        ("u" * 24, {"status": "CREATION_SUCCESS", "account_id": "abcd", "name": USER}, "expired"),
        (
            "s" * 24,
            {"status": "CREATION_SUCCESS", "account_id": "abcd", "name": "someone"},
            "rejected",
        ),
        ("s" * 24, {"status": "FAILED", "account_id": "abcd", "name": USER}, "rejected"),
    ],
)
async def test_callback_rejects_anything_else(redis, state, payload, status):
    redis.data[f"li:link:{'s' * 24}"] = USER
    db = account_db()
    with patch("app.core.database.AsyncSessionLocal", return_value=db):
        assert await li.handle_notify(state, payload) == {"status": status}
    db.add.assert_not_called()


# ── Sending ─────────────────────────────────────────────────────


def profile(distance="SECOND_DEGREE", provider_id="ACoAAAEkw"):
    return httpx.Response(200, json={"provider_id": provider_id, "network_distance": distance})


@pytest.mark.asyncio
async def test_first_degree_gets_a_message(on, redis):
    def handler(request):
        if request.method == "GET":
            return profile("FIRST_DEGREE")
        return httpx.Response(201, json={"object": "ChatStarted"})

    client, calls = unipile(handler)
    with client:
        assert await li.deliver(USER, "acc1", "priya", "Hi Priya") == ("messaged", None)
    assert calls[1].url.path == "/api/v1/chats"
    assert calls[1].headers["content-type"].startswith("multipart/form-data")
    assert b"Hi Priya" in calls[1].content and b"ACoAAAEkw" in calls[1].content


@pytest.mark.asyncio
async def test_others_get_an_invite_with_the_note_capped(on, redis):
    def handler(request):
        if request.method == "GET":
            return profile()
        return httpx.Response(201, json={"object": "UserInvitationSent"})

    client, calls = unipile(handler)
    with client:
        result = await li.deliver(USER, "acc1", "priya", "x" * 400)
    assert result == ("invited", None)
    body = json.loads(calls[1].content)
    assert calls[1].url.path == "/api/v1/users/invite"
    assert body == {"account_id": "acc1", "provider_id": "ACoAAAEkw", "message": "x" * 300}


@pytest.mark.asyncio
async def test_daily_cap_stops_before_anything_is_sent(on, redis, monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_INVITES_PER_DAY", 0)
    client, calls = unipile(lambda r: profile())
    with client:
        delivery, _ = await li.deliver(USER, "acc1", "priya", "Hi")
    assert delivery == "limit_reached"
    assert [c.method for c in calls] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "delivery"),
    [
        (httpx.Response(422, json={"type": "errors/cannot_resend_yet"}), "limit_reached"),
        (httpx.Response(429), "limit_reached"),
        (httpx.Response(401), "failed"),
        (httpx.Response(400, json={"type": "errors/invalid"}), "failed"),
        (httpx.Response(500), "outcome_unknown"),
    ],
)
async def test_send_outcomes(on, redis, response, delivery):
    client, _ = unipile(lambda r: profile() if r.method == "GET" else response)
    with client:
        assert (await li.deliver(USER, "acc1", "priya", "Hi"))[0] == delivery


@pytest.mark.asyncio
async def test_timeout_after_sending_is_an_unknown_outcome(on, redis):
    def handler(request):
        if request.method == "GET":
            return profile()
        raise httpx.ReadTimeout("slow")

    client, _ = unipile(handler)
    with client:
        assert (await li.deliver(USER, "acc1", "priya", "Hi"))[0] == "outcome_unknown"


def queue_db(item):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = item
    db.get = AsyncMock(return_value=item)
    db.commit = AsyncMock()
    return db


def item(delivery=None, url="https://www.linkedin.com/in/priya/"):
    return SimpleNamespace(
        message="Hi Priya",
        contact_linkedin_url=url,
        delivery=delivery,
        delivery_error=None,
        status="pending_approval",
        approved_at=None,
        sent_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["sending", "invited", "messaged", "outcome_unknown"])
async def test_sent_or_maybe_sent_items_are_never_sent_again(on, delivery):
    row = item(delivery)
    with (
        patch.object(li, "account_for", AsyncMock(return_value="acc1")),
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock()) as deliver,
    ):
        assert await li.send_item(USER, uuid.uuid4()) == delivery
    deliver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", [None, "failed", "limit_reached"])
async def test_unsent_items_can_be_sent(on, delivery):
    row = item(delivery)
    with (
        patch.object(li, "account_for", AsyncMock(return_value="acc1")),
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock(return_value=("invited", None))) as deliver,
    ):
        assert await li.send_item(USER, uuid.uuid4()) == "invited"
    deliver.assert_awaited_once_with(USER, "acc1", "priya", "Hi Priya")
    assert row.status == "sent" and row.sent_at is not None


@pytest.mark.asyncio
async def test_contact_without_profile_link_is_not_sent(on):
    row = item(url="https://www.linkedin.com/company/acme")
    with (
        patch.object(li, "account_for", AsyncMock(return_value="acc1")),
        patch("app.core.database.AsyncSessionLocal", return_value=queue_db(row)),
        patch.object(li, "deliver", AsyncMock()) as deliver,
    ):
        assert await li.send_item(USER, uuid.uuid4()) == "failed"
    deliver.assert_not_called()


@pytest.mark.asyncio
async def test_sending_needs_flag_and_connection(on):
    with patch.object(li, "account_for", AsyncMock(return_value=None)):
        with pytest.raises(li.LinkedInUnavailable):
            await li.send_item(USER, uuid.uuid4())


# ── Approval endpoint ───────────────────────────────────────────


def run_db(queue_ids):
    run = SimpleNamespace(
        status="awaiting_approval", output={"queue_ids": queue_ids}, completed_at=None
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = run
    db.flush = AsyncMock()
    return run, db


@pytest.mark.asyncio
async def test_approval_sends_each_draft_when_connected(on):
    from app.api.v1.linkedin import OutreachApproveRequest, approve_outreach

    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    run, db = run_db(ids)
    with (
        patch.object(li, "account_for", AsyncMock(return_value="acc1")),
        patch.object(li, "send_item", AsyncMock(side_effect=["invited", "limit_reached"])) as send,
    ):
        result = await approve_outreach(
            uuid.uuid4(),
            OutreachApproveRequest(approved=True),
            db=db,
            current_user=SimpleNamespace(id=USER),
        )
    assert send.await_count == 2
    assert run.output["deliveries"] == {ids[0]: "invited", ids[1]: "limit_reached"}
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_approval_without_connection_keeps_the_manual_handoff():
    from fastapi import HTTPException

    from app.api.v1.linkedin import OutreachApproveRequest, approve_outreach

    _, db = run_db([str(uuid.uuid4())])
    with pytest.raises(HTTPException) as exc:
        await approve_outreach(
            uuid.uuid4(),
            OutreachApproveRequest(approved=True),
            db=db,
            current_user=SimpleNamespace(id=USER),
        )
    assert exc.value.status_code == 409 and "Copy the draft" in exc.value.detail


def test_callback_path_skips_session_auth():
    from app.core.request_middleware import PUBLIC_PATHS

    assert "/api/v1/inbound/unipile" in PUBLIC_PATHS
