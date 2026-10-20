"""
memory/tests/test_manager.py — Unit tests for MemoryManager.

All external I/O (asyncpg pool, Redis, embedder) is mocked so the test suite
runs offline without a real database or Redis instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memory.embedder import MemoryEmbedder
from memory.manager import MemoryManager
from memory.models import AgentContext, Episode, Memory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

USER_ID = uuid4()
_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)


def _fake_record(**kwargs) -> MagicMock:
    """Build a MagicMock that behaves like an asyncpg.Record."""
    defaults = {
        "id": uuid4(),
        "memory_type": "preference",
        "content": "User prefers remote roles",
        "source_agent": "resume_agent",
        "confidence": 1.0,
        "times_used": 0,
        "is_active": True,
        "created_at": _NOW,
        "updated_at": _NOW,
        "similarity": 0.95,
    }
    defaults.update(kwargs)
    rec = MagicMock()
    rec.__getitem__ = lambda self, k: defaults[k]
    rec.get = lambda k, default=None: defaults.get(k, default)
    return rec


def _make_embedder(vec: list[float] | None = None) -> MemoryEmbedder:
    """Return a MemoryEmbedder whose embed() always returns vec (or zeros)."""
    embedder = MagicMock(spec=MemoryEmbedder)
    embedder.DIMS = 1536
    embedder.embed = AsyncMock(return_value=vec or [0.1] * 1536)
    return embedder


def _make_redis() -> MagicMock:
    rc = MagicMock()
    rc.get.return_value = None
    rc.setex.return_value = True
    rc.keys.return_value = []
    rc.delete.return_value = 0
    return rc


def _make_conn(fetchrow_return=None, fetch_return=None) -> AsyncMock:
    """Build a mock asyncpg connection context-manager."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    return conn


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Wrap a mock connection in a mock pool."""
    pool = MagicMock()
    pool.acquire = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return pool


def _manager(pool, embedder=None, redis_client=None) -> MemoryManager:
    mgr = MemoryManager(
        db_url="postgresql://fake",
        redis_client=redis_client or _make_redis(),
        embedder=embedder or _make_embedder(),
    )
    mgr._pool = pool
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveMemory:
    """1. save_memory creates record"""

    @pytest.mark.asyncio
    async def test_save_memory_inserts_row(self):
        new_id = uuid4()
        row_mock = MagicMock()
        row_mock.__getitem__ = lambda self, k: new_id if k == "id" else None

        conn = _make_conn(fetchrow_return=None)  # no duplicate found
        # First fetchrow (dedup check) returns None; second fetchrow (INSERT RETURNING) returns new_id
        conn.fetchrow.side_effect = [None, row_mock]
        pool = _make_pool(conn)

        mgr = _manager(pool)
        memory = Memory(user_id=USER_ID, memory_type="preference", content="Prefers remote roles")
        result = await mgr.save_memory(memory)

        assert result == new_id
        # INSERT should have been called once
        assert conn.fetchrow.call_count == 2

    """2. save_memory deduplicates on high similarity"""

    @pytest.mark.asyncio
    async def test_save_memory_deduplicates(self):
        existing_id = uuid4()
        existing_row = MagicMock()
        existing_row.__getitem__ = lambda self, k: existing_id if k == "id" else None

        conn = _make_conn(fetchrow_return=existing_row)
        # fetchrow returns existing on dedup check — execute called for SET LOCAL + UPDATE
        pool = _make_pool(conn)

        mgr = _manager(pool, embedder=_make_embedder([0.5] * 1536))
        memory = Memory(user_id=USER_ID, memory_type="preference", content="Remote only")
        result = await mgr.save_memory(memory)

        assert result == existing_id
        # At least one execute call should be the UPDATE (SET LOCAL search_path is the first)
        assert conn.execute.call_count >= 1
        all_sqls = [call[0][0] for call in conn.execute.call_args_list]
        assert any("UPDATE user_memories" in sql for sql in all_sqls)


class TestRecall:
    """3. recall returns top-k by similarity"""

    @pytest.mark.asyncio
    async def test_recall_returns_memories(self):
        rows = [_fake_record(content=f"Memory {i}", similarity=0.9 - i * 0.05) for i in range(3)]
        conn = _make_conn(fetch_return=rows)
        pool = _make_pool(conn)

        mgr = _manager(pool)
        results = await mgr.recall(USER_ID, query="remote job preference", k=3)

        assert len(results) == 3
        assert all(isinstance(m, Memory) for m in results)

    @pytest.mark.asyncio
    async def test_recall_keyword_fallback_on_zero_embedding(self):
        """When embedding is all zeros, fall back to ILIKE keyword search."""
        rows = [_fake_record(content="Prefers remote")]
        conn = _make_conn(fetch_return=rows)
        pool = _make_pool(conn)

        zero_embedder = _make_embedder([0.0] * 1536)
        mgr = _manager(pool, embedder=zero_embedder)
        results = await mgr.recall(USER_ID, query="remote", k=5)

        assert len(results) == 1
        # Keyword path uses $2 as ILIKE param — check fetch was called
        conn.fetch.assert_called_once()


class TestBlacklist:
    """4. blacklist save and retrieve"""

    @pytest.mark.asyncio
    async def test_save_and_get_blacklist(self):
        new_id = uuid4()
        row_mock = MagicMock()
        row_mock.__getitem__ = lambda self, k: new_id if k == "id" else None

        conn = _make_conn(fetchrow_return=None)
        conn.fetchrow.side_effect = [None, row_mock]
        list_rows = [_fake_record(memory_type="blacklist", content="Infosys")]
        conn.fetch.return_value = list_rows
        pool = _make_pool(conn)

        mgr = _manager(pool)

        # Save
        memory = Memory(user_id=USER_ID, memory_type="blacklist", content="Infosys")
        saved_id = await mgr.save_memory(memory)
        assert saved_id == new_id

        # Retrieve
        blacklist = await mgr.get_blacklist(USER_ID)
        assert len(blacklist) == 1
        assert blacklist[0].memory_type == "blacklist"
        assert blacklist[0].content == "Infosys"


class TestEpisodes:
    """5. save_episode and recall_episodes"""

    @pytest.mark.asyncio
    async def test_save_episode(self):
        ep_id = uuid4()
        row_mock = MagicMock()
        row_mock.__getitem__ = lambda self, k: ep_id if k == "id" else None

        conn = _make_conn(fetchrow_return=row_mock)
        pool = _make_pool(conn)

        mgr = _manager(pool)
        ep = Episode(
            user_id=USER_ID,
            agent_type="resume_agent",
            summary="Tailored resume for TechCorp",
            outcome="success",
        )
        result_id = await mgr.save_episode(ep)
        assert result_id == ep_id

    @pytest.mark.asyncio
    async def test_recall_episodes_returns_list(self):
        ep_row = MagicMock()
        ep_row.__getitem__ = lambda self, k: {
            "id": uuid4(),
            "agent_type": "resume_agent",
            "summary": "Tailored for role",
            "input": None,
            "output": None,
            "outcome": "success",
            "created_at": _NOW,
        }[k]
        ep_row.get = lambda k, d=None: {
            "summary": "Tailored for role",
            "input": None,
            "output": None,
            "outcome": "success",
            "created_at": _NOW,
        }.get(k, d)

        conn = _make_conn(fetch_return=[ep_row])
        pool = _make_pool(conn)

        mgr = _manager(pool)
        episodes = await mgr.recall_episodes(USER_ID, "resume_agent")
        assert len(episodes) == 1
        assert isinstance(episodes[0], Episode)


class TestBuildAgentContext:
    """6. build_agent_context returns correct structure"""

    @pytest.mark.asyncio
    async def test_build_agent_context_structure(self):
        pref_id = uuid4()
        bl_id = uuid4()
        style_id = uuid4()
        ep_id = uuid4()
        lr_id = uuid4()

        def _make_mem_row(row_id, mtype, content):
            data = {
                "id": row_id,
                "memory_type": mtype,
                "content": content,
                "source_agent": None,
                "confidence": 1.0,
                "times_used": 0,
                "is_active": True,
                "created_at": _NOW,
                "updated_at": _NOW,
                "similarity": 0.9,
            }
            rec = MagicMock()
            rec.__getitem__ = lambda self, k, _d=data: _d[k]
            rec.get = lambda k, d=None, _d=data: _d.get(k, d)
            return rec

        pref_row = _make_mem_row(pref_id, "preference", "Remote only")
        bl_row = _make_mem_row(bl_id, "blacklist", "TCS")
        style_row = _make_mem_row(style_id, "style", "concise")

        ep_data = {
            "id": ep_id,
            "agent_type": "resume_agent",
            "summary": "Tailored resume",
            "input": None,
            "output": None,
            "outcome": "success",
            "created_at": _NOW,
        }
        ep_row = MagicMock()
        ep_row.__getitem__ = lambda self, k, _d=ep_data: _d[k]
        ep_row.get = lambda k, d=None, _d=ep_data: _d.get(k, d)

        lr_data = {
            "id": lr_id,
            "agent_type": "resume_agent",
            "learning": "Metrics double callback rate",
            "evidence_count": 3,
            "success_rate": 0.9,
            "last_applied": _NOW,
            "created_at": _NOW,
        }
        lr_row = MagicMock()
        lr_row.__getitem__ = lambda self, k, _d=lr_data: _d[k]
        lr_row.get = lambda k, d=None, _d=lr_data: _d.get(k, d)

        # Route fetch calls by SQL content so concurrent tasks get correct rows
        async def smart_fetch(sql, *args, **kwargs):
            sql_str = sql if isinstance(sql, str) else ""
            if "agent_episodes" in sql_str:
                return [ep_row]
            if "agent_learnings" in sql_str:
                return [lr_row]
            # user_memories queries — distinguish by memory_type param
            # args[0] is user_id (UUID), optional args may carry memory_type string
            for a in args:
                if a == "preference":
                    return [pref_row]
                if a == "blacklist":
                    return [bl_row]
                if a == "style":
                    return [style_row]
            # semantic recall (embedding query) — return empty so no task-extras
            return []

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = smart_fetch
        pool = _make_pool(conn)

        redis_mock = _make_redis()
        redis_mock.keys.return_value = []
        mgr = _manager(pool, redis_client=redis_mock)

        ctx = await mgr.build_agent_context(USER_ID, "resume_agent", task="tailor my resume")

        assert isinstance(ctx, AgentContext)
        assert "Remote only" in ctx.preferences
        assert "TCS" in ctx.blacklist
        assert len(ctx.past_episodes) >= 1
        assert ctx.past_episodes[0]["outcome"] == "success"
        assert len(ctx.learnings) >= 1
        assert "concise" in ctx.user_style


class TestSession:
    """7. session set/get/clear"""

    def test_set_and_get_session(self):
        rc = MagicMock()
        stored: dict[str, bytes] = {}
        rc.setex.side_effect = lambda k, ttl, v: stored.__setitem__(k, v)
        rc.get.side_effect = lambda k: stored.get(k)

        mgr = MemoryManager("postgresql://fake", rc, MagicMock())

        mgr.set_session(USER_ID, "last_role", "Software Engineer")
        value = mgr.get_session(USER_ID, "last_role")
        assert value == "Software Engineer"

    def test_clear_session_removes_keys(self):
        rc = MagicMock()
        rc.keys.return_value = [
            f"session:{USER_ID}:last_role",
            f"session:{USER_ID}:location",
        ]
        rc.delete.return_value = 2

        mgr = MemoryManager("postgresql://fake", rc, MagicMock())
        deleted = mgr.clear_session(USER_ID)
        assert deleted == 2
        rc.delete.assert_called_once()


class TestEmbeddingFallback:
    """8. Embedding failure falls back gracefully (save with NULL embedding)"""

    @pytest.mark.asyncio
    async def test_save_memory_with_failed_embedding(self):
        """When embedder returns zero vector, memory is still saved with NULL embedding."""
        new_id = uuid4()
        row_mock = MagicMock()
        row_mock.__getitem__ = lambda self, k: new_id if k == "id" else None

        # embed() returns all-zeros (simulates provider failure)
        zero_embedder = _make_embedder([0.0] * 1536)

        conn = _make_conn(fetchrow_return=row_mock)
        # Only one fetchrow call expected (INSERT RETURNING) — no dedup check for zeros
        pool = _make_pool(conn)

        mgr = _manager(pool, embedder=zero_embedder)
        memory = Memory(user_id=USER_ID, memory_type="fact", content="User is a Python developer")
        result = await mgr.save_memory(memory)

        # Should still get a valid ID back
        assert result is not None

        # The INSERT fetchrow should have been called once
        assert conn.fetchrow.call_count == 1
        insert_call_args = conn.fetchrow.call_args[0]
        # INSERT args: sql, user_id, memory_type, content, embedding ($4), source_agent, confidence
        # Index 0=sql, 1=user_id, 2=memory_type, 3=content, 4=embedding
        embedding_param = insert_call_args[4]
        assert (
            embedding_param is None
        ), f"Expected NULL embedding param when all zeros, got: {embedding_param!r}"
