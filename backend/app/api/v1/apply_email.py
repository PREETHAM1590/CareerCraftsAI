"""The member's application email address and the portal mail it receives."""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.db import ApplyInboxMessage, User
from app.services import apply_inbox

router = APIRouter(prefix="/apply-email", tags=["apply-email"])
# Public: authenticated by the Resend (Svix) webhook signature, not a session.
inbound_router = APIRouter(prefix="/inbound", tags=["inbound"], include_in_schema=False)

_MAX_WEBHOOK_BYTES = 256_000


@router.get("")
async def get_address(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if not apply_inbox.configured():
        return {"configured": False, "address": None}
    return {"configured": True, "address": await apply_inbox.get_or_create_alias(db, user)}


def view(row: ApplyInboxMessage) -> dict:
    return {
        "id": str(row.id),
        "sender": row.sender,
        "subject": row.subject,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "excerpt": row.text_body[:1500],
        "codes": row.codes or [],
        "links": row.links or [],
        "filled_for_nova": row.filled_run_id is not None,
    }


@router.get("/messages")
async def messages(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        (
            await db.execute(
                select(ApplyInboxMessage)
                .where(ApplyInboxMessage.user_id == user.id)
                .order_by(ApplyInboxMessage.received_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [view(row) for row in rows]


@router.delete("/messages/{message_id}")
async def delete_message(
    message_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = (
        await db.execute(
            select(ApplyInboxMessage).where(
                ApplyInboxMessage.id == message_id, ApplyInboxMessage.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Message not found")
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


@inbound_router.post("/resend")
@limiter.limit("300/minute", key_func=get_remote_address)
async def resend_inbound(request: Request) -> dict:
    if not apply_inbox.configured() or not settings.RESEND_WEBHOOK_SECRET:
        raise HTTPException(503, "Application email is not configured")
    # Unauthenticated until the signature check: cap size before buffering.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "Webhook payload too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Webhook payload too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not apply_inbox.verify_signature(body, request.headers, settings.RESEND_WEBHOOK_SECRET):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid webhook payload")
    return await apply_inbox.handle_event(payload)
