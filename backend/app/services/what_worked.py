"""What worked: which resume variants, sources and outreach templates
produced interviews, computed from application outcomes the member already
records. Read-only aggregation — nothing here mutates an application."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.db import JobApplication

# Statuses the member sets that count as the outcome we optimize for.
INTERVIEW_STATUSES = {"interview", "interviewing", "offer", "hired"}

WINDOW_DAYS = 90
MIN_GROUP_FOR_RATE = 5


async def what_worked(db, user_id: uuid.UUID, now: datetime | None = None) -> dict:
    """Group the member's applications from the window by resume variant,
    source and status, and report interview rates per group."""
    since = (now or datetime.now(UTC)) - timedelta(days=WINDOW_DAYS)
    rows = (
        await db.execute(
            select(
                JobApplication.status,
                JobApplication.source,
                JobApplication.resume_id,
                JobApplication.match_score,
            ).where(
                JobApplication.user_id == user_id,
                JobApplication.deleted_at.is_(None),
                JobApplication.found_at > since,
            )
        )
        .all()
    )
    return summarize(rows, now)


def summarize(rows, now: datetime | None = None) -> dict:
    """Pure aggregation over (status, source, resume_id, match_score) rows so
    the grouping is testable without a database."""
    outcome_counts: dict[str, Counter] = defaultdict(Counter)
    resume_counts: Counter = Counter()
    resume_wins: Counter = Counter()
    source_counts: Counter = Counter()
    source_wins: Counter = Counter()
    by_status: Counter = Counter()
    scores: list[int] = []

    for status, source, resume_id, match_score in rows:
        status = (status or "saved").lower()
        by_status[status] += 1
        if match_score is not None:
            scores.append(match_score)
        if resume_id:
            key = str(resume_id)
            resume_counts[key] += 1
            if status.lower() in INTERVIEW_STATUSES:
                resume_wins[key] += 1
        if source:
            src = str(source)
            source_counts[src] += 1
            if status.lower() in INTERVIEW_STATUSES:
                source_wins[src] += 1

    def ranked(counts: Counter, wins: Counter) -> list[dict]:
        out = []
        for key, total in counts.most_common(10):
            interviews = wins.get(key, 0)
            out.append(
                {
                    "key": key,
                    "total": total,
                    "interviews": interviews,
                    "rate": round(interviews / total, 3) if total else 0.0,
                }
            )
        return out

    applied = sum(n for s, n in by_status.items() if s not in {"saved"})
    interviews = sum(n for s, n in by_status.items() if s in INTERVIEW_STATUSES)
    return {
        "window_days": WINDOW_DAYS,
        "total": len(rows),
        "applied": applied,
        "interviews": interviews,
        "interview_rate": round(interviews / applied, 3) if applied else None,
        "by_status": dict(by_status),
        "by_resume": ranked(resume_counts, resume_wins),
        "by_source": ranked(source_counts, source_wins),
        "median_match_score": sorted(scores)[len(scores) // 2] if scores else None,
        "note": (
            "Rates are informative only below "
            f"{MIN_GROUP_FOR_RATE} applications per group."
            if len(rows) < MIN_GROUP_FOR_RATE
            else None
        ),
    }
