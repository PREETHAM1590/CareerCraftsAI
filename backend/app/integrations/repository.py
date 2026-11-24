"""Persistence operations for provider-neutral connection state."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.providers import provider_for_config_key
from app.integrations.schemas import IntegrationConnectionResult
from app.models.db import IntegrationConnection, IntegrationWebhookEvent


async def get_connection(
    db: AsyncSession, *, user_id: UUID, provider: str
) -> IntegrationConnection | None:
    result = await db.execute(
        select(IntegrationConnection).where(
            IntegrationConnection.user_id == user_id,
            IntegrationConnection.provider == provider,
        )
    )
    return result.scalar_one_or_none()


async def list_connections(db: AsyncSession, *, user_id: UUID) -> list[IntegrationConnection]:
    """Return only local records belonging to the authenticated user."""
    result = await db.execute(
        select(IntegrationConnection)
        .where(IntegrationConnection.user_id == user_id)
        .order_by(IntegrationConnection.provider)
    )
    return list(result.scalars())


async def mark_pending(
    db: AsyncSession, *, user_id: UUID, provider: str, provider_config_key: str
) -> IntegrationConnection:
    connection = await get_connection(db, user_id=user_id, provider=provider)
    if connection is None:
        connection = IntegrationConnection(
            user_id=user_id,
            provider=provider,
            provider_config_key=provider_config_key,
            status="pending",
        )
        db.add(connection)
    elif connection.status != "connected":
        connection.status = "pending"
        connection.disconnected_at = None
    connection.provider_metadata_enc = None
    await db.flush()
    return connection


async def sync_connection(
    db: AsyncSession, *, user_id: UUID, result: IntegrationConnectionResult
) -> IntegrationConnection:
    connection = await get_connection(db, user_id=user_id, provider=result.provider)
    if connection is None:
        connection = IntegrationConnection(
            user_id=user_id,
            provider=result.provider,
            provider_config_key=result.provider_config_key,
        )
        db.add(connection)
    elif (
        connection.status == "revoked"
        and connection.external_connection_id == result.external_connection_id
    ):
        # Nango's list can briefly return a connection after its delete succeeds.
        # Keep the local tombstone so a refresh cannot show it as connected again.
        return connection
    connection.provider_config_key = result.provider_config_key
    if connection.external_connection_id != result.external_connection_id:
        # A different external account: its address must be read and checked again.
        connection.provider_metadata_enc = None
    connection.external_connection_id = result.external_connection_id
    connection.status = result.status
    connection.connected_at = result.connected_at or connection.connected_at or datetime.now(UTC)
    connection.last_synced_at = datetime.now(UTC)
    if result.status == "connected":
        connection.disconnected_at = None
    await db.flush()
    return connection


async def mark_revoked(db: AsyncSession, *, user_id: UUID, provider: str) -> None:
    connection = await get_connection(db, user_id=user_id, provider=provider)
    if connection is None:
        return
    connection.status = "revoked"
    connection.disconnected_at = datetime.now(UTC)
    await db.flush()


async def reserve_webhook_event(
    db: AsyncSession, *, event_hash: str, event_type: str | None, connection_id: str | None
) -> IntegrationWebhookEvent | None:
    """Insert replay key atomically; duplicate Nango retries become no-ops."""
    event = IntegrationWebhookEvent(
        event_hash=event_hash,
        event_type=event_type,
        external_connection_id=connection_id,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        return None
    return event


async def apply_auth_webhook(
    db: AsyncSession, payload: dict, *, provider_config_keys: dict[str, str]
) -> str:
    """Reconcile only an already-reserved local pending/owned connection."""
    if payload.get("type") != "auth":
        return "ignored"
    provider = provider_for_config_key(
        str(payload.get("providerConfigKey", "")), provider_config_keys
    )
    tags = payload.get("tags")
    if provider is None or not isinstance(tags, dict):
        return "ignored"
    try:
        user_id = UUID(str(tags.get("end_user_id", "")))
    except ValueError:
        return "ignored"
    connection = await get_connection(db, user_id=user_id, provider=provider)
    if connection is None:
        # A tag alone is insufficient authority: Connect must have been started
        # by this user and reserved a local pending connection first.
        return "ignored"
    external_connection_id = payload.get("connectionId")
    if not isinstance(external_connection_id, str) or not external_connection_id:
        return "ignored"
    operation = payload.get("operation")
    success = payload.get("success")
    connection.external_connection_id = external_connection_id
    connection.last_synced_at = datetime.now(UTC)
    if operation == "deletion":
        connection.status = "disconnected"
        connection.disconnected_at = datetime.now(UTC)
    elif operation == "refresh" and success is False:
        connection.status = "error"
    elif operation in {"creation", "override", "refresh"} and success is True:
        connection.status = "connected"
        connection.connected_at = connection.connected_at or datetime.now(UTC)
        connection.disconnected_at = None
    else:
        return "ignored"
    await db.flush()
    return "processed"
