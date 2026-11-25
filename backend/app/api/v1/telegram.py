"""Telegram connection for Nova's one-time code requests."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from slowapi.util import get_remote_address

from app.api.v1.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.db import User
from app.services import telegram_codes

router = APIRouter(prefix="/telegram", tags=["telegram"])
# Public: authenticated by Telegram's secret-token header, not a session.
inbound_router = APIRouter(prefix="/inbound", tags=["inbound"], include_in_schema=False)

_MAX_UPDATE_BYTES = 64_000


@router.get("")
async def status(user: User = Depends(get_current_user)):
    if not telegram_codes.configured():
        return {"configured": False, "connected": False}
    return {"configured": True, "connected": await telegram_codes.chat_for(user.id) is not None}


@router.post("/link")
@limiter.limit("10/minute")
async def link(request: Request, user: User = Depends(get_current_user)):
    if not telegram_codes.configured():
        raise HTTPException(503, "Telegram is not set up on this site")
    return {"url": await telegram_codes.link_url(user.id), "expires_in": telegram_codes.LINK_TTL}


@router.delete("")
async def unlink(user: User = Depends(get_current_user)):
    await telegram_codes.unlink_user(user.id)
    return {"connected": False}


@inbound_router.post("/telegram")
@limiter.limit("600/minute", key_func=get_remote_address)
async def telegram_update(request: Request) -> dict:
    if not telegram_codes.configured():
        raise HTTPException(503, "Telegram is not configured")
    if not telegram_codes.secret_ok(request.headers.get("x-telegram-bot-api-secret-token")):
        raise HTTPException(401, "Invalid webhook secret")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_UPDATE_BYTES:
        raise HTTPException(413, "Update too large")
    body = await request.body()
    if len(body) > _MAX_UPDATE_BYTES:
        raise HTTPException(413, "Update too large")
    try:
        update = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid update") from exc
    if not isinstance(update, dict):
        raise HTTPException(400, "Invalid update")
    # Telegram retries non-200 answers; handled updates always answer 200.
    return await telegram_codes.handle_update(update)
