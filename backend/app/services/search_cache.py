"""Redis cache for the shared (non-personal) stages of job search.

Job search results are public job postings, identical for every member who
runs the same query, while producing them costs a catalog scan (~120 ms of
Postgres plus Python filtering) or a live scrape (LinkedIn via JobSpy takes
~40 s). This module caches those stages across members and processes:

- ``get_or_compute`` returns a cached payload, or runs ``compute`` once per
  key: concurrent callers for the same key wait for the first one instead of
  scraping in parallel (a short Redis lock).
- Keys hold a hash of the normalised query, never a user id. Per-member work
  (resume ranking, red flags, saved applications) always runs after the cache.
- Every key expires (Redis runs with ``noeviction``) and payloads are size
  capped. Any Redis error or slowness falls back to computing directly, so a
  Redis outage only makes search as slow as it was before.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "jobsearch:v1"
CATALOG_TTL = 600  # catalog refreshes bump the version, so this only bounds staleness
LIVE_TTL = 1800  # live adapters already look back 72h; half an hour is plenty fresh
MAX_PAYLOAD_BYTES = 1_000_000
_OP_TIMEOUT = 0.5  # a slow Redis must never slow search down
_POLL = 0.25
_STATS_TTL = 8 * 24 * 3600

_ERROR = object()  # an _op that failed, as opposed to Redis answering None


def _redis():
    from app.core.config import settings

    if not settings.SEARCH_CACHE_ENABLED:
        return None
    from app.core.redis_client import get_redis

    return get_redis()


async def _op(awaitable, default=None):
    try:
        return await asyncio.wait_for(awaitable, _OP_TIMEOUT)
    except Exception as exc:
        logger.debug("search cache redis op failed: %s", type(exc).__name__)
        return default


def query_hash(parts: dict[str, Any]) -> str:
    """Stable hash of an already-normalised dict (sorted keys, no whitespace)."""
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def terms_of(query: dict) -> list[str]:
    """Search words, order-insensitive: matching requires every word anyway."""
    text = " ".join(query.get("titles") or []) or query.get("search_query") or "software engineer"
    return sorted(set(str(text).lower().split()))


def places_of(query: dict) -> list[str]:
    raw = query.get("locations") or [query.get("location") or ""]
    return sorted({str(x).strip().lower() for x in raw if x})


async def catalog_version() -> str:
    r = _redis()
    if r is None:
        return "off"
    return str(await _op(r.get(f"{PREFIX}:catalog_version")) or "0")


async def bump_catalog_version() -> None:
    """Called after a catalog refresh so new jobs show up before the TTL ends."""
    r = _redis()
    if r is not None:
        await _op(r.incr(f"{PREFIX}:catalog_version"))


async def _count(kind: str, outcome: str) -> None:
    r = _redis()
    if r is None:
        return
    key = f"{PREFIX}:stats:{datetime.now(UTC):%Y%m%d}:{kind}:{outcome}"
    await _op(r.incr(key))
    await _op(r.expire(key, _STATS_TTL))


async def get_or_compute(
    kind: str,
    key: str,
    ttl: int,
    compute: Callable[[], Awaitable[tuple[Any, str | None]]],
    *,
    wait_s: float,
) -> tuple[Any, str | None]:
    """Serve ``key`` from Redis, or compute it once across all callers.

    ``compute()`` returns ``(value, payload)``; ``payload`` is the string to
    cache, or ``None`` to skip caching (e.g. an empty or failed result).
    Returns ``(value, None)`` when computed here and ``(None, payload)`` on a
    cache hit. Exceptions from ``compute`` propagate and nothing is cached.
    """
    r = _redis()
    if r is None:
        return (await compute())[0], None

    cached = await _op(r.get(key), default=_ERROR)
    if cached is _ERROR:
        return (await compute())[0], None  # Redis unavailable: search uncached
    if cached is not None:
        await _count(kind, "hit")
        return None, cached

    lock, token = f"{key}:lock", secrets.token_hex(8)
    owner = await _op(r.set(lock, token, nx=True, ex=int(wait_s) + 30), default=_ERROR)
    if owner is _ERROR:
        return (await compute())[0], None
    if not owner:
        # Someone else is computing this key: wait for their result.
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL)
            cached = await _op(r.get(key))
            if cached is not None:
                await _count(kind, "hit")
                return None, cached
            if not await _op(r.exists(lock), default=0):
                break  # they finished without caching (failure or empty result)

    await _count(kind, "miss")
    try:
        value, payload = await compute()
        if payload is not None and len(payload) <= MAX_PAYLOAD_BYTES:
            await _op(r.set(key, payload, ex=ttl))
        return value, None
    finally:
        if owner:
            # Only drop our own lock (it may have expired and been re-taken).
            # ponytail: get-then-delete is not atomic; the window is a single
            # round trip and the worst case is one extra concurrent compute.
            if await _op(r.get(lock)) == token:
                await _op(r.delete(lock))
