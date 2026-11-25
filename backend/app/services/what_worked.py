"""What worked: which resume variants, sources and outreach templates
produced interviews, computed from application outcomes the member already
records. Read-only aggregation — nothing here mutates an application."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from statistics import median

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
    result = await db.execute(
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
    return summarize(result.all(), now)


def summarize(rows, now: datetime | None = None) -> dict:
    """Pure aggregation over (status, source, resume_id, match_score) rows so
    the grouping is testable without a database."""
    resume_counts: Counter = Counter()
    resume_applied: Counter = Counter()
    resume_wins: Counter = Counter()
    source_counts: Counter = Counter()
    source_applied: Counter = Counter()
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
            if status != "saved":
                resume_applied[key] += 1
            if status in INTERVIEW_STATUSES:
                resume_wins[key] += 1
        if source:
            src = str(source)
            source_counts[src] += 1
            if status != "saved":
                source_applied[src] += 1
            if status in INTERVIEW_STATUSES:
                source_wins[src] += 1

    def ranked(counts: Counter, applied_counts: Counter, wins: Counter) -> list[dict]:
        out = []
        for key, total in counts.most_common(10):
            interviews = wins[key]
            applied = applied_counts[key]
            out.append(
                {
                    "key": key,
                    "total": total,
                    "interviews": interviews,
                    "rate": round(interviews / applied, 3) if applied else 0.0,
                    "small_sample": applied < MIN_GROUP_FOR_RATE,
                }
            )
        return out

    applied = sum(n for s, n in by_status.items() if s not in {"saved"})
    interviews = sum(n for s, n in by_status.items() if s in INTERVIEW_STATUSES)
    by_resume = ranked(resume_counts, resume_applied, resume_wins)
    by_source = ranked(source_counts, source_applied, source_wins)
    return {
        "window_days": WINDOW_DAYS,
        "total": len(rows),
        "applied": applied,
        "interviews": interviews,
        "interview_rate": round(interviews / applied, 3) if applied else None,
        "by_status": dict(by_status),
        "by_resume": by_resume,
        "by_source": by_source,
        "median_match_score": median(scores) if scores else None,
        "note": (
            f"Rates are unreliable below {MIN_GROUP_FOR_RATE} applications per group."
            if any(group["small_sample"] for group in by_resume + by_source)
            else None
        ),
    }


def feedback_text(summary: dict) -> str:
    """A short, plain summary for the tailoring prompt, or "" when the member
    has too few applications for the rates to mean anything."""
    applied = summary.get("applied") or 0
    if applied < MIN_GROUP_FOR_RATE:
        return ""
    parts = [
        f"Last {summary['window_days']} days: {applied} applications, "
        f"{summary['interviews']} reached interview ({summary['interview_rate']:.0%})."
    ]
    reliable = [g for g in summary.get("by_source") or [] if not g["small_sample"]]
    if reliable:
        best = max(reliable, key=lambda g: g["rate"])
        parts.append(f"Best source: {best['key']} ({best['rate']:.0%} of its applications).")
    return " ".join(parts)


def feedback_text_sync(user_id: str) -> str:
    """feedback_text for the (sync) resume agent. Never raises: outcome
    feedback is an enhancement, never a blocker."""
    import logging

    from app.core.sync_db import _get_sync_factory, _to_uuid

    try:
        since = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
        with _get_sync_factory()() as session:
            rows = session.execute(
                select(
                    JobApplication.status,
                    JobApplication.source,
                    JobApplication.resume_id,
                    JobApplication.match_score,
                ).where(
                    JobApplication.user_id == _to_uuid(user_id),
                    JobApplication.deleted_at.is_(None),
                    JobApplication.found_at > since,
                )
            ).all()
        return feedback_text(summarize(rows))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "what_worked_feedback_unavailable error_type=%s", type(exc).__name__
        )
        return ""
