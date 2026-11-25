"""Nova's code requests over Telegram: linking, asking, answering (no network)."""

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import telegram_codes as tg

SECRET = "s" * 40


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def getdel(self, key):
        return self.data.pop(key, None)

    async def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(tg.redis_client, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "CareerCraftNovaBot")
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", SECRET)


@pytest.fixture
def calls(monkeypatch):
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(tg, "call", sent)
    return sent


def update(text, chat_id=555, chat_type="private", update_id=1, message_id=9):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


def test_secret_must_match_exactly(bot):
    assert tg.secret_ok(SECRET)
    assert not tg.secret_ok(SECRET[:-1])
    assert not tg.secret_ok(None)


@pytest.mark.asyncio
async def test_bot_call_keeps_token_on_wire_and_out_of_httpx_logs(bot, caplog, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        tg.httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs)
    )
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert await tg.call("sendMessage", {"chat_id": 555})
        async with client(transport=transport) as other:
            await other.get("https://example.com/public")

    assert requests[0].url.path == "/bot123:abc/sendMessage"
    assert "123:abc" not in caplog.text
    assert "https://example.com/public" in caplog.text


@pytest.mark.asyncio
async def test_link_url_is_one_time_deep_link(bot, redis):
    url = await tg.link_url("8f7c2b5e-6a0d-4f7e-9d6c-0b1a2c3d4e5f")
    assert url.startswith("https://t.me/CareerCraftNovaBot?start=")
    token = url.split("start=")[1]
    assert redis.data[f"tg:link:{token}"] == "8f7c2b5e-6a0d-4f7e-9d6c-0b1a2c3d4e5f"


@pytest.mark.asyncio
async def test_group_chats_and_duplicates_are_ignored(bot, redis, calls):
    assert await tg.handle_update(update("123456", chat_type="group")) == {"status": "ignored"}
    with patch.object(tg, "link", AsyncMock(return_value=True)):
        first = await tg.handle_update(update("/start " + "a" * 20, update_id=7))
        again = await tg.handle_update(update("/start " + "a" * 20, update_id=7))
    assert first == {"status": "linked"}
    assert again == {"status": "duplicate"}


@pytest.mark.asyncio
async def test_start_with_expired_or_bad_token(bot, redis, calls):
    with patch.object(tg, "link", AsyncMock(return_value=False)):
        assert (await tg.handle_update(update("/start " + "b" * 20)))["status"] == "link_failed"
    with patch.object(tg, "link", AsyncMock()) as link:
        assert (await tg.handle_update(update("/start x;drop", update_id=2)))["status"] == (
            "link_failed"
        )
    link.assert_not_called()


@pytest.mark.asyncio
async def test_stop_unlinks(bot, redis, calls):
    with patch.object(tg, "unlink_chat", AsyncMock()) as unlink:
        assert (await tg.handle_update(update("/stop")))["status"] == "unlinked"
    unlink.assert_awaited_once_with(555)


@pytest.mark.asyncio
async def test_code_reply_is_delivered_and_deleted_from_chat(bot, redis, calls):
    run_id = uuid.uuid4()
    redis.data["tg:await:555"] = str(run_id)
    with patch.object(tg, "deliver_code", AsyncMock(return_value=True)) as deliver:
        result = await tg.handle_update(update(" 482 913 "))
    assert result == {"status": "code_accepted"}
    deliver.assert_awaited_once_with(555, run_id, "482913")
    assert "tg:await:555" not in redis.data
    methods = [c.args[0] for c in calls.await_args_list]
    assert methods == ["deleteMessage", "sendMessage"]
    assert calls.await_args_list[0].args[1] == {"chat_id": 555, "message_id": 9}
    assert all("482913" not in str(c.args[1]) for c in calls.await_args_list)


@pytest.mark.asyncio
async def test_chatter_while_waiting_keeps_the_request_open(bot, redis, calls):
    redis.data["tg:await:555"] = str(uuid.uuid4())
    with patch.object(tg, "deliver_code", AsyncMock()) as deliver:
        result = await tg.handle_update(update("what is this?"))
    assert result == {"status": "help"}
    deliver.assert_not_called()
    assert "tg:await:555" in redis.data


@pytest.mark.asyncio
async def test_codes_nobody_asked_for_are_not_used(bot, redis, calls):
    with patch.object(tg, "deliver_code", AsyncMock()) as deliver:
        assert (await tg.handle_update(update("482913")))["status"] == "help"
    deliver.assert_not_called()


def session(owner, run, user):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = owner
    db.get = AsyncMock(side_effect=lambda model, _id: run if model.__name__ == "AgentRun" else user)
    return db


def waiting_run(owner, operation="enter_code"):
    return SimpleNamespace(
        user_id=owner,
        status="awaiting_approval",
        output={"type": "computer_action", "action": {"operation": operation}},
    )


@pytest.mark.asyncio
async def test_code_only_reaches_the_chat_owners_waiting_code_step():
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    run_id = uuid.uuid4()
    cases = [
        (owner, waiting_run(owner), True),
        (stranger, waiting_run(owner), False),
        (owner, waiting_run(owner, "click"), False),
        (None, waiting_run(owner), False),
    ]
    for chat_owner, run, ok in cases:
        db = session(chat_owner, run, SimpleNamespace(id=chat_owner))
        with (
            patch("app.core.database.AsyncSessionLocal", return_value=db),
            patch("app.services.nova_signin.store_code", AsyncMock()) as store,
            patch("app.api.v1.agents.decide_run", AsyncMock()) as decide,
        ):
            assert await tg.deliver_code(555, run_id, "482913") is ok
        assert store.await_count == int(ok)
        assert decide.await_count == int(ok)


@pytest.mark.asyncio
async def test_ask_for_code_only_when_linked(bot, redis, calls):
    run_id = uuid.uuid4()
    with patch.object(tg, "chat_for", AsyncMock(return_value=None)):
        assert not await tg.ask_for_code(uuid.uuid4(), run_id, "www.naukri.com")
    calls.assert_not_called()
    with patch.object(tg, "chat_for", AsyncMock(return_value=555)):
        assert await tg.ask_for_code(uuid.uuid4(), run_id, "www.naukri.com")
    assert redis.data["tg:await:555"] == str(run_id)
    text = calls.await_args.args[1]["text"]
    assert "www.naukri.com" in text and "Never send passwords" in text


@pytest.mark.asyncio
async def test_ask_for_code_is_silent_when_unconfigured(monkeypatch, calls):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    assert not await tg.ask_for_code(uuid.uuid4(), uuid.uuid4(), "site")
    calls.assert_not_called()


def inbound_client():
    from app.api.v1 import telegram
    from app.core.rate_limit import limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(telegram.inbound_router)
    return TestClient(app)


def test_webhook_requires_the_secret_header(bot):
    with patch.object(tg, "handle_update", AsyncMock(return_value={"status": "help"})) as handle:
        bad = inbound_client().post(
            "/inbound/telegram", json=update("1"), headers={"x-telegram-bot-api-secret-token": "no"}
        )
        good = inbound_client().post(
            "/inbound/telegram",
            json=update("1"),
            headers={"x-telegram-bot-api-secret-token": SECRET},
        )
    assert bad.status_code == 401
    assert good.status_code == 200
    handle.assert_awaited_once()


def test_webhook_path_skips_session_auth():
    from app.core.request_middleware import PUBLIC_PATHS

    assert "/api/v1/inbound/telegram" in PUBLIC_PATHS
