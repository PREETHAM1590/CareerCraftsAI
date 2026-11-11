"""Search one company's careers portal directly, and the member's saved boards.

Only URLs that ``resolve_careers_url`` maps to an allow-listed ATS host are ever
fetched, and every request goes through the hardened public HTTP client."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select, text

from app.services.job_catalog import location_matches, matches_query
from app.services.job_connectors import (
    SERVER_SIDE_SEARCH,
    Source,
    merge_duplicates,
    posted,
    resolve_careers_url,
    role_key,
    search_board,
    url_key,
)

logger = logging.getLogger(__name__)

MAX_SAVED_BOARDS = 20
PORTAL_TIMEOUT_S = 40
BOARD_TIMEOUT_S = 25
BOARD_CONCURRENCY = 4
SUPPORTED_PORTALS = (
    "Workday, Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Personio, "
    "BambooHR, Teamtailor, Breezy, Pinpoint, HiBob, Trakstar, JobScore, Gem, Rippling, "
    "Oracle Recruiting and iCIMS"
)


class PortalError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


@dataclass(frozen=True)
class SavedBoard:
    id: uuid.UUID
    source_id: str
    family: str
    label: str
    careers_url: str
    created_at: object


def resolve_board(url: str) -> Source:
    source = resolve_careers_url(url.strip())
    if source is None:
        raise PortalError(
            422,
            "That link is not a careers page we can read. Paste a job-board link from "
            f"one of: {SUPPORTED_PORTALS}.",
        )
    return source


def default_label(source: Source, jobs: list[dict] | None = None) -> str:
    company = next((j["company"] for j in jobs or [] if j.get("company")), "")
    if company:
        return company[:150]
    parts = source.tenant.split("|")
    name = parts[1] if source.family == "workday" and len(parts) > 1 else parts[0].split(".")[0]
    return name.replace("-", " ").replace("_", " ")[:150].title()


def _failure(exc: Exception) -> PortalError:
    if isinstance(exc, PortalError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (404, 410, 422):
            return PortalError(404, "That portal has no job board at this address.")
        if code == 429:
            return PortalError(429, "That portal is rate limiting us. Try again in a few minutes.")
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return PortalError(504, "That portal took too long to answer. Try again shortly.")
    return PortalError(502, "We could not read that portal right now.")


async def search_portal(
    source: Source, query: str, locations: list[str], max_results: int
) -> tuple[list[dict], int]:
    """Live-read the board; returns (newest-first jobs capped at max_results, total matched)."""
    try:
        jobs = await asyncio.wait_for(search_board(source, query, 5), PORTAL_TIMEOUT_S)
    except Exception as exc:
        logger.warning(
            "portal_search_failed",
            extra={"source_id": source.id, "error_type": type(exc).__name__},
        )
        raise _failure(exc) from exc
    if locations:
        jobs = [job for job in jobs if location_matches(job, locations)]
    jobs.sort(key=lambda job: job.get("posted_at") or "", reverse=True)
    return jobs[:max_results], len(jobs)


async def save_jobs(db, user_id: uuid.UUID, jobs: list[dict]) -> tuple[list[uuid.UUID], int]:
    """Save jobs as the member's applications like a normal search does: skipped when the
    URL or the role (company + title + place) is already there, removed ones stay removed.
    Returns (ids of the member's live rows for these jobs, newly created count)."""
    from app.models.db import JobApplication

    rows = (
        await db.execute(
            select(
                JobApplication.id,
                JobApplication.job_url,
                JobApplication.company,
                JobApplication.role,
                JobApplication.location,
                JobApplication.deleted_at,
            )
            .where(JobApplication.user_id == user_id)
            .execution_options(include_deleted=True)
        )
    ).all()
    by_url = {url_key(r.job_url): r for r in rows if r.job_url}
    by_role = {
        role_key({"company": r.company, "title": r.role, "location": r.location or ""}): r
        for r in rows
        if r.company not in (None, "", "Unknown") and r.role not in (None, "", "Unknown")
    }
    ids: dict[uuid.UUID, None] = {}
    created = 0
    for job in jobs:
        if not job.get("url"):
            continue
        key = role_key(job) if job.get("company") and job.get("title") else None
        existing = by_url.get(url_key(job["url"])) or (by_role.get(key) if key else None)
        if existing is not None:
            if existing.deleted_at is None:
                ids[existing.id] = None
            continue
        row = JobApplication(
            id=uuid.uuid4(),
            user_id=user_id,
            company=job.get("company") or "Unknown",
            role=job.get("title") or "Unknown",
            location=job.get("location"),
            job_url=job["url"],
            jd_text=(job.get("description") or "")[:4000],
            status="saved",
            source=job.get("platform") or job.get("source"),
            posted_at=posted(job.get("posted_at")),
        )
        db.add(row)
        by_url[url_key(job["url"])] = row
        if key:
            by_role[key] = row
        ids[row.id] = None
        created += 1
    await db.commit()
    return list(ids), created


async def list_boards(db, user_id: uuid.UUID) -> list[SavedBoard]:
    rows = await db.execute(
        text(
            "SELECT id, source_id, family, label, careers_url, created_at "
            "FROM saved_job_boards WHERE user_id=:u ORDER BY created_at, id"
        ),
        {"u": user_id},
    )
    return [SavedBoard(*row) for row in rows]


async def add_board(db, user_id: uuid.UUID, source: Source, url: str, label: str) -> SavedBoard:
    existing = await db.execute(
        text("SELECT count(*), bool_or(source_id=:s) FROM saved_job_boards WHERE user_id=:u"),
        {"u": user_id, "s": source.id},
    )
    count, has_it = existing.one()
    if not has_it and count >= MAX_SAVED_BOARDS:
        raise PortalError(409, f"You can save up to {MAX_SAVED_BOARDS} boards. Remove one first.")
    row = await db.execute(
        text(
            "INSERT INTO saved_job_boards(user_id, source_id, family, tenant, label, careers_url) "
            "VALUES (:u,:s,:f,:t,:l,:url) ON CONFLICT (user_id, source_id) DO UPDATE "
            "SET label=EXCLUDED.label, careers_url=EXCLUDED.careers_url "
            "RETURNING id, source_id, family, label, careers_url, created_at"
        ),
        {
            "u": user_id,
            "s": source.id,
            "f": source.family,
            "t": source.tenant,
            "l": label,
            "url": url.strip()[:2048],
        },
    )
    board = SavedBoard(*row.one())
    await db.commit()
    return board


async def remove_board(db, user_id: uuid.UUID, board_id: uuid.UUID) -> bool:
    row = await db.execute(
        text("DELETE FROM saved_job_boards WHERE id=:id AND user_id=:u RETURNING id"),
        {"id": board_id, "u": user_id},
    )
    removed = row.first() is not None
    await db.commit()
    return removed


async def search_saved_boards(user_id: str, query: dict) -> tuple[list[dict], list[str]]:
    """Jobs from the member's saved boards that fit a normal search; failures become warnings."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        boards = await list_boards(db, uuid.UUID(str(user_id)))
    if not boards:
        return [], []
    titles = [t for t in (query.get("titles") or []) if t][:3] or [
        str(query.get("search_query") or "")
    ]
    gate = asyncio.Semaphore(BOARD_CONCURRENCY)
    warnings: list[str] = []

    async def one(board: SavedBoard) -> list[dict]:
        # Re-resolve the stored link so only allow-listed hosts are ever fetched.
        source = resolve_careers_url(board.careers_url)
        if source is None:
            warnings.append(f"{board.label}: saved link is no longer supported")
            return []
        async with gate:
            try:
                # Server-side search families need each role; the rest return everything.
                roles = titles if source.family in SERVER_SIDE_SEARCH else [""]
                batches = await asyncio.wait_for(
                    asyncio.gather(*(search_board(source, t, 3) for t in roles)),
                    BOARD_TIMEOUT_S,
                )
            except Exception as exc:
                logger.warning(
                    "saved_board_failed",
                    extra={"source_id": source.id, "error_type": type(exc).__name__},
                )
                warnings.append(f"{board.label}: {_failure(exc).detail}")
                return []
        return [job for batch in batches for job in batch if matches_query(job, query)]

    found = await asyncio.gather(*(one(b) for b in boards))
    return merge_duplicates([job for group in found for job in group]), warnings
