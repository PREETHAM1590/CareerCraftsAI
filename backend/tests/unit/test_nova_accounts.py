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
        db.rollback = AsyncMock()

        async def execute(statement):
            self.events.append("sign_in_on")

        db.execute = execute
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
        patch.object(
            nova_signin.computer_service,
            "computer_backend",
            AsyncMock(return_value={"backend": "cloud"}),
        ),
    ):
        yield rec


@pytest.mark.asyncio
async def test_password_is_saved_to_the_vault_before_it_is_typed(recorder):
    result = await nova_signin.create_account(USER, signup(confirmRef="e3"), PAGE)
    # Saved first with sign-in off; turned on only once the site took the form.
    assert recorder.events == ["vault", "type", "sign_in_on"]
    [row] = recorder.saved
    assert row.origin == ORIGIN
    assert row.created_by_nova is True and row.auto_signin is False
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


# ── Review fixes ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409, 422])
async def test_refusal_before_typing_forgets_the_login_so_retries_work(recorder, status):
    from fastapi import HTTPException

    async def refuse(user_id, body, audit):
        recorder.events.append("type")
        raise HTTPException(status, "refused before typing")

    with (
        patch.object(nova_signin, "_dispatch", refuse),
        patch.object(nova_signin, "_forget_login", AsyncMock()) as forget,
    ):
        with pytest.raises(HTTPException):
            await nova_signin.create_account(USER, signup(), PAGE)
    [row] = recorder.saved
    forget.assert_awaited_once_with(uuid.UUID(USER), ORIGIN, row.password_enc)
    assert "sign_in_on" not in recorder.events


@pytest.mark.asyncio
async def test_unknown_outcome_keeps_the_password_with_sign_in_off(recorder):
    from fastapi import HTTPException

    async def lost(user_id, body, audit):
        raise HTTPException(502, "Private credential entry failed")

    with (
        patch.object(nova_signin, "_dispatch", lost),
        patch.object(nova_signin, "_forget_login", AsyncMock()) as forget,
    ):
        with pytest.raises(HTTPException):
            await nova_signin.create_account(USER, signup(), PAGE)
    forget.assert_not_called()
    [row] = recorder.saved
    assert row.auto_signin is False
    assert "sign_in_on" not in recorder.events


@pytest.mark.asyncio
async def test_unsubmitted_form_keeps_the_login_with_sign_in_off(recorder):
    async def filled_not_sent(user_id, body, audit):
        return {"filled": True, "submitted": False}

    with patch.object(nova_signin, "_dispatch", filled_not_sent):
        result = await nova_signin.create_account(USER, signup(), PAGE)
    assert result["submitted"] is False
    assert "sign_in_on" not in recorder.events


@pytest.mark.asyncio
async def test_a_login_saved_concurrently_is_a_clear_refusal(recorder):
    from sqlalchemy.exc import IntegrityError

    recorder.db.commit = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("dup")))
    with pytest.raises(ValueError, match="already have a saved login"):
        await nova_signin.create_account(USER, signup(), PAGE)
    recorder.db.rollback.assert_awaited_once()
    assert "type" not in recorder.events


@pytest.mark.asyncio
async def test_browser_mode_is_refused_before_using_a_try(recorder):
    with (
        patch.object(
            nova_signin.computer_service,
            "computer_backend",
            AsyncMock(return_value={"backend": "browser"}),
        ),
        patch.object(nova_signin, "claim_signup", AsyncMock(return_value=True)) as claim,
    ):
        with pytest.raises(ValueError, match="cloud computer"):
            await nova_signin.create_account(USER, signup(), PAGE)
    claim.assert_not_called()
    assert recorder.events == []


@pytest.mark.asyncio
async def test_email_problems_do_not_use_up_a_daily_try(recorder):
    with (
        patch.object(nova_signin, "signup_email", AsyncMock(side_effect=ValueError("no email"))),
        patch.object(nova_signin, "claim_signup", AsyncMock(return_value=True)) as claim,
    ):
        with pytest.raises(ValueError, match="no email"):
            await nova_signin.create_account(USER, signup(), PAGE)
    claim.assert_not_called()


def alias_session():
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get = AsyncMock(return_value=SimpleNamespace(id=USER, email="m@x.co"))
    return db


@pytest.mark.asyncio
async def test_alias_allocation_failure_becomes_a_message():
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=alias_session()),
        patch("app.services.apply_inbox.alias_for", AsyncMock(return_value=None)),
        patch("app.services.apply_inbox.configured", return_value=True),
        patch(
            "app.services.apply_inbox.get_or_create_alias",
            AsyncMock(side_effect=RuntimeError("Could not allocate")),
        ),
    ):
        with pytest.raises(ValueError, match="application email"):
            await nova_signin.signup_email(USER)


@pytest.mark.asyncio
async def test_never_signs_up_with_the_personal_account_email():
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=alias_session()),
        patch("app.services.apply_inbox.alias_for", AsyncMock(return_value=None)),
        patch("app.services.apply_inbox.configured", return_value=False),
    ):
        with pytest.raises(ValueError, match="application email"):
            await nova_signin.signup_email(USER)


async def plan_with_create_account_result(outcome):
    """Run plan() once with create_account returning or raising `outcome`."""
    from contextlib import ExitStack

    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(), id=uuid.uuid4(), input={"context": {"task": "Apply"}}, tokens_used=5
    )
    llm, db = planner(PROPOSAL)
    llm.bind_tools.return_value.ainvoke.return_value.usage_metadata = {"total_tokens": 100}
    original = agent.plan
    with ExitStack() as stack:
        for p in planner_patches(agent, llm, db):
            stack.enter_context(p)
        stack.enter_context(
            patch.object(agent.nova_signin, "account_consent", AsyncMock(return_value=CONSENT))
        )
        mock = (
            AsyncMock(side_effect=outcome)
            if isinstance(outcome, Exception)
            else AsyncMock(return_value=outcome)
        )
        stack.enter_context(patch.object(agent.nova_signin, "create_account", mock))
        stack.enter_context(patch.object(agent, "_record_step", AsyncMock()))
        replan = stack.enter_context(
            patch.object(agent, "plan", AsyncMock(return_value={"status": "completed"}))
        )
        result = await original(run)
    return run, result, replan


@pytest.mark.asyncio
async def test_cloud_only_refusal_is_reported_not_retried():
    from fastapi import HTTPException

    run, result, replan = await plan_with_create_account_result(
        HTTPException(409, nova_signin.CLOUD_ONLY)
    )
    replan.assert_not_called()
    assert result["status"] == "completed"
    assert nova_signin.CLOUD_ONLY in result["result"]["message"]


@pytest.mark.asyncio
async def test_busy_computer_replans_with_a_note():
    from fastapi import HTTPException

    detail = "Browser state changed or human control is active; refresh before continuing"
    run, result, replan = await plan_with_create_account_result(HTTPException(409, detail))
    assert result == {"status": "completed"}
    assert detail in replan.await_args.args[1]
    assert run.tokens_used == 105


@pytest.mark.asyncio
async def test_other_computer_errors_end_with_a_message():
    from fastapi import HTTPException

    _, result, replan = await plan_with_create_account_result(
        HTTPException(422, "Confirm field must be a password input on this site")
    )
    replan.assert_not_called()
    assert "Confirm field" in result["result"]["message"]


@pytest.mark.asyncio
async def test_tokens_of_the_proposal_carry_into_the_next_plan():
    run, _, replan = await plan_with_create_account_result(
        {"submitted": True, "origin": ORIGIN, "email": "a@b.co"}
    )
    replan.assert_awaited_once()
    assert run.tokens_used == 105


@pytest.mark.asyncio
async def test_unaccepted_sign_up_form_stops_instead_of_replanning():
    run, result, replan = await plan_with_create_account_result(
        {"submitted": False, "origin": ORIGIN, "email": "a@b.co"}
    )
    replan.assert_not_called()
    assert result["status"] == "completed"
    assert result["result"]["input_required"] == "signup"
    assert "didn't accept" in result["result"]["message"]


@pytest.mark.asyncio
async def test_stored_create_account_payload_is_re_proposed_not_replayed():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        agent_type="computer_task", user_id=uuid.uuid4(), id=uuid.uuid4(), input={}, tokens_used=0
    )
    pending = {"action": signup().model_dump(), "page_url": PAGE, "summary": "Create account"}
    with (
        patch.object(agent.nova_signin, "create_account", AsyncMock()) as create,
        patch.object(
            agent, "plan", AsyncMock(return_value={"status": "awaiting_approval"})
        ) as replan,
    ):
        result = await agent.continue_step(run, pending)
    create.assert_not_called()
    assert result == {"status": "awaiting_approval"}
    assert "fresh proposal" in replan.await_args.args[1]


@pytest.mark.asyncio
async def test_automatic_sign_in_rechecks_the_login_it_types(monkeypatch):
    row = SimpleNamespace(auto_signin=False, username_enc="u", password_enc="p")
    with (
        patch.object(nova_signin, "saved_login", AsyncMock(return_value=row)),
        patch.object(nova_signin, "_dispatch", AsyncMock()) as dispatch,
    ):
        with pytest.raises(ValueError, match="Automatic sign-in is off"):
            await nova_signin.sign_in(
                USER,
                ComputerAction(
                    operation="sign_in",
                    parameters={"usernameRef": "e1", "passwordRef": "e2", "snapshotId": 4},
                    computer_run="run1",
                    approved_snapshot=4,
                ),
                PAGE,
                automatic=True,
            )
    dispatch.assert_not_called()
