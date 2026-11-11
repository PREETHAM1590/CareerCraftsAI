import uuid
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agents.state import AgentState
from app.api.v1.agents import RunRequest
from app.services.workflow_service import validate_approval


@pytest.mark.parametrize(
    "context",
    [
        {"linkedin_credentials": {"password": "secret"}},
        {"nested": [{"api_key": "secret"}]},
        {"_durable": True},
    ],
)
def test_run_context_rejects_secrets_and_reserved_fields(context):
    with pytest.raises(ValidationError):
        RunRequest(task_type="auto_apply", context=context)


def test_approval_keeps_target_and_invalidates_edited_pdf():
    original = {"type": "resume_ready", "pdf_document_id": "old", "resume_markdown": "before"}
    result = validate_approval(original, {"body": "after"})
    assert result["pdf_document_id"] is None
    assert original["pdf_document_id"] == "old"
    with pytest.raises(ValueError):
        validate_approval({"type": "send_email", "to": "a@example.com"}, {"to": "b@example.com"})
    with pytest.raises(ValueError):
        validate_approval({"type": "browser_review"}, {"body": "changed"})


@pytest.mark.asyncio
async def test_real_async_graph_runner_preserves_approval():
    from app.agents.orchestrator import _auto_apply_wrapper, _make_node_runner

    graph = StateGraph(AgentState)
    graph.add_node("apply", _make_node_runner("apply", _auto_apply_wrapper))
    graph.add_edge(START, "apply")
    graph.add_edge("apply", END)
    pending = {"requires_approval": True, "type": "auto_apply_approval", "actions_pending": []}
    model_settings = object()
    pipeline = AsyncMock(return_value=pending)
    with (
        patch("app.agents.orchestrator.run_auto_apply_pipeline", pipeline),
        patch("app.agents.orchestrator.emit"),
    ):
        result = await graph.compile().ainvoke(
            {
                "user_id": str(uuid.uuid4()),
                "run_id": str(uuid.uuid4()),
                "task_type": "auto_apply",
                "status": "running",
                "context": {"_durable": True},
                "model_settings": model_settings,
            }
        )
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"] == pending
    assert pipeline.await_args.kwargs["model_settings"] is model_settings



# --- Regression tests for the durable-workflow hardening pass. ---


def _fake_session_cm(yielded):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm():
        yield yielded

    return _cm()


class _ScalarResult:
    def scalar_one(self):
        return 0

    def scalars(self):
        return self

    def all(self):
        return []


class _WorkflowFakeDB:
    """Minimal AsyncSession stand-in for the agent-run activities."""

    def __init__(self, run=None):
        self.run = run
        self.commits = 0

    async def get(self, model, key, with_for_update=False):
        from app.models.db import AgentRun

        if model is AgentRun:
            return self.run
        return None

    async def refresh(self, obj):
        pass

    async def execute(self, *args, **kwargs):
        return _ScalarResult()

    async def commit(self):
        self.commits += 1

    def add(self, obj):
        pass


def _make_queued_run():
    from app.models.db import AgentRun

    return AgentRun(
        id=uuid.uuid4(), user_id=uuid.uuid4(), agent_type="auto_apply", status="queued", input={}
    )


def _patch_activity_db(monkeypatch, fake):
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", lambda: _fake_session_cm(fake))
    monkeypatch.setattr("app.core.event_bus.publish", lambda *args: None)


@pytest.mark.asyncio
async def test_execute_activity_skips_a_run_that_is_no_longer_queued(monkeypatch):
    from app.workflows import agent_activities

    run = _make_queued_run()
    run.status = "failed"  # cancelled before the workflow got to it
    _patch_activity_db(monkeypatch, _WorkflowFakeDB(run))
    execute = AsyncMock()
    monkeypatch.setattr("app.services.workflow_service.execute_agent", execute)

    result = await agent_activities.execute_agent_run_activity({"run_id": str(run.id)})

    assert result == {"status": "failed", "action_type": None}
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_shortage_is_retried_by_temporal_not_recorded(monkeypatch):
    """No browser capacity is an admission failure before any external
    action: raise a retryable ApplicationError instead of failing the run."""
    from temporalio.exceptions import ApplicationError

    from app.services.workflow_service import CapacityUnavailable
    from app.workflows import agent_activities

    run = _make_queued_run()
    fake = _WorkflowFakeDB(run)
    _patch_activity_db(monkeypatch, fake)
    monkeypatch.setattr(
        "app.services.workflow_service.execute_agent",
        AsyncMock(side_effect=CapacityUnavailable("Browser capacity is occupied")),
    )

    with pytest.raises(ApplicationError) as exc_info:
        await agent_activities.execute_agent_run_activity({"run_id": str(run.id)})

    assert exc_info.value.type == "CapacityUnavailable"
    assert not exc_info.value.non_retryable
    assert run.status == "running"  # the retry re-claims it
    assert fake.commits == 1  # only the claim


@pytest.mark.asyncio
async def test_result_never_overwrites_a_run_cancelled_mid_flight(monkeypatch):
    from app.workflows import agent_activities

    run = _make_queued_run()
    fake = _WorkflowFakeDB(run)
    _patch_activity_db(monkeypatch, fake)

    async def _cancelled_while_running(agent_run):
        agent_run.status = "failed"  # the user cancelled meanwhile
        return {"status": "completed", "result": {"ok": True}}

    monkeypatch.setattr("app.services.workflow_service.execute_agent", _cancelled_while_running)
    result = await agent_activities.execute_agent_run_activity({"run_id": str(run.id)})

    assert result["status"] == "failed"
    assert run.output is None


@pytest.mark.asyncio
async def test_empty_job_search_failure_is_persisted(monkeypatch):
    from app.agents.job_search import job_search_agent_node
    from app.workflows import agent_activities

    run = _make_queued_run()
    run.agent_type = "job_search"
    run.input = {"context": {"titles": [], "search_query": None}}
    fake = _WorkflowFakeDB(run)
    _patch_activity_db(monkeypatch, fake)

    async def execute_agent(agent_run):
        return job_search_agent_node(
            {
                "user_id": str(agent_run.user_id),
                "run_id": str(agent_run.id),
                "task_type": "job_search",
                "context": agent_run.input["context"],
                "status": "running",
            }
        )

    monkeypatch.setattr("app.services.workflow_service.execute_agent", execute_agent)
    result = await agent_activities.execute_agent_run_activity({"run_id": str(run.id)})

    assert result["status"] == "failed"
    assert run.status == "failed"
    assert run.output["error"] == "missing: titles (or search_query)"
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_continue_action_rejects_malformed_search_confirmation():
    from unittest.mock import MagicMock

    from app.services.workflow_service import continue_action

    run = MagicMock()
    run.user_id = uuid.uuid4()
    run.id = uuid.uuid4()
    with pytest.raises(ValueError, match="interpretation"):
        await continue_action(run, {"type": "search_confirmation"})


@pytest.mark.asyncio
async def test_continue_action_marks_linkedin_outreach_reviewed():
    from unittest.mock import MagicMock

    from app.services.workflow_service import continue_action

    run = MagicMock()
    result = await continue_action(run, {"type": "linkedin_outreach", "messages": []})

    assert result["status"] == "completed"
    assert result["result"]["reviewed"] is True


@pytest.mark.asyncio
async def test_auto_apply_approval_missing_parent_fails_closed(monkeypatch):
    from unittest.mock import MagicMock

    import app.services.workflow_service as workflow_service

    run = MagicMock()
    run.id = uuid.uuid4()
    run.user_id = uuid.uuid4()

    class _MissingParentDB:
        async def get(self, *args, **kwargs):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(
        workflow_service, "AsyncSessionLocal", lambda: _fake_session_cm(_MissingParentDB())
    )
    with pytest.raises(ValueError, match="Parent run"):
        await workflow_service.continue_action(
            run, {"type": "auto_apply_approval", "actions_pending": []}
        )


class _AutoApplyBatchFakeDB:
    """Minimal AsyncSession stand-in for the batch auto_apply_approval branch.

    `attempt_lookup_results` is consumed in order, one per apply_browser item
    that reaches the ApplicationAttempt lookup (mirrors _AttemptFakeDB's
    style but needs `execute` for the SELECT rather than `get`).
    """

    def __init__(self, parent, attempt_lookup_results=()):
        from app.models.db import AgentRun

        self._agent_run_model = AgentRun
        self.parent = parent
        self._attempt_lookup_results = list(attempt_lookup_results)
        self.added = []

    async def get(self, model, key, with_for_update=False):
        if model is self._agent_run_model:
            return self.parent
        return None

    async def execute(self, *args, **kwargs):
        class _R:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        return _R(self._attempt_lookup_results.pop(0))

    def add(self, obj):
        # Mirrors the real session's flush-assigned default id, like the
        # established fake-DB pattern in _EmailFakeDB above.
        obj.id = getattr(obj, "id", None) or uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_auto_apply_approval_starts_one_application_workflow_per_job(monkeypatch):
    """Each approved application becomes its own AutoApplyWorkflow (which
    reserves the ApplicationAttempt); each email its own AgentRunWorkflow
    starting at the approved send."""
    from unittest.mock import MagicMock

    import app.services.workflow_service as workflow_service
    from app.models.db import AgentRun
    from app.workflows import starters

    run = MagicMock()
    run.id = uuid.uuid4()
    run.user_id = uuid.uuid4()
    parent = AgentRun(
        id=run.id, user_id=run.user_id, agent_type="auto_apply", status="running", output={}
    )
    job_application_id = uuid.uuid4()

    fake = _AutoApplyBatchFakeDB(parent, attempt_lookup_results=[None])
    monkeypatch.setattr(workflow_service, "AsyncSessionLocal", lambda: _fake_session_cm(fake))
    start_apply = AsyncMock(return_value={"workflow_id": "auto-apply/u/j", "status": "queued"})
    start_run = AsyncMock(return_value="agent-run/x")
    monkeypatch.setattr(starters, "start_auto_apply", start_apply)
    monkeypatch.setattr(starters, "start_agent_run", start_run)

    pending = {
        "type": "auto_apply_approval",
        "actions_pending": [
            {
                "action": "apply_browser",
                "job_url": "https://jobs.example.test/apply",
                "job_application_id": str(job_application_id),
            },
            {"action": "send_email", "recipient": "hr@acme.test", "subject": "Hi", "body": "Hello"},
        ],
    }
    result = await workflow_service.continue_action(run, pending)

    start_apply.assert_awaited_once_with(run.user_id, job_application_id)
    assert result["result"]["application_workflows"] == ["auto-apply/u/j"]
    assert result["result"]["skipped_actions"] == []

    emails = [o for o in fake.added if isinstance(o, AgentRun)]
    assert len(emails) == 1 and emails[0].status == "queued"
    assert result["result"]["child_run_ids"] == [str(emails[0].id)]
    child_id, user_id = start_run.await_args.args
    assert (child_id, user_id) == (emails[0].id, run.user_id)
    assert start_run.await_args.kwargs["initial_continuation"]["type"] == "send_email"
    assert parent.output["application_workflows"] == ["auto-apply/u/j"]


@pytest.mark.asyncio
async def test_auto_apply_approval_skips_child_with_active_attempt(monkeypatch):
    from unittest.mock import MagicMock

    import app.services.workflow_service as workflow_service
    from app.models.db import AgentRun, ApplicationAttempt
    from app.workflows import starters

    run = MagicMock()
    run.id = uuid.uuid4()
    run.user_id = uuid.uuid4()
    parent = AgentRun(
        id=run.id, user_id=run.user_id, agent_type="auto_apply", status="running", output={}
    )
    job_application_id = uuid.uuid4()
    existing_attempt = ApplicationAttempt(
        id=uuid.uuid4(),
        user_id=run.user_id,
        job_application_id=job_application_id,
        state="submitting",
    )

    fake = _AutoApplyBatchFakeDB(parent, attempt_lookup_results=[existing_attempt])
    monkeypatch.setattr(workflow_service, "AsyncSessionLocal", lambda: _fake_session_cm(fake))
    start_apply = AsyncMock()
    monkeypatch.setattr(starters, "start_auto_apply", start_apply)

    pending = {
        "type": "auto_apply_approval",
        "actions_pending": [
            {
                "action": "apply_browser",
                "job_url": "https://jobs.example.test/apply",
                "job_application_id": str(job_application_id),
            }
        ],
    }
    result = await workflow_service.continue_action(run, pending)

    assert result["result"]["child_run_ids"] == []
    assert len(result["result"]["skipped_actions"]) == 1
    skipped = result["result"]["skipped_actions"][0]
    assert skipped["job_application_id"] == str(job_application_id)
    assert "submitting" in skipped["reason"]
    start_apply.assert_not_awaited()
    assert not fake.added


@pytest.mark.asyncio
async def test_auto_apply_approval_skips_apply_browser_without_job_application_id(monkeypatch):
    from unittest.mock import MagicMock

    import app.services.workflow_service as workflow_service
    from app.models.db import AgentRun

    run = MagicMock()
    run.id = uuid.uuid4()
    run.user_id = uuid.uuid4()
    parent = AgentRun(
        id=run.id, user_id=run.user_id, agent_type="auto_apply", status="running", output={}
    )

    fake = _AutoApplyBatchFakeDB(parent, attempt_lookup_results=[])
    monkeypatch.setattr(workflow_service, "AsyncSessionLocal", lambda: _fake_session_cm(fake))

    pending = {
        "type": "auto_apply_approval",
        "actions_pending": [
            {"action": "apply_browser", "job_url": "https://jobs.example.test/apply"}
        ],
    }
    result = await workflow_service.continue_action(run, pending)

    assert result["result"]["child_run_ids"] == []
    assert len(result["result"]["skipped_actions"]) == 1
    assert "job_application_id" in result["result"]["skipped_actions"][0]["reason"]


# --- Task 2: idempotent application submission + email sends ---


class _AttemptFakeDB:
    """Minimal AsyncSession stand-in for ApplicationAttempt row-lock reads."""

    def __init__(self, attempt=None):
        self.attempt = attempt
        self.commits = 0

    async def get(self, model, key, with_for_update=False):
        from app.models.db import ApplicationAttempt

        if model is ApplicationAttempt:
            return self.attempt
        return None

    async def commit(self):
        self.commits += 1


def _make_attempt(state="awaiting_approval"):
    from app.models.db import ApplicationAttempt

    return ApplicationAttempt(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        job_application_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        state=state,
    )


@pytest.mark.asyncio
async def test_maintenance_flips_stuck_submitting_attempt(monkeypatch):
    """A worker that dies mid-submit leaves the attempt "submitting" and its
    workflow failed; reconciliation records outcome_unknown (never retries)."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    from temporalio.service import RPCError, RPCStatusCode

    from app.models.db import AgentRun, ApplicationAttempt
    from app.workflows import job_activities

    user_id = uuid.uuid4()
    run = AgentRun(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_type="apply_prepare",
        status="running",
        input={"workflow_id": "auto-apply/u/j"},
        started_at=datetime.now(UTC) - timedelta(hours=1),
    )
    attempt = ApplicationAttempt(
        id=uuid.uuid4(),
        user_id=user_id,
        job_application_id=uuid.uuid4(),
        run_id=run.id,
        state="submitting",
    )

    class _ScalarsList:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

    class _MaintenanceDB:
        async def execute(self, statement, *a, **k):
            compiled = str(statement).lower()
            if "from application_attempts" in compiled:
                return _ScalarsList([attempt])
            if "from extension_tasks" in compiled or "from users" in compiled:
                return _ScalarsList([])  # no tasks; no account due for deletion
            return _ScalarsList([run])

        async def get(self, model, key, with_for_update=False):
            return run if model is AgentRun else None

        async def commit(self):
            pass

    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RPCError("failed", RPCStatusCode.NOT_FOUND, b""))
    client = MagicMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    monkeypatch.setattr(
        "app.core.database.AsyncSessionLocal", lambda: _fake_session_cm(_MaintenanceDB())
    )
    monkeypatch.setattr(
        "app.core.temporal_client.get_temporal_client", AsyncMock(return_value=client)
    )
    monkeypatch.setattr("app.core.event_bus.publish", lambda *args: None)

    await job_activities.maintenance_activity({})

    client.get_workflow_handle.assert_called_once_with("auto-apply/u/j")
    assert run.status == "failed"
    assert attempt.state == "outcome_unknown"
    assert "verify the external outcome" in attempt.last_error


@pytest.mark.asyncio
async def test_send_approved_email_suppresses_concurrent_duplicate(monkeypatch):
    import app.services.workflow_service as workflow_service
    from app.models.db import OutboundMessage

    already_sending = OutboundMessage(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        channel="email",
        recipient="hr@acme.test",
        subject="Following up",
        body_hash="x",
        state="sending",
        idempotency_key="agent_run:same-run",
    )

    class _OneRow:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

    class _EmailFakeDB:
        async def execute(self, *a, **k):
            # The ON CONFLICT insert is a no-op: the row already exists.
            return _OneRow(already_sending)

        async def commit(self):
            return None

    monkeypatch.setattr(
        workflow_service, "AsyncSessionLocal", lambda: _fake_session_cm(_EmailFakeDB())
    )
    gmail_send = AsyncMock()
    monkeypatch.setattr("app.services.gmail_service.GmailMCPClient.send_message", gmail_send)

    result = await workflow_service.send_approved_email(
        already_sending.user_id,
        already_sending.run_id,
        "hr@acme.test",
        "Following up",
        "body",
    )
    assert result["duplicate_suppressed"] is True
    gmail_send.assert_not_called()


@pytest.mark.asyncio
async def test_send_approved_email_success_marks_sent(monkeypatch):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql.dml import Insert

    import app.services.workflow_service as workflow_service
    from app.models.db import OutboundMessage

    class _OneRow:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

    class _EmailFakeDB:
        """Models the insert-if-absent, then SELECT … FOR UPDATE sequence."""

        def __init__(self):
            self.added = None

        async def execute(self, statement, *a, **k):
            if isinstance(statement, Insert):
                values = statement.compile(dialect=postgresql.dialect()).params
                if self.added is None:
                    self.added = OutboundMessage(**values)
                return _OneRow(None)
            return _OneRow(self.added)

        async def flush(self):
            pass

        async def get(self, model, key, with_for_update=False):
            return self.added

        async def commit(self):
            pass

    fake = _EmailFakeDB()
    monkeypatch.setattr(workflow_service, "AsyncSessionLocal", lambda: _fake_session_cm(fake))
    monkeypatch.setattr(
        "app.services.gmail_service.GmailMCPClient.send_message",
        lambda self, to, subject, body: {"id": "gmail-msg-1"},
    )

    result = await workflow_service.send_approved_email(
        uuid.uuid4(),
        uuid.uuid4(),
        "hr@acme.test",
        "Following up",
        "body text",
    )
    assert result["sent"] is True
    assert result["provider_message_id"] == "gmail-msg-1"
    assert fake.added.state == "sent"
    assert fake.added.provider_message_id == "gmail-msg-1"


# --- Task 3 slice 3a: answer-required checkpoint integration ---
