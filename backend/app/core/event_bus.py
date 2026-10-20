from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)
suppress_terminal_events: ContextVar[bool] = ContextVar("suppress_terminal_events", default=False)

# Redis channel agent:{run_id}:events — the SSE endpoint subscribes here.
# We publish from any thread via a dedicated publisher loop+thread.
_channel_events = lambda run_id: f"agent:{run_id}:events"
_clock_channel = lambda: f"agent:{'clock'}:events"


# Per-process publisher state
_redis: aioredis.Redis | None = None
_publisher_loop: asyncio.AbstractEventLoop | None = None
_publisher_thread: threading.Thread | None = None
_publisher_lock = threading.Lock()
_publisher_ready = threading.Event()


def _get_redis() -> aioredis.Redis:
    global _redis
    with _publisher_lock:
        if _redis is None:
            _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        return _redis


def _run_publisher_loop() -> None:
    global _publisher_loop
    _publisher_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_publisher_loop)
    # Signal ready only after the loop is actually running, via a scheduled callback
    _publisher_loop.call_soon(lambda: _publisher_ready.set())
    try:
        _get_redis()
    except Exception as exc:
        logger.warning("SSE publisher Redis init failed: %s", exc)
    try:
        _publisher_loop.run_forever()
    finally:
        try:
            pending = asyncio.all_tasks(_publisher_loop)
            for task in pending:
                task.cancel()
            if pending:
                _publisher_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        try:
            _publisher_loop.close()
        except Exception:
            pass


def _ensure_publisher() -> None:
    global _publisher_thread
    if _publisher_thread is not None and _publisher_thread.is_alive():
        return
    with _publisher_lock:
        if _publisher_thread is not None and _publisher_thread.is_alive():
            return
        _publisher_ready.clear()
        _publisher_thread = threading.Thread(
            target=_run_publisher_loop, name="sse-publisher", daemon=True
        )
        _publisher_thread.start()
        _publisher_ready.wait(timeout=5.0)


async def _publish_coro(channel: str, payload: str) -> None:
    try:
        r = _get_redis()
        async with asyncio.timeout(2.0):
            await r.publish(channel, payload)
    except Exception as exc:
        logger.warning("SSE publish failed on %s: %s", channel, exc)


def publish(run_id: str, event_type: str, payload: Any) -> None:
    """
    Publish an SSE event payload to Redis for a specific run.
    TASK 4 wire format is handled by SSE endpoint (event/data lines),
    while Redis carries a JSON envelope.
    """
    _ensure_publisher()
    if _publisher_loop is None or not _publisher_ready.is_set():
        logger.warning("SSE publisher not ready; dropping event %s for run %s", event_type, run_id)
        return

    envelope = {"type": event_type, "data": payload, "ts": int(time.time())}
    message = json.dumps(envelope, default=str)

    try:
        asyncio.run_coroutine_threadsafe(
            _publish_coro(_channel_events(run_id), message),
            _publisher_loop,
        )
    except Exception as exc:
        logger.warning("SSE publish dispatch failed for run %s: %s", run_id, exc)


# Backwards-compatible function name used across codebase
def emit(run_id: str, event_type: str, data: str | dict) -> None:
    if suppress_terminal_events.get() and event_type in {"complete", "checkpoint", "error"}:
        return  # Durable worker publishes only after its database transaction commits.
    publish(run_id, event_type, data)


async def stream_events(run_id: str, timeout_s: int = 300) -> AsyncIterator[str]:
    """
    Subscribe to TASK 4 Redis channel and yield SSE formatted strings:
      event: {type}
      data: {json}
    Hardened: Redis AuthenticationError/ConnectionError yields ONE
      `event: error` with {"message": "event bus unavailable"} and
      closes cleanly instead of raising (prevents SSE 500).
    Allowed event names unchanged: thinking, tool_call, tool_result,
      checkpoint, complete, error (plus existing ping keepalive).
    """
    r = None
    pubsub = None
    try:
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(_channel_events(run_id))

        start = time.time()
        while True:
            if (time.time() - start) > timeout_s:
                # Let SSE endpoint finalize naturally; also push ping to keep UI alive.
                yield 'event: ping\ndata: {"type":"ping"}\n\n'
                return

            try:
                msg = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0),
                    timeout=5.0,
                )
            except TimeoutError:
                yield 'event: ping\ndata: {"type":"ping"}\n\n'
                continue

            if msg is None:
                yield 'event: ping\ndata: {"type":"ping"}\n\n'
                continue

            raw = msg.get("data", "")
            if not raw:
                continue

            # raw is a JSON envelope
            try:
                envelope = json.loads(raw)
                event_type = envelope.get("type", "message")
                data = envelope.get("data", {})
            except Exception:
                # fallback: treat raw as data string
                event_type = "message"
                data = raw

            yield f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"

            if event_type in ("complete", "error"):
                return
    except RedisError as exc:
        logger.warning("SSE event bus unavailable for run %s: %s", run_id, exc)
        yield f"event: error\ndata: {json.dumps({'message': 'event bus unavailable'})}\n\n"
        return
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(_channel_events(run_id))
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
        if r is not None:
            try:
                await r.aclose()
            except Exception:
                pass
