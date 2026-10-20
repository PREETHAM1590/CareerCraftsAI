"""Owner-scoped erasure and generation fences for both memory implementations.

A run captures the generation before retrieval. Persistence locks the owner and
checks it again, so work started before Forget cannot recreate erased memories.
Only local memory persistence holds the lock. Provider embeddings and extraction
are prepared outside it, then discarded if the generation has changed.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text

from app.models.db import User

MEMORY_DELETES = {
    "public.user_memories": "DELETE FROM public.user_memories WHERE user_id = CAST(:uid AS uuid)",
    "public.agent_episodes": "DELETE FROM public.agent_episodes WHERE user_id = CAST(:uid AS uuid)",
    "public.memory_access_log": (
        "DELETE FROM public.memory_access_log WHERE user_id = CAST(:uid AS uuid)"
    ),
    "public.agent_memory_episodes": "DELETE FROM public.agent_memory_episodes WHERE user_id = :uid",
    "public.agent_memory_learnings": (
        "DELETE FROM public.agent_memory_learnings WHERE user_id = :uid"
    ),
    "public.agent_memory_preferences": (
        "DELETE FROM public.agent_memory_preferences WHERE user_id = :uid"
    ),
    "public.agent_memory_procedures": (
        "DELETE FROM public.agent_memory_procedures WHERE user_id = :uid"
    ),
}


async def delete_memory_rows(db, user_id) -> None:
    for table, statement in MEMORY_DELETES.items():
        exists = await db.execute(text("SELECT to_regclass(:table)"), {"table": table})
        if exists.scalar():
            await db.execute(text(statement), {"uid": str(user_id)})


async def delete_memory_cache(user_id) -> None:
    from app.core.redis_client import get_redis

    redis = get_redis()
    # SCAN in bounded batches rather than collecting all session keys in RAM.
    for pattern in (f"session:{user_id}:*", f"memory-emb:{user_id}:*"):
        batch = []
        async for key in redis.scan_iter(match=pattern, count=100):
            batch.append(key)
            if len(batch) == 100:
                await redis.delete(*batch)
                batch.clear()
        if batch:
            await redis.delete(*batch)


@asynccontextmanager
async def memory_access(user_id: str, generation: int | None = None):
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        async with db.begin():
            owner = (
                await db.execute(
                    select(User)
                    .where(User.id == UUID(user_id))
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            eligible = owner is not None and (
                owner.deletion_scheduled_for is None
                or owner.deletion_scheduled_for > datetime.now(UTC)
            )
            if not eligible or (generation is not None and owner.memory_generation != generation):
                yield None
            else:
                yield owner.memory_generation
