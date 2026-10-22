"""Structured candidate profile + saved-answer lookups for the answer resolver.

Every value returned here comes from an explicit user-entered field or a
previously user-approved answer — never generated. See answer_resolver.py
for the priority order this feeds into.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import CandidateAnswer, CandidateProfile

# question_key -> CandidateProfile attribute. Keys with no entry here (e.g.
# generative "why this role?" questions) are never answered from structured
# profile data.
PROFILE_FIELD_FOR_KEY: dict[str, str] = {
    "authorization.requires_sponsorship": "requires_sponsorship",
    "authorization.work_authorized": "work_authorization",
    "location.willing_to_relocate": "willing_to_relocate",
    "location.city": "city",
    "compensation.expected_salary": "expected_salary",
    "compensation.current_salary": "current_salary",
    "experience.notice_period_days": "notice_period_days",
    "experience.years_experience": "years_experience",
    "experience.current_company": "current_company",
    "experience.current_title": "current_title",
    "personal.first_name": "first_name",
    "personal.last_name": "last_name",
    "personal.email": "email",
    "personal.phone": "phone",
    "links.linkedin_url": "linkedin_url",
    "links.github_url": "github_url",
    "links.portfolio_url": "portfolio_url",
    "work.remote_preference": "remote_preference",
}


async def get_profile(db: AsyncSession, user_id: uuid.UUID) -> CandidateProfile | None:
    return (await db.execute(
        select(CandidateProfile).where(CandidateProfile.user_id == user_id)
    )).scalar_one_or_none()


async def get_saved_answer(
    db: AsyncSession, user_id: uuid.UUID, question_key: str,
) -> CandidateAnswer | None:
    return (await db.execute(
        select(CandidateAnswer).where(
            CandidateAnswer.user_id == user_id,
            CandidateAnswer.question_key == question_key,
            CandidateAnswer.approved_by_user.is_(True),
        )
    )).scalar_one_or_none()


def structured_profile_value(profile: CandidateProfile, question_key: str) -> object | None:
    if question_key == "personal.full_name":
        parts = [p for p in (profile.first_name, profile.last_name) if p]
        return " ".join(parts) if parts else None
    field = PROFILE_FIELD_FOR_KEY.get(question_key)
    if not field:
        return None
    return getattr(profile, field, None)


async def save_approved_answer(
    db: AsyncSession, user_id: uuid.UUID, question_key: str, normalized_question: str,
    answer: object, answer_type: str = "text", source: str = "user", confidence: float = 1.0,
) -> CandidateAnswer:
    """Upsert the (user_id, question_key) row the unique constraint enforces."""
    existing = (await db.execute(
        select(CandidateAnswer).where(
            CandidateAnswer.user_id == user_id, CandidateAnswer.question_key == question_key,
        )
    )).scalar_one_or_none()
    if existing:
        existing.normalized_question = normalized_question
        existing.answer_type = answer_type
        existing.answer = {"value": answer}
        existing.source = source
        existing.confidence = confidence
        existing.approved_by_user = True
        return existing
    row = CandidateAnswer(
        id=uuid.uuid4(), user_id=user_id, question_key=question_key,
        normalized_question=normalized_question, answer_type=answer_type,
        answer={"value": answer}, source=source, confidence=confidence, approved_by_user=True,
    )
    db.add(row)
    return row
