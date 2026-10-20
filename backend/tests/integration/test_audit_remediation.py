"""Real PostgreSQL regressions: privacy fencing and durable extension claims."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import psycopg
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models.db import ExtensionTask, User

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1", reason="Disposable PostgreSQL required"
)
ADMIN = "postgresql://workflow_test:workflow_test@127.0.0.1:55439/workflow_test"


@pytest.fixture
async def factory(monkeypatch):
    from app.agents.memory.manager import _DDL
    from app.core import database
    from app.services import memory_lifecycle

    name = "remediation_" + uuid.uuid4().hex[:12]
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = ADMIN.rsplit("/", 1)[0] + "/" + name
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        with psycopg.connect(url) as conn:
            conn.execute(_DDL)
            conn.execute(Path("memory/schemas.sql").read_text())
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(database, "AsyncSessionLocal", maker)
        monkeypatch.setattr(memory_lifecycle, "delete_memory_cache", AsyncMock())
        yield maker
    finally:
        await engine.dispose()
        with psycopg.connect(ADMIN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


async def seed_user(factory):
    async with factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4()}@example.test",
            policy_accepted_at=datetime.now(UTC),
        )
        db.add(user)
        await db.commit()
        return user


async def test_forget_clears_both_stores_and_fences_old_run(factory):
    from app.api.v1.agent_memory import forget_everything
    from app.services.memory_lifecycle import memory_access

    user = await seed_user(factory)
    other = await seed_user(factory)
    async with memory_access(str(user.id)) as generation:
        assert generation == 0
    async with factory() as db:
        for owner in (user, other):
            await db.execute(
                text(
                    "INSERT INTO user_memories(user_id,memory_type,content) "
                    "VALUES (:u,'preference','concise')"
                ),
                {"u": owner.id},
            )
            await db.execute(
                text(
                    "INSERT INTO agent_memory_preferences(user_id,preference_key,preference_value) "
                    "VALUES (:u,'tone','concise')"
                ),
                {"u": str(owner.id)},
            )
        await db.commit()
        await forget_everything(db, user)
    async with memory_access(str(user.id), generation) as stale:
        assert stale is None
    async with memory_access(str(user.id)) as fresh:
        assert fresh == 1
    async with factory() as db:
        for table in ("user_memories", "agent_memory_preferences"):
            rows = (await db.execute(text(f"SELECT user_id::text FROM {table}"))).scalars().all()
            assert rows == [str(other.id)]


async def test_forget_waits_for_writer_then_removes_its_write(factory):
    from app.api.v1.agent_memory import forget_everything
    from app.services.memory_lifecycle import memory_access

    user = await seed_user(factory)
    entered = asyncio.Event()

    async def forget():
        async with factory() as db:
            entered.set()
            await forget_everything(db, user)

    async with memory_access(str(user.id), 0):
        task = asyncio.create_task(forget())
        await entered.wait()
        # Writer uses a different connection with an FK: NO KEY UPDATE must
        # permit the FK check while serializing Forget against this writer.
        async with factory() as db:
            await asyncio.wait_for(
                db.execute(
                    text(
                        "INSERT INTO user_memories(user_id,memory_type,content) "
                        "VALUES (:u,'fact','old run')"
                    ),
                    {"u": user.id},
                ),
                timeout=3,
            )
            await db.commit()
        assert not task.done()
    await asyncio.wait_for(task, timeout=3)
    async with factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM user_memories"))).scalar() == 0


@pytest.mark.parametrize("reason", ["overdue", "consent"])
async def test_paired_device_rechecks_owner_eligibility(factory, reason):
    from app.services.extension_service import authenticate, pair_device

    user = await seed_user(factory)
    async with factory() as db:
        device, token = await pair_device(db, user.id, "Browser")
        owner = await db.get(User, user.id)
        if reason == "overdue":
            owner.deletion_scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
        else:
            owner.policy_accepted_at = None
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await authenticate(db, token)
        assert exc.value.status_code == (410 if reason == "overdue" else 403)


async def test_claim_recovers_after_signal_failure_and_is_device_scoped(factory, monkeypatch):
    from app.api.v1 import extension
    from app.services.extension_service import claim_next_task, pair_device

    user = await seed_user(factory)
    async with factory() as db:
        device, _ = await pair_device(db, user.id, "Browser")
        other, _ = await pair_device(db, user.id, "Other")
        task = ExtensionTask(user_id=user.id, workflow_id="synthetic", status="pending", payload={})
        db.add(task)
        await db.commit()
        signal = AsyncMock(side_effect=HTTPException(503, "outage"))
        monkeypatch.setattr(extension, "_signal", signal)
        monkeypatch.setattr(extension, "_record_progress", AsyncMock())
        with pytest.raises(HTTPException):
            await extension.claim_task.__wrapped__(request=None, device=device, db=db)
    async with factory() as db:
        assert await claim_next_task(db, other) is None
        signal.side_effect = None
        recovered = await extension.claim_task.__wrapped__(request=None, device=device, db=db)
        assert recovered["id"] == str(task.id)
        assert signal.await_count == 2
        # Lost HTTP response is also recoverable without a new claim timestamp.
        again = await claim_next_task(db, device)
        assert again.id == task.id and again.claimed_at == task.claimed_at


@pytest.mark.parametrize("phase", ["read", "write"])
async def test_forget_completes_while_provider_is_pending(factory, monkeypatch, phase):
    from unittest.mock import MagicMock

    from app.agents import harness as module
    from app.api.v1.agent_memory import forget_everything

    user = await seed_user(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare_read(*args):
        if phase == "read":
            entered.set()
            await release.wait()
        return object()

    async def prepare_write(**kwargs):
        if phase == "write":
            entered.set()
            await release.wait()
        return object()

    agent = module.AgentHarness("postgresql://unused", "redis://unused")
    agent._ensure_initialized = AsyncMock()
    agent._memory = MagicMock()
    agent._memory.build_agent_context = AsyncMock(return_value={})
    agent._memory.save_episode = AsyncMock()
    agent._memory.upsert_learning = AsyncMock()
    agent._extract_and_save_memories = AsyncMock()
    agent._semantic_memory = MagicMock()
    agent._semantic_memory.prepare_context = AsyncMock(side_effect=prepare_read)
    agent._semantic_memory.prepare_after_run = AsyncMock(side_effect=prepare_write)
    agent._semantic_memory.build_context = AsyncMock(return_value={})
    agent._semantic_memory.save_prepared = AsyncMock()
    monkeypatch.setattr("app.core.sync_db.fetch_model_settings", lambda *args: None)
    monkeypatch.setattr(
        module.orchestrator,
        "ainvoke",
        AsyncMock(return_value={"status": "completed", "result": {"draft": "letter"}}),
    )
    run = asyncio.create_task(agent.run(str(user.id), "cover_letter", {}, {}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        async with factory() as db:
            # A provider may take minutes, but account controls remain responsive.
            await asyncio.wait_for(forget_everything(db, user), timeout=2)
    finally:
        release.set()
        await asyncio.wait_for(run, timeout=3)
    agent._semantic_memory.save_prepared.assert_not_awaited()
    agent._memory.save_episode.assert_not_awaited()


async def test_device_fence_allows_child_fk_checks_but_blocks_erasure(factory):
    from app.services.extension_service import authenticate, pair_device

    user = await seed_user(factory)
    async with factory() as db:
        _, token = await pair_device(db, user.id, "Browser")
        await authenticate(db, token)
        async with factory() as other:
            locked = (
                await other.execute(
                    select(User.id).where(User.id == user.id).with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            assert locked is None
            other.add(
                ExtensionTask(
                    user_id=user.id, workflow_id="synthetic", status="pending", payload={}
                )
            )
            await asyncio.wait_for(other.commit(), timeout=2)


@pytest.mark.parametrize(
    ("role", "distractor"),
    [
        ("AI", "Retail Associate"),
        ("C#", "C#embedded Specialist"),
        ("QA Engineer", "Engineer"),
        ("Backend Engineer", "Frontend Engineer"),
        ("100% Engineer", "1000 Engineer"),
        ("data_scientist", "dataXscientist"),
        ("x' OR 1=1 --", "Unrelated Engineer"),
    ],
)
async def test_role_catalog_filter_applies_before_candidate_limit(
    factory, monkeypatch, role, distractor
):
    from app.services import job_catalog

    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", factory)
    # Use the actual catalog schema; the rest of this migration changes unrelated tables.
    migration = Path("../supabase/migrations/20261001091000_b_job_catalog.sql").read_text()
    catalog_schema = migration.split("CREATE TABLE public.job_match_embeddings", 1)[0]
    async with factory() as db:
        for statement in catalog_schema.split(";"):
            if statement.strip():
                await db.execute(text(statement))
        await db.execute(text("INSERT INTO job_source_health(source_id) VALUES ('live:jobspy')"))
        # More recent substring matches must not consume the 2,000-row candidate cap.
        await db.execute(
            text("""INSERT INTO job_catalog(job_id,url,title,company,posted_at,data)
            SELECT 'noise-' || n, 'https://example.test/noise/' || n, :title, 'Synthetic', now(),
                jsonb_build_object('job_id','noise-' || n,'title',CAST(:title AS text),
                    'company','Synthetic','url','https://example.test/noise/' || n,
                    'location','Remote','remote','remote')
            FROM generate_series(1, 2001) AS n"""),
            {"title": distractor},
        )
        await db.execute(
            text("""INSERT INTO job_catalog(job_id,url,title,company,posted_at,data)
            VALUES ('target','https://example.test/target',:title,'Synthetic',
                now()-interval '1 day',
                jsonb_build_object('job_id','target','title',CAST(:title AS text),
                    'company','Synthetic','url','https://example.test/target',
                    'location','Remote','remote','remote'))"""),
            {"title": f"{role} Engineer"},
        )
        await db.execute(text("""INSERT INTO job_source_occurrences(job_id,source_id)
            SELECT job_id, 'live:jobspy' FROM job_catalog"""))
        await db.commit()
    jobs, warnings = await job_catalog.search_catalog(
        {"titles": [role]}, [], live_platforms=["jobspy"]
    )
    assert not warnings
    assert [job["job_id"] for job in jobs] == ["target"]
