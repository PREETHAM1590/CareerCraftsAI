import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.api.v1.run_utils import queue_agent_run
from app.core.rate_limit import limiter
from app.models.db import AgentRun, LinkedInOutreachQueue, User
from app.services import linkedin_sender

router = APIRouter(prefix="/linkedin", tags=["linkedin"])
# Public: Unipile's hosted-login callback, matched by a single-use state token.
inbound_router = APIRouter(prefix="/inbound", tags=["inbound"], include_in_schema=False)
logger = logging.getLogger(__name__)


class ProfileOptimizeResponse(BaseModel):
    run_id: str
    status: str
    sections: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@router.post("/profile/optimize", response_model=ProfileOptimizeResponse)
async def optimize_uploaded_profile(
    file: UploadFile = File(...),
    target_role: str = Form(min_length=1, max_length=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.core.llm_gateway import get_gateway_llm
    from app.services.linkedin_profile import (
        MAX_PROFILE_BYTES,
        analyze_profile,
        parse_profile_pdf,
    )

    if not target_role.strip():
        raise HTTPException(status_code=422, detail="Enter a target role.")
    try:
        if not (file.filename or "").lower().endswith(".pdf") or file.content_type not in (
            "application/pdf",
            "application/octet-stream",
        ):
            raise HTTPException(status_code=415, detail="Upload a LinkedIn profile PDF.")
        content = await file.read(MAX_PROFILE_BYTES + 1)
    finally:
        await file.close()
    if len(content) > MAX_PROFILE_BYTES:
        raise HTTPException(status_code=413, detail="Profile PDF must be at most 5 MB.")
    try:
        profile, pages, warnings = await asyncio.to_thread(parse_profile_pdf, content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    from app.api.v1.run_utils import check_run_admission

    await check_run_admission(db, current_user)
    run = AgentRun(
        id=uuid.uuid4(),
        user_id=current_user.id,
        agent_type="linkedin_pdf",
        status="running",
        tokens_used=0,
        input={
            "bytes": len(content),
            "pages": pages,
            "target_role_length": len(target_role),
        },
    )
    db.add(run)
    await db.commit()
    start = time.monotonic()
    from app.core.model_router import (
        TokenTrackingCallback,
        begin_token_tracking,
        get_and_reset_tokens,
    )
    from app.services.llm_proxy_service import get_redaction_callback

    begin_token_tracking()
    try:
        llm = await get_gateway_llm(str(current_user.id), db)
        llm.callbacks = [
            get_redaction_callback(),
            TokenTrackingCallback(str(current_user.id)),
        ]
        sections, tokens = await asyncio.wait_for(
            analyze_profile(llm, profile, target_role.strip()), timeout=120
        )
        run.status = "completed"
        run.output = {"sections": sections, "warnings": warnings}
        run.tokens_used = tokens
    except Exception as exc:
        run.status = "failed"
        run.output = {"error": "profile_analysis_failed"}
        logger.warning("linkedin_pdf_failed run_id=%s error_type=%s", run.id, type(exc).__name__)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, asyncio.TimeoutError):
            raise HTTPException(
                status_code=504, detail="Profile analysis timed out. Try again."
            ) from None
        raise HTTPException(
            status_code=422,
            detail="Could not produce grounded profile edits. "
            "Check your model settings and try again.",
        ) from None
    finally:
        run.tokens_used = get_and_reset_tokens() or run.tokens_used or 0
        run.duration_ms = int((time.monotonic() - start) * 1000)
        run.completed_at = datetime.now(UTC)
        await db.commit()
    return ProfileOptimizeResponse(
        run_id=str(run.id), status=run.status, sections=sections, warnings=warnings
    )


class OutreachIdentifyRequest(BaseModel):
    company_name: str
    role_context: str | None = None


class OutreachApproveRequest(BaseModel):
    approved: bool
    edited_message: str | None = None


@router.post("/outreach/identify")
async def identify_contacts(
    body: OutreachIdentifyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Find contacts at a company via Proxycurl, filter, and draft messages.

    Runs as a durable agent run; drafts appear in GET /outreach/queue.
    """
    run_id = await queue_agent_run(
        db,
        current_user,
        "linkedin_outreach",
        {"company_name": body.company_name, "role_context": body.role_context},
    )
    return {"run_id": run_id, "status": "queued"}


@router.get("/outreach/queue")
async def get_outreach_queue(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """View pending outreach messages."""
    result = await db.execute(
        select(AgentRun)
        .where(
            AgentRun.user_id == current_user.id,
            AgentRun.agent_type == "linkedin_outreach",
        )
        .order_by(AgentRun.started_at.desc().nulls_last())
        .limit(20)
    )
    return result.scalars().all()


@router.post("/outreach/{run_id}/approve")
async def approve_outreach(
    run_id: uuid.UUID,
    body: OutreachApproveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve or reject a pending outreach message. HITL gate enforced."""
    # Locked, so a double click cannot send every connection request twice.
    result = await db.execute(
        select(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.user_id == current_user.id,
            AgentRun.agent_type == "linkedin_outreach",
        )
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Outreach run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=400, detail="Run is not awaiting approval")

    if body.approved:
        if not linkedin_sender.enabled() or not await linkedin_sender.account_for(current_user.id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Copy the draft and send it on LinkedIn yourself, or connect LinkedIn "
                    "(beta) to send approved drafts from here."
                ),
            )
        # The member approved these drafts: send each at most once, within
        # today's caps. The run row stays locked, so a double click waits.
        output = run.output or {}
        deliveries = {}
        for queue_id in output.get("queue_ids") or []:
            deliveries[queue_id] = await linkedin_sender.send_item(
                current_user.id, uuid.UUID(queue_id)
            )
        run.status = "completed"
        run.output = {**output, "deliveries": deliveries}
    else:
        output = run.output or {}
        for queue_id in output.get("queue_ids") or []:
            qres = await db.execute(
                select(LinkedInOutreachQueue).where(
                    LinkedInOutreachQueue.id == uuid.UUID(queue_id),
                    LinkedInOutreachQueue.user_id == current_user.id,
                )
            )
            queue_item = qres.scalar_one_or_none()
            if queue_item:
                queue_item.status = "rejected"
        run.status = "failed"
        run.output = {**output, "error": "Outreach rejected by user"}

    run.completed_at = datetime.now(UTC)
    await db.flush()
    deliveries = (run.output or {}).get("deliveries") if body.approved else None
    return {"status": run.status, "run_id": str(run_id), "deliveries": deliveries}


# ── LinkedIn sending (beta) ─────────────────────────────────────


@router.get("/account")
async def linkedin_account(current_user: User = Depends(get_current_user)):
    if not linkedin_sender.enabled():
        return {"enabled": False, "connected": False}
    return {
        "enabled": True,
        "connected": await linkedin_sender.account_for(current_user.id) is not None,
        "invites_per_day": linkedin_sender.settings.LINKEDIN_INVITES_PER_DAY,
        "messages_per_day": linkedin_sender.settings.LINKEDIN_MESSAGES_PER_DAY,
    }


@router.post("/account/link")
@limiter.limit("10/minute")
async def linkedin_account_link(request: Request, current_user: User = Depends(get_current_user)):
    try:
        return {"url": await linkedin_sender.link_url(current_user.id)}
    except linkedin_sender.LinkedInUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except Exception as exc:
        logger.warning("unipile_link_failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(
            status_code=502, detail="Could not start the LinkedIn connection"
        ) from None


@router.delete("/account")
async def linkedin_account_disconnect(current_user: User = Depends(get_current_user)):
    await linkedin_sender.disconnect(current_user.id)
    return {"connected": False}


@inbound_router.post("/unipile")
@limiter.limit("60/minute")
async def unipile_notify(request: Request, state: str = "") -> dict:
    if not linkedin_sender.enabled():
        raise HTTPException(status_code=503, detail="LinkedIn sending is not enabled")
    body = await request.body()
    if len(body) > 16_000:
        raise HTTPException(status_code=413, detail="Callback too large")
    try:
        import json

        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid callback") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid callback")
    return await linkedin_sender.handle_notify(state, payload)
