"""Device approval is required, fresh and at most once."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import extension
from app.api.v1.deps import get_db
from app.models.db import ApplicationAttempt, ExtensionDevice, ExtensionTask

MANUAL_APPROVAL = {"review_hash": "a" * 64, "user_confirmed": True, "auto_approved": False}


@pytest.fixture
def gate(monkeypatch):
    device = ExtensionDevice(id=uuid.uuid4(), user_id=uuid.uuid4())
    task = ExtensionTask(
        id=uuid.uuid4(),
        user_id=device.user_id,
        device_id=device.id,
        status="review",
        review_hash="a" * 64,
        review_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        workflow_id="test",
        payload={},
    )
    attempt = ApplicationAttempt(id=uuid.uuid4(), state="awaiting_approval")
    db = MagicMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(extension, "_device_task", AsyncMock(return_value=task))
    monkeypatch.setattr(extension, "_task_attempt", AsyncMock(return_value=attempt))
    signal = AsyncMock()
    monkeypatch.setattr(extension, "_signal", signal)
    app = FastAPI()
    app.include_router(extension.router)
    app.dependency_overrides[extension.get_device] = lambda: device
    app.dependency_overrides[get_db] = lambda: db
    return app, task, attempt, signal


@pytest.mark.asyncio
async def test_approval_once_and_capability_required(gate):
    app, task, attempt, signal = gate
    prefix = f"/extension/device/tasks/{task.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        bypass = await client.post(
            prefix + "/events",
            json={"stage": "submitted", "confirmation_text": "Application received"},
        )
        assert bypass.status_code == 409
        signal.assert_not_awaited()
        approved = await client.post(prefix + "/approve-submit", json=MANUAL_APPROVAL)
        assert approved.status_code == 200
        token = approved.json()["submission_token"]
        assert task.submission_token_hash != token
        assert attempt.state == "submitting"
        repeated = await client.post(prefix + "/approve-submit", json=MANUAL_APPROVAL)
        assert repeated.status_code == 409
        bad_token = await client.post(
            prefix + "/events",
            json={
                "stage": "submitted",
                "submission_token": "wrong",
                "confirmation_text": "Application received",
            },
        )
        assert bad_token.status_code == 409
        result = await client.post(
            prefix + "/events",
            json={
                "stage": "submitted",
                "submission_token": token,
                "confirmation_text": "Application received",
            },
        )
        assert result.status_code == 200
        assert task.submission_reported_at
        repeated = await client.post(
            prefix + "/events",
            json={
                "stage": "submitted",
                "submission_token": token,
                "confirmation_text": "Application received",
            },
        )
        assert repeated.status_code == 409
        signal.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_review_cannot_approve(gate):
    app, task, attempt, _ = gate
    task.review_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/extension/device/tasks/{task.id}/approve-submit",
            json=MANUAL_APPROVAL,
        )
    assert response.status_code == 409
    assert attempt.state == "awaiting_approval"


@pytest.mark.asyncio
async def test_site_challenge_stops_without_retry(monkeypatch):
    from app.services import browser_control_service as browser

    run = AsyncMock(return_value="Please complete the security check: captcha")
    monkeypatch.setattr(browser, "run_browser_task", run)
    with pytest.raises(browser.CaptchaBlocked):
        await browser.run_browser_task_with_captcha_retry(None, "search", "user")
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,approved,expected,extra",
    [
        ("submitted", True, "submitted", {}),
        ("submitted", False, "outcome_unknown", {}),
        ("expired", True, "outcome_unknown", {}),
        ("failed", True, "outcome_unknown", {}),
        ("failed", True, "outcome_unknown", {"submit_attempted": True}),
        # Permit never consumed: nothing was clicked, so the job stays retryable.
        ("failed", True, "failed", {"submit_attempted": False}),
    ],
)
async def test_worker_requires_persisted_authority_and_preserves_uncertainty(
    monkeypatch,
    outcome,
    approved,
    expected,
    extra,
):
    from app.core import database, event_bus
    from app.models.db import AgentRun, JobApplication
    from app.workflows.extension_activities import finish_extension_task_activity

    user_id, job_id, run_id, task_id, attempt_id = [uuid.uuid4() for _ in range(5)]
    now = datetime.now(UTC)
    task = ExtensionTask(
        id=task_id,
        user_id=user_id,
        job_application_id=job_id,
        run_id=run_id,
        status="submitting" if approved else "review",
        review_hash="a" * 64,
        approved_at=now if approved else None,
        submission_reported_at=now if approved else None,
    )
    attempt = ApplicationAttempt(
        id=attempt_id,
        user_id=user_id,
        run_id=run_id,
        job_application_id=job_id,
        state="submitting" if approved else "awaiting_approval",
        approved_snapshot_hash="a" * 64 if approved else None,
    )
    job = JobApplication(id=job_id, user_id=user_id, status="saved")
    run = AgentRun(id=run_id, user_id=user_id, status="running")
    db = MagicMock()
    db.get = AsyncMock(
        side_effect=lambda model, key, **kw: {
            ExtensionTask: task,
            ApplicationAttempt: attempt,
            JobApplication: job,
            AgentRun: run,
        }[model]
    )
    db.commit = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: context)
    monkeypatch.setattr(event_bus, "publish", MagicMock())
    result = await finish_extension_task_activity(
        {
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "run_id": str(run_id),
            "user_id": str(user_id),
            "job_application_id": str(job_id),
            "outcome": outcome,
            "details": (
                {"confirmation_text": "Application received"} if outcome == "submitted" else {}
            )
            | extra,
        }
    )
    assert result["outcome"] == expected
    assert attempt.state == ("verified" if expected == "submitted" else expected)
    assert job.status == ("applied" if expected == "submitted" else "saved")


@pytest.mark.asyncio
async def test_failed_signal_lets_extension_retry_the_outcome(gate):
    from fastapi import HTTPException

    app, task, attempt, signal = gate
    prefix = f"/extension/device/tasks/{task.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        approved = await client.post(prefix + "/approve-submit", json=MANUAL_APPROVAL)
        event = {
            "stage": "submitted",
            "submission_token": approved.json()["submission_token"],
            "confirmation_text": "Application received",
        }
        signal.side_effect = HTTPException(status_code=503, detail="unavailable")
        failed = await client.post(prefix + "/events", json=event)
        assert failed.status_code == 503
        assert task.submission_reported_at is None
        signal.side_effect = None
        retried = await client.post(prefix + "/events", json=event)
        assert retried.status_code == 200
        assert task.submission_reported_at
        assert signal.await_args.args[1]["details"]["submit_attempted"] is True
