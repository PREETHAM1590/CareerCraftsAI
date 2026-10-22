"""Agent browser mode: backend resolution, dispatch and the extension command channel."""

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1 import computer, extension
from app.api.v1.deps import get_current_user, get_db
from app.services import computer_service as service


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: client)
    monkeypatch.setattr("app.core.redis_client.get_blocking_redis", lambda purpose="poll": client)
    return client


def session(monkeypatch, *results):
    db = AsyncMock()
    db.execute.side_effect = list(results)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", lambda: context)
    return db


def device_client(device_id):
    app = FastAPI()
    app.include_router(extension.router)
    app.dependency_overrides[extension.get_device] = lambda: SimpleNamespace(
        id=device_id, revoked_at=None
    )
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def state(backend, device=None, mode=None):
    return {"mode": mode or backend, "backend": backend, "paired": True, "device": device}


# ── Mode resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode,seen_ago,backend,online,paired",
    [
        ("auto", 10, "browser", True, True),
        ("auto", 300, "cloud", False, True),
        ("auto", None, "cloud", False, False),
        ("browser", 300, "browser", False, True),
        ("cloud", 10, "cloud", True, True),
    ],
)
@pytest.mark.asyncio
async def test_backend_resolution(monkeypatch, mode, seen_ago, backend, online, paired):
    device = uuid.uuid4()
    mode_result = MagicMock()
    mode_result.scalar_one_or_none.return_value = mode
    devices = MagicMock()
    devices.all.return_value = (
        [SimpleNamespace(id=device, agent_seen_at=datetime.now(UTC) - timedelta(seconds=seen_ago))]
        if seen_ago is not None
        else []
    )
    session(monkeypatch, mode_result, devices)
    resolved = await service.computer_backend(uuid.uuid4())
    assert resolved == {
        "mode": mode,
        "backend": backend,
        "paired": paired,
        "device": str(device) if online else None,
    }


# ── Dispatch ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_dispatch_uses_the_relay(monkeypatch):
    relay = AsyncMock(return_value={"ok": True})
    send = AsyncMock()
    monkeypatch.setattr(service, "relay", relay)
    monkeypatch.setattr(service, "send_command", send)
    owner, body = uuid.uuid4(), {"operation": "screenshot"}
    assert await service.dispatch(owner, "action", body, state("cloud")) == {"ok": True}
    relay.assert_awaited_once_with(owner, "/action", "POST", body)
    await service.dispatch(owner, "start", state=state("cloud"))
    assert relay.await_args.args == (owner, "/start", "POST", None)
    send.assert_not_called()


@pytest.mark.asyncio
async def test_browser_dispatch_queues_a_command(monkeypatch):
    relay = AsyncMock()
    send = AsyncMock(return_value={"ok": True, "url": "https://jobs.example.com"})
    monkeypatch.setattr(service, "relay", relay)
    monkeypatch.setattr(service, "send_command", send)
    body = {"operation": "click", "parameters": {"ref": "e1", "snapshotId": 2}}
    await service.dispatch(uuid.uuid4(), "action", body, state("browser", "dev-1"))
    send.assert_awaited_once_with("dev-1", "action", body)
    await service.dispatch(uuid.uuid4(), "stop", state=state("browser", "dev-1"))
    assert send.await_args.args == ("dev-1", "stop", None)
    relay.assert_not_called()


@pytest.mark.parametrize("operation", ["screenshot", "control/take", "human/click", "files/read"])
@pytest.mark.asyncio
async def test_browser_mode_refuses_cloud_only_operations(redis, operation):
    with pytest.raises(HTTPException) as exc:
        await service.dispatch(
            uuid.uuid4(), "action", {"operation": operation}, state("browser", "dev-1")
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == service.BROWSER_UNSUPPORTED
    assert await redis.keys("*") == []


@pytest.mark.asyncio
async def test_saved_passwords_never_reach_the_browser_queue(redis):
    body = {"operation": "credentials/fill", "parameters": {"password": "privatepassword"}}
    with pytest.raises(HTTPException) as exc:
        await service.dispatch(uuid.uuid4(), "action", body, state("browser", "dev-1"))
    assert exc.value.status_code == 409
    assert await redis.keys("*") == []


@pytest.mark.asyncio
async def test_forced_browser_mode_without_a_device(redis):
    offline = state("browser")
    assert await service.dispatch(uuid.uuid4(), "status", state=offline) == {
        "available": True,
        "backend": "browser",
        "running": False,
        "enabled": False,
    }
    with pytest.raises(HTTPException) as exc:
        await service.dispatch(uuid.uuid4(), "start", state=offline)
    assert exc.value.status_code == 503 and exc.value.detail == service.BROWSER_OFFLINE


# ── Command channel ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_round_trip(redis):
    device = uuid.uuid4()
    body = {"operation": "read", "parameters": {}}
    caller = asyncio.create_task(service.send_command(str(device), "action", body))
    async with device_client(device) as client:
        response = await client.get("/extension/device/commands/next", params={"wait": 3})
        assert response.status_code == 200
        command = response.json()
        assert command["kind"] == "action" and command["body"] == body
        assert command["expires"] > time.time()
        page = {"url": "https://jobs.example.com", "title": "Job", "text": "hi"}
        posted = await client.post(
            f"/extension/device/commands/{command['id']}/result",
            json={"ok": True, "result": page},
        )
        assert posted.status_code == 200
    assert await caller == page


@pytest.mark.asyncio
async def test_device_error_becomes_http_error(redis):
    device = uuid.uuid4()
    caller = asyncio.create_task(service.send_command(str(device), "action", {}))
    async with device_client(device) as client:
        command = (await client.get("/extension/device/commands/next?wait=3")).json()
        await client.post(
            f"/extension/device/commands/{command['id']}/result",
            json={
                "ok": False,
                "status": 403,
                "error": "permission_required",
                "origin": "https://jobs.example.com",
            },
        )
    with pytest.raises(HTTPException) as exc:
        await caller
    assert exc.value.status_code == 403
    assert exc.value.detail == (
        "Allow the CareerCraft extension on https://jobs.example.com to continue."
    )


@pytest.mark.asyncio
async def test_unknown_device_status_maps_to_502(redis):
    device = uuid.uuid4()
    caller = asyncio.create_task(service.send_command(str(device), "start"))
    async with device_client(device) as client:
        command = (await client.get("/extension/device/commands/next?wait=3")).json()
        await client.post(
            f"/extension/device/commands/{command['id']}/result",
            json={"ok": False, "status": 200, "error": "x" * 1000},
        )
    with pytest.raises(HTTPException) as exc:
        await caller
    assert exc.value.status_code == 502 and len(exc.value.detail) == 300


@pytest.mark.asyncio
async def test_unanswered_command_times_out(redis, monkeypatch):
    monkeypatch.setattr(service, "COMMAND_TIMEOUT", 1)
    device = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await service.send_command(device, "start")
    assert exc.value.status_code == 504
    # A browser that reconnects later must not run what the caller saw fail.
    assert await redis.llen(f"ext:cmd:{device}") == 0


@pytest.mark.asyncio
async def test_revoked_device_does_not_receive_a_waiting_command(redis):
    device = SimpleNamespace(id=uuid.uuid4(), agent_seen_at=None, revoked_at=None)
    queue = f"ext:cmd:{device.id}"
    command = {"id": str(uuid.uuid4()), "kind": "action", "body": {}, "expires": time.time() + 60}
    await redis.rpush(queue, json.dumps(command))
    db = AsyncMock()

    async def revoke(target, attrs):
        target.revoked_at = datetime.now(UTC)  # the owner revoked it mid-wait

    db.refresh.side_effect = revoke

    class LiveRequest:
        async def is_disconnected(self):
            return False

    route = extension.next_command.__wrapped__
    with pytest.raises(HTTPException) as exc:
        await route(request=LiveRequest(), wait=1, device=device, db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_device_cannot_answer_another_devices_command(redis):
    owner, intruder = uuid.uuid4(), uuid.uuid4()
    caller = asyncio.create_task(service.send_command(str(owner), "action", {}))
    async with device_client(owner) as client:
        command = (await client.get("/extension/device/commands/next?wait=3")).json()
    async with device_client(intruder) as client:
        forged = await client.post(
            f"/extension/device/commands/{command['id']}/result",
            json={"ok": True, "result": {"forged": True}},
        )
        assert forged.status_code == 404
        # The intruder's queue never sees the owner's command either.
        assert (await client.get("/extension/device/commands/next?wait=0")).status_code == 204
    async with device_client(owner) as client:
        real = await client.post(
            f"/extension/device/commands/{command['id']}/result",
            json={"ok": True, "result": {"real": True}},
        )
        assert real.status_code == 200
    assert await caller == {"real": True}


@pytest.mark.asyncio
async def test_expired_commands_are_dropped(redis):
    device = uuid.uuid4()
    stale = {"id": str(uuid.uuid4()), "kind": "start", "body": {}, "expires": time.time() - 1}
    await redis.rpush(f"ext:cmd:{device}", json.dumps(stale))
    async with device_client(device) as client:
        response = await client.get("/extension/device/commands/next?wait=0")
    assert response.status_code == 204
    assert await redis.llen(f"ext:cmd:{device}") == 0


@pytest.mark.asyncio
async def test_result_body_is_capped(redis):
    device = uuid.uuid4()
    await redis.set("ext:owner:" + str(uuid.uuid4()), str(device))
    async with device_client(device) as client:
        response = await client.post(
            f"/extension/device/commands/{uuid.uuid4()}/result",
            content=json.dumps({"ok": True, "result": {"text": "x" * (1024 * 1024)}}),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


# ── Member mode endpoints ───────────────────────────────────────────────


def member_client(monkeypatch, resolved):
    app = FastAPI()
    app.include_router(computer.router)
    db = AsyncMock()
    member = SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_current_user] = lambda: member
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(service, "computer_backend", AsyncMock(return_value=resolved))
    monkeypatch.setattr(service.settings, "SANDBOX_RELAY_URL", "")
    monkeypatch.setattr(service.settings, "SANDBOX_RELAY_TOKEN", "")
    return TestClient(app), db, member


def test_put_mode_validates_and_returns_the_mode_view(monkeypatch):
    client, db, member = member_client(
        monkeypatch, {"mode": "browser", "backend": "browser", "paired": True, "device": "d"}
    )
    assert client.put("/computer/mode", json={"mode": "bogus"}).status_code == 422
    db.execute.assert_not_called()
    response = client.put("/computer/mode", json={"mode": "browser"})
    assert response.status_code == 200
    assert response.json() == {
        "mode": "browser",
        "effective": "browser",
        "extension": {"paired": True, "online": True},
        "cloud": {"available": False},
    }
    update = db.execute.await_args.args[0]
    assert update.compile().params["computer_mode"] == "browser"
    # Scoped to the signed-in member, never every user.
    assert "WHERE users.id = " in str(update.compile())
    assert member.id in update.compile().params.values()
    db.commit.assert_awaited_once()


def test_status_reports_the_browser_backend(monkeypatch):
    client, _, _ = member_client(
        monkeypatch, {"mode": "auto", "backend": "browser", "paired": True, "device": "d"}
    )
    assert client.get("/computer").json() == {
        "available": True,
        "backend": "browser",
        "running": True,
        "enabled": True,
    }
    assert client.get("/computer/mode").json()["effective"] == "browser"


# ── Erasure ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_ignores_actions_run_in_the_members_browser(monkeypatch):
    monkeypatch.setattr(service.settings, "SANDBOX_RELAY_URL", "")
    monkeypatch.setattr(service.settings, "SANDBOX_RELAY_TOKEN", "")
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = session(monkeypatch, result)
    await service.purge_user(uuid.uuid4())
    query = db.execute.await_args.args[0].compile()
    assert "IS DISTINCT FROM" in str(query)
    assert {"backend", "browser"} <= set(query.params.values())


@pytest.mark.asyncio
async def test_configured_purge_always_erases_the_cloud_computer(monkeypatch):
    monkeypatch.setattr(service.settings, "SANDBOX_RELAY_URL", "http://relay")
    monkeypatch.setattr(service.settings, "SANDBOX_RELAY_TOKEN", "token")
    relay = AsyncMock(return_value={"purged": True})
    monkeypatch.setattr(service, "relay", relay)
    owner = uuid.uuid4()
    await service.purge_user(owner)
    relay.assert_awaited_once_with(owner, method="DELETE")


@pytest.mark.asyncio
async def test_long_poll_marks_the_agent_online(redis):
    # Only the command loop counts as online; other device routes don't set this.
    device = SimpleNamespace(id=uuid.uuid4(), agent_seen_at=None)
    app = FastAPI()
    app.include_router(extension.router)
    app.dependency_overrides[extension.get_device] = lambda: device
    db = AsyncMock()
    seen_at_commit = []
    db.commit.side_effect = lambda: seen_at_commit.append(device.agent_seen_at)
    app.dependency_overrides[get_db] = lambda: db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/extension/device/commands/next", params={"wait": 0})
    assert response.status_code == 204
    # Persisted, not just set in memory: the commit saw the new timestamp.
    assert seen_at_commit and seen_at_commit[0] is not None


@pytest.mark.asyncio
async def test_abandoned_poll_hands_the_command_back(redis):
    device = SimpleNamespace(id=uuid.uuid4(), agent_seen_at=None)
    queue = f"ext:cmd:{device.id}"
    command = {"id": str(uuid.uuid4()), "kind": "action", "body": {}, "expires": time.time() + 60}
    await redis.rpush(queue, json.dumps(command))

    class GoneRequest:
        async def is_disconnected(self):
            return True

    route = extension.next_command.__wrapped__  # past the rate-limit wrapper
    response = await route(request=GoneRequest(), wait=1, device=device, db=AsyncMock())
    assert response.status_code == 204
    # Still queued for the next live poll.
    assert json.loads(await redis.lpop(queue))["id"] == command["id"]
