"""Durable source cache/health and failure-isolated public discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import text

from app.services.job_connectors import Source, dedupe, fetch_page, normalize
from app.services.jobs_database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Catalog rows seen within this window are served before any live fetch.
CATALOG_FRESH_DAYS = 14
# Cold/gap-fill refresh targets: bounded set of fast public sources.
PREFERRED_SOURCES = {
    "greenhouse:stripe",
    "greenhouse:gitlab",
    "greenhouse:figma",
    "lever:netflix",
    "lever:benchling",
    "ashby:linear",
    "ashby:vanta",
    "remotive",
    "remoteok",
    "arbeitnow",
}


def sources() -> list[Source]:
    from app.core.config import settings

    rows = json.loads(Path(__file__).with_name("job_sources.json").read_text())
    mapped = {
        row["id"]: Source(
            **{
                k: row[k]
                for k in ("id", "family", "tenant", "permitted", "url", "refresh_hours")
                if k in row
            }
        )
        for row in rows
    }
    for row in settings.JOB_SOURCE_OVERRIDES[:100]:
        source = Source(**row)
        mapped[source.id] = source
    return [source for source in mapped.values() if source.permitted]


async def upsert_jobs(db, jobs: list[dict], source_id: str) -> None:
    """Upsert normalized jobs into the shared catalog + occurrences (caller commits)."""
    for job in dedupe(jobs, 90):
        await db.execute(
            text("""INSERT INTO job_catalog(job_id,url,title,company,posted_at,data)
            VALUES(:id,:url,:title,:company,:posted,CAST(:data AS jsonb))
            ON CONFLICT(job_id) DO UPDATE SET data=EXCLUDED.data ||
            jsonb_build_object('first_seen_at',job_catalog.first_seen_at,
            -- A thinner sighting (e.g. a live scrape snippet) never replaces a fuller description.
            'description',CASE WHEN length(coalesce(job_catalog.data->>'description',''))
                > length(coalesce(EXCLUDED.data->>'description',''))
              THEN job_catalog.data->'description'
              ELSE coalesce(EXCLUDED.data->'description',to_jsonb(''::text)) END),
            title=EXCLUDED.title,
            company=EXCLUDED.company,posted_at=EXCLUDED.posted_at,last_seen_at=now()"""),
            {
                "id": job["job_id"],
                "url": job["url"],
                "title": job["title"],
                "company": job["company"],
                "posted": (datetime.fromisoformat(job["posted_at"]) if job["posted_at"] else None),
                "data": json.dumps(job),
            },
        )
        await db.execute(
            text("""INSERT INTO job_source_occurrences(job_id,source_id)
            VALUES(:job,:source) ON CONFLICT(job_id,source_id)
            DO UPDATE SET last_seen_at=now()"""),
            {"job": job["job_id"], "source": source_id},
        )


async def write_through(raw_jobs: list[dict], platform: str) -> int:
    """Store live-fetched jobs (url/title/company/... dicts) under source ``live:<platform>``.

    Never raises: a failed catalog write must not fail the user's search.
    """
    try:
        source = Source(id=f"live:{platform}", family=platform)
        jobs = []
        for raw in raw_jobs:
            # Live adapters emit free-text "remote"; only the literal flag counts here.
            flag = str(raw.get("remote", "")).lower() == "remote"
            job = normalize({**raw, "remote": flag}, source)
            if job:
                jobs.append(job)
        if not jobs:
            return 0
        async with AsyncSessionLocal() as db:
            await db.execute(
                text(
                    "INSERT INTO job_source_health(source_id) VALUES (:id) ON CONFLICT DO NOTHING"
                ),
                {"id": source.id},
            )
            await upsert_jobs(db, jobs, source.id)
            await db.commit()
        return len(jobs)
    except Exception as exc:
        logger.warning(
            "job_catalog_write_through_failed",
            extra={"platform": platform, "error_type": type(exc).__name__},
        )
        return 0


async def refresh_source(source: Source, query="") -> tuple[list[dict], str | None]:
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("INSERT INTO job_source_health(source_id) VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": source.id},
        )
        cached = (
            (
                await db.execute(
                    text("SELECT * FROM job_source_health WHERE source_id=:id FOR UPDATE"),
                    {"id": source.id},
                )
            )
            .mappings()
            .one()
        )
        if cached["next_allowed_at"] and cached["next_allowed_at"] > now:
            return cached["cached_jobs"] or [], cached["warning"]
        # A persisted lease prevents multiple API/worker processes refreshing one source.
        await db.execute(
            text("UPDATE job_source_health SET next_allowed_at=:lease WHERE source_id=:id"),
            {"id": source.id, "lease": now + timedelta(minutes=5)},
        )
        await db.commit()
    jobs, warning, cursor, retry_after = [], None, None, None
    try:
        for _ in range(3):
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(fetch_page(source, query, cursor), 15)
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {401, 403, 429}:
                        raise
                    if attempt or exc.response.status_code < 500:
                        raise
                    await asyncio.sleep(1)
                except (httpx.TransportError, TimeoutError):
                    if attempt:
                        raise
                    await asyncio.sleep(1)
            jobs.extend(result.jobs)
            cursor = result.next_cursor
            if not cursor or len(jobs) >= 1000:
                break
            await asyncio.sleep(1)
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
            401,
            403,
            429,
        }:
            retry_after = 3600
            try:
                retry_after = max(
                    3600,
                    min(86400, int(exc.response.headers.get("retry-after", "3600"))),
                )
            except ValueError:
                pass
        # Never log URL/request/response bodies: they may contain API keys.
        warning = f"{source.id}: {type(exc).__name__}"
        logger.warning(
            "job_source_failure",
            extra={"source_id": source.id, "error_type": type(exc).__name__},
        )
        jobs = cached["cached_jobs"] or []
    interval = timedelta(hours=24 if source.family == "remotive" else source.refresh_hours)
    if warning:
        interval = timedelta(minutes=min(60, 2 ** min(int(cached["failures"] or 0) + 1, 6)))
        if retry_after:
            interval = timedelta(seconds=retry_after)
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""UPDATE job_source_health SET checked_at=:now, next_allowed_at=:next,
            cached_jobs=CAST(:jobs AS jsonb), warning=:warning,
            failures=CASE WHEN CAST(:warning AS text) IS NULL THEN 0 ELSE failures+1 END,
            status=CASE WHEN CAST(:warning AS text) IS NULL THEN 'healthy' ELSE 'degraded' END
            WHERE source_id=:id"""),
            {
                "id": source.id,
                "now": now,
                "next": now + interval,
                "jobs": json.dumps(jobs),
                "warning": warning,
            },
        )
        if not warning:
            await upsert_jobs(db, jobs, source.id)
            if cursor is None:
                await db.execute(
                    text(
                        "DELETE FROM job_source_occurrences WHERE source_id=:id "
                        "AND NOT(job_id=ANY(:jobs))"
                    ),
                    {"id": source.id, "jobs": [job["job_id"] for job in jobs]},
                )
        await db.commit()
    logger.info(
        "job_source_health",
        extra={
            "source_id": source.id,
            "status": "degraded" if warning else "healthy",
            "job_count": len(jobs),
        },
    )
    return jobs, warning


async def refresh_catalog() -> dict:
    semaphore = asyncio.Semaphore(8)

    async def one(source):
        async with semaphore:
            try:
                jobs, warning = await refresh_source(source)
                return len(jobs), warning
            except Exception as exc:
                logger.warning(
                    "job_source_storage_failure",
                    extra={"source_id": source.id, "error_type": type(exc).__name__},
                )
                return 0, source.id + ": unavailable"

    results = await asyncio.gather(*(one(source) for source in sources()))
    # New jobs are searchable now, not when cached searches expire.
    from app.services import search_cache

    await search_cache.bump_catalog_version()
    return {
        "sources": len(results),
        "healthy": sum(w is None for _, w in results),
        "jobs": sum(n for n, _ in results),
    }


_STOPWORDS = {"and", "the", "for", "with", "role", "job", "jobs"}
_ANYWHERE = {"", "remote", "anywhere", "worldwide", "any"}
_CITY_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai"}


def _country_of(text: str) -> str | None:
    from app.services.job_platforms_service import _COUNTRY_MAP

    for key in sorted(_COUNTRY_MAP, key=len, reverse=True):
        if re.search(r"\b" + re.escape(key) + r"\b", text):
            return _COUNTRY_MAP[key]
    return None


def _cities(locations: list[str]) -> list[str] | None:
    """Requested cities (aliases folded), or None for a remote/anywhere search."""
    wanted = [loc.split(",")[0].strip().lower() for loc in locations]
    if not wanted or any(w in _ANYWHERE for w in wanted):
        return None
    return [_CITY_ALIASES.get(w, w) for w in wanted]


def in_city(job: dict, locations: list[str]) -> bool:
    """The job is located in one of the requested cities (False for remote searches)."""
    where = str(job.get("location") or "").lower()
    for city in _cities(locations) or []:
        if city in where or any(a in where for a, c in _CITY_ALIASES.items() if c == city):
            return True
    return False


def _location_ok(job: dict, locations: list[str]) -> bool:
    """A job fits a city search when it is in that city, or remote and not
    restricted to another country ("Remote, United States" is not a fit for
    Bengaluru). Remote/anywhere searches accept every location."""
    wanted = _cities(locations)
    if wanted is None:
        return True
    if in_city(job, locations):
        return True
    where = str(job.get("location") or "").lower().replace(".", "")
    hay = f"{where} {str(job.get('remote') or '').lower()}"
    for city in wanted:
        if "remote" in hay or job.get("remote") is True:
            job_country = _country_of(where)
            if job_country is None or job_country == _country_of(city):
                return True
    return False


def matches_query(job: dict, query: dict) -> bool:
    """The same relevance contract for catalog and live results."""
    alternatives = query.get("titles") or [query.get("search_query") or "software engineer"]
    title = str(job.get("title") or "").lower()

    def matches_role(role: str) -> bool:
        keywords = [
            word for word in role.lower().split() if len(word) > 2 and word not in _STOPWORDS
        ]
        return all(word in title for word in keywords)

    locations = [str(x) for x in (query.get("locations") or [query.get("location") or ""]) if x]
    if not any(matches_role(role) for role in alternatives):
        return False
    if not _location_ok(job, locations):
        return False
    mode = str(query.get("remote") or "any").lower()
    if mode not in {"remote", "hybrid", "onsite"}:
        return True
    where = str(job.get("location") or "").lower()
    remote = str(job.get("remote") or "").lower()
    actual = (
        "hybrid"
        if "hybrid" in remote or "hybrid" in where
        else (
            "remote"
            if job.get("remote") is True or "remote" in remote or "remote" in where
            else "onsite"
        )
    )
    return actual == mode


async def search_catalog(query: dict, selected_sources=None, live_platforms=()):
    """Serve fresh shared-catalog jobs first; refresh public sources only for a shortfall.

    ``live_platforms`` also reads jobs other users' live searches stored under ``live:<name>``.
    """
    families = set(selected_sources or [])
    live_ids = [f"live:{p}" for p in live_platforms]
    public = (
        []
        if live_ids and not families
        else [s for s in sources() if not families or s.family in families or s.id in families]
    )
    if not public and not live_ids:
        return [], ["No public source matches the requested filters"]
    source_ids = sorted({s.id for s in public} | set(live_ids))
    days = query.get("posted_within_days", 30)

    def relevant_of(rows):
        return [job for job in dedupe(rows, days) if matches_query(job, query)]

    # The result is the same for every member who runs this query, so it is
    # cached as the ids of every matching row (before duplicates are merged);
    # a hit re-reads just those rows by primary key and merges them again,
    # instead of scanning the catalog (see app/services/search_cache.py).
    from app.services import search_cache

    version = await search_cache.catalog_version()
    if version is None:
        relevant, warnings, _ = await _search_catalog_db(query, public, live_ids, relevant_of)
        return relevant, warnings

    key = (
        f"{search_cache.PREFIX}:catalog:"
        + search_cache.query_hash(
            {
                "terms": search_cache.terms_of(query),
                "places": search_cache.places_of(query),
                "remote": str(query.get("remote") or "any").lower(),
                "days": days,
                "need": int(query.get("max_results") or 10),
                "sources": source_ids,
            }
        )
        + f":{version}"
    )

    async def compute():
        relevant, warnings, ids = await _search_catalog_db(query, public, live_ids, relevant_of)
        # A source that failed during gap fill should be retried by the next
        # search, not hidden behind a thin cached result.
        payload = None if warnings else json.dumps({"ids": ids, "warnings": []})
        return (relevant, warnings), payload

    value, cached = await search_cache.get_or_compute(
        "catalog", key, search_cache.CATALOG_TTL, compute, wait_s=20
    )
    if cached is None:
        return value
    hit = json.loads(cached)
    return relevant_of(await _catalog_rows_by_id(hit["ids"], source_ids)), hit["warnings"]


async def _catalog_rows_by_id(ids: list[str], source_ids: list[str]) -> list[dict]:
    """The cached search's rows, in the cached order, shaped like the search query's."""
    if not ids:
        return []
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    text("""SELECT c.data || jsonb_build_object('occurrences',
            jsonb_agg(jsonb_build_object('source_id',o.source_id,'url',c.url)),
            'last_seen_at',c.last_seen_at) FROM job_catalog c
            JOIN job_source_occurrences o ON o.job_id=c.job_id
            WHERE c.job_id=ANY(:ids) AND o.source_id=ANY(:sources)
            GROUP BY c.job_id"""),
                    {"ids": ids, "sources": source_ids},
                )
            )
            .scalars()
            .all()
        )
    order = {job_id: i for i, job_id in enumerate(ids)}
    return sorted(rows, key=lambda row: order.get(row.get("job_id"), len(order)))


async def _search_catalog_db(query: dict, public, live_ids, relevant_of):
    terms = (
        (" ".join(query.get("titles") or []) or query.get("search_query") or "software engineer")
        .lower()
        .split()
    )
    days = query.get("posted_within_days", 30)
    need = int(query.get("max_results") or 10)

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    # ponytail: LIMIT 2000 is a safety ceiling on rows read per search, not a
                    # product cap; page by posted_at if a query ever saturates it.
                    text("""SELECT c.data || jsonb_build_object('occurrences',
            jsonb_agg(jsonb_build_object('source_id',o.source_id,'url',c.url)),
            'last_seen_at',c.last_seen_at) FROM job_catalog c
            JOIN job_source_occurrences o ON o.job_id=c.job_id
            WHERE o.source_id=ANY(:sources) AND c.last_seen_at > :fresh
            AND (c.posted_at IS NULL OR c.posted_at >= :cutoff)
            AND lower(c.title) LIKE ANY(:terms)
            GROUP BY c.job_id ORDER BY c.posted_at DESC NULLS LAST LIMIT 2000"""),
                    {
                        "sources": [s.id for s in public] + live_ids,
                        "terms": ["%" + t + "%" for t in terms if len(t) > 2],
                        "fresh": datetime.now(UTC) - timedelta(days=CATALOG_FRESH_DAYS),
                        "cutoff": datetime.now(UTC) - timedelta(days=days),
                    },
                )
            )
            .scalars()
            .all()
        )
    warnings = []
    relevant = relevant_of(rows)
    if public and len(relevant) < need:
        semaphore = asyncio.Semaphore(6)
        # Gap fill is bounded (<=12 sources, 6 at a time); refresh_source honours its own
        # lease/backoff so repeated shortfalls do not re-hit upstream. The Schedule refreshes
        # the complete employer catalog.
        preferred = [s for s in public if s.id in PREFERRED_SOURCES]

        async def one(source):
            async with semaphore:
                try:
                    return await refresh_source(source)
                except Exception as exc:
                    return [], source.id + ": " + type(exc).__name__

        results = await asyncio.gather(*(one(s) for s in (preferred or public)[:12]))
        rows = rows + [j for jobs, _ in results for j in jobs]
        warnings.extend(w for _, w in results if w)
        relevant = relevant_of(rows)
    # Every row that matches on its own, so a cache hit can redo the merge.
    candidates = [row["job_id"] for row in rows if row.get("job_id") and matches_query(row, query)]
    return relevant, warnings, list(dict.fromkeys(candidates))
