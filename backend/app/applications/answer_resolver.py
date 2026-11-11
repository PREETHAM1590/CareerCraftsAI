"""Strict-order answer resolution for application form fields.

Order: approved saved answer -> structured profile value -> resume-derived
fact -> allowed generative answer -> NEEDS_USER_INPUT. Sensitive question
keys (sponsorship, authorization, salary, notice period) never fall through
to resume-derived or generated answers — the user must answer directly or
have a previously approved saved answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.applications import profile_service
from app.applications.models import ApplicationField, ResolvedAnswer
from app.applications.question_normalizer import normalize_question

SENSITIVE_KEY_PREFIXES = (
    "authorization.",
    "compensation.",
    "experience.notice_period_days",
    "availability.",
    "disclosure.",
)

# (label, question_key) -> (value, confidence 0-1, evidence) or None.
ResumeFactResolver = Callable[[str, str], Awaitable[tuple[object, float, list[str]] | None]]
# Only ever called for fields with no canonical question_key (free-form
# questions) — never for a sensitive or factual field.
NarrativeGenerator = Callable[[ApplicationField], Awaitable[str | None]]


def _is_sensitive(question_key: str) -> bool:
    return any(question_key == p or question_key.startswith(p) for p in SENSITIVE_KEY_PREFIXES)


async def resolve_field(
    db,
    user_id,
    field: ApplicationField,
    resume_resolver: ResumeFactResolver | None = None,
    narrative_generator: NarrativeGenerator | None = None,
) -> ResolvedAnswer:
    question_key = field.normalized_key or normalize_question(field.label) or ""
    sensitive = _is_sensitive(question_key)

    if question_key:
        saved = await profile_service.get_saved_answer(db, user_id, question_key)
        if saved is not None:
            return ResolvedAnswer(
                field_id=field.field_id,
                value=saved.answer.get("value"),
                source="user",
                confidence=1.0,
                evidence=["candidate_answers"],
                requires_review=False,
            )

        profile = await profile_service.get_profile(db, user_id)
        if profile is not None:
            value = profile_service.structured_profile_value(profile, question_key)
            if value is not None and value != "":
                return ResolvedAnswer(
                    field_id=field.field_id,
                    value=value,
                    source="profile",
                    confidence=0.99,
                    evidence=["candidate_profiles"],
                    requires_review=False,
                )

    if not sensitive and resume_resolver is not None:
        fact = await resume_resolver(field.label, question_key)
        if fact is not None:
            value, confidence, evidence = fact
            bounded = min(max(confidence, 0.75), 0.90)
            return ResolvedAnswer(
                field_id=field.field_id,
                value=value,
                source="resume",
                confidence=bounded,
                evidence=evidence,
                requires_review=bounded < 0.90,
            )

    if not sensitive and not question_key and narrative_generator is not None:
        narrative = await narrative_generator(field)
        if narrative:
            return ResolvedAnswer(
                field_id=field.field_id,
                value=narrative,
                source="generated",
                confidence=0.5,
                evidence=["generated"],
                requires_review=True,
            )

    return ResolvedAnswer(
        field_id=field.field_id,
        value=None,
        source="unresolved",
        confidence=0.0,
        requires_review=True,
        missing_reason=(
            "This question requires a direct answer and cannot be inferred."
            if sensitive
            else "No saved or structured answer available."
        ),
    )


async def resolve_fields(
    db,
    user_id,
    fields: list[ApplicationField],
    **kwargs,
) -> list[ResolvedAnswer]:
    return [await resolve_field(db, user_id, f, **kwargs) for f in fields]
