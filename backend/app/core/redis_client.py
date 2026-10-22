import asyncio
import logging

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_pool() -> aioredis.ConnectionPool:
    # Redis sockets and locks belong to the loop that first uses them. Agent
    # nodes run on short-lived loops as well as the API/Temporal worker loop.
    loop = asyncio.get_running_loop()
    pool = getattr(loop, "_careercraft_redis_pool", None)
    if pool is None:
        pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=20,
            decode_responses=True,
        )
        loop._careercraft_redis_pool = pool
    return pool


def get_redis() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_get_pool())


def get_blocking_redis(purpose: str = "poll") -> aioredis.Redis:
    """For long BLPOP waits on the extension command channel. Each wait holds a
    connection for up to a minute, so these never share the pool the SSE bus
    and caches use. Device long-polls ("poll") and the backend's result waits
    ("result") get separate pools so idle paired devices can't starve a
    member's pending action."""
    loop = asyncio.get_running_loop()
    name = f"_careercraft_blocking_redis_pool_{purpose}"
    pool = getattr(loop, name, None)
    if pool is None:
        pool = aioredis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=500,
            decode_responses=True,
        )
        setattr(loop, name, pool)
    return aioredis.Redis(connection_pool=pool)


async def check_redis_connection() -> bool:
    try:
        r = get_redis()
        await r.ping()
        return True
    except Exception as exc:
        logger.error("Redis connection check failed: %s", exc)
        return False


async def close_redis() -> None:
    loop = asyncio.get_running_loop()
    for name in (
        "_careercraft_redis_pool",
        "_careercraft_blocking_redis_pool_poll",
        "_careercraft_blocking_redis_pool_result",
    ):
        pool = getattr(loop, name, None)
        if pool is not None:
            await pool.disconnect()
            delattr(loop, name)
