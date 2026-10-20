"""Authenticated integration connection APIs and verified Nango webhooks."""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.api.v1.github import router as github_router
from app.core.clerk_auth import ClerkIdentityError, get_verified_primary_email
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import decrypt_api_key, encrypt_api_key
from app.integrations.exceptions import (
    ConnectionNotFoundError,
    IntegrationActionError,
    IntegrationDisabledError,
    ProviderUnavailableError,
)
from app.integrations.factory import get_integration_gateway, nango_provider_config_keys
from app.integrations.gateway import IntegrationGateway
from app.integrations.providers import provider_definition
from app.integrations.repository import (
    apply_auth_webhook,
    get_connection,
    mark_pending,
    mark_revoked,
    reserve_webhook_event,
    sync_connection,
)
from app.integrations.repository import (
    list_connections as list_local_connections,
)
from app.integrations.validation import InvalidReturnPath, validate_return_path
from app.integrations.webhooks import verify_nango_webhook, webhook_event_hash
from app.models.db import IntegrationConnection, User

router = APIRouter(prefix="/integrations", tags=["integrations"])

router.include_router(github_router)


class ConnectSessionRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    return_path: str = Field(default="/settings/integrations", max_length=2048)


class ConnectSessionResponse(BaseModel):
    provider: str
    connect_session_token: str
    connect_link: str | None
    expires_at: datetime


class ConnectionResponse(BaseModel):
    provider: str
    provider_config_key: str
    status: str
    connected_at: datetime | None
    last_synced_at: datetime | None
    account_email: str | None


def _connection_response(connection: IntegrationConnection) -> ConnectionResponse:
    return ConnectionResponse(
        provider=connection.provider,
        provider_config_key=connection.provider_config_key,
        status=connection.status,
        connected_at=connection.connected_at,
        last_synced_at=connection.last_synced_at,
        account_email=_account_email(connection),
    )


def _account_email(connection: IntegrationConnection) -> str | None:
    if not connection.provider_metadata_enc:
        return None
    try:
        metadata = json.loads(
            decrypt_api_key(connection.provider_metadata_enc, settings.APP_SECRET_KEY)
        )
    except (ValueError, json.JSONDecodeError):
        return None
    email = metadata.get("account_email") if isinstance(metadata, dict) else None
    return email if isinstance(email, str) else None


async def _sync_gmail_account_email(
    connection: IntegrationConnection,
    *,
    db: AsyncSession,
    current_user: User,
    gateway: IntegrationGateway,
    login_email: str | None,
) -> None:
    if connection.provider != "gmail" or connection.status != "connected":
        return
    email = _account_email(connection)
    if not email:
        try:
            profile = await gateway.proxy_request(
                user_id=current_user.id,
                provider="gmail",
                method="GET",
                path="gmail/v1/users/me/profile",
            )
        except (ConnectionNotFoundError, IntegrationActionError, ProviderUnavailableError):
            return
        email = profile.data.get("emailAddress") if isinstance(profile.data, dict) else None
        if isinstance(email, str) and email:
            connection.provider_metadata_enc = encrypt_api_key(
                json.dumps({"account_email": email}), settings.APP_SECRET_KEY
            )

    if (
        isinstance(email, str)
        and login_email
        and login_email.casefold().endswith("@gmail.com")
        and email.casefold() != login_email.casefold()
    ):
        try:
            await gateway.revoke_connection(user_id=current_user.id, provider="gmail")
        except ConnectionNotFoundError:
            pass  # Nango has already removed the mismatched connection.
        await mark_revoked(db, user_id=current_user.id, provider="gmail")


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, IntegrationDisabledError):
        return HTTPException(status_code=503, detail="Integrations are currently disabled")
    return HTTPException(status_code=503, detail="Integration provider is unavailable")


@router.post(
    "/connect-session",
    response_model=ConnectSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("5/minute")
async def create_connect_session(
    request: Request,
    payload: ConnectSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    gateway: IntegrationGateway = Depends(get_integration_gateway),
) -> ConnectSessionResponse:
    if payload.provider == "github" and not settings.NANGO_GITHUB_PUBLIC_ONLY:
        raise HTTPException(
            503, "Configure a public-only GitHub OAuth template or use a public profile URL"
        )
    try:
        provider_definition(payload.provider)
        return_path = validate_return_path(payload.return_path)
        await mark_pending(
            db,
            user_id=current_user.id,
            provider=payload.provider,
            provider_config_key=gateway.provider_config_key(payload.provider),
        )
        session = await gateway.create_connect_session(
            user_id=current_user.id, provider=payload.provider, return_path=return_path
        )
        await db.commit()
    except (ValueError, InvalidReturnPath) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (IntegrationDisabledError, ProviderUnavailableError) as exc:
        await db.rollback()
        raise _api_error(exc) from exc
    return ConnectSessionResponse(
        provider=session.provider,
        connect_session_token=session.connect_session_token,
        connect_link=session.connect_link,
        expires_at=session.expires_at,
    )


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    gateway: IntegrationGateway = Depends(get_integration_gateway),
) -> list[ConnectionResponse]:
    try:
        remote_connections = await gateway.list_connections(user_id=current_user.id)
        login_email = None
        if any(result.provider == "gmail" for result in remote_connections):
            login_email = await get_verified_primary_email(current_user.clerk_user_id)
        connections = [
            await sync_connection(db, user_id=current_user.id, result=result)
            for result in remote_connections
        ]
        for connection in connections:
            await _sync_gmail_account_email(
                connection,
                db=db,
                current_user=current_user,
                gateway=gateway,
                login_email=login_email,
            )
        await db.commit()
    except (ClerkIdentityError, IntegrationDisabledError, ProviderUnavailableError) as exc:
        await db.rollback()
        if isinstance(exc, ClerkIdentityError):
            raise HTTPException(
                status_code=503, detail="Could not verify the sign-in email with Clerk"
            ) from exc
        raise _api_error(exc) from exc
    # Nango may not list a newly-created Connect session until its signed
    # webhook arrives. Preserve its local pending state in the interim.
    by_provider = {
        connection.provider: connection
        for connection in await list_local_connections(db, user_id=current_user.id)
    }
    by_provider.update({connection.provider: connection for connection in connections})
    return [_connection_response(by_provider[key]) for key in sorted(by_provider)]


@router.get("/{provider}", response_model=ConnectionResponse)
async def read_connection(
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectionResponse:
    try:
        provider_definition(provider)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Integration provider was not found") from exc
    connection = await get_connection(db, user_id=current_user.id, provider=provider)
    if connection is None:
        raise HTTPException(status_code=404, detail="Integration connection was not found")
    return _connection_response(connection)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def revoke_connection(
    request: Request,
    provider: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    gateway: IntegrationGateway = Depends(get_integration_gateway),
) -> Response:
    if provider == "github":
        from app.services.github_profile import delete_profile

        await delete_profile(current_user.id)
    try:
        provider_definition(provider)
        await gateway.revoke_connection(user_id=current_user.id, provider=provider)
        await mark_revoked(db, user_id=current_user.id, provider=provider)
        await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Integration provider was not found") from exc
    except (IntegrationDisabledError, ProviderUnavailableError) as exc:
        await db.rollback()
        raise _api_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/webhooks/nango", include_in_schema=False)
@limiter.exempt
async def nango_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    if not settings.NANGO_ENABLED:
        raise HTTPException(status_code=503, detail="Integrations are currently disabled")
    body = await request.body()
    if not verify_nango_webhook(
        body=body,
        signature=request.headers.get("X-Nango-Hmac-Sha256"),
        signing_key=settings.NANGO_WEBHOOK_SECRET,
    ):
        raise HTTPException(status_code=401, detail="Invalid integration webhook")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid integration webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid integration webhook payload")
    event = await reserve_webhook_event(
        db,
        event_hash=webhook_event_hash(body),
        event_type=(payload.get("type") if isinstance(payload.get("type"), str) else None),
        connection_id=(
            payload.get("connectionId") if isinstance(payload.get("connectionId"), str) else None
        ),
    )
    if event is None:
        return {"status": "duplicate"}
    try:
        provider_config_keys = nango_provider_config_keys()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Integration configuration is invalid") from exc
    event.status = await apply_auth_webhook(db, payload, provider_config_keys=provider_config_keys)
    await db.commit()
    return {"status": event.status}
