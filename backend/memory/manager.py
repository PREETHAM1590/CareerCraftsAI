"""
memory/manager.py — Core memory manager for JobAgent AI.

Uses asyncpg directly (no SQLAlchemy) for all PostgreSQL/pgvector operations.
Short-term session state is stored in Redis under key pattern
`session:{user_id}:{key}`.

Deduplication: if a new memory has cosine similarity > 0.92 with an existing
active memory of the same type, we UPDATE the existing row instead of
inserting a new one.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg
import redis

from memory.embedder import EmbeddingProvider
from memory.models import AgentContext, Episode, Learning, Memory

logger = logging.getLogger(__name__)

# Similarity threshold above which we consider two memories duplicates.
_DEDUP_THRESHOLD = 0.92

# pgvector cosine-distance query — returns rows ordered nearest-first.
_RECALL_SQL = """
    SELECT id, memory_type, content, source_agent, confidence,
           times_used, is_active, created_at, updated_at,
           1 - (embedding <=> $1::vector) AS similarity
    FROM user_memories
    WHERE user_id = $2
      AND is_active = true
      AND embedding IS NOT NULL
      AND ({type_filter})
    ORDER BY embedding <=> $1::vector
    LIMIT $3
"""

_RECALL_NO_EMB_SQL = """
    SELECT id, memory_type, content, source_agent, confidence,
           times_used, is_active, created_at, updated_at,
           0.0 AS similarity
    FROM user_memories
    WHERE user_id = $1
      AND is_active = true
      AND ({type_filter})
      AND content ILIKE $2
    ORDER BY updated_at DESC
    LIMIT $3
"""


class MemoryManager:
    """
    Manages long-term memories (PostgreSQL/pgvector) and short-term session
    state (Redis) for a single deployment instance.

    Typical usage inside an agent node::

        manager = MemoryManager(db_url, redis_client, embedder)
        ctx = await manager.build_agent_context(user_id, "resume_agent", task="tailor resume")
        # ... run agent ...
        await manager.save_episode(Episode(...))
    """

    def __init__(
        self,
        db_url: str,
        redis_client: redis.Redis,
        embedder: EmbeddingProvider,
    ) -> None:
        self._db_url = db_url
        self.redis = redis_client
        self.embedder = embedder
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    async def _get_pool(self) -> asyncpg.Pool:
        """Lazy-initialise the asyncpg connection pool."""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                dsn=self._db_url,
                min_size=1,
                max_size=2,
                statement_cache_size=0,
            )
        return self._pool

    async def close(self) -> None:
        """Gracefully close the connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # save_memory
    # ------------------------------------------------------------------

    async def save_memory(self, memory: Memory) -> Optional[UUID]:
        """
        Persist a Memory record to user_memories.

        If embedding succeeds and a near-duplicate (similarity > 0.92) already
        exists for this user + type, the existing row is updated (content,
        confidence, updated_at) rather than creating a duplicate.

        Returns the UUID of the inserted / updated row, or None on failure.
        """
        try:
            embedding = await self.embedder.embed(memory.content)
            has_embedding = any(v != 0.0 for v in embedding)

            pool = await self._get_pool()
            async with pool.acquire() as conn:
                # Register pgvector codec so asyncpg accepts list[float] as vector
                await conn.execute("SET LOCAL search_path TO public")

                if has_embedding:
                    # Check for near-duplicate
                    existing = await conn.fetchrow(
                        """
                        SELECT id FROM user_memories
                        WHERE user_id = $1
                          AND memory_type = $2
                          AND is_active = true
                          AND 1 - (embedding <=> $3::vector) > $4
                        ORDER BY embedding <=> $3::vector
                        LIMIT 1
                        """,
                        memory.user_id,
                        memory.memory_type,
                        json.dumps(embedding),
                        _DEDUP_THRESHOLD,
                    )

                    if existing:
                        # Update existing row
                        await conn.execute(
                            """
                            UPDATE user_memories
                            SET content     = $1,
                                confidence  = $2,
                                source_agent= COALESCE($3, source_agent),
                                embedding   = $4::vector,
                                updated_at  = NOW()
                            WHERE id = $5
                            """,
                            memory.content,
                            memory.confidence,
                            memory.source_agent,
                            json.dumps(embedding),
                            existing["id"],
                        )
                        return existing["id"]

                # Insert new row
                emb_param = json.dumps(embedding) if has_embedding else None
                row = await conn.fetchrow(
                    """
                    INSERT INTO user_memories
                        (user_id, memory_type, content, embedding,
                         source_agent, confidence)
                    VALUES ($1, $2, $3, $4::vector, $5, $6)
                    RETURNING id
                    """,
                    memory.user_id,
                    memory.memory_type,
                    memory.content,
                    emb_param,
                    memory.source_agent,
                    memory.confidence,
                )
                return row["id"]

        except Exception as e:
            logger.error(f"save_memory failed for user={memory.user_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # recall
    # ------------------------------------------------------------------

    async def recall(
        self,
        user_id: UUID,
        query: str,
        k: int = 5,
        memory_types: Optional[list[str]] = None,
    ) -> list[Memory]:
        """
        Return the top-k memories most similar to query.

        Falls back to keyword ILIKE search when the query embedding is all
        zeros (i.e. embedding provider unavailable).
        """
        try:
            types = memory_types or ["preference", "fact", "outcome", "blacklist", "skill", "style"]
            type_filter = " OR ".join(f"memory_type = '{t}'" for t in types)

            embedding = await self.embedder.embed(query)
            has_embedding = any(v != 0.0 for v in embedding)

            pool = await self._get_pool()
            async with pool.acquire() as conn:
                if has_embedding:
                    sql = _RECALL_SQL.format(type_filter=type_filter)
                    rows = await conn.fetch(sql, json.dumps(embedding), user_id, k)
                else:
                    # keyword fallback
                    kw = f"%{query}%"
                    sql = _RECALL_NO_EMB_SQL.format(type_filter=type_filter)
                    rows = await conn.fetch(sql, user_id, kw, k)

                # Bump times_used for returned rows
                if rows:
                    ids = [r["id"] for r in rows]
                    await conn.execute(
                        "UPDATE user_memories SET times_used = times_used + 1 WHERE id = ANY($1)",
                        ids,
                    )

                return [self._row_to_memory(r, user_id) for r in rows]

        except Exception as e:
            logger.error(f"recall failed for user={user_id}: {e}")
            return []

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

    async def get_preferences(self, user_id: UUID) -> list[Memory]:
        """Return all active preference memories for a user."""
        return await self._list_by_type(user_id, "preference")

    async def get_blacklist(self, user_id: UUID) -> list[Memory]:
        """Return all active blacklist memories for a user."""
        return await self._list_by_type(user_id, "blacklist")

    async def _list_by_type(self, user_id: UUID, memory_type: str) -> list[Memory]:
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, memory_type, content, source_agent, confidence,
                           times_used, is_active, created_at, updated_at,
                           0.0 AS similarity
                    FROM user_memories
                    WHERE user_id = $1 AND memory_type = $2 AND is_active = true
                    ORDER BY created_at DESC
                    """,
                    user_id,
                    memory_type,
                )
                return [self._row_to_memory(r, user_id) for r in rows]
        except Exception as e:
            logger.error(f"_list_by_type failed user={user_id} type={memory_type}: {e}")
            return []

    # ------------------------------------------------------------------
    # delete_memory
    # ------------------------------------------------------------------

    async def delete_memory(self, memory_id: UUID, user_id: UUID | None = None) -> bool:
        """Soft-delete a memory by setting is_active = false."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                if user_id is None:
                    result = await conn.execute(
                        "UPDATE user_memories SET is_active = false, updated_at = NOW() WHERE id = $1",
                        memory_id,
                    )
                else:
                    result = await conn.execute(
                        """
                        UPDATE user_memories
                        SET is_active = false, updated_at = NOW()
                        WHERE id = $1 AND user_id = $2
                        """,
                        memory_id,
                        user_id,
                    )
                return result == "UPDATE 1"
        except Exception as e:
            logger.error(f"delete_memory failed id={memory_id}: {e}")
            return False

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    async def save_episode(self, episode: Episode) -> Optional[UUID]:
        """
        Persist an agent episode to agent_episodes.

        Embeds the summary text for future semantic recall.
        """
        try:
            embedding: Optional[list[float]] = None
            if episode.summary:
                vec = await self.embedder.embed(episode.summary)
                if any(v != 0.0 for v in vec):
                    embedding = vec

            pool = await self._get_pool()
            async with pool.acquire() as conn:
                emb_param = json.dumps(embedding) if embedding else None
                row = await conn.fetchrow(
                    """
                    INSERT INTO agent_episodes
                        (user_id, agent_type, summary, input, output, outcome, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                    RETURNING id
                    """,
                    episode.user_id,
                    episode.agent_type,
                    episode.summary,
                    json.dumps(episode.input) if episode.input is not None else None,
                    json.dumps(episode.output) if episode.output is not None else None,
                    episode.outcome,
                    emb_param,
                )
                return row["id"]
        except Exception as e:
            logger.error(f"save_episode failed user={episode.user_id}: {e}")
            return None

    async def recall_episodes(
        self,
        user_id: UUID,
        agent_type: str,
        limit: int = 10,
    ) -> list[Episode]:
        """Return the most recent episodes for a given agent type."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, agent_type, summary, input, output, outcome, created_at
                    FROM agent_episodes
                    WHERE user_id = $1 AND agent_type = $2
                    ORDER BY created_at DESC
                    LIMIT $3
                    """,
                    user_id,
                    agent_type,
                    limit,
                )
                return [self._row_to_episode(r, user_id) for r in rows]
        except Exception as e:
            logger.error(f"recall_episodes failed user={user_id} agent={agent_type}: {e}")
            return []

    # ------------------------------------------------------------------
    # Learnings
    # ------------------------------------------------------------------

    async def save_learning(self, learning: Learning) -> Optional[UUID]:
        """
        Upsert a cross-user agent learning.

        If a learning with the same agent_type + learning text already exists,
        increment evidence_count and recompute success_rate.
        """
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO agent_learnings
                        (agent_type, learning, success_rate, evidence_count, last_applied)
                    VALUES ($1, $2, $3, 1, NOW())
                    ON CONFLICT (agent_type, learning) DO NOTHING
                    RETURNING id
                    """,
                    learning.agent_type,
                    learning.learning,
                    learning.success_rate,
                )
                if row:
                    return row["id"]

                # Already exists — bump evidence_count
                existing = await conn.fetchrow(
                    """
                    UPDATE agent_learnings
                    SET evidence_count = evidence_count + 1,
                        success_rate   = (success_rate * evidence_count + $1) / (evidence_count + 1),
                        last_applied   = NOW()
                    WHERE agent_type = $2 AND learning = $3
                    RETURNING id
                    """,
                    learning.success_rate,
                    learning.agent_type,
                    learning.learning,
                )
                return existing["id"] if existing else None
        except Exception as e:
            logger.error(f"save_learning failed agent={learning.agent_type}: {e}")
            return None

    async def get_learnings(self, agent_type: str, limit: int = 10) -> list[Learning]:
        """Return top learnings for an agent type ordered by evidence × success_rate."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, agent_type, learning, evidence_count,
                           success_rate, last_applied, created_at
                    FROM agent_learnings
                    WHERE agent_type = $1
                    ORDER BY (evidence_count * success_rate) DESC
                    LIMIT $2
                    """,
                    agent_type,
                    limit,
                )
                return [self._row_to_learning(r) for r in rows]
        except Exception as e:
            logger.error(f"get_learnings failed agent={agent_type}: {e}")
            return []

    # ------------------------------------------------------------------
    # Session (Redis short-term memory)
    # ------------------------------------------------------------------

    def set_session(self, user_id: UUID | str, key: str, value: Any, ttl: int = 3600) -> None:
        """Store a value in the user's Redis session. TTL defaults to 1 hour."""
        try:
            rkey = f"session:{user_id}:{key}"
            self.redis.setex(rkey, ttl, json.dumps(value))
        except Exception as e:
            logger.error(f"set_session failed user={user_id} key={key}: {e}")

    def get_session(self, user_id: UUID | str, key: str) -> Any:
        """Retrieve a value from the user's Redis session. Returns None if missing."""
        try:
            rkey = f"session:{user_id}:{key}"
            raw = self.redis.get(rkey)
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.error(f"get_session failed user={user_id} key={key}: {e}")
            return None

    def get_all_session(self, user_id: UUID | str) -> dict[str, Any]:
        """Return all session keys for a user as a dict."""
        try:
            pattern = f"session:{user_id}:*"
            keys = self.redis.keys(pattern)
            result: dict[str, Any] = {}
            for k in keys:
                raw = self.redis.get(k)
                if raw:
                    short_key = k.decode() if isinstance(k, bytes) else k
                    short_key = short_key.removeprefix(f"session:{user_id}:")
                    result[short_key] = json.loads(raw)
            return result
        except Exception as e:
            logger.error(f"get_all_session failed user={user_id}: {e}")
            return {}

    def clear_session(self, user_id: UUID | str) -> int:
        """Delete all session keys for a user. Returns number of keys deleted."""
        try:
            pattern = f"session:{user_id}:*"
            keys = self.redis.keys(pattern)
            if keys:
                return self.redis.delete(*keys)
            return 0
        except Exception as e:
            logger.error(f"clear_session failed user={user_id}: {e}")
            return 0

    # ------------------------------------------------------------------
    # build_agent_context  (THE most critical method)
    # ------------------------------------------------------------------

    async def build_agent_context(
        self,
        user_id: UUID,
        agent_type: str,
        task: str = "",
    ) -> AgentContext:
        """
        Assemble the full memory context that is injected into every agent run.

        Combines:
        - preferences: all active preference memories
        - blacklist: all active blacklist memories
        - past_episodes: last 5 episodes for this agent_type
        - learnings: top learnings for this agent_type
        - session_context: live Redis keys for this user
        - user_style: merged text from 'style' memories

        If task is provided, a semantic recall against task text is run to
        include the most relevant facts as extra preferences context.
        """
        try:
            # Fan out concurrent DB calls
            import asyncio

            prefs_task = asyncio.create_task(self.get_preferences(user_id))
            blacklist_task = asyncio.create_task(self.get_blacklist(user_id))
            episodes_task = asyncio.create_task(self.recall_episodes(user_id, agent_type, limit=5))
            learnings_task = asyncio.create_task(self.get_learnings(agent_type, limit=10))
            style_task = asyncio.create_task(self._list_by_type(user_id, "style"))

            task_recall: list[Memory] = []
            if task:
                task_recall = await self.recall(
                    user_id,
                    query=task,
                    k=5,
                    memory_types=["fact", "skill"],
                )

            prefs, blacklist, episodes, learnings, styles = await asyncio.gather(
                prefs_task, blacklist_task, episodes_task, learnings_task, style_task
            )

            # Merge task-relevant facts into preferences (deduplicate by content)
            pref_contents = {m.content for m in prefs}
            extra_prefs = [m for m in task_recall if m.content not in pref_contents]

            return AgentContext(
                preferences=[m.content for m in prefs + extra_prefs],
                blacklist=[m.content for m in blacklist],
                past_episodes=[
                    {
                        "summary": ep.summary or "",
                        "outcome": ep.outcome or "pending",
                        "created_at": ep.created_at.isoformat() if ep.created_at else "",
                    }
                    for ep in episodes
                ],
                learnings=[l.learning for l in learnings],
                session_context=self.get_all_session(user_id),
                user_style=" ".join(m.content for m in styles),
            )

        except Exception as e:
            logger.error(f"build_agent_context failed user={user_id} agent={agent_type}: {e}")
            return AgentContext()

    # ------------------------------------------------------------------
    # access log
    # ------------------------------------------------------------------

    async def log_access(
        self,
        user_id: UUID,
        query: str,
        results: list,
        agent_type: str = "",
    ) -> None:
        """Append a row to memory_access_log (fire-and-forget; errors suppressed)."""
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO memory_access_log (user_id, query, results, agent_type)
                    VALUES ($1, $2, $3, $4)
                    """,
                    user_id,
                    query,
                    json.dumps(results),
                    agent_type or None,
                )
        except Exception as e:
            logger.debug(f"log_access suppressed: {e}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_memory(row: asyncpg.Record, user_id: UUID) -> Memory:
        return Memory(
            id=row["id"],
            user_id=user_id,
            memory_type=row["memory_type"],
            content=row["content"],
            source_agent=row.get("source_agent"),
            confidence=row.get("confidence", 1.0),
            times_used=row.get("times_used", 0),
            is_active=row.get("is_active", True),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    @staticmethod
    def _row_to_episode(row: asyncpg.Record, user_id: UUID) -> Episode:
        raw_input = row.get("input")
        raw_output = row.get("output")
        return Episode(
            id=row["id"],
            user_id=user_id,
            agent_type=row["agent_type"],
            summary=row.get("summary"),
            input=json.loads(raw_input) if isinstance(raw_input, str) else raw_input,
            output=json.loads(raw_output) if isinstance(raw_output, str) else raw_output,
            outcome=row.get("outcome"),
            created_at=row.get("created_at"),
        )

    @staticmethod
    def _row_to_learning(row: asyncpg.Record) -> Learning:
        return Learning(
            id=row["id"],
            agent_type=row["agent_type"],
            learning=row["learning"],
            evidence_count=row.get("evidence_count", 1),
            success_rate=row.get("success_rate", 1.0),
            last_applied=row.get("last_applied"),
            created_at=row.get("created_at"),
        )
