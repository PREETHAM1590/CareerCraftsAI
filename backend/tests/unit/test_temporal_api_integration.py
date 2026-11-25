"""API-layer Temporal integration: starting AutoApplyWorkflow from
prepare-apply, and routing approve/cancel through workflow signals for an
application run. The ownership/job-url/resume checks that run before are
covered by test_fixes_e2e.py's test_prepare_application_apply_* tests.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError


@pytest.mark.asyncio
async def test_start_temporal_auto_apply_starts_workflow_with_stable_id(monkeypatch):
    from app.api.v1 import jobs as jobs_module
    from app.workflows.auto_apply import auto_apply_workflow_id

    user_id = uuid.uuid4()
    application_id = uuid.uuid4()

    fake_client = MagicMock()
    fake_client.start_workflow = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(
        "app.workflows.starters.get_temporal_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(jobs_module, "_await_temporal_run_id", AsyncMock(return_value="run-123"))

    result = await jobs_module._start_temporal_auto_apply(user_id, application_id)

    expected_id = auto_apply_workflow_id(str(user_id), str(application_id))
    fake_client.start_workflow.assert_awaited_once()
    intent = fake_client.start_workflow.call_args.args[1]
    call_kwargs = fake_client.start_workflow.call_args.kwargs
    assert call_kwargs["id"] == expected_id
    assert intent.mode == "extension"
    # The run id is chosen up front, so the response never carries the run
    # of a previous attempt for the same application.
    assert intent.run_id
    jobs_module._await_temporal_run_id.assert_awaited_once_with(
        user_id, application_id, intent.run_id
    )
    # The extension flow waits on a person and bounds itself with timers.
    assert call_kwargs.get("execution_timeout") is None
    assert result == {
        "run_id": "run-123",
        "workflow_id": expected_id,
        "engine": "temporal",
        "mode": "extension",
        "status": "queued",
    }


@pytest.mark.asyncio
async def test_start_temporal_auto_apply_reuses_existing_workflow_on_repeated_start(
    monkeypatch,
):
    """Required behavior: repeated start requests return the existing run
    instead of creating an orphan — Temporal itself enforces this via the
    stable workflow id; this test proves the API layer handles that
    rejection gracefully instead of raising a 500."""
    from app.api.v1 import jobs as jobs_module

    user_id = uuid.uuid4()
    application_id = uuid.uuid4()

    fake_client = MagicMock()
    fake_client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError(
            workflow_id="wf-1",
            workflow_type="AutoApplyWorkflow",
        )
    )
    monkeypatch.setattr(
        "app.workflows.starters.get_temporal_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(
        jobs_module, "_await_temporal_run_id", AsyncMock(return_value="run-existing")
    )

    result = await jobs_module._start_temporal_auto_apply(user_id, application_id)

    assert result["status"] == "already_running"
    assert result["run_id"] == "run-existing"


@pytest.mark.asyncio
async def test_start_temporal_auto_apply_answers_503_when_temporal_is_down(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import jobs as jobs_module

    monkeypatch.setattr(
        "app.workflows.starters.get_temporal_client",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    )
    with pytest.raises(HTTPException) as exc_info:
        await jobs_module._start_temporal_auto_apply(uuid.uuid4(), uuid.uuid4())
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_reject_cancels_the_pending_application_attempt():
    """Rejecting must close the attempt row, not just the workflow's in-memory
    state, so claim_attempt_for_submit can never claim a rejected attempt."""
    import app.api.v1.agents as agents_module
    from app.models.db import AgentRun, ApplicationAttempt

    run = AgentRun(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        agent_type="apply_prepare",
        status="awaiting_approval",
        input={"engine": "temporal", "workflow_id": "auto-apply/u1/j1"},
    )
    attempt = ApplicationAttempt(id=uuid.uuid4(), run_id=run.id, state="awaiting_approval")
    other_run_attempt = ApplicationAttempt(
        id=uuid.uuid4(), run_id=uuid.uuid4(), state="awaiting_approval"
    )

    fake_handle = MagicMock()
    fake_handle.signal = AsyncMock()
    reject = agents_module.approve_or_cancel.__wrapped__  # skip slowapi's Request check

    for target, expected in ((attempt, "cancelled"), (other_run_attempt, "awaiting_approval")):
        run.status = "awaiting_approval"
        run.output = {"attempt_id": str(target.id)}
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
        )
        db.get = AsyncMock(return_value=target)
        db.commit = AsyncMock()
        with (
            patch("app.workflows.starters.auto_apply_handle", AsyncMock(return_value=fake_handle)),
            patch.object(agents_module, "publish"),
        ):
            result = await reject(
                str(run.id),
                agents_module.ApproveRequest(approved=False),
                None,
                db=db,
                current_user=MagicMock(id=run.user_id),
            )
        assert result == {"status": "cancelled"}
        assert target.state == expected


@pytest.mark.asyncio
async def test_approval_queue_identifies_extension_runs_without_exposing_input():
    from app.api.v1 import agents
    from app.models.db import AgentRun

    user_id = uuid.uuid4()
    application = AgentRun(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_type="apply_prepare",
        status="awaiting_approval",
        input={"workflow_id": "auto-apply/u1/j1"},
    )
    draft = AgentRun(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_type="cover_letter",
        status="awaiting_approval",
        output={"cover_letter": "A draft to review"},
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [application, draft]
    db = MagicMock(execute=AsyncMock(return_value=result))

    data = await agents.list_runs.__wrapped__(
        None, status="awaiting_approval", db=db, current_user=MagicMock(id=user_id)
    )

    app_row, draft_row = data["runs"]
    assert app_row["requires_extension"] is True
    assert "input" not in app_row
    assert draft_row["requires_extension"] is False
    assert draft_row["output"] == draft.output
