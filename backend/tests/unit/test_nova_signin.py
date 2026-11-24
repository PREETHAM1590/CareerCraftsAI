"""Nova's private steps: saved-login sign-in and one-time codes."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.services import nova_signin
from app.services.computer_service import AGENT_WRITES, NOVA_STEPS, ComputerAction


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def exists(self, key):
        return int(key in self.data)

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
    with patch.object(nova_signin.redis_client, "get_redis", return_value=fake):
        yield fake


def sign_in_action(**overrides):
    params = {"usernameRef": "e1", "passwordRef": "e2", "snapshotId": 4, **overrides}
    return ComputerAction(
        operation="sign_in", parameters=params, computer_run="run1", approved_snapshot=4
    )


# ── Step validation ──────────────────────────────────────────────


def test_nova_steps_are_not_relay_agent_operations():
    # The model reaches the relay only through nova_signin, never directly.
    assert not NOVA_STEPS & AGENT_WRITES
    with pytest.raises(ValueError):
        sign_in_action().validate_operation(AGENT_WRITES)


def test_sign_in_needs_refs_and_current_run():
    sign_in_action().validate_operation(NOVA_STEPS)
    with pytest.raises(ValueError):
        sign_in_action(passwordRef="e2; drop").validate_operation(NOVA_STEPS)
    with pytest.raises(ValueError):
        ComputerAction(
            operation="sign_in",
            parameters={"usernameRef": "e1", "passwordRef": "e2", "snapshotId": 4},
        ).validate_operation(NOVA_STEPS)


def test_sign_in_cannot_carry_a_secret():
    with pytest.raises(ValueError):
        sign_in_action(password="hunter2").validate_operation(NOVA_STEPS)
    with pytest.raises(ValueError):
        ComputerAction(
            operation="enter_code",
            parameters={"ref": "e3", "snapshotId": 4, "code": "123456"},
            computer_run="run1",
        ).validate_operation(NOVA_STEPS)


@pytest.mark.parametrize(
    "url",
    [None, "http://portal.example/login", "https://portal.example:8443/login", "about:blank"],
)
def test_page_origin_requires_plain_https(url):
    with pytest.raises(ValueError):
        nova_signin.page_origin(url)


def test_page_origin_is_exact_and_canonical():
    assert nova_signin.page_origin("https://Jobs.Example.com/login?next=/a") == (
        "https://jobs.example.com"
    )


# ── One-time codes ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_is_sealed_and_read_once(redis):
    run_id = uuid.uuid4()
    await nova_signin.store_code(run_id, "482913")
    assert "482913" not in str(redis.data)
    assert await nova_signin.has_code(run_id)
    assert await nova_signin.take_code(run_id) == "482913"
    assert await nova_signin.take_code(run_id) is None


@pytest.mark.asyncio
async def test_enter_code_without_a_code_does_nothing(redis):
    action = ComputerAction(
        operation="enter_code", parameters={"ref": "e3", "snapshotId": 4}, computer_run="run1"
    )
    with patch.object(nova_signin, "_dispatch", AsyncMock()) as dispatch:
        with pytest.raises(ValueError):
            await nova_signin.enter_code(
                uuid.uuid4(), uuid.uuid4(), action, "https://a.example/otp"
            )
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_enter_code_types_it_once_on_the_page_origin(redis):
    run_id = uuid.uuid4()
    await nova_signin.store_code(run_id, "482913")
    action = ComputerAction(
        operation="enter_code", parameters={"ref": "e3", "snapshotId": 4}, computer_run="run1"
    )
    with patch.object(
        nova_signin, "_dispatch", AsyncMock(return_value={"submitted": True})
    ) as dispatch:
        result = await nova_signin.enter_code(uuid.uuid4(), run_id, action, "https://a.example/otp")
    body = dispatch.await_args.args[1]
    assert body["operation"] == "codes/fill"
    assert body["actor"] == "nova"
    assert body["parameters"] == {
        "origin": "https://a.example",
        "ref": "e3",
        "snapshotId": 4,
        "code": "482913",
    }
    assert result == {"entered": True, "origin": "https://a.example"}
    assert not await nova_signin.has_code(run_id)


# ── Saved-login sign-in ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sign_in_resolves_the_login_by_page_origin():
    row = SimpleNamespace(username_enc="u", password_enc="p")
    with (
        patch.object(nova_signin, "saved_login", AsyncMock(return_value=row)) as lookup,
        patch.object(nova_signin, "decrypt_api_key", side_effect=lambda v, _: f"plain-{v}"),
        patch.object(
            nova_signin, "_dispatch", AsyncMock(return_value={"submitted": True})
        ) as dispatch,
    ):
        result = await nova_signin.sign_in(
            "8f7c2b5e-6a0d-4f7e-9d6c-0b1a2c3d4e5f", sign_in_action(), "https://portal.example/login"
        )
    assert lookup.await_args.args[1] == "https://portal.example"
    body = dispatch.await_args.args[1]
    assert body["operation"] == "credentials/fill"
    assert body["actor"] == "nova"
    assert body["parameters"]["submit"] is True
    assert body["parameters"]["origin"] == "https://portal.example"
    assert body["parameters"]["password"] == "plain-p"
    assert result == {"signed_in": True, "origin": "https://portal.example"}


@pytest.mark.asyncio
async def test_sign_in_without_saved_login_refuses():
    with (
        patch.object(nova_signin, "saved_login", AsyncMock(return_value=None)),
        patch.object(nova_signin, "_dispatch", AsyncMock()) as dispatch,
    ):
        with pytest.raises(ValueError):
            await nova_signin.sign_in(uuid.uuid4(), sign_in_action(), "https://portal.example/")
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_private_steps_refuse_the_extension_browser():
    with (
        patch.object(
            nova_signin.computer_service,
            "computer_backend",
            AsyncMock(return_value={"backend": "browser"}),
        ),
        patch.object(nova_signin.computer_service, "dispatch", AsyncMock()) as dispatch,
    ):
        with pytest.raises(HTTPException) as exc:
            await nova_signin._dispatch(uuid.uuid4(), {}, "nova_sign_in")
    assert exc.value.status_code == 409
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_auto_signin_needs_consent_and_stops_after_daily_limit(redis):
    user = uuid.uuid4()
    with patch.object(
        nova_signin, "saved_login", AsyncMock(return_value=SimpleNamespace(auto_signin=False))
    ):
        assert not await nova_signin.claim_auto_signin(user, "https://portal.example")
    assert redis.data == {}
    with patch.object(
        nova_signin, "saved_login", AsyncMock(return_value=SimpleNamespace(auto_signin=True))
    ):
        claims = [
            await nova_signin.claim_auto_signin(user, "https://portal.example")
            for _ in range(nova_signin.AUTO_SIGNIN_DAILY_LIMIT + 1)
        ]
    assert claims == [True] * nova_signin.AUTO_SIGNIN_DAILY_LIMIT + [False]


# ── Planner and continuation ─────────────────────────────────────


def planner_fixtures(proposal):
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[{"id": "c1", "name": "propose_browser_step", "args": proposal}],
        )
    )
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            full_name="T", email="t@example.com", phone=None, linkedin_url=None
        )
    )
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalars.return_value.first.return_value = None
    return llm, db


SNAPSHOT = {
    "snapshotId": 4,
    "computer_run": "run1",
    "url": "https://portal.example/login",
    "elements": [],
}
SIGN_IN = {
    "operation": "sign_in",
    "parameters": {"usernameRef": "e1", "passwordRef": "e2", "snapshotId": 4},
    "summary": "Sign in",
}


@pytest.mark.asyncio
async def test_planner_asks_before_signing_in_without_consent():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=0
    )
    llm, db = planner_fixtures(SIGN_IN)
    with (
        patch.object(agent, "act", AsyncMock(return_value=SNAPSHOT)),
        patch.object(agent, "AsyncSessionLocal", return_value=db),
        patch.object(agent, "get_chat_gateway_llm", AsyncMock(return_value=llm)),
        patch.object(agent.nova_signin, "saved_logins", AsyncMock(return_value=[])),
        patch.object(agent.nova_signin, "saved_login", AsyncMock(return_value=SimpleNamespace())),
        patch.object(agent.nova_signin, "claim_auto_signin", AsyncMock(return_value=False)),
        patch.object(agent.nova_signin, "sign_in", AsyncMock()) as sign_in,
    ):
        result = await agent.plan(run)
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["action"]["operation"] == "sign_in"
    assert "never shown to the AI" in " ".join(result["pending_action"]["warnings"])
    sign_in.assert_not_called()


@pytest.mark.asyncio
async def test_planner_reports_missing_login_instead_of_proposing():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=0
    )
    llm, db = planner_fixtures(SIGN_IN)
    with (
        patch.object(agent, "act", AsyncMock(return_value=SNAPSHOT)),
        patch.object(agent, "AsyncSessionLocal", return_value=db),
        patch.object(agent, "get_chat_gateway_llm", AsyncMock(return_value=llm)),
        patch.object(agent.nova_signin, "saved_logins", AsyncMock(return_value=[])),
        patch.object(agent.nova_signin, "saved_login", AsyncMock(return_value=None)),
    ):
        result = await agent.plan(run)
    assert result["status"] == "completed"
    assert result["result"]["input_required"] == "login"


@pytest.mark.asyncio
async def test_planner_marks_code_steps_for_private_entry():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=0
    )
    llm, db = planner_fixtures(
        {"operation": "enter_code", "parameters": {"ref": "e3", "snapshotId": 4}, "summary": "Code"}
    )
    with (
        patch.object(agent, "act", AsyncMock(return_value=SNAPSHOT)),
        patch.object(agent, "AsyncSessionLocal", return_value=db),
        patch.object(agent, "get_chat_gateway_llm", AsyncMock(return_value=llm)),
        patch.object(agent.nova_signin, "saved_logins", AsyncMock(return_value=[])),
    ):
        result = await agent.plan(run)
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["input_required"] == "code"


@pytest.mark.asyncio
async def test_continuation_routes_code_step_through_nova():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(user_id=uuid.uuid4(), id=uuid.uuid4(), agent_type="computer_task")
    pending = {
        "type": "computer_action",
        "page_url": "https://portal.example/otp",
        "summary": "Enter code",
        "action": {
            "operation": "enter_code",
            "parameters": {"ref": "e3", "snapshotId": 4},
            "computer_run": "run1",
            "approved_snapshot": 4,
        },
    }
    with (
        patch.object(agent.nova_signin, "enter_code", AsyncMock()) as enter,
        patch.object(agent, "act", AsyncMock()) as act,
        patch.object(agent, "_record_step", AsyncMock()),
        patch.object(agent, "plan", AsyncMock(return_value={"status": "completed"})),
    ):
        await agent.continue_step(run, pending)
    assert enter.await_args.args[1] == run.id
    assert enter.await_args.args[3] == "https://portal.example/otp"
    act.assert_not_called()


# ── API ──────────────────────────────────────────────────────────


def code_client(run):
    from app.api.v1.computer import router
    from app.api.v1.deps import get_current_user, get_db

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = run
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def waiting_run(operation="enter_code"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="awaiting_approval",
        output={"type": "computer_action", "action": {"operation": operation}},
    )


def test_code_route_refuses_runs_not_waiting_for_a_code():
    with patch.object(nova_signin, "store_code", AsyncMock()) as store:
        response = code_client(waiting_run("click")).post(
            f"/computer/runs/{uuid.uuid4()}/code", json={"code": "482913"}
        )
    assert response.status_code == 409
    store.assert_not_called()


def test_code_route_never_echoes_an_invalid_code():
    response = code_client(waiting_run()).post(
        f"/computer/runs/{uuid.uuid4()}/code", json={"code": "48 29<script>"}
    )
    assert response.status_code == 422
    assert "script" not in response.text


def test_code_route_seals_code_then_approves_without_it():
    run_id = uuid.uuid4()
    with (
        patch.object(nova_signin, "store_code", AsyncMock()) as store,
        patch(
            "app.api.v1.agents.approve_or_cancel", AsyncMock(return_value={"status": "queued"})
        ) as approve,
    ):
        response = code_client(waiting_run()).post(
            f"/computer/runs/{run_id}/code", json={"code": " 482913 "}
        )
    assert response.status_code == 200
    assert store.await_args.args == (run_id, "482913")
    payload = approve.await_args.kwargs["payload"]
    assert payload.approved is True
    assert "482913" not in payload.model_dump_json()
