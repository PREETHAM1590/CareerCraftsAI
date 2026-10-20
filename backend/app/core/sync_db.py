"""
sync_db.py — Thread-safe synchronous DB helpers for agent node functions.

Agent nodes run inside a thread-pool executor (harness → orchestrator.invoke).
The shared AsyncSessionLocal engine is bound to the main event loop and cannot
be reused from a worker thread.  A dedicated *synchronous* SQLAlchemy engine is
created once (protected by a threading.Lock) and reused across all calls,
avoiding both connection pool exhaustion and the asyncio.run() RuntimeError.
"""

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

_lock = threading.Lock()
_sync_engine = None
_sync_factory = None


def _run_fresh(coro):
    """Run a coroutine on a fresh event loop.

    On Windows, use a ProactorEventLoop so Playwright (and any other code that
    spawns a subprocess) works — the default Selector loop that uvicorn installs
    raises NotImplementedError on subprocess creation.
    """

    async def run_and_close():
        from app.core.redis_client import close_redis

        try:
            return await coro
        finally:
            await close_redis()

    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(run_and_close())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
    return asyncio.run(run_and_close())


def run_coro_sync(coro):
    """Run an async coroutine to completion from a synchronous context.

    Agent nodes execute inside a thread-pool worker (harness runs
    orchestrator.invoke via run_in_executor), so there is normally no event
    loop bound to the current thread and asyncio.run() is correct. If a loop
    happens to be running in this thread, fall back to executing the coroutine
    on a dedicated worker thread so we never call run_until_complete on a loop
    owned by another thread (the source of "attached to a different loop"
    errors). Always use this instead of asyncio.get_event_loop()/new_event_loop.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is None:
        # No loop in this thread — safe to spin up a throwaway loop.
        return _run_fresh(coro)

    # A loop is already running here; run the coroutine on a separate thread
    # with its own fresh loop to avoid cross-loop reuse.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run_fresh, coro).result()


# Agent nodes write through this engine while API requests may hold row locks
# on the async engine; never wait on a lock forever.
def _set_lock_timeout(dbapi_connection, _record) -> None:
    with dbapi_connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '30s'")
    dbapi_connection.commit()


def _get_sync_factory():
    global _sync_engine, _sync_factory
    if _sync_engine is None:
        with _lock:
            if _sync_engine is None:
                # Convert async URL to sync: postgresql+asyncpg → postgresql+psycopg
                sync_url = settings.DATABASE_URL.replace("+asyncpg", "+psycopg")
                _sync_engine = create_engine(
                    sync_url,
                    pool_size=settings.SYNC_DB_POOL_SIZE,
                    max_overflow=settings.SYNC_DB_MAX_OVERFLOW,
                    pool_pre_ping=True,
                )
                event.listen(_sync_engine, "connect", _set_lock_timeout)
                _sync_factory = sessionmaker(_sync_engine, class_=Session, expire_on_commit=False)
    return _sync_factory


import uuid as _uuid


def _to_uuid(user_id: str):
    try:
        return _uuid.UUID(user_id)
    except (ValueError, AttributeError):
        return user_id


# Per-run choices the member made on the agents page. Set by the activity that
# executes the run (contextvars propagate through asyncio.to_thread), so every
# fetch_model_settings / resume lookup below honors them without per-agent code.
# Both ids are re-checked against user_id on every lookup: never trust the client.
_run_choice: ContextVar[dict | None] = ContextVar("agent_run_choice", default=None)


@contextmanager
def run_choice(context: dict | None):
    """Apply context["model_setting_id"] / ["resume_document_id"] for this run."""
    ctx = context or {}
    token = _run_choice.set(
        {
            "model_setting_id": ctx.get("model_setting_id"),
            "resume_document_id": ctx.get("resume_document_id"),
        }
    )
    try:
        yield
    finally:
        _run_choice.reset(token)


def _chosen(key: str):
    value = (_run_choice.get() or {}).get(key)
    if not value:
        return None
    try:
        return _uuid.UUID(str(value))
    except ValueError:
        return None


def fetch_chosen_resume(user_id: str):
    """Return the resume UserDocument picked for this run (owned by user_id), or None."""
    from app.models.db import UserDocument

    doc_id = _chosen("resume_document_id")
    if doc_id is None:
        return None
    with _get_sync_factory()() as db:
        return (
            db.execute(
                select(UserDocument).where(
                    UserDocument.id == doc_id,
                    UserDocument.user_id == _to_uuid(user_id),
                    UserDocument.doc_type == "resume",
                )
            )
            .scalars()
            .first()
        )


def fetch_model_settings(user_id: str):
    """Return the model row picked for this run if it is user_id's, else the active row, or None."""
    from app.models.db import UserModelSettings

    factory = _get_sync_factory()
    with factory() as db:
        chosen_id = _chosen("model_setting_id")
        if chosen_id is not None:
            chosen = (
                db.execute(
                    select(UserModelSettings).where(
                        UserModelSettings.id == chosen_id,
                        UserModelSettings.user_id == _to_uuid(user_id),
                    )
                )
                .scalars()
                .first()
            )
            if chosen is not None:
                return chosen
        result = db.execute(
            select(UserModelSettings).where(
                UserModelSettings.user_id == _to_uuid(user_id),
                UserModelSettings.is_active == True,  # noqa: E712
            )
        )
        return result.scalars().first()


def fetch_user_full_name(user_id: str) -> str:
    """Return user full_name string, or empty string."""
    from app.models.db import User

    factory = _get_sync_factory()
    with factory() as db:
        result = db.execute(select(User.full_name).where(User.id == _to_uuid(user_id)))
        return result.scalars().first() or ""


def fetch_user_profile_text(user_id: str) -> str:
    """Return primary resume raw_text (first 2000 chars) for user, or empty string."""
    from app.models.db import UserDocument

    factory = _get_sync_factory()
    with factory() as db:
        doc = fetch_chosen_resume(user_id)
        if doc is None:
            result = db.execute(
                select(UserDocument).where(
                    UserDocument.user_id == _to_uuid(user_id),
                    UserDocument.doc_type == "resume",
                    UserDocument.is_primary == True,  # noqa: E712
                )
            )
            doc = result.scalars().first()
        return doc.raw_text[:2000] if doc and doc.raw_text else ""
