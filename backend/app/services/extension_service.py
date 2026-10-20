"""Browser-extension pairing, task claiming, and form-fill planning.

The extension authenticates with a device token (never the user's Clerk
session): one per paired browser, revocable, and scoped to the
/api/v1/extension/device/* routes. Only the token's SHA-256 is stored.

Filling follows answer_resolver's strict order — saved answer → structured
profile → (non-sensitive only) generated draft → ask the user. Sponsorship,
work authorization, salary and notice period are never inferred; consent
checkboxes are never ticked on the user's behalf.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import secrets
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.answer_resolver import resolve_fields
from app.applications.models import ApplicationField
from app.applications.question_normalizer import normalize_question
from app.applications.schema_extractor import extract_fields
from app.models.db import (
    AgentRun,
    CandidateAnswer,
    ExtensionDevice,
    ExtensionTask,
    User,
    UserDocument,
)
from app.services.document_lifecycle import RESUME_DOCUMENT_TYPES

logger = logging.getLogger(__name__)

# A token prefix and a placeholder value, not credentials.
TOKEN_PREFIX = "ccx_"  # noqa: S105  # nosec B105
RESUME_TOKEN = "__resume__"  # noqa: S105  # nosec B105
MAX_FIELDS = 120
MAX_GENERATED_ANSWERS = 3

# Personal details the User row already knows, for users who never filled
# the structured candidate profile.
_USER_FALLBACK = {
    "personal.email": lambda u: u.email,
    "personal.phone": lambda u: u.phone,
    "personal.full_name": lambda u: u.full_name,
    "personal.first_name": lambda u: (u.full_name or "").split(" ")[0] or None,
    "personal.last_name": lambda u: " ".join((u.full_name or "").split(" ")[1:]) or None,
    "links.linkedin_url": lambda u: u.linkedin_url,
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def pair_device(
    db: AsyncSession, user_id: uuid.UUID, name: str
) -> tuple[ExtensionDevice, str]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = ExtensionDevice(
        id=uuid.uuid4(),
        user_id=user_id,
        name=(name or "Browser").strip()[:100] or "Browser",
        token_hash=hash_token(token),
    )
    db.add(device)
    await db.commit()
    return device, token


async def authenticate(db: AsyncSession, token: str) -> ExtensionDevice | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    device = (
        await db.execute(
            select(ExtensionDevice).where(
                ExtensionDevice.token_hash == hash_token(token),
                ExtensionDevice.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if device is not None:
        from app.api.v1.deps import require_eligible_owner

        # Persist activity first: read-only device routes never commit.
        device.last_seen_at = datetime.now(UTC)
        await db.commit()
        # Fence erasure for the rest of the request. FOR KEY SHARE makes erasure's
        # FOR UPDATE ... SKIP LOCKED pass this owner by, without blocking child FK
        # checks, ordinary users-row updates or memory_access's NO KEY UPDATE.
        owner = (
            await db.execute(
                select(User)
                .where(User.id == device.user_id)
                .with_for_update(read=True, key_share=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if owner is None:
            return None
        require_eligible_owner(owner)
    return device


async def revoke_device(db: AsyncSession, device: ExtensionDevice) -> None:
    """Revoke and return the device's unacknowledged claims to the pending pool."""
    device.revoked_at = datetime.now(UTC)
    await db.execute(
        update(ExtensionTask)
        .where(ExtensionTask.device_id == device.id, ExtensionTask.status == "claimed")
        .values(status="pending", device_id=None, claimed_at=None)
    )
    await db.commit()


async def has_active_device(db: AsyncSession, user_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(ExtensionDevice.id)
            .where(
                ExtensionDevice.user_id == user_id,
                ExtensionDevice.revoked_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None


def task_view(task: ExtensionTask) -> dict:
    payload = task.payload or {}
    return {
        "id": str(task.id),
        "kind": task.kind,
        "status": task.status,
        "job_url": payload.get("job_url"),
        "company": payload.get("company"),
        "role": payload.get("role"),
        "platform": payload.get("platform"),
        "has_resume": bool(payload.get("resume_document_id")),
        "application_id": str(task.job_application_id) if task.job_application_id else None,
        "run_id": str(task.run_id) if task.run_id else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


async def claim_next_task(db: AsyncSession, device: ExtensionDevice) -> ExtensionTask | None:
    """Recover this device's unacknowledged claim before taking pending work.

    The persisted claim is the retry record: every retry re-signals the workflow.
    A lost signal or HTTP response therefore cannot strand the task.
    """
    # Serialize this device's claims so a locked claimed row can't be skipped
    # in favour of a second pending task.
    await db.execute(
        select(ExtensionDevice.id).where(ExtensionDevice.id == device.id).with_for_update()
    )
    task = (
        await db.execute(
            select(ExtensionTask)
            .where(
                ExtensionTask.user_id == device.user_id,
                or_(
                    ExtensionTask.status == "pending",
                    and_(ExtensionTask.status == "claimed", ExtensionTask.device_id == device.id),
                ),
            )
            .order_by((ExtensionTask.status == "claimed").desc(), ExtensionTask.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if task is None:
        return None
    # Status only moves past "claimed" on the first progress event, so a task still
    # "claimed" here was never acknowledged; the route re-signals but skips the event.
    task.redelivered = task.status == "claimed"
    task.status = "claimed"
    task.device_id = device.id
    task.claimed_at = task.claimed_at or datetime.now(UTC)
    await db.commit()
    await db.refresh(task)
    return task


async def get_device_task(
    db: AsyncSession, device: ExtensionDevice, task_id: uuid.UUID
) -> ExtensionTask | None:
    return (
        await db.execute(
            select(ExtensionTask)
            .where(
                ExtensionTask.id == task_id,
                ExtensionTask.user_id == device.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


def _file_answer(field: ApplicationField) -> str | None:
    label = field.label.lower()
    if re.search(
        r"cover|passport|\bid\b|photo|picture|transcript|certificate|portfolio|sample", label
    ):
        return None
    # Whole words only: "Upload your passport" must not get the resume.
    if re.search(r"\b(resume|cv|curriculum)\b", label):
        return RESUME_TOKEN
    return None


def _narrative_generator(user_id: uuid.UUID, task: ExtensionTask, budget: list[int]):
    """Drafts free-form answers ("Why do you want to work here?") with the
    user's own model. Always requires review; bounded per plan."""
    payload = task.payload or {}

    async def generate(field: ApplicationField) -> str | None:
        if field.input_type not in {"textarea", "text"} or budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            from app.core.database import AsyncSessionLocal
            from app.core.model_router import begin_token_tracking, get_and_reset_tokens, get_llm

            async with AsyncSessionLocal() as db:
                llm = await get_llm(str(user_id), db, task_type="auto_apply")
                document_id = payload.get("resume_document_id")
                if not document_id:
                    return None
                document = (
                    await db.execute(
                        select(UserDocument).where(
                            UserDocument.id == uuid.UUID(document_id),
                            UserDocument.user_id == user_id,
                            UserDocument.doc_type.in_(RESUME_DOCUMENT_TYPES),
                        )
                    )
                ).scalar_one_or_none()
                if document is None:
                    return None
                resume = (document.raw_text or "")[:6000]
            messages = [
                SystemMessage(
                    content=(
                        "You answer one job-application form question for the candidate, "
                        "in the first person, using only facts from their resume. Be specific "
                        "and concise (under 120 words for long questions, one line for short "
                        "ones). Never invent employers, degrees, numbers, visa status or "
                        "salary. Output only the answer."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Role: {payload.get('role') or 'unknown'} "
                        f"at {payload.get('company') or 'unknown'}\n"
                        f"Question: {field.label}\n\nResume:\n{resume}"
                    )
                ),
            ]
            begin_token_tracking()
            try:
                # The model router bounds provider timeout/retries. Do not cancel
                # a thread while its billable call can still finish unaccounted.
                result = await asyncio.to_thread(llm.invoke, messages)
            finally:
                tokens = get_and_reset_tokens()
                if tokens and task.run_id:
                    from sqlalchemy import func, update

                    async with AsyncSessionLocal() as db:
                        await db.execute(
                            update(AgentRun)
                            .where(AgentRun.id == task.run_id, AgentRun.user_id == user_id)
                            .values(tokens_used=func.coalesce(AgentRun.tokens_used, 0) + tokens)
                        )
                        await db.commit()
            text = (getattr(result, "content", "") or "").strip()
            return text[:4000] or None
        except Exception as exc:
            logger.info("Draft answer unavailable for task %s: %s", task.id, type(exc).__name__)
            return None

    return generate


def custom_question_key(label: str) -> str:
    """Stable key for a free-form question ("Why do you want to join us?")
    that has no canonical rule in question_normalizer."""
    return "custom." + " ".join(re.findall(r"[a-z0-9]+", label.lower()))[:180]


def answer_key(label: str) -> str:
    return normalize_question(label) or custom_question_key(label)


async def _attach_custom_answer_keys(
    db: AsyncSession, user_id: uuid.UUID, fields: list[ApplicationField]
) -> None:
    """Point free-form questions the user already answered at that saved
    answer. Questions never answered keep no key, so a draft can still be
    generated for them."""
    custom = {
        f.field_id: custom_question_key(f.label)
        for f in fields
        if f.label and not normalize_question(f.label)
    }
    if not custom:
        return
    saved = set(
        (
            await db.execute(
                select(CandidateAnswer.question_key).where(
                    CandidateAnswer.user_id == user_id,
                    CandidateAnswer.question_key.in_(set(custom.values())),
                    CandidateAnswer.approved_by_user.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for field in fields:
        key = custom.get(field.field_id)
        if key in saved:
            field.normalized_key = key


async def plan_fields(db: AsyncSession, task: ExtensionTask, raw_fields: list[dict]) -> dict:
    """Resolve an answer for each form field the extension found.

    raw_fields use the same shape as application.schema_extractor's
    FIELD_SNAPSHOT_JS (name, id, type, label, group_label, options, …) plus
    an optional `field_id` the extension uses to find the element again.
    """
    from app.services.decision_engine import match_option

    user = await db.get(User, task.user_id)
    fields = extract_fields(raw_fields[:MAX_FIELDS])
    fields = [f for f in fields if f.visible and not f.disabled]
    await _attach_custom_answer_keys(db, task.user_id, fields)
    budget = [MAX_GENERATED_ANSWERS]
    resolved = await resolve_fields(
        db,
        task.user_id,
        fields,
        narrative_generator=_narrative_generator(task.user_id, task, budget),
    )

    plan = []
    for field, answer in zip(fields, resolved, strict=True):
        entry = {
            "field_id": field.field_id,
            "label": field.label,
            "input_type": field.input_type,
            "required": field.required,
            "value": answer.value,
            "source": answer.source,
            "confidence": answer.confidence,
            "requires_review": answer.requires_review,
            "missing_reason": answer.missing_reason,
        }
        key = normalize_question(field.label) or ""

        if field.input_type == "file":
            token = _file_answer(field)
            entry.update(
                value=token,
                source="profile" if token else "unresolved",
                confidence=1.0 if token else 0.0,
                requires_review=token is None,
                missing_reason=None if token else "Attach this file yourself.",
            )
        elif answer.source == "unresolved" and user is not None and key in _USER_FALLBACK:
            value = _USER_FALLBACK[key](user)
            if value:
                entry.update(
                    value=value,
                    source="profile",
                    confidence=0.95,
                    requires_review=False,
                    missing_reason=None,
                )

        if field.input_type == "checkbox" and not field.options:
            # Consent / attestation boxes are the user's to tick.
            entry.update(
                value=None,
                source="unresolved",
                confidence=0.0,
                requires_review=True,
                missing_reason="Tick this yourself if it applies.",
            )
        elif field.input_type in {"select", "radio"} and entry["value"] not in (None, ""):
            option, confidence = await match_option(entry["value"], field.options, field.label)
            if option is None:
                entry.update(
                    value=None,
                    source="unresolved",
                    requires_review=True,
                    missing_reason="No option clearly matches your saved answer.",
                )
            else:
                entry.update(value=option, confidence=min(entry["confidence"], confidence))

        plan.append(entry)

    unresolved_required = [
        p["field_id"] for p in plan if p["required"] and p["value"] in (None, "")
    ]
    _remember_open_questions(task, plan)
    return {"fields": plan, "unresolved_required": unresolved_required}


def _remember_open_questions(task: ExtensionTask, plan: list[dict]) -> None:
    """Keep the unanswered questions on the task so the member can answer
    them from the "waiting for you" email."""
    open_questions = []
    for entry in plan:
        label = (entry["label"] or "").strip()
        if (
            entry["required"]
            and entry["value"] in (None, "")
            and entry["input_type"] not in {"file", "checkbox"}
            and label
        ):
            open_questions.append({"key": answer_key(label), "label": label[:200]})
    if open_questions:
        task.payload = {**(task.payload or {}), "open_questions": open_questions[:10]}


# ── Download package ───────────────────────────────────────────────────

# The repository's extension/ folder. In Docker it is mounted read-only at
# /extension (deploy/oracle-vm/compose.yml), which is the same relative
# location: /app/app/services/ -> /extension.
EXTENSION_DIR = Path(__file__).resolve().parents[3] / "extension"
_PACKAGE_ROOT = "careercraft-extension"
_EXCLUDED_DIRS = {"test", "node_modules", "__pycache__"}


def package_extension(root: Path = EXTENSION_DIR) -> tuple[str, bytes]:
    """Zip the unpacked extension for "Load unpacked". Returns (version, zip
    bytes); everything sits under one top-level folder so unzipping gives
    the folder to select. Raises FileNotFoundError if it is not deployed."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if not path.is_file() or any(
                part in _EXCLUDED_DIRS or part.startswith(".") for part in relative.parts
            ):
                continue
            archive.write(path, f"{_PACKAGE_ROOT}/{relative.as_posix()}")
    return str(manifest.get("version", "0")), buffer.getvalue()
