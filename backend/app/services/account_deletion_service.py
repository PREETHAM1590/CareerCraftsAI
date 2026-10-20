"""Hard-deletes accounts whose 15-day deletion grace period has elapsed.

Requesting deletion (see app.api.v1.users) never removes anything itself —
it only stamps deletion_scheduled_for. This module is the other half: a
periodic sweep (called from the maintenance activity) that finds accounts
past that date and actually deletes them.

Deleting the users row cascades to every table keyed to it, but some of a
member's data lives elsewhere: running workflows, Nango grants, uploaded
files, RAG collections and agent memory (keyed by a text user_id with no
foreign key), Redis keys, and the Clerk identity. Each of those is erased
first, Clerk last; only then is the row deleted. Every step is idempotent,
so when one fails the row stays and the next sweep finishes the job.
"""

import json
import logging
import shutil
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AgentRun, JobApplication, RecruiterOutreach, User

logger = logging.getLogger(__name__)

_OPEN_RUN_STATUSES = ("queued", "running", "awaiting_approval")
# Agent memory tables (created by MemoryManager, so possibly absent) and the
# statement that clears one member from each.


async def _terminate_workflows(user_id: uuid.UUID) -> int:
    """Stop anything still running on the member's behalf, so no workflow
    sends an email or submits an application after the account is gone."""
    from temporalio.service import RPCError, RPCStatusCode

    from app.core.database import AsyncSessionLocal
    from app.core.temporal_client import get_temporal_client
    from app.workflows.auto_apply import auto_apply_workflow_id
    from app.workflows.followup import followup_workflow_id
    from app.workflows.job_activities import _workflow_id_for

    async with AsyncSessionLocal() as db:
        runs = (
            (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.user_id == user_id,
                        AgentRun.status.in_(_OPEN_RUN_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        application_ids = (
            (
                await db.execute(
                    select(JobApplication.id)
                    .where(JobApplication.user_id == user_id)
                    .execution_options(include_deleted=True)
                )
            )
            .scalars()
            .all()
        )

    workflow_ids = [_workflow_id_for(run) for run in runs]
    for application_id in application_ids:
        workflow_ids.append(auto_apply_workflow_id(str(user_id), str(application_id)))
        workflow_ids.append(followup_workflow_id(str(application_id)))

    client = await get_temporal_client()
    terminated = 0
    for workflow_id in workflow_ids:
        try:
            await client.get_workflow_handle(workflow_id).terminate(reason="Account deleted")
            terminated += 1
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:  # never started or already closed
                raise
    return terminated


async def _revoke_integrations(user_id: uuid.UUID) -> None:
    from app.integrations.exceptions import IntegrationDisabledError
    from app.integrations.factory import build_integration_gateway

    gateway = build_integration_gateway()
    try:
        try:
            connections = await gateway.list_connections(user_id=user_id)
        except IntegrationDisabledError:
            return
        for connection in connections:
            await gateway.revoke_connection(user_id=user_id, provider=connection.provider)
    finally:
        close = getattr(gateway, "aclose", None)
        if close is not None:
            await close()


def _delete_files(user_id: uuid.UUID) -> None:
    from app.services.storage_service import _root

    root = _root()
    folder = (root / str(user_id)).resolve()
    if folder.parent != root:
        raise PermissionError(f"Refusing to delete {folder}")
    if folder.exists():
        shutil.rmtree(folder)


async def _delete_unlinked_rows(user_id: uuid.UUID) -> None:
    """RAG collections are named "{user_id}_..." and agent memory stores the
    id as text; neither has a foreign key, so the users cascade misses both."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        if (
            await db.execute(text("SELECT to_regclass('public.langchain_pg_collection')"))
        ).scalar():
            prefix = f"{user_id}\\_%"
            await db.execute(
                text(
                    "DELETE FROM public.langchain_pg_embedding WHERE collection_id IN "
                    "(SELECT uuid FROM public.langchain_pg_collection WHERE name LIKE :prefix)"
                ),
                {"prefix": prefix},
            )
            await db.execute(
                text("DELETE FROM public.langchain_pg_collection WHERE name LIKE :prefix"),
                {"prefix": prefix},
            )
        # Account erasure already deleted the owner folder and vector collections.
        # This no-FK outbox must no longer retain private filenames afterward.
        exists = await db.execute(text("SELECT to_regclass('public.document_cleanup_queue')"))
        if exists.scalar():
            await db.execute(
                text("DELETE FROM public.document_cleanup_queue WHERE user_id = :uid"),
                {"uid": user_id},
            )
        from app.services.memory_lifecycle import delete_memory_rows

        await delete_memory_rows(db, user_id)
        await db.commit()


async def _delete_redis_keys(user_id: uuid.UUID) -> None:
    from app.core.redis_client import get_redis

    redis = get_redis()
    for pattern in (
        f"token_budget:{user_id}:*",
        f"{user_id}:*",
        f"session:{user_id}:*",
        f"memory-emb:{user_id}:*",
    ):
        keys = [key async for key in redis.scan_iter(match=pattern, count=500)]
        if keys:
            await redis.delete(*keys)
    # Gateway tokens are intentionally opaque keys. Inspect only their bounded
    # owner field rather than guessing a prefix or deleting other members' keys.
    from app.core.llm_gateway import _SESSION_KEY_PREFIX

    async for key in redis.scan_iter(match=f"{_SESSION_KEY_PREFIX}*", count=500):
        raw = await redis.get(key)
        if raw:
            try:
                session = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(session, dict) and session.get("user_id") == str(user_id):
                await redis.delete(key)


async def erase_external_data(user: User) -> None:
    """Remove everything the users cascade cannot reach. Raises on the first
    failure; every step is safe to repeat."""
    from app.core.clerk_auth import delete_clerk_user
    from app.services.computer_service import purge_user

    await _terminate_workflows(user.id)
    await _revoke_integrations(user.id)
    await purge_user(user.id)
    _delete_files(user.id)
    await _delete_unlinked_rows(user.id)
    await _delete_redis_keys(user.id)
    if user.clerk_user_id:
        # Last, so a failure above leaves the member able to sign in and
        # cancel while the sweep retries.
        await delete_clerk_user(str(user.clerk_user_id), strict=True)


async def reap_expired_account_deletions(db: AsyncSession) -> int:
    """Hard-delete every account whose grace period has passed. Returns the
    count removed. Callers are expected to commit the session afterward."""
    now = datetime.now(UTC)
    users = (
        (
            await db.execute(
                select(User).where(
                    User.deletion_scheduled_for.is_not(None),
                    User.deletion_scheduled_for <= now,
                )
            )
        )
        .scalars()
        .all()
    )

    removed = 0
    for user in users:
        # The list above is a moment old. A member who cancelled since then
        # (the grace period's last minute) must not lose anything: re-check
        # under a row lock, which the cancel request takes too.
        fresh = (
            await db.execute(
                select(User)
                .where(
                    User.id == user.id,
                    User.deletion_scheduled_for.is_not(None),
                    User.deletion_scheduled_for <= datetime.now(UTC),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if fresh is None:
            continue
        # These runs execute inside API requests, not Temporal. Their bounded
        # streams/provider threads must finish before external erasure or they
        # could recreate a gateway session after its cleanup. Holding the owner
        # lock prevents new admission while this check and erasure run.
        active_request_work = (
            await db.execute(
                select(AgentRun.id)
                .where(
                    AgentRun.user_id == user.id,
                    AgentRun.status == "running",
                    AgentRun.started_at >= datetime.now(UTC) - timedelta(minutes=10),
                    or_(
                        AgentRun.agent_type == "chat_orchestrator",
                        AgentRun.agent_type == "linkedin_pdf",
                        and_(
                            AgentRun.agent_type == "email",
                            AgentRun.input["source"].astext == "application_outreach",
                        ),
                    ),
                )
                .limit(1)
            )
        ).first()
        active_send = (
            await db.execute(
                select(RecruiterOutreach.id)
                .where(
                    RecruiterOutreach.user_id == user.id,
                    RecruiterOutreach.state == "sending",
                    RecruiterOutreach.sending_at >= datetime.now(UTC) - timedelta(minutes=10),
                )
                .limit(1)
            )
        ).first()
        if active_request_work is not None or active_send is not None:
            logger.info(
                "Deferring account erasure until bounded request work completes: %s", user.id
            )
            continue
        try:
            await erase_external_data(user)
        except Exception:
            logger.exception("Erasing account %s failed; retrying next sweep", user.id)
            continue
        # Foreign-key cascades own child erasure. ORM delete tries to null
        # loaded relationship FKs, including non-null preferences.user_id.
        await db.execute(
            delete(User).where(User.id == user.id).execution_options(synchronize_session=False)
        )
        await db.flush()
        logger.info(
            "Account hard-deleted after grace period: %s (requested %s)",
            user.clerk_user_id or user.id,
            user.deletion_requested_at,
        )
        removed += 1

    return removed
