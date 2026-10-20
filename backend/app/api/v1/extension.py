"""CareerCraft browser extension API.

Two audiences:
* /extension/*        — the signed-in web app (Clerk JWT): pair a browser,
                        list/revoke devices, see and cancel browser tasks.
* /extension/device/* — the extension itself (device token "ccx_…"):
                        claim work, fetch a fill plan, report progress.
                        Exempt from the Clerk middleware in main.py.

Progress reports are relayed to the application's AutoApplyWorkflow as
signals; the workflow alone records the final outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from app.api.v1.deps import get_current_user, get_db
from app.core.event_bus import publish
from app.core.rate_limit import limiter
from app.models.db import AgentRun, ApplicationAttempt, ExtensionDevice, ExtensionTask, User
from app.services import extension_service
from app.services.attention_notices import notify_needs_attention
from app.workflows.starters import WorkflowUnavailable

router = APIRouter(prefix="/extension", tags=["extension"])
logger = logging.getLogger(__name__)

PROGRESS_STAGES = {"claimed", "filling", "needs_input", "review", "login_required"}
TERMINAL_STAGES = {"submitted", "failed", "cancelled"}
STAGE_MESSAGES = {
    "claimed": "Your browser picked up this application",
    "filling": "Filling the application in your browser",
    "needs_input": "A few questions need your answer in the extension panel",
    "review": "Ready for your review — press Submit in the extension panel",
    "login_required": "Sign in to the job site in your browser; the extension will continue",
}


# ── Web app (Clerk) ────────────────────────────────────────────────────


class PairRequest(BaseModel):
    name: str = Field(default="Browser", max_length=100)


@router.post("/pair")
@limiter.limit("10/hour")
async def pair(
    request: Request,
    body: PairRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a device token. It is returned once — paste it into the
    extension (or let the web app hand it over) and it is never shown again."""
    device, token = await extension_service.pair_device(db, current_user.id, body.name)
    return {"device_id": str(device.id), "name": device.name, "token": token}


@router.get("/download")
@limiter.limit("10/minute")
async def download_extension(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """The unpacked extension as a zip, for chrome://extensions → Load unpacked."""
    try:
        version, content = await asyncio.to_thread(extension_service.package_extension)
    except FileNotFoundError as exc:
        logger.error("Extension package unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Extension download is not available") from exc
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="careercraft-extension-v{version}.zip"'
        },
    )


@router.get("/devices")
async def list_devices(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        (
            await db.execute(
                select(ExtensionDevice)
                .where(
                    ExtensionDevice.user_id == current_user.id,
                    ExtensionDevice.revoked_at.is_(None),
                )
                .order_by(ExtensionDevice.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "devices": [
            {
                "id": str(d.id),
                "name": d.name,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
            }
            for d in rows
        ]
    }


@router.delete("/devices/{device_id}")
async def revoke_device(
    device_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    device = (
        await db.execute(
            select(ExtensionDevice).where(
                ExtensionDevice.id == device_id,
                ExtensionDevice.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    await extension_service.revoke_device(db, device)
    return {"status": "revoked"}


@router.get("/tasks")
async def list_tasks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        (
            await db.execute(
                select(ExtensionTask)
                .where(ExtensionTask.user_id == current_user.id)
                .order_by(ExtensionTask.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return {"tasks": [extension_service.task_view(t) for t in rows]}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = (
        await db.execute(
            select(ExtensionTask).where(
                ExtensionTask.id == task_id,
                ExtensionTask.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in extension_service_open_statuses():
        return {"status": task.status}
    await _signal(
        task.workflow_id,
        {"stage": "cancelled", "details": {"message": "Cancelled from the web app"}},
    )
    return {"status": "cancelling"}


def extension_service_open_statuses() -> tuple[str, ...]:
    from app.workflows.extension_activities import OPEN_TASK_STATUSES

    return OPEN_TASK_STATUSES


# ── Extension (device token) ───────────────────────────────────────────


async def get_device(request: Request, db: AsyncSession = Depends(get_db)) -> ExtensionDevice:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    device = await extension_service.authenticate(db, token) if token else None
    if device is None:
        raise HTTPException(status_code=401, detail="Extension is not connected — pair it again")
    return device


async def _device_task(
    db: AsyncSession, device: ExtensionDevice, task_id: uuid.UUID
) -> ExtensionTask:
    task = await extension_service.get_device_task(db, device, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.device_id != device.id:
        raise HTTPException(
            status_code=409, detail="Another browser is working on this application"
        )
    return task


async def _signal(workflow_id: str, update: dict) -> None:
    from app.workflows.starters import signal_extension_update

    try:
        await signal_extension_update(workflow_id, update)
    except WorkflowUnavailable as exc:
        raise HTTPException(status_code=503, detail="Application service unavailable") from exc
    except RPCError as exc:
        # The workflow already finished (e.g. it expired): nothing to update.
        logger.info("Extension update for closed workflow %s: %s", workflow_id, exc)
        raise HTTPException(status_code=409, detail="This application is no longer active") from exc


@router.get("/device/me")
async def device_me(
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, device.user_id)
    return {
        "device_id": str(device.id),
        "device_name": device.name,
        "user": {"email": user.email if user else None, "name": user.full_name if user else None},
    }


@router.delete("/device/me")
async def device_revoke_self(
    device: ExtensionDevice = Depends(get_device), db: AsyncSession = Depends(get_db)
):
    """The extension disconnects or re-pairs: retire its own token."""
    await extension_service.revoke_device(db, device)
    return {"status": "revoked"}


@router.post("/device/tasks/claim")
@limiter.limit("120/minute")
async def claim_task(
    request: Request,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    task = await extension_service.claim_next_task(db, device)
    if task is None:
        return Response(status_code=204)
    await _signal(task.workflow_id, {"stage": "claimed"})
    if not getattr(task, "redelivered", False):
        await _record_progress(db, task, "claimed", STAGE_MESSAGES["claimed"])
    return extension_service.task_view(task)


@router.get("/device/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    return extension_service.task_view(await _device_task(db, device, task_id))


class TaskEvent(BaseModel):
    stage: Literal[
        "filling",
        "needs_input",
        "review",
        "login_required",
        "submitted",
        "failed",
        "cancelled",
    ]
    message: str | None = Field(default=None, max_length=500)
    confirmation_text: str | None = Field(default=None, max_length=2000)
    confirmation_url: str | None = Field(default=None, max_length=2000)
    error: str | None = Field(default=None, max_length=1000)
    submission_token: str | None = Field(default=None, max_length=200)
    # False only when the extension never consumed its submit permit, so no
    # click can have happened. Older extensions omit it: treated as attempted.
    submit_attempted: bool = True


class ReviewField(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=500)
    type: Literal[
        "text",
        "textarea",
        "email",
        "tel",
        "url",
        "number",
        "date",
        "select",
        "checkbox",
        "radio",
        "file",
        "other",
    ]
    value: str | bool | list[str] | None = None
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ReviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    fields: list[ReviewField] = Field(max_length=120)
    submit_control: str = Field(min_length=1, max_length=2000)


class SubmitApproval(BaseModel):
    review_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_confirmed: Literal[True]


def _review_url(url: str) -> None:
    """No fetch occurs here; nevertheless never authorize credentials/local targets."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="Review requires a public HTTPS job URL")
    if parsed.hostname.lower() == "localhost" or "." not in parsed.hostname:
        raise HTTPException(status_code=422, detail="Review requires a public job host")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise HTTPException(status_code=422, detail="Review requires a public job host")


async def _task_attempt(db: AsyncSession, task: ExtensionTask) -> ApplicationAttempt:
    try:
        attempt_id = uuid.UUID((task.payload or {}).get("attempt_id", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=409, detail="Application attempt is unavailable") from exc
    attempt = (
        await db.execute(
            select(ApplicationAttempt)
            .where(
                ApplicationAttempt.id == attempt_id,
                ApplicationAttempt.user_id == task.user_id,
                ApplicationAttempt.job_application_id == task.job_application_id,
                ApplicationAttempt.run_id == task.run_id,
                ApplicationAttempt.workflow_id == task.workflow_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status_code=409, detail="Application attempt is unavailable")
    return attempt


@router.post("/device/tasks/{task_id}/review")
@limiter.limit("30/minute")
async def review_task(
    request: Request,
    task_id: uuid.UUID,
    body: ReviewRequest,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    _review_url(body.url)
    snapshot = body.model_dump()
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > 64 * 1024:
        raise HTTPException(status_code=422, detail="Review is too large")
    task = await _device_task(db, device, task_id)
    if task.status != "review" or task.approved_at:
        raise HTTPException(status_code=409, detail="Request a fresh form review before submitting")
    attempt = await _task_attempt(db, task)
    if attempt.state not in {"preparing", "awaiting_approval"}:
        raise HTTPException(status_code=409, detail="Application cannot be submitted again")
    document_id = (task.payload or {}).get("resume_document_id")
    resume_digest = None
    if document_id:
        from app.applications.submission import load_resume

        try:
            _, resume_digest = await load_resume(task.user_id, document_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Approved resume is unavailable") from exc
    binding = [str(task.id), str(device.id), document_id, resume_digest, snapshot]
    task.review_hash = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    task.review_url = body.url
    task.review_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    attempt.state = "awaiting_approval"
    await db.commit()
    return {"review_hash": task.review_hash, "expires_at": task.review_expires_at.isoformat()}


@router.post("/device/tasks/{task_id}/approve-submit")
@limiter.limit("30/minute")
async def approve_submit(
    request: Request,
    task_id: uuid.UUID,
    body: SubmitApproval,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    task = await _device_task(db, device, task_id)
    now = datetime.now(UTC)
    if (
        task.status != "review"
        or task.approved_at
        or task.review_hash != body.review_hash
        or not task.review_expires_at
        or task.review_expires_at <= now
    ):
        raise HTTPException(status_code=409, detail="Review expired, changed or already approved")
    attempt = await _task_attempt(db, task)
    if attempt.state != "awaiting_approval":
        raise HTTPException(status_code=409, detail="Application cannot be submitted again")
    token = secrets.token_urlsafe(32)
    task.submission_token_hash = hashlib.sha256(token.encode()).hexdigest()
    task.submission_expires_at = now + timedelta(minutes=2)
    task.approved_at = now
    task.status = "submitting"
    attempt.state = "submitting"
    attempt.approved_snapshot_hash = task.review_hash
    await db.commit()
    return {"submission_token": token, "expires_at": task.submission_expires_at.isoformat()}


async def _record_progress(db: AsyncSession, task: ExtensionTask, stage: str, message: str) -> None:
    task.status = stage
    output = {
        "type": "extension_apply",
        "stage": stage,
        "message": message,
        "task_id": str(task.id),
        "job_url": (task.payload or {}).get("job_url"),
        "platform": (task.payload or {}).get("platform"),
    }
    if task.run_id:
        run = await db.get(AgentRun, task.run_id, with_for_update=True)
        if run is not None and run.status in {"queued", "running"}:
            run.status = "running"
            run.output = output
    await db.commit()
    if task.run_id:
        publish(str(task.run_id), "thinking", output)


@router.post("/device/tasks/{task_id}/events")
@limiter.limit("240/minute")
async def task_event(
    request: Request,
    task_id: uuid.UUID,
    event: TaskEvent,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    task = await _device_task(db, device, task_id)
    if task.status not in extension_service_open_statuses():
        return {"status": task.status, "active": False}

    if task.submission_reported_at:
        raise HTTPException(status_code=409, detail="Submission outcome already reported")
    if task.status == "submitting" and event.stage not in TERMINAL_STAGES:
        raise HTTPException(status_code=409, detail="Submission is in progress; do not retry")
    if event.stage == "submitted":
        digest = hashlib.sha256((event.submission_token or "").encode()).hexdigest()
        if (
            task.status != "submitting"
            or not task.approved_at
            or not task.submission_token_hash
            or not secrets.compare_digest(digest, task.submission_token_hash)
            or not task.submission_expires_at
            or task.submission_expires_at <= datetime.now(UTC)
            or not event.confirmation_text
        ):
            raise HTTPException(
                status_code=409, detail="Submission requires an approved review and confirmation"
            )

    if event.stage in TERMINAL_STAGES:
        details = {
            k: v
            for k, v in {
                "message": event.message,
                "confirmation_text": event.confirmation_text,
                "confirmation_url": event.confirmation_url,
                "error": event.error,
            }.items()
            if v
        }
        details["submit_attempted"] = event.submit_attempted
        reporting = task.status == "submitting"
        if reporting:
            # Committed before signalling: finish_extension_task_activity only
            # trusts a "submitted" outcome once this is recorded.
            task.submission_reported_at = datetime.now(UTC)
            await db.commit()
        try:
            await _signal(task.workflow_id, {"stage": event.stage, "details": details})
        except HTTPException:
            if reporting:
                # The workflow never got the outcome; let the extension retry.
                task.submission_reported_at = None
                await db.commit()
            raise
        return {"status": event.stage, "active": False}

    await _signal(task.workflow_id, {"stage": event.stage})
    previous_stage = task.status
    if event.stage in {"needs_input", "login_required"} and not (task.payload or {}).get(
        "needed_you"
    ):
        # Counted by the "no user input" metric.
        task.payload = {**(task.payload or {}), "needed_you": True}
    await _record_progress(db, task, event.stage, event.message or STAGE_MESSAGES[event.stage])
    await notify_needs_attention(
        task.user_id, event.stage, previous_stage, task.payload, task_id=task.id
    )
    return {"status": event.stage, "active": True}


class PlanRequest(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


@router.post("/device/tasks/{task_id}/plan")
@limiter.limit("60/minute")
async def plan(
    request: Request,
    task_id: uuid.UUID,
    body: PlanRequest,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    task = await _device_task(db, device, task_id)
    if task.status not in extension_service_open_statuses():
        raise HTTPException(status_code=409, detail="This application is no longer active")
    result = await extension_service.plan_fields(db, task, body.fields)
    await db.commit()
    return result


@router.get("/device/tasks/{task_id}/resume")
async def task_resume(
    task_id: uuid.UUID,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    from app.models.db import UserDocument
    from app.services.storage_service import download_file

    task = await _device_task(db, device, task_id)
    document_id = (task.payload or {}).get("resume_document_id")
    if not document_id:
        raise HTTPException(status_code=404, detail="No resume attached to this application")
    document = (
        await db.execute(
            select(UserDocument).where(
                UserDocument.id == uuid.UUID(document_id),
                UserDocument.user_id == task.user_id,
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    content = await asyncio.to_thread(download_file, document.storage_path, str(task.user_id))
    filename = (document.filename or "resume.pdf").replace('"', "")
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


class AnswerRequest(BaseModel):
    label: str = Field(min_length=1, max_length=500)
    value: str | bool | list[str]
    question_key: str | None = Field(default=None, max_length=200)


@router.post("/device/answers")
@limiter.limit("120/minute")
async def save_answer(
    request: Request,
    body: AnswerRequest,
    device: ExtensionDevice = Depends(get_device),
    db: AsyncSession = Depends(get_db),
):
    """Remember an answer the user typed in the review panel, so the same
    question is filled automatically next time."""
    from app.applications import profile_service

    key = body.question_key or extension_service.answer_key(body.label)
    await profile_service.save_approved_answer(db, device.user_id, key, body.label, body.value)
    await db.commit()
    return {"question_key": key}


class DecideRequest(BaseModel):
    state: Any
    questions: dict[str, dict[str, Any]]


@router.post("/device/decide")
@limiter.limit("300/minute")
async def decide(
    request: Request,
    body: DecideRequest,
    device: ExtensionDevice = Depends(get_device),
):
    """Typed in-page decisions (which button advances the form, is this a
    confirmation page, …) via the configured System One model."""
    from app.services import decision_engine

    state = body.state
    if len(str(state)) > decision_engine.MAX_STATE_CHARS:
        raise HTTPException(status_code=413, detail="State too large")
    try:
        decision_engine.validate_questions(body.questions)
        return {
            "provider": "heuristic",
            "answers": {
                key: decision_engine._heuristic(state, question)
                for key, question in body.questions.items()
            },
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
