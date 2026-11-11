"""Real PostgreSQL/Redis and Chromium checks on disposable test services only.

Run docker compose -p careercraft-workflow-tests -f docker-compose.test.yml up -d
then RUN_WORKFLOW_INTEGRATION=1 pytest tests/integration/test_durable_workflows.py.
Never reads deployment credentials or uses the application's production database.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1",
    reason="Disposable workflow infrastructure required",
)


@pytest.fixture
async def database(monkeypatch):
    import app.core.database as core_database
    from app.applications import submission
    from app.core.database import Base
    from app.services import workflow_service

    engine = create_async_engine(
        "postgresql+asyncpg://workflow_test:workflow_test@127.0.0.1:55439/workflow_test"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (workflow_service, submission, core_database):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    monkeypatch.setattr("app.core.event_bus.publish", lambda *args: None)
    yield factory
    await engine.dispose()


async def new_run(factory, status="queued"):
    from app.models.db import AgentRun, User

    async with factory() as db:
        user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.test")
        db.add(user)
        await db.flush()
        run = AgentRun(
            id=uuid.uuid4(),
            user_id=user.id,
            agent_type="resume_optimize",
            status=status,
            input={"context": {}},
        )
        db.add(run)
        await db.commit()
        return user, run


async def _agent_worker(env):
    from temporalio.worker import Worker

    from app.workflows.agent_activities import (
        continue_agent_run_activity,
        execute_agent_run_activity,
        expire_agent_run_activity,
    )
    from app.workflows.agent_run import AgentRunWorkflow

    return Worker(
        env.client,
        task_queue="it-agent-runs",
        workflows=[AgentRunWorkflow],
        activities=[
            execute_agent_run_activity,
            continue_agent_run_activity,
            expire_agent_run_activity,
        ],
    )


@pytest.mark.asyncio
async def test_duplicate_start_runs_once_and_approval_continues(database):
    """Two starts for one run execute the agent once; the approval arrives
    by signal-with-start and the continuation completes the run."""
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.testing import WorkflowEnvironment

    from app.models.db import AgentRun
    from app.workflows.agent_run import (
        AgentRunInput,
        AgentRunWorkflow,
        ApprovalDecision,
        agent_run_workflow_id,
    )

    _, run = await new_run(database)
    execute = AsyncMock(
        return_value={
            "status": "awaiting_approval",
            "pending_action": {"type": "resume_ready", "resume_markdown": "review me"},
        }
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with await _agent_worker(env):
            with patch("app.services.workflow_service.execute_agent", execute):
                workflow_id = agent_run_workflow_id(str(run.id))
                handle = await env.client.start_workflow(
                    AgentRunWorkflow.run,
                    AgentRunInput(run_id=str(run.id), user_id=str(run.user_id)),
                    id=workflow_id,
                    task_queue="it-agent-runs",
                )
                with pytest.raises(WorkflowAlreadyStartedError):
                    await env.client.start_workflow(
                        AgentRunWorkflow.run,
                        AgentRunInput(run_id=str(run.id), user_id=str(run.user_id)),
                        id=workflow_id,
                        task_queue="it-agent-runs",
                    )
                for _ in range(200):
                    if await handle.query(AgentRunWorkflow.status) == "awaiting_approval":
                        break
                    await asyncio.sleep(0.02)
                async with database() as db:
                    saved = await db.get(AgentRun, run.id)
                    assert saved.status == "awaiting_approval"
                    assert saved.completed_at is None
                    saved.status = "queued"  # what the approve endpoint does
                    continuation = saved.output
                    await db.commit()
                await env.client.start_workflow(
                    AgentRunWorkflow.run,
                    AgentRunInput(
                        run_id=str(run.id),
                        user_id=str(run.user_id),
                        start_at_checkpoint=True,
                    ),
                    id=workflow_id,
                    task_queue="it-agent-runs",
                    start_signal="decide",
                    start_signal_args=[ApprovalDecision(approved=True, continuation=continuation)],
                )
                result = await handle.result()

    assert execute.await_count == 1
    assert result["status"] == "completed"
    async with database() as db:
        saved = await db.get(AgentRun, run.id)
        assert saved.status == "completed"
        assert saved.output["reviewed"] is True


@pytest.mark.asyncio
async def test_failed_continuation_is_never_replayed(database, monkeypatch):
    """A continuation that dies mid-send ends the workflow without a retry;
    maintenance then closes the run instead of leaving it "running"."""
    from temporalio.client import WorkflowFailureError
    from temporalio.testing import WorkflowEnvironment

    from app.models.db import AgentRun
    from app.workflows import job_activities
    from app.workflows.agent_run import (
        AgentRunInput,
        AgentRunWorkflow,
        agent_run_workflow_id,
    )

    _, run = await new_run(database)
    action = AsyncMock(side_effect=RuntimeError("connection reset during send"))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with await _agent_worker(env):

            async def passthrough(coro):
                # Let the failure escape the activity, as a crash would.
                return await coro

            with (
                patch("app.services.workflow_service.continue_action", action),
                patch("app.workflows.agent_activities._run_with_timeout", new=passthrough),
            ):
                with pytest.raises(WorkflowFailureError):
                    await env.client.execute_workflow(
                        AgentRunWorkflow.run,
                        AgentRunInput(
                            run_id=str(run.id),
                            user_id=str(run.user_id),
                            initial_continuation={"type": "send_email"},
                        ),
                        id=agent_run_workflow_id(str(run.id)),
                        task_queue="it-agent-runs",
                    )
        assert action.await_count == 1

        async with database() as db:
            saved = await db.get(AgentRun, run.id)
            assert saved.status == "running"  # the claim, never overwritten
            saved.started_at = datetime.now(UTC) - timedelta(hours=1)
            await db.commit()
        monkeypatch.setattr(
            "app.core.temporal_client.get_temporal_client",
            AsyncMock(return_value=env.client),
        )
        assert (await job_activities.maintenance_activity({}))["reconciled"] >= 1

    async with database() as db:
        saved = await db.get(AgentRun, run.id)
        assert saved.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_extension_task_lifecycle_on_real_database(database, authorized):
    """Create/reuse the browser task, then record a submission on the task,
    attempt, application and run in one place."""
    from app.models.db import (
        AgentRun,
        ApplicationAttempt,
        ExtensionTask,
        JobApplication,
    )
    from app.workflows.extension_activities import (
        create_extension_task_activity,
        finish_extension_task_activity,
    )

    user, run = await new_run(database, "running")
    workflow_id = f"auto-apply/{user.id}/{uuid.uuid4()}"
    async with database() as db:
        application = JobApplication(
            id=uuid.uuid4(),
            user_id=user.id,
            company="Acme",
            role="Backend Engineer",
            job_url="https://jobs.lever.co/acme/1",
            status="saved",
        )
        db.add(application)
        await db.flush()
        attempt = ApplicationAttempt(
            id=uuid.uuid4(),
            user_id=user.id,
            job_application_id=application.id,
            run_id=run.id,
            workflow_id=workflow_id,
            state="preparing",
        )
        db.add(attempt)
        await db.commit()

    params = {
        "user_id": str(user.id),
        "job_application_id": str(application.id),
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "workflow_id": workflow_id,
        "job_url": application.job_url,
        "company": "Acme",
        "role": "Backend Engineer",
        "pdf_document_id": None,
    }
    first = await create_extension_task_activity(params)
    retried = await create_extension_task_activity(params)  # at-least-once delivery
    assert first == retried

    async with database() as db:
        tasks = (
            (await db.execute(select(ExtensionTask).where(ExtensionTask.run_id == run.id)))
            .scalars()
            .all()
        )
        assert len(tasks) == 1 and tasks[0].status == "pending"
        assert tasks[0].payload["platform"] == "lever"
        assert (await db.get(AgentRun, run.id)).status == "queued"
        if authorized:
            task = tasks[0]
            task.approved_at = datetime.now(UTC)
            task.submission_reported_at = datetime.now(UTC)
            task.review_hash = "a" * 64
            task.status = "submitted"
            saved_attempt = await db.get(ApplicationAttempt, attempt.id)
            saved_attempt.state = "submitting"
            saved_attempt.approved_snapshot_hash = task.review_hash
            await db.commit()

    finished = await finish_extension_task_activity(
        {
            "task_id": first["task_id"],
            "run_id": str(run.id),
            "attempt_id": str(attempt.id),
            "user_id": str(user.id),
            "job_application_id": str(application.id),
            "outcome": "submitted",
            "details": {"confirmation_text": "Thank you for applying"},
        }
    )
    expected = "submitted" if authorized else "outcome_unknown"
    assert bool(finished["applied_at"]) is authorized

    async with database() as db:
        assert (await db.get(ExtensionTask, uuid.UUID(first["task_id"]))).status == expected
        assert (await db.get(ApplicationAttempt, attempt.id)).state == (
            "verified" if authorized else "outcome_unknown"
        )
        saved_app = await db.get(JobApplication, application.id)
        assert (
            saved_app.status == ("applied" if authorized else "saved")
            and (saved_app.followup_day5 is not None) is authorized
        )
        saved_run = await db.get(AgentRun, run.id)
        assert saved_run.status == ("completed" if authorized else "failed")
        assert saved_run.output["outcome"] == expected


# --- Task 2: idempotent external actions (application submission + email) ---


async def new_application_attempt(factory, state="awaiting_approval"):
    from app.models.db import ApplicationAttempt, JobApplication, User

    async with factory() as db:
        user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.test")
        db.add(user)
        await db.flush()
        app_row = JobApplication(
            id=uuid.uuid4(),
            user_id=user.id,
            company="Acme",
            role="Backend Engineer",
            job_url="https://jobs.example.test/apply",
        )
        db.add(app_row)
        await db.flush()
        attempt = ApplicationAttempt(
            id=uuid.uuid4(), user_id=user.id, job_application_id=app_row.id, state=state
        )
        db.add(attempt)
        await db.commit()
        return user, app_row, attempt


@pytest.mark.asyncio
async def test_two_simultaneous_email_approvals_cause_one_gmail_call(database, monkeypatch):
    """Required test #2: real Postgres row lock on outbound_messages."""
    from app.services import workflow_service

    monkeypatch.setattr(workflow_service, "AsyncSessionLocal", database)

    user, run = await new_run(database)
    gmail_send = MagicMock(return_value={"id": "gmail-msg-1"})
    monkeypatch.setattr("app.services.gmail_service.GmailMCPClient.send_message", gmail_send)

    # Two concurrent approvals of the SAME run's pending email — the
    # idempotency_key ("agent_run:{run_id}") is what they collide on.
    await asyncio.gather(
        workflow_service.send_approved_email(
            user.id, run.id, "hr@acme.test", "Following up", "body"
        ),
        workflow_service.send_approved_email(
            user.id, run.id, "hr@acme.test", "Following up", "body"
        ),
    )
    assert gmail_send.call_count == 1


@pytest.mark.asyncio
async def test_same_user_cannot_start_second_attempt_for_same_job(database):
    """Required test #6: the unique constraint is the backstop even if
    application-level checks are ever bypassed."""
    from sqlalchemy.exc import IntegrityError

    from app.models.db import ApplicationAttempt

    _, app_row, _ = await new_application_attempt(database)
    async with database() as db:
        with pytest.raises(IntegrityError):
            db.add(
                ApplicationAttempt(
                    id=uuid.uuid4(),
                    user_id=app_row.user_id,
                    job_application_id=app_row.id,
                    state="preparing",
                )
            )
            await db.commit()


@pytest.mark.asyncio
async def test_different_users_can_apply_to_same_job_independently(database):
    """Required test #7."""
    from app.models.db import ApplicationAttempt, JobApplication, User

    async with database() as db:
        user_a = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.test")
        user_b = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.test")
        db.add_all([user_a, user_b])
        await db.flush()
        app_a = JobApplication(
            id=uuid.uuid4(),
            user_id=user_a.id,
            company="Acme",
            role="Backend Engineer",
            job_url="https://jobs.example.test/apply",
        )
        app_b = JobApplication(
            id=uuid.uuid4(),
            user_id=user_b.id,
            company="Acme",
            role="Backend Engineer",
            job_url="https://jobs.example.test/apply",
        )
        db.add_all([app_a, app_b])
        await db.flush()
        db.add_all(
            [
                ApplicationAttempt(
                    id=uuid.uuid4(),
                    user_id=user_a.id,
                    job_application_id=app_a.id,
                    state="preparing",
                ),
                ApplicationAttempt(
                    id=uuid.uuid4(),
                    user_id=user_b.id,
                    job_application_id=app_b.id,
                    state="preparing",
                ),
            ]
        )
        await db.commit()  # no IntegrityError — different users, independent rows


# --- Task 3 slice 3b: the one controlled ATS adapter end-to-end test ---


@pytest.mark.asyncio
async def test_public_sources_cache_pagination_failure_and_removal(database, monkeypatch):
    from pathlib import Path

    import httpx
    from sqlalchemy import text

    from app.services import job_catalog, jobs_database
    from app.services.job_connectors import Page, Source, normalize

    async with database() as db:
        for role in ("anon", "authenticated", "service_role"):
            exists = (
                await db.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role})
            ).scalar()
            if not exists:
                await db.execute(text("CREATE ROLE " + role))
        exists = (await db.execute(text("SELECT to_regclass('public.job_catalog')"))).scalar()
        if not exists:
            path = (
                Path(__file__).resolve().parents[3]
                / "supabase/migrations/20261001091000_b_job_catalog.sql"
            )
            for statement in path.read_text().split(";"):
                # ORM metadata already includes application source/date columns.
                if statement.strip() and not statement.strip().startswith(
                    "ALTER TABLE public.job_applications"
                ):
                    await db.execute(text(statement))
        await db.commit()
    monkeypatch.setattr(jobs_database, "AsyncSessionLocal", database)
    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", database)
    source = Source("recorded:" + str(uuid.uuid4()), "greenhouse", "fixture")
    broken = Source("recorded:" + str(uuid.uuid4()), "lever", "broken")
    job = normalize(
        {
            "id": "1",
            "title": "Python Engineer",
            "company": "Fixture",
            "url": "https://jobs.example/" + str(uuid.uuid4()),
            "posted_at": datetime.now(UTC).isoformat(),
        },
        source,
    )
    calls = []

    async def fetch(src, query="", cursor=None):
        calls.append((src.id, cursor))
        if src.id == broken.id:
            response = httpx.Response(
                429,
                headers={"Retry-After": "7200"},
                request=httpx.Request("GET", "https://public.example/jobs"),
            )
            raise httpx.HTTPStatusError("rate limit", request=response.request, response=response)
        return Page([job], "page2" if cursor is None else None)

    monkeypatch.setattr(job_catalog, "fetch_page", fetch)
    first, warning = await job_catalog.refresh_source(source)
    assert len(first) == 2 and warning is None
    before = len(calls)
    cached, warning = await job_catalog.refresh_source(source)
    assert cached == first and len(calls) == before
    monkeypatch.setattr(job_catalog, "sources", lambda: [source, broken])
    report = await job_catalog.refresh_catalog()
    assert report["sources"] == 2
    async with database() as db:
        health = (
            await db.execute(
                text(
                    "SELECT status,next_allowed_at FROM job_source_health WHERE source_id=:source"
                ),
                {"source": broken.id},
            )
        ).one()
        assert health.status == "degraded" and health.next_allowed_at > datetime.now(
            UTC
        ) + timedelta(hours=1)
        assert (
            await db.execute(
                text("SELECT count(*) FROM job_source_occurrences WHERE source_id=:source"),
                {"source": source.id},
            )
        ).scalar() == 1
        await db.execute(
            text("UPDATE job_source_health SET next_allowed_at=NULL WHERE source_id=:source"),
            {"source": source.id},
        )
        await db.commit()

    async def empty(*args, **kwargs):
        return Page([])

    monkeypatch.setattr(job_catalog, "fetch_page", empty)
    await job_catalog.refresh_source(source)
    async with database() as db:
        assert (
            await db.execute(
                text("SELECT count(*) FROM job_source_occurrences WHERE source_id=:source"),
                {"source": source.id},
            )
        ).scalar() == 0


@pytest.mark.asyncio
async def test_github_fallback_cache_private_exclusion_and_delete_race(database, monkeypatch):
    import base64
    from pathlib import Path

    from sqlalchemy import text

    from app.services import github_profile

    async with database() as db:
        if not (await db.execute(text("SELECT to_regclass('public.github_profiles')"))).scalar():
            path = (
                Path(__file__).resolve().parents[3]
                / "supabase/migrations/20261001092000_b_github_profiles.sql"
            )
            for statement in path.read_text().split(";"):
                if statement.strip():
                    await db.execute(text(statement))
            await db.commit()
    monkeypatch.setattr(github_profile, "AsyncSessionLocal", database)
    user, _ = await new_run(database, "running")
    calls = []
    race = False

    async def public(path):
        calls.append(path)
        if "/repos?" in path:
            return [
                {
                    "name": "public",
                    "html_url": "https://github.com/fixture/public",
                    "owner": {"login": "fixture"},
                    "language": "Python",
                    "pushed_at": datetime.now(UTC).isoformat(),
                },
                {
                    "name": "private",
                    "html_url": "https://github.com/fixture/private",
                    "owner": {"login": "fixture"},
                    "private": True,
                },
            ]
        if "/events/" in path:
            return []
        if path.endswith("/languages"):
            return {"Python": 100}
        if path.endswith("/readme"):
            if race:
                await github_profile.delete_profile(user.id)
            return {"encoding": "base64", "content": base64.b64encode(b"FastAPI project").decode()}
        raise AssertionError(path)

    monkeypatch.setattr(github_profile, "_public", public)
    assert await github_profile.get_profile(user.id) is None
    profile = await github_profile.refresh_profile(user.id, "https://github.com/fixture")
    assert set(profile) == {"skills", "top_repos", "suggested_projects"}
    assert profile["suggested_projects"][0]["name"] == "public"
    assert not any("/private/" in path for path in calls)
    count = len(calls)
    assert await github_profile.refresh_profile(user.id, "https://github.com/fixture") == profile
    assert len(calls) == count
    await github_profile.delete_profile(user.id)
    assert await github_profile.get_profile(user.id) is None
    with pytest.raises(LookupError):
        await github_profile.refresh_profile(user.id)
    race = True
    with pytest.raises(LookupError):
        await github_profile.refresh_profile(user.id, "https://github.com/fixture")
    assert await github_profile.get_profile(user.id) is None
