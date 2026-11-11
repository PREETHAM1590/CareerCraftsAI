"""The member's reusable screening answers, as listed in Settings.

Stored as approved candidate_answers rows (source "user"), so the planner
reuses them ahead of anything else; see answer_resolver.py. The catalogue
names the questions almost every application asks; any other question the
member answered in the extension panel is listed after them.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.applications import profile_service
from app.models.db import CandidateAnswer

# (question_key, question as shown, group, optional)
CATALOGUE: tuple[tuple[str, str, str, bool], ...] = (
    (
        "authorization.work_authorized",
        "Are you legally authorized to work in the country of the job?",
        "screening",
        False,
    ),
    (
        "authorization.requires_sponsorship",
        "Will you now or in the future require visa sponsorship?",
        "screening",
        False,
    ),
    ("experience.notice_period_days", "What is your notice period?", "screening", False),
    ("compensation.expected_salary", "What are your salary expectations?", "screening", False),
    ("location.willing_to_relocate", "Are you willing to relocate?", "screening", False),
    ("availability.start_date", "When can you start?", "screening", False),
    ("disclosure.gender", "Gender", "voluntary", True),
    ("disclosure.race_ethnicity", "Race / ethnicity", "voluntary", True),
    ("disclosure.veteran_status", "Veteran status", "voluntary", True),
    ("disclosure.disability_status", "Disability status", "voluntary", True),
)
_CATALOGUE_KEYS = {key for key, *_ in CATALOGUE}
# Internal rows that share the table but are not screening answers.
_INTERNAL_KEYS = {"resume.facts"}
KEY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_ ]{1,180}$")
MAX_VALUE_CHARS = 2000


def valid_key(question_key: str) -> bool:
    return bool(KEY_RE.match(question_key)) and question_key not in _INTERNAL_KEYS


def _value(row: CandidateAnswer | None):
    return (row.answer or {}).get("value") if row is not None else None


async def list_answers(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(CandidateAnswer).where(
                    CandidateAnswer.user_id == user_id,
                    CandidateAnswer.approved_by_user.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    by_key = {r.question_key: r for r in rows if r.question_key not in _INTERNAL_KEYS}
    items = [
        {
            "question_key": key,
            "question": question,
            "group": group,
            "optional": optional,
            "value": _value(by_key.get(key)),
            "updated_at": _updated(by_key.get(key)),
        }
        for key, question, group, optional in CATALOGUE
    ]
    for key in sorted(set(by_key) - _CATALOGUE_KEYS):
        row = by_key[key]
        items.append(
            {
                "question_key": key,
                "question": row.normalized_question or key,
                "group": "other",
                "optional": True,
                "value": _value(row),
                "updated_at": _updated(row),
            }
        )
    return items


def _updated(row: CandidateAnswer | None) -> str | None:
    return row.updated_at.isoformat() if row is not None and row.updated_at else None


async def save_answer(
    db: AsyncSession, user_id: uuid.UUID, question_key: str, value: str | bool | list[str]
) -> None:
    question = next((q for k, q, *_ in CATALOGUE if k == question_key), None)
    if question is None:
        existing = await profile_service.get_saved_answer(db, user_id, question_key)
        question = existing.normalized_question if existing is not None else question_key
    await profile_service.save_approved_answer(db, user_id, question_key, question, value)


async def delete_answer(db: AsyncSession, user_id: uuid.UUID, question_key: str) -> None:
    await db.execute(
        delete(CandidateAnswer).where(
            CandidateAnswer.user_id == user_id, CandidateAnswer.question_key == question_key
        )
    )
