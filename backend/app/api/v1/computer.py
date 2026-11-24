"""Member computer controls and write-only encrypted portal credentials."""

import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import decrypt_api_key, encrypt_api_key
from app.models.db import AgentRun, PortalCredential, User
from app.services import computer_service as service
from app.services import nova_signin


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
    return await service.dispatch(user.id, "status")


@router.post("/start")
@limiter.limit("10/minute")
async def start(request: Request, user: User = Depends(get_current_user)):
    state = await service.computer_backend(user.id)
    result = await service.dispatch(user.id, "start", state=state)
    await service.audit(user.id, "start", backend=state["backend"])
    return result


@router.post("/stop")
async def stop(user: User = Depends(get_current_user)):
    state = await service.computer_backend(user.id)
    result = await service.dispatch(user.id, "stop", state=state)
    await service.audit(user.id, "stop", backend=state["backend"])
    return result


def mode_view(state):
    return {
        "mode": state["mode"],
        "effective": state["backend"],
        "extension": {"paired": state["paired"], "online": state["device"] is not None},
        "cloud": {"available": service.is_configured()},
    }


class ComputerMode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "browser", "cloud"]


@router.get("/mode")
async def get_mode(user: User = Depends(get_current_user)):
    return mode_view(await service.computer_backend(user.id))


@router.put("/mode")
@limiter.limit("30/minute")
async def put_mode(
    request: Request,
    payload: ComputerMode,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await db.execute(update(User).where(User.id == user.id).values(computer_mode=payload.mode))
    await db.commit()
    return mode_view(await service.computer_backend(user.id))


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
    return {
        "id": str(row.id),
        "origin": row.origin,
        "label": row.label,
        "auto_signin": bool(getattr(row, "auto_signin", False)),
    }


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
    state = await service.computer_backend(user.id)
    audit_id = await service.audit(user.id, "credential_fill", "running", backend=state["backend"])
    try:
        # Browser mode refuses this operation before anything is queued.
        result = await service.dispatch(user.id, "action", body, state)
    except Exception:
        await service.finish_audit(audit_id, "failed", 0)
        raise
    await service.finish_audit(audit_id, "completed", 0)
    return {"filled": bool(result.get("filled")), "submitted": False}


class AutoSignin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.put("/credentials/{credential_id}/auto-signin")
@limiter.limit("30/minute")
async def set_auto_signin(
    request: Request,
    credential_id: uuid.UUID,
    payload: AutoSignin,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Consent for Nova to use this login without asking each time."""
    row = (
        await db.execute(
            select(PortalCredential).where(
                PortalCredential.user_id == user.id, PortalCredential.id == credential_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Saved login not found")
    row.auto_signin = payload.enabled
    row.consent_at = datetime.now(UTC) if payload.enabled else None
    row.consent_version = nova_signin.CONSENT_VERSION if payload.enabled else None
    await db.commit()
    await service.audit(user.id, "auto_signin_on" if payload.enabled else "auto_signin_off")
    return metadata(row)


CODE = re.compile(r"[A-Za-z0-9-]{4,12}")


class OneTimeCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr

    @field_validator("code")
    @classmethod
    def code_shape(cls, value):
        if not CODE.fullmatch(value.get_secret_value().strip()):
            raise ValueError("Enter the 4 to 12 letter or digit code")
        return SecretStr(value.get_secret_value().strip())


@router.post("/runs/{run_id}/code")
@limiter.limit("10/minute")
async def enter_code(
    request: Request,
    run_id: uuid.UUID,
    payload: OneTimeCode,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Hand Nova a one-time code for the step it is waiting on, then approve it.

    The code goes only to Redis (encrypted, short-lived, read once); the
    approval that follows carries no code into the workflow history."""
    run = (
        await db.execute(select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user.id))
    ).scalar_one_or_none()
    pending = (run.output or {}) if run else {}
    step = pending.get("action") or {}
    if (
        run is None
        or run.status != "awaiting_approval"
        or pending.get("type") != "computer_action"
        or step.get("operation") != "enter_code"
    ):
        raise HTTPException(409, "Nova isn't waiting for a code on this task")
    await nova_signin.store_code(run_id, payload.code.get_secret_value())
    from app.api.v1.agents import ApproveRequest, approve_or_cancel

    return await approve_or_cancel(
        run_id=str(run_id),
        payload=ApproveRequest(approved=True, action_type="computer_action"),
        request=request,
        db=db,
        current_user=user,
    )
