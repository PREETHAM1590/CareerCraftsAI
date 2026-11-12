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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications.answer_resolver import resolve_fields
from app.applications.models import ApplicationField
from app.applications.question_normalizer import normalize_question
from app.applications.schema_extractor import extract_fields
from app.core.pii_vault import strip_contact_details
from app.models.db import (
    AgentRun,
    ApplicationSubmitApproval,
    CandidateAnswer,
    ExtensionDevice,
    ExtensionTask,
    User,
    UserDocument,
    UserPreferences,
)
from app.services import apply_limits
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


TOKEN_TTL = timedelta(days=90)
# The extension swaps its token for a fresh one once it is this close to expiry.
TOKEN_ROTATE_WITHIN = timedelta(days=30)
MAX_DEVICES_PER_USER = 5


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


async def pair_device(
    db: AsyncSession, user_id: uuid.UUID, name: str
) -> tuple[ExtensionDevice, str]:
    """Create a device token. At most MAX_DEVICES_PER_USER stay active: the
    least recently used ones are revoked to make room; their ids are left on
    the new device as `evicted_ids`."""
    # Serialize pairings of one member so two at once cannot both skip the cap
    # (NO KEY UPDATE: device requests' FOR KEY SHARE on the owner is not blocked).
    await db.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
    active = (
        (
            await db.execute(
                select(ExtensionDevice)
                .where(ExtensionDevice.user_id == user_id, ExtensionDevice.revoked_at.is_(None))
                .order_by(
                    func.coalesce(ExtensionDevice.last_seen_at, ExtensionDevice.created_at),
                    ExtensionDevice.created_at,
                )
            )
        )
        .scalars()
        .all()
    )
    evicted = list(active[: max(0, len(active) - (MAX_DEVICES_PER_USER - 1))])
    for old in evicted:
        await revoke_device(db, old, commit=False)
    token = _new_token()
    device = ExtensionDevice(
        id=uuid.uuid4(),
        user_id=user_id,
        name=(name or "Browser").strip()[:100] or "Browser",
        token_hash=hash_token(token),
        expires_at=datetime.now(UTC) + TOKEN_TTL,
    )
    db.add(device)
    await db.commit()
    device.evicted_ids = [d.id for d in evicted]
    return device, token


async def rotate_token(db: AsyncSession, device: ExtensionDevice) -> str:
    """Replace the device's token; the old one stops working immediately."""
    token = _new_token()
    device.token_hash = hash_token(token)
    device.expires_at = datetime.now(UTC) + TOKEN_TTL
    await db.commit()
    return token


async def authenticate(db: AsyncSession, token: str) -> ExtensionDevice | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    device = (
        await db.execute(
            select(ExtensionDevice).where(
                ExtensionDevice.token_hash == hash_token(token),
                ExtensionDevice.revoked_at.is_(None),
                or_(
                    ExtensionDevice.expires_at.is_(None),
                    ExtensionDevice.expires_at > datetime.now(UTC),
                ),
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


async def revoke_device(db: AsyncSession, device: ExtensionDevice, *, commit: bool = True) -> None:
    """Revoke and return the device's unacknowledged claims to the pending pool."""
    device.revoked_at = datetime.now(UTC)
    await db.execute(
        update(ExtensionTask)
        .where(ExtensionTask.device_id == device.id, ExtensionTask.status == "claimed")
        .values(status="pending", device_id=None, claimed_at=None)
    )
    if commit:
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


# ── Auto-submit and the approval audit trail ───────────────────────────


def _host(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def auto_submit_host_blocked(task: ExtensionTask, *urls: str | None) -> bool:
    payload = task.payload or {}
    if payload.get("platform") in {"linkedin", "naukri"}:
        return True
    hosts = [_host(u) for u in (payload.get("job_url"), task.review_url, *urls)]
    return apply_limits.auto_submit_host_blocked(*hosts)


async def auto_submit_status(db: AsyncSession, user_id: uuid.UUID, *, lock: bool = False) -> dict:
    """The member's auto-submit setting and how much of today's cap is used.
    lock=True serializes concurrent auto-approvals so none slips past the cap."""
    query = select(UserPreferences).where(UserPreferences.user_id == user_id)
    if lock:
        query = query.with_for_update()
    prefs = (await db.execute(query)).scalar_one_or_none()
    used = (
        await db.execute(
            select(func.count())
            .select_from(ApplicationSubmitApproval)
            .where(
                ApplicationSubmitApproval.user_id == user_id,
                ApplicationSubmitApproval.auto_approved.is_(True),
                ApplicationSubmitApproval.created_at >= datetime.now(UTC) - timedelta(hours=24),
            )
        )
    ).scalar_one()
    return {
        "enabled": bool(prefs and prefs.auto_submit_enabled),
        "daily_cap": int(prefs.auto_submit_daily_cap if prefs else 10),
        "used_last_24h": int(used or 0),
    }


def auto_submit_refusal(status: dict, task: ExtensionTask) -> str | None:
    """Why this task may not be submitted without the member's click, or None."""
    if not status["enabled"]:
        return "Auto-submit is off in your CareerCraft settings."
    if auto_submit_host_blocked(task):
        return "LinkedIn and Naukri applications are always submitted by you."
    blockers = (task.payload or {}).get("auto_blockers") or []
    if blockers:
        return "Some answers were drafted, not saved by you: " + ", ".join(blockers[:5])
    return apply_limits.auto_submit_cap_error(status["used_last_24h"], status["daily_cap"])


def reviewed_answers(task: ExtensionTask) -> list[dict]:
    """The reviewed form as it was submitted, with where each answer came from."""
    payload = task.payload or {}
    sources = payload.get("answer_sources") or {}
    answers = []
    for field in payload.get("review_fields") or []:
        label = (field.get("label") or field.get("id") or "").strip()[:200]
        entry = {
            "label": label,
            "type": field.get("type"),
            "value": None if field.get("type") == "file" else field.get("value"),
            "source": sources.get(label, "page"),
        }
        if field.get("digest"):
            entry["file_digest"] = field["digest"]
        answers.append(entry)
    return answers


def record_approval(
    db: AsyncSession, task: ExtensionTask, device: ExtensionDevice, *, auto: bool
) -> ApplicationSubmitApproval:
    payload = task.payload or {}
    screenshot = payload.get("review_screenshot") or {}
    approval = ApplicationSubmitApproval(
        id=uuid.uuid4(),
        user_id=task.user_id,
        job_application_id=task.job_application_id,
        extension_task_id=task.id,
        device_id=device.id,
        auto_approved=auto,
        approved_by="auto_submit_setting" if auto else "member",
        review_hash=task.review_hash,
        review_url=task.review_url,
        answers=reviewed_answers(task),
        screenshot_path=(
            screenshot.get("path") if screenshot.get("review_hash") == task.review_hash else None
        ),
    )
    db.add(approval)
    return approval


def approval_view(approval: ApplicationSubmitApproval) -> dict:
    return {
        "id": str(approval.id),
        "application_id": (
            str(approval.job_application_id) if approval.job_application_id else None
        ),
        "auto_approved": approval.auto_approved,
        "approved_by": approval.approved_by,
        "approved_at": approval.created_at.isoformat() if approval.created_at else None,
        "review_url": approval.review_url,
        "answers": approval.answers or [],
        "has_screenshot": bool(approval.screenshot_path),
    }


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
        # A value the page already shows (an ATS's resume parse) is kept.
        if field.input_type not in {"textarea", "text"} or budget[0] <= 0 or field.value:
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
                # Contact lines are never needed to draft an answer; the
                # gateway hides any personal value that is left.
                resume = strip_contact_details(document.raw_text or "")[:6000]
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
        if f.label and not normalize_question(f.label) and not _section_of(f.label)
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


# Questions whose stored answer (identity, contact, location, EEO/disclosure,
# work authorisation, pay) is matched to a dropdown option locally, never by
# the external decision service.
_LOCAL_MATCH_PREFIXES = (
    "personal.",
    "identity.",
    "address.",
    "location.",
    "links.",
    "disclosure.",
    "authorization.",
    "compensation.",
)


def _local_match_only(question_key: str) -> bool:
    from app.applications.answer_resolver import SENSITIVE_KEY_PREFIXES
    from app.services.pii_profile import key_type

    key = question_key or ""
    return (
        key.startswith(_LOCAL_MATCH_PREFIXES + SENSITIVE_KEY_PREFIXES) or key_type(key) is not None
    )


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
    sections = await _section_answers(db, task.user_id, fields)
    for field in fields:
        if _section_of(field.label):
            # Repeated work-history/education blocks are answered only from
            # the member's own resume facts below, never drafted.
            field.normalized_key = "section." + custom_question_key(field.label)
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
        if field.field_id in sections.answers:
            entry.update(
                value=sections.answers[field.field_id],
                source="profile",
                confidence=0.95,
                requires_review=False,
                missing_reason=None,
            )
        # A custom dropdown whose options the extension could not read is
        # matched in the page, which reports a miss back as unresolved.
        if (
            field.input_type in {"select", "radio"}
            and field.options
            and entry["value"] not in (None, "")
        ):
            option, confidence = await match_option(
                entry["value"],
                field.options,
                field.label,
                local_only=_local_match_only(field.normalized_key or "") or _local_match_only(key),
            )
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
    _remember_answer_sources(task, plan)
    return {
        "fields": plan,
        "unresolved_required": unresolved_required,
        "add_blocks": sections.add_blocks,
    }


# Sources auto-submit may rely on: answers the member saved or typed into
# their profile. Anything else (an AI draft, a resume guess) needs a person.
AUTO_SUBMIT_SOURCES = frozenset({"user", "profile"})
_MAX_RECORDED_SOURCES = 200


def _remember_answer_sources(task: ExtensionTask, plan: list[dict]) -> None:
    """Record where every planned answer came from, across all steps of the
    form, for the approval audit and the auto-submit check."""
    payload = dict(task.payload or {})
    sources = dict(payload.get("answer_sources") or {})
    blockers = list(payload.get("auto_blockers") or [])
    for entry in plan:
        if entry["value"] in (None, ""):
            continue
        label = (entry["label"] or entry["field_id"] or "").strip()[:200]
        if len(sources) < _MAX_RECORDED_SOURCES:
            sources[label] = entry["source"]
        if entry["source"] not in AUTO_SUBMIT_SOURCES and label not in blockers:
            blockers.append(label)
    payload["answer_sources"] = sources
    payload["auto_blockers"] = blockers[:20]
    task.payload = payload


# ── Repeated Workday-style blocks (Work Experience N / Education N) ──────

_SECTION_RE = re.compile(r"^(work experience|education)\s+(\d+):\s*(.+)$", re.I)
_SECTION_ATTRS = {
    "work_experience": (
        (re.compile(r"description|summary", re.I), None),
        (re.compile(r"currently work", re.I), "current"),
        (re.compile(r"title|position", re.I), "role"),
        (re.compile(r"company|employer", re.I), "employer"),
        (re.compile(r"location", re.I), "location"),
        (re.compile(r"^(from|start)", re.I), "start"),
        (re.compile(r"^(to|end)\b", re.I), "end"),
    ),
    "education": (
        (re.compile(r"school|university|institution|college", re.I), "institution"),
        (re.compile(r"degree", re.I), "degree"),
        (re.compile(r"^(from|start)", re.I), "start"),
        (re.compile(r"^(to|end)\b", re.I), "end"),
    ),
}
_NAME_ATTR = {"work_experience": "employer", "education": "institution"}
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}
_PRESENT_RE = re.compile(r"^(present|current|now|ongoing)$", re.I)


def _section_of(label: str) -> tuple[str, int, str | None] | None:
    """("work_experience", 2, "employer") for "Work Experience 2: Company*"."""
    match = _SECTION_RE.match((label or "").strip())
    if not match:
        return None
    kind = "work_experience" if match.group(1).lower().startswith("work") else "education"
    name = match.group(3).replace("*", "").strip()
    attr = next((a for pattern, a in _SECTION_ATTRS[kind] if pattern.search(name)), None)
    return kind, int(match.group(2)), attr


def is_section_label(label: str) -> bool:
    return _SECTION_RE.match((label or "").strip()) is not None


def _form_date(text: str | None) -> str | None:
    """Resume dates ("Mar 2019", "2019-03", "03/2019", "2019") as the
    MM/YYYY or YYYY the page's date inputs take; None for "Present"."""
    text = (text or "").strip()
    if not text or _PRESENT_RE.match(text):
        return None
    if match := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", text):
        return f"{int(match.group(2)):02d}/{match.group(1)}"
    if match := re.fullmatch(r"(\d{1,2})/(\d{4})", text):
        return f"{int(match.group(1)):02d}/{match.group(2)}"
    if match := re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", text):
        month = _MONTHS.get(match.group(1).lower())
        return f"{month:02d}/{match.group(2)}" if month else None
    if re.fullmatch(r"\d{4}", text):
        return text
    return None


class _Sections:
    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.add_blocks: dict[str, int] = {"work_experience": 0, "education": 0}


async def _section_answers(
    db: AsyncSession, user_id: uuid.UUID, fields: list[ApplicationField]
) -> _Sections:
    """Fill empty repeated blocks from the member's own resume facts.

    Blocks the page already filled (an ATS resume parse) are left alone, and
    facts whose employer/school already appears on the page are not used
    again. `add_blocks` says how many more blocks the page needs for the
    remaining facts; the extension clicks "Add" that many times."""
    result = _Sections()
    groups: dict[tuple[str, int], list[tuple[ApplicationField, str | None]]] = {}
    for field in fields:
        parsed = _section_of(field.label)
        if parsed:
            groups.setdefault(parsed[:2], []).append((field, parsed[2]))
    from app.services.resume_facts import load_facts_row

    row = await load_facts_row(db, user_id)
    facts = dict(row.answer or {}) if row is not None else {}
    entries = {
        # Only values the member typed; older records may hold model output.
        "work_experience": [
            {k: e[k] for k in e.get("submitted") or [] if e.get(k)}
            for e in facts.get("experience") or []
            if isinstance(e, dict) and e.get("submitted")
        ],
        "education": [e for e in facts.get("education") or [] if isinstance(e, dict)],
    }
    for kind, items in entries.items():
        name_attr = _NAME_ATTR[kind]
        on_page = {
            str(f.value).strip().casefold()
            for (k, _), members in groups.items()
            if k == kind
            for f, attr in members
            if attr == name_attr and f.value
        }
        remaining = [
            e for e in items if e and str(e.get(name_attr, "")).strip().casefold() not in on_page
        ]
        empty = sorted(
            n
            for (k, n), members in groups.items()
            if k == kind and not any(f.value and f.input_type != "checkbox" for f, _ in members)
        )
        for n, entry in zip(empty, remaining, strict=False):
            current = _PRESENT_RE.match(str(entry.get("end", "")).strip()) is not None
            for field, attr in groups[(kind, n)]:
                if attr == "current":
                    if current:
                        result.answers[field.field_id] = True
                elif attr in {"start", "end"}:
                    value = _form_date(entry.get(attr))
                    if value:
                        result.answers[field.field_id] = value
                elif attr and entry.get(attr):
                    result.answers[field.field_id] = entry[attr]
        result.add_blocks[kind] = max(0, len(remaining) - len(empty))
    return result


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
