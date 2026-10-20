"""
token_budget_service.py — Daily token usage limits per user.

Tracks tokens consumed via Redis counters with 24h TTL.
Raises TokenBudgetExceeded when user exceeds their daily limit.

Default: 500,000 tokens/day (adjustable per user tier).
"""

import logging
from datetime import date

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT = 500_000  # tokens per user per day


async def _get_redis() -> aioredis.Redis:
    from app.core.redis_client import get_redis

    return get_redis()


def _budget_key(user_id: str) -> str:
    return f"token_budget:{user_id}:{date.today().isoformat()}"


class TokenBudgetExceeded(Exception):
    """Raised when a user exceeds their daily token budget."""

    def __init__(self, user_id: str, used: int, limit: int):
        self.user_id = user_id
        self.used = used
        self.limit = limit
        super().__init__(f"User {user_id} exceeded daily token limit: {used}/{limit}")


async def check_budget(user_id: str, limit: int = DEFAULT_DAILY_LIMIT) -> int:
    """Check remaining token budget. Returns tokens remaining."""
    r = await _get_redis()
    key = _budget_key(user_id)
    used = int(await r.get(key) or 0)
    return max(0, limit - used)


def record_tokens_sync(user_id: str, tokens: int) -> int:
    """Add to today's usage from synchronous code (LLM callbacks run on worker
    threads where no event loop exists). Never raises: the check happens
    before a call, this only keeps the count honest."""
    import redis

    client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        key = _budget_key(user_id)
        new_total = client.incrby(key, tokens)
        if new_total == tokens:
            client.expire(key, 86400)
        return int(new_total)
    finally:
        client.close()
