"""Nova creating portal accounts: consent per site, vault before typing, limits."""

import string
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import nova_signin
from app.services.computer_service import NOVA_STEPS, ComputerAction

USER = "8f7c2b5e-6a0d-4f7e-9d6c-0b1a2c3d4e5f"
PAGE = "https://acme.wd5.myworkdayjobs.com/en-US/careers/signup"
ORIGIN = "https://acme.wd5.myworkdayjobs.com"


def signup(**params):
    base = {"emailRef": "e1", "passwordRef": "e2", "snapshotId": 4, **params}
    return ComputerAction(
        operation="create_account", parameters=base, computer_run="run1", approved_snapshot=4
    )


# ── The step itself ─────────────────────────────────────────────


def test_create_account_takes_refs_only():
    signup().validate_operation(NOVA_STEPS)
    signup(confirmRef="e3").validate_operation(NOVA_STEPS)
    with pytest.raises(ValueError):
        signup(password="hunter2").validate_operation(NOVA_STEPS)
    with pytest.raises(ValueError):
        signup(email="me@example.com").validate_operation(NOVA_STEPS)
    with pytest.raises(ValueError):
        signup(confirmRef="e3; drop").validate_operation(NOVA_STEPS)


def test_new_passwords_meet_common_rules_and_differ():
    passwords = {nova_signin.new_password() for _ in range(50)}
    assert len(passwords) == 50
    for value in passwords:
        assert len(value) == 20
        assert any(c in string.ascii_lowercase for c in value)
        assert any(c in string.ascii_uppercase for c in value)
        assert any(c in string.digits for c in value)
        assert any(c in "!@#$%^&*-_+=" for c in value)


# ── create_account ──────────────────────────────────────────────


class Recorder:
    """Records vault writes and relay dispatches in the order they happen."""

    def __init__(self):
        self.events = []
        self.saved = []
        db = MagicMock()
        db.__aenter__ = AsyncMock(return_value=db)
        db.__aexit__ = AsyncMock(return_value=False)
        db.add = lambda row: (self.saved.append(row), self.events.append("vault"))
        db.commit = AsyncMock()
        self.db = db

    async def dispatch(self, user_id, body, audit):
        self.events.append("type")
        self.body = body
        return {"filled": True, "submitted": True}


CONSENT = SimpleNamespace(consent_at="2026-11-24T00:00:00+00:00", consent_version="2026-11-24")


@pytest.fixture
def recorder():
    rec = Recorder()
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=rec.db),
        patch.object(nova_signin, "_dispatch", rec.dispatch),
        patch.object(nova_signin, "signup_email", AsyncMock(return_value="priya.k7f2m@apply.ex")),
        patch.object(nova_signin, "claim_signup", AsyncMock(return_value=True)),
        patch.object(nova_signin, "saved_login", AsyncMock(return_value=None)),
        patch.object(nova_signin, "account_consent", AsyncMock(return_value=CONSENT)),
        patch.object(nova_signin, "encrypt_api_key", side_effect=lambda v, _: f"sealed({v})"),
    ):
        yield rec


@pytest.mark.asyncio
async def test_password_is_saved_to_the_vault_before_it_is_typed(recorder):
    result = await nova_signin.create_account(USER, signup(confirmRef="e3"), PAGE)
    assert recorder.events == ["vault", "type"]
    [row] = recorder.saved
    assert row.origin == ORIGIN
    assert row.created_by_nova is True and row.auto_signin is True
    assert row.username_enc == "sealed(priya.k7f2m@apply.ex)"
    params = recorder.body["parameters"]
    assert recorder.body["actor"] == "nova"
    assert recorder.body["operation"] == "credentials/fill"
    assert params["usernameRef"] == "e1" and params["confirmRef"] == "e3"
    assert params["submit"] is True
    assert row.password_enc == f"sealed({params['password']})"
    assert result == {"submitted": True, "origin": ORIGIN, "email": "priya.k7f2m@apply.ex"}


@pytest.mark.asyncio
async def test_no_consent_no_account(recorder):
    with patch.object(nova_signin, "account_consent", AsyncMock(return_value=None)):
        with pytest.raises(ValueError, match="Allow Nova"):
            await nova_signin.create_account(USER, signup(), PAGE)
    assert recorder.events == []


@pytest.mark.asyncio
async def test_existing_login_means_sign_in_not_a_second_account(recorder):
    with patch.object(nova_signin, "saved_login", AsyncMock(return_value=SimpleNamespace())):
        with pytest.raises(ValueError, match="already have a saved login"):
            await nova_signin.create_account(USER, signup(), PAGE)
    assert recorder.events == []


@pytest.mark.asyncio
async def test_daily_limit_stops_retries(recorder):
    with patch.object(nova_signin, "claim_signup", AsyncMock(return_value=False)):
        with pytest.raises(ValueError, match="today"):
            await nova_signin.create_account(USER, signup(), PAGE)
    assert recorder.events == []


@pytest.mark.asyncio
async def test_signup_limit_counts_per_site_per_day():
    class Counter:
        def __init__(self):
            self.n = {}

        async def incr(self, key):
            self.n[key] = self.n.get(key, 0) + 1
            return self.n[key]

        async def expire(self, key, seconds):
            return True

    counter = Counter()
    with patch.object(nova_signin.redis_client, "get_redis", return_value=counter):
        tries = [await nova_signin.claim_signup(USER, ORIGIN) for _ in range(4)]
        other = await nova_signin.claim_signup(USER, "https://jobs.example.com")
    assert tries == [True, True, True, False]
    assert other is True


# ── Planner ─────────────────────────────────────────────────────


def planner(proposal):
    from langchain_core.messages import AIMessage

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
        return_value=SimpleNamespace(full_name="T", email="t@x.co", phone=None, linkedin_url=None)
    )
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalars.return_value.first.return_value = None
    return llm, db


SNAPSHOT = {"snapshotId": 4, "computer_run": "run1", "url": PAGE, "elements": []}
PROPOSAL = {
    "operation": "create_account",
    "parameters": {"emailRef": "e1", "passwordRef": "e2", "snapshotId": 4},
    "summary": "Create account",
}


def planner_patches(agent, llm, db):
    return (
        patch.object(agent, "act", AsyncMock(return_value=SNAPSHOT)),
        patch.object(agent, "AsyncSessionLocal", return_value=db),
        patch.object(agent, "get_chat_gateway_llm", AsyncMock(return_value=llm)),
        patch.object(agent.nova_signin, "saved_logins", AsyncMock(return_value=[])),
        patch.object(agent.nova_signin, "account_sites", AsyncMock(return_value=[ORIGIN])),
    )


@pytest.mark.asyncio
async def test_planner_asks_for_consent_on_unlisted_sites():
    from contextlib import ExitStack

    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=0
    )
    llm, db = planner(PROPOSAL)
    with ExitStack() as stack:
        for p in planner_patches(agent, llm, db):
            stack.enter_context(p)
        stack.enter_context(
            patch.object(agent.nova_signin, "account_consent", AsyncMock(return_value=None))
        )
        create = stack.enter_context(patch.object(agent.nova_signin, "create_account", AsyncMock()))
        result = await agent.plan(run)
    assert result["result"]["input_required"] == "signup"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_planner_creates_the_account_on_allowed_sites_and_continues():
    from contextlib import ExitStack

    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=0
    )
    llm, db = planner(PROPOSAL)
    original = agent.plan
    with ExitStack() as stack:
        for p in planner_patches(agent, llm, db):
            stack.enter_context(p)
        stack.enter_context(
            patch.object(agent.nova_signin, "account_consent", AsyncMock(return_value=CONSENT))
        )
        create = stack.enter_context(
            patch.object(
                agent.nova_signin,
                "create_account",
                AsyncMock(return_value={"submitted": True, "origin": ORIGIN, "email": "a@b.co"}),
            )
        )
        record = stack.enter_context(patch.object(agent, "_record_step", AsyncMock()))
        replan = stack.enter_context(
            patch.object(agent, "plan", AsyncMock(return_value={"status": "completed"}))
        )
        result = await original(run)
    assert result == {"status": "completed"}
    create.assert_awaited_once()
    assert record.await_args.args[1] == "create_account"
    assert "a@b.co" in record.await_args.args[2]
    replan.assert_awaited_once()


@pytest.mark.asyncio
async def test_planner_reports_refusals_instead_of_failing():
    from contextlib import ExitStack

    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=0
    )
    llm, db = planner(PROPOSAL)
    with ExitStack() as stack:
        for p in planner_patches(agent, llm, db):
            stack.enter_context(p)
        stack.enter_context(
            patch.object(agent.nova_signin, "account_consent", AsyncMock(return_value=CONSENT))
        )
        stack.enter_context(
            patch.object(
                agent.nova_signin,
                "create_account",
                AsyncMock(side_effect=ValueError("Nova already tried today.")),
            )
        )
        result = await agent.plan(run)
    assert result["status"] == "completed"
    assert result["result"]["message"] == "Nova already tried today."


# ── Consent API ─────────────────────────────────────────────────


def consent_client(existing=None):
    from app.api.v1.computer import router
    from app.api.v1.deps import get_current_user, get_db

    db = MagicMock()
    db.get = AsyncMock(return_value=existing)
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


def test_allowing_a_site_stores_exact_origin_and_consent_version():
    client, db = consent_client()
    with patch("app.services.computer_service.audit", AsyncMock()):
        response = client.post(
            "/computer/account-sites", json={"origin": "https://Jobs.Example.com/"}
        )
    assert response.json() == {"origin": "https://jobs.example.com", "allowed": True}
    row = db.add.call_args.args[0]
    assert row.origin == "https://jobs.example.com"
    assert row.consent_version == nova_signin.CONSENT_VERSION


@pytest.mark.parametrize("origin", ["http://jobs.example.com", "https://jobs.example.com/path"])
def test_sites_must_be_exact_https_origins(origin):
    client, db = consent_client()
    assert client.post("/computer/account-sites", json={"origin": origin}).status_code == 422
    db.add.assert_not_called()


def test_removing_a_site():
    client, db = consent_client(existing=SimpleNamespace())
    with patch("app.services.computer_service.audit", AsyncMock()):
        response = client.delete(
            "/computer/account-sites", params={"origin": "https://jobs.example.com"}
        )
    assert response.json() == {"origin": "https://jobs.example.com", "allowed": False}
    db.delete.assert_awaited_once()
