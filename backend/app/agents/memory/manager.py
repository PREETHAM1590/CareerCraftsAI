"""
memory.manager — MemoryManager for episodic memory, preference storage, and
                  agent learnings backed by PostgreSQL + Redis.

Tables expected (created lazily on first use):
  - agent_memory_episodes     (id, user_id, agent_type, task_type, strategy, success,
                        context_summary, output_summary, created_at)
  - agent_memory_learnings    (id, user_id, agent_type, learning, success_rate,
                        sample_count, created_at, updated_at)
  - agent_memory_preferences  (id, user_id, preference_key, preference_value, created_at)

If the tables do not exist the manager logs a warning and returns safe empty
values — it never raises so the harness can degrade gracefully.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# DDL executed once per connection pool lifetime
_DDL = """
CREATE TABLE IF NOT EXISTS agent_memory_episodes (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    agent_type    TEXT        NOT NULL,
    task_type     TEXT        NOT NULL,
    strategy      TEXT        NOT NULL DEFAULT 'standard',
    success       BOOLEAN     NOT NULL DEFAULT FALSE,
    context_summary TEXT,
    output_summary  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_episodes_user_agent
    ON agent_memory_episodes (user_id, agent_type, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory_learnings (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT        NOT NULL,
    agent_type   TEXT        NOT NULL,
    learning     TEXT        NOT NULL,
    success_rate FLOAT       NOT NULL DEFAULT 0.0,
    sample_count INT         NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, agent_type, learning)
);
CREATE INDEX IF NOT EXISTS idx_learnings_user_agent
    ON agent_memory_learnings (user_id, agent_type, success_rate DESC);

CREATE TABLE IF NOT EXISTS agent_memory_preferences (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL,
    preference_key   TEXT NOT NULL,
    preference_value TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, preference_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_prefs_user ON agent_memory_preferences (user_id);

CREATE TABLE IF NOT EXISTS agent_memory_procedures (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    agent_type    TEXT        NOT NULL,
    trigger_desc  TEXT        NOT NULL,
    workflow_json TEXT        NOT NULL,
    success_count INT         NOT NULL DEFAULT 1,
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, agent_type, trigger_desc)
);
CREATE INDEX IF NOT EXISTS idx_procedures_user_agent
    ON agent_memory_procedures (user_id, agent_type, success_count DESC);
"""


class MemoryManager:
    """
    Manages episodic memory, learnings, and preferences for an agent harness.

    Usage::

        mgr = MemoryManager(db_url="postgresql://...", redis_url="redis://...")
        await mgr.initialize()           # creates tables, warms caches
        ctx = await mgr.build_agent_context(user_id, task_type)
        await mgr.save_episode(...)
        await mgr.close()
    """

    def __init__(self, db_url: str, redis_url: str) -> None:
        # Convert SQLAlchemy URL to asyncpg-compatible URL
        self._db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        self._redis_url = redis_url
        self._pool: asyncpg.Pool | None = None
        self._redis: Any | None = None  # redis.asyncio.Redis
        self._tables_ready: bool = False
        self._tables_checked: bool = False  # log once only

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the connection pool and ensure memory tables exist."""
        try:
            self._pool = await asyncpg.create_pool(
                self._db_url,
                min_size=1,
                max_size=5,
                command_timeout=10,
            )
            await self._ensure_tables()
        except Exception as exc:
            logger.warning("MemoryManager: DB pool init failed — memory disabled: %s", exc)
            self._pool = None

        try:
            import redis.asyncio as aioredis  # type: ignore[import]

            self._redis = aioredis.from_url(
                self._redis_url, encoding="utf-8", decode_responses=True
            )
            await self._redis.ping()
        except Exception as exc:
            logger.warning("MemoryManager: Redis init failed — caching disabled: %s", exc)
            self._redis = None

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
        if self._redis:
            await self._redis.aclose()

    # ------------------------------------------------------------------
    # Table bootstrap
    # ------------------------------------------------------------------

    async def _ensure_tables(self) -> None:
        if self._tables_checked:
            return
        self._tables_checked = True
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(_DDL)
            self._tables_ready = True
            logger.debug("MemoryManager: memory tables verified/created")
        except Exception as exc:
            logger.warning(
                "MemoryManager: could not create memory tables — %s. "
                "Memory features will be skipped for this session.",
                exc,
            )
            self._tables_ready = False

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    async def build_agent_context(
        self,
        user_id: str,
        agent_type: str,
        max_episodes: int = 5,
    ) -> dict[str, Any]:
        """
        Returns a context dict with:
          - preferences:  {key: value} from user_preferences
          - learnings:    list[str] of top learnings sorted by success_rate
          - past_episodes: list[dict] summarising recent episodes
          - blacklist:    list[str] of strategies with success_rate < 0.2
          - procedures:   list[dict] of reusable workflow patterns
        """
        empty: dict[str, Any] = {
            "preferences": {},
            "learnings": [],
            "past_episodes": [],
            "blacklist": [],
            "procedures": [],
        }
        if not self._tables_ready or not self._pool:
            return empty

        try:
            async with self._pool.acquire() as conn:
                # Preferences
                prefs_rows = await conn.fetch(
                    "SELECT preference_key, preference_value "
                    "FROM agent_memory_preferences WHERE user_id = $1",
                    user_id,
                )
                preferences = {r["preference_key"]: r["preference_value"] for r in prefs_rows}

                # Learnings
                learning_rows = await conn.fetch(
                    "SELECT learning, success_rate FROM agent_memory_learnings "
                    "WHERE user_id = $1 AND agent_type = $2 "
                    "ORDER BY success_rate DESC LIMIT 20",
                    user_id,
                    agent_type,
                )
                learnings = [r["learning"] for r in learning_rows if r["success_rate"] >= 0.2]
                blacklist = [r["learning"] for r in learning_rows if r["success_rate"] < 0.2]

                # Past episodes
                ep_rows = await conn.fetch(
                    "SELECT task_type, strategy, success, output_summary, created_at "
                    "FROM agent_memory_episodes "
                    "WHERE user_id = $1 AND agent_type = $2 "
                    "ORDER BY created_at DESC LIMIT $3",
                    user_id,
                    agent_type,
                    max_episodes,
                )
                past_episodes = [
                    {
                        "task_type": r["task_type"],
                        "strategy": r["strategy"],
                        "success": r["success"],
                        "summary": r["output_summary"] or "",
                        "when": r["created_at"].isoformat() if r["created_at"] else "",
                    }
                    for r in ep_rows
                ]

                # Procedural memory
                proc_rows = await conn.fetch(
                    "SELECT trigger_desc, workflow_json, success_count "
                    "FROM agent_memory_procedures "
                    "WHERE user_id = $1 AND agent_type = $2 "
                    "ORDER BY success_count DESC LIMIT 5",
                    user_id,
                    agent_type,
                )
                procedures = [
                    {
                        "trigger": r["trigger_desc"],
                        "workflow": json.loads(r["workflow_json"]),
                        "success_count": r["success_count"],
                    }
                    for r in proc_rows
                ]

            return {
                "preferences": preferences,
                "learnings": learnings,
                "past_episodes": past_episodes,
                "blacklist": blacklist,
                "procedures": procedures,
            }
        except Exception as exc:
            logger.warning("MemoryManager.build_agent_context failed: %s", exc)
            return empty

    # ------------------------------------------------------------------
    # Episode saving
    # ------------------------------------------------------------------

    async def save_episode(
        self,
        user_id: str,
        agent_type: str,
        task_type: str,
        strategy: str,
        success: bool,
        context_summary: str = "",
        output_summary: str = "",
    ) -> None:
        """Persist a completed episode."""
        if not self._tables_ready or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_memory_episodes
                        (user_id, agent_type, task_type, strategy,
                         success, context_summary, output_summary)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    user_id,
                    agent_type,
                    task_type,
                    strategy,
                    success,
                    context_summary[:2000] if context_summary else "",
                    output_summary[:2000] if output_summary else "",
                )
        except Exception as exc:
            logger.warning("MemoryManager.save_episode failed: %s", exc)

    # ------------------------------------------------------------------
    # Learning upsert
    # ------------------------------------------------------------------

    async def upsert_learning(
        self,
        user_id: str,
        agent_type: str,
        learning: str,
        success: bool,
    ) -> None:
        """Increment success/failure count for a strategy learning entry."""
        if not self._tables_ready or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                # Upsert: create or increment counters
                await conn.execute(
                    """
                    INSERT INTO agent_memory_learnings
                        (user_id, agent_type, learning, success_rate, sample_count, updated_at)
                    VALUES ($1, $2, $3, $4, 1, NOW())
                    ON CONFLICT (user_id, agent_type, learning) DO UPDATE SET
                        sample_count = agent_memory_learnings.sample_count + 1,
                        success_rate = (
                            agent_memory_learnings.success_rate * agent_memory_learnings.sample_count
                            + $4
                        ) / (agent_memory_learnings.sample_count + 1),
                        updated_at = NOW()
                    """,
                    user_id,
                    agent_type,
                    learning,
                    1.0 if success else 0.0,
                )
        except Exception as exc:
            logger.warning("MemoryManager.upsert_learning failed: %s", exc)

    # ------------------------------------------------------------------
    # Bulk learning save (from reflection)
    # ------------------------------------------------------------------

    async def save_learnings(
        self,
        user_id: str,
        agent_type: str,
        learnings: list[dict[str, Any]],
    ) -> None:
        """
        Bulk-save reflection-derived learnings.

        Each item: {"learning": str, "success_rate": float, "sample_count": int}
        """
        if not self._tables_ready or not self._pool:
            return
        for item in learnings:
            try:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO agent_memory_learnings
                            (user_id, agent_type, learning,
                             success_rate, sample_count, updated_at)
                        VALUES ($1, $2, $3, $4, $5, NOW())
                        ON CONFLICT (user_id, agent_type, learning) DO UPDATE SET
                            success_rate = EXCLUDED.success_rate,
                            sample_count = EXCLUDED.sample_count,
                            updated_at   = NOW()
                        """,
                        user_id,
                        agent_type,
                        item.get("learning", ""),
                        float(item.get("success_rate", 0.0)),
                        int(item.get("sample_count", 0)),
                    )
            except Exception as exc:
                logger.warning("MemoryManager.save_learnings item failed: %s", exc)

    # ------------------------------------------------------------------
    # Redis helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Episode count since timestamp (for reflection gating)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Recent episodes for reflection
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Preference learning — auto-extract from application history
    # ------------------------------------------------------------------

    async def set_preference(self, user_id: str, key: str, value: str) -> None:
        """Set or update a user preference."""
        if not self._tables_ready or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                        INSERT INTO agent_memory_preferences (user_id, preference_key, preference_value)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, preference_key) DO UPDATE SET
                        preference_value = EXCLUDED.preference_value
                    """,
                    user_id,
                    key,
                    value,
                )
        except Exception as exc:
            logger.warning("MemoryManager.set_preference failed: %s", exc)

    async def get_preferences(self, user_id: str) -> dict[str, str]:
        """Get all preferences for a user."""
        if not self._tables_ready or not self._pool:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT preference_key, preference_value FROM agent_memory_preferences WHERE user_id = $1",
                    user_id,
                )
                return {r["preference_key"]: r["preference_value"] for r in rows}
        except Exception as exc:
            logger.warning("MemoryManager.get_preferences failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Blacklist — companies/roles user rejected or that didn't match
    # ------------------------------------------------------------------

    async def get_blacklist(self, user_id: str) -> dict[str, list[str]]:
        """Get user's blacklisted companies and roles."""
        companies = await self._get_list_preference(user_id, "blacklist_company")
        roles = await self._get_list_preference(user_id, "blacklist_role")
        return {"companies": companies, "roles": roles}

    async def _get_list_preference(self, user_id: str, key: str) -> list[str]:
        """Get a JSON list preference."""
        prefs = await self.get_preferences(user_id)
        raw = prefs.get(key, "[]")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Cross-session context — persistent user profile data
    # ------------------------------------------------------------------

    async def get_user_context(self, user_id: str) -> dict[str, str]:
        """Retrieve cross-session context for injection into agent prompts."""
        prefs = await self.get_preferences(user_id)
        return {
            "target_salary": prefs.get("target_salary", ""),
            "preferred_locations": prefs.get("preferred_locations", ""),
            "deal_breakers": prefs.get("deal_breakers", ""),
            "target_roles": prefs.get("target_roles", ""),
        }

    # ------------------------------------------------------------------
    # Post-pipeline memory extraction
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Procedural memory — store successful workflows as reusable patterns
    # ------------------------------------------------------------------

    async def save_procedure(
        self, user_id: str, agent_type: str, trigger_desc: str, workflow: dict
    ) -> None:
        """Save a successful workflow as a reusable procedure.

        Args:
            user_id: User who executed the workflow
            agent_type: Which agent type produced this workflow
            trigger_desc: Description of when to use this procedure
            workflow: The workflow steps/config as a dict
        """
        if not self._tables_ready or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO agent_memory_procedures
                        (user_id, agent_type, trigger_desc, workflow_json, success_count, last_used_at)
                    VALUES ($1, $2, $3, $4, 1, NOW())
                    ON CONFLICT (user_id, agent_type, trigger_desc) DO UPDATE SET
                        success_count = agent_memory_procedures.success_count + 1,
                        last_used_at = NOW()
                    """,
                    user_id,
                    agent_type,
                    trigger_desc,
                    json.dumps(workflow),
                )
        except Exception as exc:
            logger.warning("MemoryManager.save_procedure failed: %s", exc)
