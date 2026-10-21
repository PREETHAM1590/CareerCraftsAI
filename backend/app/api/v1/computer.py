"""Member computer controls and write-only encrypted portal credentials."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import decrypt_api_key, encrypt_api_key
from app.models.db import PortalCredential, User
from app.services import computer_service as service


class PrivateInputRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default errors echo input, including invalid secrets.
                raise HTTPException(
                    422, "Invalid computer request; check the required fields"
                ) from None

        return safe


router = APIRouter(prefix="/computer", tags=["computer"], route_class=PrivateInputRoute)


@router.get("")
async def status(user: User = Depends(get_current_user)):
    if not service.is_configured():
        # Not deployed on this site yet: a state the page shows, not a server
        # error. Start, stop and actions still answer 503.
        return {"available": False, "running": False}
    return await service.relay(user.id)


@router.post("/start")
@limiter.limit("10/minute")
async def start(request: Request, user: User = Depends(get_current_user)):
    result = await service.relay(user.id, "/start", "POST")
    await service.audit(user.id, "start")
    return result


@router.post("/stop")
async def stop(user: User = Depends(get_current_user)):
    result = await service.relay(user.id, "/stop", "POST")
    await service.audit(user.id, "stop")
    return result


@router.post("/action")
@limiter.limit("600/minute")
async def action(
    request: Request, payload: service.ComputerAction, user: User = Depends(get_current_user)
):
    try:
        return await service.act(user.id, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


class SaveCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str
    label: str = Field(min_length=1, max_length=100)
    username: SecretStr = Field(min_length=1, max_length=1000)
    password: SecretStr = Field(min_length=1, max_length=1000)

    @field_validator("origin")
    @classmethod
    def origin_only(cls, value):
        return service.credential_origin(value)


def metadata(row):
    return {"id": str(row.id), "origin": row.origin, "label": row.label}


@router.get("/credentials")
async def credentials(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        (await db.execute(select(PortalCredential).where(PortalCredential.user_id == user.id)))
        .scalars()
        .all()
    )
    return [metadata(row) for row in rows]


@router.post("/credentials")
@limiter.limit("10/minute")
async def save(
    request: Request,
    payload: SaveCredential,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Serialize updates of the member's vault, including concurrent first saves.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    row = (
        await db.execute(
            select(PortalCredential).where(
                PortalCredential.user_id == user.id, PortalCredential.origin == payload.origin
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PortalCredential(user_id=user.id, origin=payload.origin)
        db.add(row)
    row.label = payload.label
    row.username_enc = encrypt_api_key(payload.username.get_secret_value(), settings.APP_SECRET_KEY)
    row.password_enc = encrypt_api_key(payload.password.get_secret_value(), settings.APP_SECRET_KEY)
    await db.commit()
    await db.refresh(row)
    await service.audit(user.id, "credential_saved")
    return metadata(row)


@router.delete("/credentials/{credential_id}")
async def remove(
    credential_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(PortalCredential).where(
                PortalCredential.user_id == user.id, PortalCredential.id == credential_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Saved login not found")
    await db.delete(row)
    await db.commit()
    await service.audit(user.id, "credential_deleted")
    return {
        "deleted": True,
        "note": (
            "Deleting a saved password does not revoke an existing portal session; "
            "sign out on the portal to revoke it."
        ),
    }


class FillCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_id: uuid.UUID
    username_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,30}$")
    password_ref: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,30}$")
    snapshot_id: int = Field(gt=0)
    computer_run: str


@router.post("/credentials/fill")
@limiter.limit("10/minute")
async def fill(
    request: Request,
    payload: FillCredential,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(PortalCredential).where(
                PortalCredential.user_id == user.id, PortalCredential.id == payload.credential_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Saved login not found")
    # Only the member route can use this operation. The model has no credential
    # resolver, cookie accessor, screenshot, script or shell tool.
    body = {
        "operation": "credentials/fill",
        "computer_run": payload.computer_run,
        "actor": "human",
        "parameters": {
            "origin": row.origin,
            "usernameRef": payload.username_ref,
            "passwordRef": payload.password_ref,
            "snapshotId": payload.snapshot_id,
            "username": decrypt_api_key(row.username_enc, settings.APP_SECRET_KEY),
            "password": decrypt_api_key(row.password_enc, settings.APP_SECRET_KEY),
        },
    }
    audit_id = await service.audit(user.id, "credential_fill", "running")
    try:
        result = await service.relay(user.id, "/action", "POST", body)
    except Exception:
        await service.finish_audit(audit_id, "failed", 0)
        raise
    await service.finish_audit(audit_id, "completed", 0)
    return {"filled": bool(result.get("filled")), "submitted": False}
