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
        device = (
            await linkedin_sender.device_for(current_user.id)
            if linkedin_sender.enabled()
            else None
        )
        if device is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Copy the draft and send it on LinkedIn yourself, or open Chrome with the "
                    "CareerCraft extension to send approved drafts from your LinkedIn."
                ),
            )
        # The member approved these drafts: the extension sends each at most
        # once, in the background, within today's caps.
        # agent_runs.status has a fixed set of values: the run is approved
        # (completed) and output.sending tracks the background send.
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        run.output = {
            **(run.output or {}),
            "deliveries": {},
            "sending": True,
            "sending_started_at": datetime.now(UTC).isoformat(),
        }
        await db.flush()
        await db.commit()
        linkedin_sender.start_run(current_user.id, run.id)
        return {"status": "completed", "sending": True, "run_id": str(run_id)}
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
    return {"status": run.status, "run_id": str(run_id)}


# ── LinkedIn sending (beta) ─────────────────────────────────────


@router.get("/account")
async def linkedin_sending_status(current_user: User = Depends(get_current_user)):
    """Whether approved drafts can be sent from the member's own LinkedIn."""
    if not linkedin_sender.enabled():
        return {"enabled": False, "ready": False}
    return {
        "enabled": True,
        # Sent by the paired extension in the member's Chrome, so it must be online.
        "ready": await linkedin_sender.device_for(current_user.id) is not None,
        "invites_per_day": linkedin_sender.settings.LINKEDIN_INVITES_PER_DAY,
        "messages_per_day": linkedin_sender.settings.LINKEDIN_MESSAGES_PER_DAY,
    }


@router.post("/outreach/{run_id}/resend")
@limiter.limit("10/minute")
async def resend_outreach(
    request: Request,
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send the drafts of an approved run that failed or hit a limit."""
    run = (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.user_id == current_user.id,
                AgentRun.agent_type == "linkedin_outreach",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Outreach run not found")
    if run.status != "completed" or not (run.output or {}).get("queue_ids"):
        raise HTTPException(status_code=409, detail="Only approved outreach can be resent")
    if not linkedin_sender.enabled() or await linkedin_sender.device_for(current_user.id) is None:
        raise HTTPException(
            status_code=409, detail="Open Chrome with the CareerCraft extension, then resend."
        )
    # A batch interrupted by a restart never clears its flag: past the lock
    # TTL it is stale and the drafts can be resent.
    if (run.output or {}).get("sending") and not linkedin_sender.sending_stale(run.output):
        raise HTTPException(status_code=409, detail="These drafts are being sent right now")
    run.output = {
        **(run.output or {}),
        "sending": True,
        "sending_started_at": datetime.now(UTC).isoformat(),
    }
    await db.commit()
    # Items already sent, or maybe sent, are skipped by send_item.
    linkedin_sender.start_run(current_user.id, run.id)
    return {"status": "completed", "sending": True, "run_id": str(run_id)}
