"""A member can see and delete what the agents remember, and only their own."""

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1",
    reason="Disposable workflow infrastructure required",
)

ASYNC_URL = "postgresql+asyncpg://workflow_test:workflow_test@127.0.0.1:55439/workflow_test"


_TABLES = (
    "agent_memory_episodes",
    "agent_memory_learnings",
    "agent_memory_preferences",
    "agent_memory_procedures",
)


@pytest.fixture
async def factory(monkeypatch):
    from unittest.mock import AsyncMock

    from app.agents.memory.manager import _DDL
    from app.services import memory_lifecycle

    monkeypatch.setattr(memory_lifecycle, "delete_memory_cache", AsyncMock())

    engine = create_async_engine(ASYNC_URL)
    async with engine.begin() as connection:
        # other suites create id/user_id-only stubs of these tables; this one
        # needs the real columns
        await connection.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_generation "
                "integer NOT NULL DEFAULT 0"
            )
        )
        for table in _TABLES:
            await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))  # noqa: S608
        for statement in _DDL.split(";"):
            if statement.strip():
                await connection.execute(text(statement))
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        for table in _TABLES:
            await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))  # noqa: S608
    await engine.dispose()


async def _seed(db, uid: str):
    from app.models.db import User

    # Use mapped defaults with both migrated and ORM-created suite schemas.
    await db.execute(
        insert(User).values(id=uuid.UUID(uid), email=f"{uid}@example.test").on_conflict_do_nothing()
    )
    await db.execute(
        text(
            "INSERT INTO agent_memory_learnings (user_id, agent_type, learning) "
            "VALUES (:u, 'resume', 'prefers short bullets')"
        ),
        {"u": uid},
    )
    await db.execute(
        text(
            "INSERT INTO agent_memory_preferences (user_id, preference_key, preference_value) "
            "VALUES (:u, 'tone', 'bold')"
        ),
        {"u": uid},
    )
    await db.commit()


async def test_lists_only_my_memory_and_forgets_it(factory):
    from app.api.v1 import agent_memory

    mine, other = uuid.uuid4(), uuid.uuid4()
    async with factory() as db:
        await _seed(db, str(mine))
        await _seed(db, str(other))
        listing = await agent_memory.list_memory(db=db, current_user=SimpleNamespace(id=mine))
        assert [r["text"] for r in listing["learnings"]] == ["prefers short bullets"]
        assert listing["preferences"][0]["label"] == "tone"

        learning_id = listing["learnings"][0]["id"]
        # another member cannot delete my row
        with pytest.raises(HTTPException) as foreign:
            await agent_memory.forget_one("learnings", learning_id, db, SimpleNamespace(id=other))
        assert foreign.value.status_code == 404
        await agent_memory.forget_one("learnings", learning_id, db, SimpleNamespace(id=mine))
        with pytest.raises(HTTPException) as gone:
            await agent_memory.forget_one("learnings", learning_id, db, SimpleNamespace(id=mine))
        assert gone.value.status_code == 404

        with pytest.raises(HTTPException) as unknown:
            await agent_memory.forget_one("users", 1, db, SimpleNamespace(id=mine))
        assert unknown.value.status_code == 404

        await agent_memory.forget_everything(db=db, current_user=SimpleNamespace(id=mine))
        after = await agent_memory.list_memory(db=db, current_user=SimpleNamespace(id=mine))
        assert all(rows == [] for rows in after.values())
        theirs = await agent_memory.list_memory(db=db, current_user=SimpleNamespace(id=other))
        assert theirs["preferences"][0]["text"] == "bold"
