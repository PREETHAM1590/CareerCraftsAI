"""Agent runs must never deadlock against the orchestrator's run upsert.

Routes used to insert the agent_runs row on the request's async session and
run the agent inline; the orchestrator then recorded the run through a
separate sync connection whose INSERT waited on the request's open
transaction while blocking the event loop, stalling the whole API process.
Routes now queue durable runs, and the upsert runs off the event loop.

Run docker compose -p careercraft-workflow-tests -f docker-compose.test.yml up -d
then RUN_WORKFLOW_INTEGRATION=1 pytest tests/integration/test_inline_run_locking.py.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1",
    reason="Disposable workflow infrastructure required",
)

ASYNC_URL = "postgresql+asyncpg://workflow_test:workflow_test@127.0.0.1:55439/workflow_test"


@pytest.fixture
async def database(monkeypatch):
    import app.core.sync_db as sync_db
    from app.core.config import settings
    from app.core.database import Base

    engine = create_async_engine(ASYNC_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Match supabase/migrations/0006: agent_runs.user_id is nullable there,
        # so the orchestrator's placeholder insert reaches the primary-key
        # check and waits on the lock instead of failing NOT NULL first.
        await connection.execute(text("ALTER TABLE agent_runs ALTER COLUMN user_id DROP NOT NULL"))
    # Build the real sync engine (with its connect hooks) against the test DB.
    monkeypatch.setattr(settings, "DATABASE_URL", ASYNC_URL)
    monkeypatch.setattr(sync_db, "_sync_engine", None)
    monkeypatch.setattr(sync_db, "_sync_factory", None)
    monkeypatch.setattr("app.core.event_bus.publish", lambda *args: None)
    yield factory
    if sync_db._sync_engine is not None:
        sync_db._sync_engine.dispose()
    await engine.dispose()


async def _new_user(factory):
    from app.models.db import User

    async with factory() as db:
        user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.test")
        db.add(user)
        await db.commit()
        return user


@pytest.mark.parametrize("route", ["email", "resume", "jobs"])
async def test_dedicated_routes_share_admission_slots(database, monkeypatch, route):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api.v1 import email, jobs, resume
    from app.models.db import AgentRun

    user = await _new_user(database)
    async with database() as db:
        db.add_all(
            [
                AgentRun(user_id=user.id, agent_type=kind, status="running")
                for kind in ("email", "resume_optimize")
            ]
        )
        await db.commit()
    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("test", 1)}
    )
    if route == "jobs":
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            jobs,
            "_resolve_search_context",
            AsyncMock(return_value=("Engineer", "Remote", "remote", {"locations": ["Remote"]})),
        )
        monkeypatch.setattr(jobs, "_resolve_live_browser", AsyncMock(return_value=False))
        monkeypatch.setattr(
            "app.services.search_basis.resolve_basis", AsyncMock(return_value=(None, None))
        )
        endpoint, payload = jobs.search_jobs.__wrapped__, jobs.JobSearchRequest(
            search_query="Engineer"
        )
    elif route == "email":
        endpoint, payload = email.compose_email.__wrapped__, email.ComposeRequest(
            company="Acme", role="Engineer", recipient_email="hr@example.com"
        )
    else:
        endpoint, payload = resume.optimize_resume.__wrapped__, resume.OptimizeRequest(
            jd_text="Engineer"
        )
    async with database() as db:
        with pytest.raises(HTTPException) as rejected:
            await endpoint(request, payload, db, user)
        assert rejected.value.status_code == 429


async def test_http_foreign_run_rejected_for_two_valid_database_identities(database):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.api.v1 import agents
    from app.api.v1.deps import get_current_user, get_db
    from app.core.rate_limit import limiter
    from app.models.db import AgentRun

    victim, caller = await _new_user(database), await _new_user(database)
    run_id = uuid.uuid4()
    async with database() as db:
        db.add(
            AgentRun(
                id=run_id,
                user_id=victim.id,
                agent_type="email",
                status="awaiting_approval",
                output={"type": "send_email"},
            )
        )
        await db.commit()
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(agents.router)
    app.dependency_overrides[get_current_user] = lambda: caller

    async def session():
        async with database() as db:
            yield db

    app.dependency_overrides[get_db] = session
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        stream = await client.get(f"/agents/{run_id}/stream")
        approval = await client.post(f"/agents/{run_id}/approve", json={"approved": True})
    assert stream.status_code == approval.status_code == 404
    async with database() as db:
        assert (await db.get(AgentRun, run_id)).status == "awaiting_approval"


async def test_cover_letter_route_queues_a_committed_durable_run(database, monkeypatch):
    """The route no longer runs the agent in the request: it commits a queued
    row (which the workflow's first activity reads) and starts the workflow."""
    from app.api.v1 import cover_letter
    from app.models.db import AgentRun
    from app.workflows import starters

    started = []

    async def start_agent_run(run_id, user_id):
        # The row must already be committed and visible to other connections.
        async with database() as other:
            row = await other.get(AgentRun, uuid.UUID(run_id))
        started.append((run_id, row.status if row else None))

    monkeypatch.setattr(starters, "start_agent_run", start_agent_run)
    user = await _new_user(database)

    async with database() as db:
        response = await asyncio.wait_for(
            cover_letter.generate_cover_letter(
                cover_letter.GenerateRequest(jd_text="Backend engineer, Python."),
                db=db,
                current_user=user,
            ),
            timeout=15,
        )
        await db.commit()

    assert response.status == "queued"
    assert started == [(response.run_id, "queued")]
    async with database() as db:
        run = await db.scalar(select(AgentRun).where(AgentRun.id == uuid.UUID(response.run_id)))
    assert run.agent_type == "cover_letter"
    assert run.input["context"]["jd_text"] == "Backend engineer, Python."


async def test_run_upsert_waiting_on_a_lock_does_not_block_the_event_loop(database):
    from app.core.agent_runs_repository import upsert_agent_run
    from app.models.db import AgentRun

    user = await _new_user(database)
    run_id = uuid.uuid4()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    async with database() as db:
        db.add(AgentRun(id=run_id, user_id=user.id, agent_type="cover_letter", status="running"))
        await db.flush()  # hold the row lock with the insert uncommitted

        ticking = asyncio.create_task(ticker())
        upsert = asyncio.create_task(upsert_agent_run(run_id=str(run_id), status="completed"))
        await asyncio.sleep(1)
        assert not upsert.done()  # still waiting on our open transaction
        assert ticks >= 10  # but the event loop kept running meanwhile

        await db.commit()

    # The waiting INSERT now hits the committed primary key and fails fast;
    # the orchestrator logs and skips that, so a raised error is acceptable.
    await asyncio.wait_for(asyncio.gather(upsert, return_exceptions=True), timeout=10)
    ticking.cancel()
