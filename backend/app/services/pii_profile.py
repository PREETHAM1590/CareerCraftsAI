"""Which personal values the LLM gateway hides from the model, per member.

Values come only from what the member stored themselves: account settings,
the application profile, the resume "Fix gaps" facts and approved saved
answers whose question is about identity (email, phone, address, date of
birth, PAN, Aadhaar, passport, UPI, profile links).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.core.config import settings
from app.core.pii_vault import KnownPii

FACTS_KEY = "resume.facts"

# Saved-answer question keys (canonical or "custom.<label words>") whose
# value is an identity detail. First match wins, so "email address" is an
# email, not an address.
_KEY_TYPES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), kind)
    for pattern, kind in (
        (r"^personal\.first_name$", "FIRST_NAME"),
        (r"^personal\.last_name$", "LAST_NAME"),
        (r"^personal\.full_name$", "NAME"),
        (r"\be-?mail\b|personal\.email", "EMAIL"),
        (r"\b(phone|mobile|whatsapp|contact number)\b|personal\.phone", "PHONE"),
        (r"\b(date of birth|dob|birth ?date)\b", "DOB"),
        (r"\baadha?a?r\b", "AADHAAR"),
        (r"\bpan\b", "PAN"),
        (r"\bpassport\b", "PASSPORT"),
        (r"\bupi\b", "UPI"),
        (r"\b(pin ?code|postal|zip)\b", "POSTAL_CODE"),
        (r"\b(address|street)\b", "ADDRESS"),
        (r"^links\.|\b(linkedin|github|portfolio|website)\b", "URL"),
    )
)

# A stored value only counts as that kind of identifier when it looks like
# one; "Yes" saved for "Do you have a passport?" is not a passport number.
_SHAPE = {
    "EMAIL": re.compile(r"@"),
    "UPI": re.compile(r"@"),
    "PHONE": re.compile(r"\d"),
    "DOB": re.compile(r"\d"),
    "AADHAAR": re.compile(r"\d"),
    "PAN": re.compile(r"\d"),
    "PASSPORT": re.compile(r"\d"),
    "POSTAL_CODE": re.compile(r"\d"),
    "URL": re.compile(r"\w\.\w"),
}


@dataclass(frozen=True)
class PrivacyPolicy:
    enabled: bool
    redact_names: bool
    detect_patterns: bool


def key_type(question_key: str) -> str | None:
    for pattern, kind in _KEY_TYPES:
        if pattern.search(question_key or ""):
            return kind
    return None


def _add(known: KnownPii, kind: str, value) -> None:
    if value is None or isinstance(value, bool | dict | list):
        return
    text = str(value)
    shape = _SHAPE.get(kind)
    if shape is not None and not shape.search(text):
        return
    known.add(kind, text)


def build_known_pii(user, profile, answers, *, include_names: bool = True) -> KnownPii:
    """Known values from the member's rows. ``answers`` is (question_key,
    answer JSON) pairs of approved saved answers."""
    known = KnownPii()
    if user is not None:
        _add(known, "EMAIL", getattr(user, "email", None))
        _add(known, "PHONE", getattr(user, "phone", None))
        _add(known, "URL", getattr(user, "linkedin_url", None))
        if include_names:
            _add(known, "NAME", getattr(user, "full_name", None))
    if profile is not None:
        for attr, kind in (
            ("email", "EMAIL"),
            ("phone", "PHONE"),
            ("postal_code", "POSTAL_CODE"),
            ("linkedin_url", "URL"),
            ("github_url", "URL"),
            ("portfolio_url", "URL"),
        ):
            _add(known, kind, getattr(profile, attr, None))
        if include_names:
            first = getattr(profile, "first_name", None)
            last = getattr(profile, "last_name", None)
            _add(known, "FIRST_NAME", first)
            _add(known, "LAST_NAME", last)
            if first and last:
                _add(known, "NAME", f"{first} {last}")
    for question_key, answer in answers or ():
        answer = answer if isinstance(answer, dict) else {}
        if question_key == FACTS_KEY:
            contact = answer.get("contact") or {}
            for field, kind in (
                ("email", "EMAIL"),
                ("phone", "PHONE"),
                ("linkedin", "URL"),
                ("github", "URL"),
                ("portfolio", "URL"),
            ):
                _add(known, kind, contact.get(field))
            continue
        kind = key_type(question_key)
        if kind is None or (not include_names and kind in {"NAME", "FIRST_NAME", "LAST_NAME"}):
            continue
        _add(known, kind, answer.get("value"))
    if include_names and user is not None and getattr(user, "full_name", None):
        parts = str(user.full_name).split()
        if len(parts) >= 2:
            _add(known, "FIRST_NAME", parts[0])
            _add(known, "LAST_NAME", parts[-1])
    return known


def policy_for(prefs) -> PrivacyPolicy:
    """Server flags, overridden by the member's own preference when set.

    ``UserPreferences.llm_hide_personal_info`` / ``llm_hide_name`` are read
    with getattr so this works before those columns exist (default on)."""
    enabled = settings.PII_REDACTION_ENABLED
    names = settings.PII_REDACT_NAMES
    member_enabled = getattr(prefs, "llm_hide_personal_info", None)
    member_names = getattr(prefs, "llm_hide_name", None)
    if isinstance(member_enabled, bool):
        enabled = enabled and member_enabled
    if isinstance(member_names, bool):
        names = member_names
    return PrivacyPolicy(
        enabled=enabled,
        redact_names=enabled and names,
        detect_patterns=enabled and settings.PII_PATTERN_DETECTION,
    )


async def load_privacy_context(user_id: str) -> tuple[PrivacyPolicy, KnownPii]:
    """Policy and known values for a gateway session's member.

    Raises on database errors so the gateway can fail closed. A session
    whose user id is not a UUID has no member rows to load."""
    try:
        uid = uuid.UUID(str(user_id))
    except (TypeError, ValueError):
        return policy_for(None), KnownPii()
    if not settings.PII_REDACTION_ENABLED:
        return policy_for(None), KnownPii()

    from app.core.database import AsyncSessionLocal
    from app.models.db import CandidateAnswer, CandidateProfile, User, UserPreferences

    async with AsyncSessionLocal() as db:
        prefs = (
            await db.execute(select(UserPreferences).where(UserPreferences.user_id == uid))
        ).scalar_one_or_none()
        policy = policy_for(prefs)
        if not policy.enabled:
            return policy, KnownPii()
        user = await db.get(User, uid)
        profile = (
            await db.execute(select(CandidateProfile).where(CandidateProfile.user_id == uid))
        ).scalar_one_or_none()
        answers = (
            await db.execute(
                select(CandidateAnswer.question_key, CandidateAnswer.answer).where(
                    CandidateAnswer.user_id == uid,
                    CandidateAnswer.approved_by_user.is_(True),
                )
            )
        ).all()
    known = build_known_pii(user, profile, answers, include_names=policy.redact_names)
    return policy, known
