from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)
# Process-wide bound on live adapter threads. A timed-out synchronous worker keeps
# its thread until it finishes, so this (not the caller's timeout) caps the threads.
# Sized for several concurrent users: JobSpy alone holds a thread for ~40-75s.
_ADAPTER_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="job-source")


# Per-platform budget: no single source may stall a run.
PLATFORM_TIMEOUT_SEC = 25
# LinkedIn via JobSpy takes ~40s for one location; under the 25s default its
# results were always discarded.
_ADAPTER_TIMEOUT_SEC = {"jobspy": 75}

# Catalog jobs this recent can stand in for a live scrape of a default source.
LIVE_SKIP_FRESHNESS = timedelta(days=1)

# Per-adapter upstream fetch size. This bounds one live request, never the result set.
LIVE_FETCH_LIMIT = 50

# Public demo adapter: never reads/writes the shared catalog (unauthenticated callers).
NO_CATALOG = {"open_apis_keyless"}

# Canonical normalized job keys returned to agents.
JOB_KEYS = (
    "job_id",
    "title",
    "company",
    "location",
    "remote",
    "salary_text",
    "url",
    "platform",
    "posted_at",
    "description",
)


def _stable_id(url: str, title: str, company: str, location: str = "") -> str:
    """Same id the catalog gives this URL (job_connectors.normalize), so a job
    seen live and in the catalog is one job."""
    from app.services.job_connectors import url_key

    seed = url_key(url) or f"{title.strip()}|{company.strip()}|{location.strip()}".lower()
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _normalize(raw: dict, platform: str) -> dict:
    from app.services.job_connectors import url_key

    title = str(raw.get("title") or "").strip()
    company = str(raw.get("company") or "").strip()
    raw_url = str(raw.get("job_url") or raw.get("url") or "").strip()
    # Tracking params stripped; http/odd URLs pass through unchanged.
    url = url_key(raw_url) if raw_url.lower().startswith("https://") else raw_url
    location = str(raw.get("location") or "").strip()
    return {
        "job_id": _stable_id(url, title, company, location),
        "title": title,
        "company": company,
        "location": location,
        "remote": str(raw.get("remote", raw.get("work_mode", ""))).strip(),
        "salary_text": str(raw.get("salary_text", raw.get("salary", ""))).strip(),
        "url": url,
        "platform": platform,
        "posted_at": raw.get("posted_at"),
        "description": str(raw.get("description", raw.get("jd_text", ""))),
    }


# ── Platform adapters (all sync, run in threads; lazy imports avoid cycles) ──


def _adapter_open_apis(query: str, location: str, max_results: int) -> list[dict]:
    from app.agents.job_search import _search_open_job_apis

    return _search_open_job_apis(query, location, max_results)


def _adapter_open_apis_keyless(query: str, location: str, max_results: int) -> list[dict]:
    """Same sources as "open_apis", minus the keyed RapidAPI/Adzuna branches —
    for callers (the public demo endpoint) that must never touch a paid
    third-party quota, regardless of what's configured in this environment."""
    from app.agents.job_search import _search_open_job_apis

    return _search_open_job_apis(query, location, max_results, keyless_only=True)


def _adapter_jobspy(query: str, location: str, max_results: int) -> list[dict]:
    from app.agents.job_search import _job_listings_to_dicts
    from app.services.job_platforms_service import _country_from_location, scrape_jobs

    # Left at its default, scrape_jobs hits all 8 JobSpy-supported sites
    # (including glassdoor/zip_recruiter/bayt/naukri, which reliably 403/406
    # from this network) sequentially inside PLATFORM_TIMEOUT_SEC — burning
    # the whole budget on sources that can't succeed and discarding
    # LinkedIn/Indeed's results when the overall call times out as a result.
    # Restrict to the two sites that actually return results here.
    return _job_listings_to_dicts(
        scrape_jobs(
            search_term=query,
            location=location,
            # LinkedIn fetches each posting's page: 25 takes ~40s, 50 overruns
            # _ADAPTER_TIMEOUT_SEC and loses everything.
            results_wanted=min(max_results, 25),
            hours_old=72,
            platforms=["linkedin", "indeed"],
            country=_country_from_location(location),
        )
    )


def _adapter_ats(query: str, location: str, max_results: int) -> list[dict]:
    from app.agents.job_search import _search_public_ats_jobs

    return _search_public_ats_jobs(query, location, max_results, "")


def _adapter_remoteok(query: str, location: str, max_results: int) -> list[dict]:
    from app.agents.job_search import _search_remoteok_jobs

    return _search_remoteok_jobs(query, max_results)


def _adapter_searxng(query: str, location: str, max_results: int) -> list[dict]:
    from app.agents.job_search import _search_searxng_jobs
    from app.core.config import settings

    if not settings.SEARXNG_URL:
        return []
    return _search_searxng_jobs(query, location, max_results)


def _adapter_presets(query: str, location: str, max_results: int) -> list[dict]:
    from app.agents.job_search import _search_via_search_presets

    return _search_via_search_presets(query, location, max_results)


_ADAPTERS: dict[str, Callable[[str, str, int], list[dict]]] = {
    "open_apis": _adapter_open_apis,
    "open_apis_keyless": _adapter_open_apis_keyless,
    "jobspy": _adapter_jobspy,
    "ats": _adapter_ats,
    "remoteok": _adapter_remoteok,
    "searxng": _adapter_searxng,
    "presets": _adapter_presets,
}

DEFAULT_PLATFORMS = [
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "remotive",
    "remoteok",
    "arbeitnow",
    # The employer catalog is mostly US/EU boards; LinkedIn + Indeed (scoped to
    # the member's location) fill the gap, and catalog-first search only calls
    # them when the shared catalog is short. Results are written through.
    "jobspy",
]


async def search_all_platforms(
    query: dict,
    platforms: list[str] | None = None,
    timeout_s: int = 90,
) -> tuple[list[dict], list[str]]:
    """Catalog-first job search with live gap fill, write-through and no result caps.

    Args:
        query: {titles: list[str], locations: list[str], remote: str,
            max_results: int}. ``titles`` is required; locations optional.
            ``max_results`` is the target relevant-job count that triggers
            gap fill, not a cap on what is returned.
        platforms: public families/source ids (served from the shared catalog)
            and/or _ADAPTERS keys (live). Unknown names are skipped with a
            warning. Defaults to DEFAULT_PLATFORMS.
        timeout_s: overall live-search budget; each platform is also capped at
            PLATFORM_TIMEOUT_SEC.

    Returns:
        (normalized deduped jobs, warnings). Platform failures become
        warnings — this function never raises for source errors.
    """
    from app.services.job_catalog import matches_query, search_catalog, write_through
    from app.services.job_connectors import FAMILIES

    selected = platforms or DEFAULT_PLATFORMS
    public = [p for p in selected if p in FAMILIES or ":" in p]
    warnings: list[str] = []
    valid_names = []
    for name in selected:
        if name in public:
            continue
        if name in _ADAPTERS:
            valid_names.append(name)
        else:
            warnings.append(f"unknown platform skipped: {name}")

    need = int(query.get("max_results", 10))
    # 1. Shared catalog first (public sources + jobs other users' live searches stored).
    catalog_jobs: list[dict] = []
    cacheable = [n for n in valid_names if n not in NO_CATALOG]
    if public or cacheable:
        try:
            catalog_jobs, catalog_warnings = await search_catalog(
                query, public, live_platforms=cacheable
            )
            catalog_jobs = [job for job in catalog_jobs if matches_query(job, query)]
            warnings.extend(catalog_warnings)
        except Exception as exc:
            if not valid_names:
                raise
            logger.warning("Catalog read failed: %s", type(exc).__name__)
            warnings.append(f"catalog unavailable: {type(exc).__name__}")
    locations = (
        query.get("locations")
        or ([str(query["location"])] if query.get("location") else [])
        or ["Remote"]
    )
    # The catalog replaces a live search only when (a) the member didn't pick the
    # source themselves, and (b) it holds enough jobs seen in the last day that
    # are in the requested city (remote roles don't cover a city search).
    from app.services.job_catalog import _cities, in_city

    fresh_after = datetime.now(UTC) - LIVE_SKIP_FRESHNESS

    def seen_recently(job: dict) -> bool:
        try:
            return datetime.fromisoformat(str(job["last_seen_at"])) >= fresh_after
        except (KeyError, ValueError):
            return False

    covering = [
        j
        for j in catalog_jobs
        if seen_recently(j) and (in_city(j, locations) or not _cities(locations))
    ]
    if not valid_names or (
        platforms is None and len(cacheable) == len(valid_names) and len(covering) >= need
    ):
        return catalog_jobs, warnings

    # 2. Shortfall: live adapters fill the gap.
    titles = query.get("titles") or []
    queries = (
        list(dict.fromkeys(titles))
        if titles
        else [str(query.get("search_query", "software engineer"))]
    )
    fetch_n = max(need, LIVE_FETCH_LIMIT)

    from app.services import search_cache

    async def run_one(name: str, location: str, q: str) -> tuple[list[dict], bool]:
        """One adapter for a role/city -> (jobs, served_from_cache)."""
        adapter = _ADAPTERS[name]
        timeout = _ADAPTER_TIMEOUT_SEC.get(name, PLATFORM_TIMEOUT_SEC)

        async def compute():
            # Queue time in the bounded executor counts toward the source timeout.
            raw = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    _ADAPTER_EXECUTOR, adapter, q, location, fetch_n
                ),
                timeout=timeout,
            )
            jobs = [_normalize(job, name) for job in (raw or [])]
            # An empty result may be an upstream block or blip; don't pin it.
            return jobs, (json.dumps(jobs, default=str) if jobs else None)

        # Adapters receive each role verbatim; preserve word order in its key.
        key = f"{search_cache.PREFIX}:live:{name}:" + search_cache.query_hash(
            {"q": " ".join(q.lower().split()), "place": location.strip().lower(), "n": fetch_n}
        )
        try:
            if name in NO_CATALOG:
                # Public demo input never populates the shared cache.
                return (await compute())[0], False
            jobs, cached = await search_cache.get_or_compute(
                "live", key, search_cache.LIVE_TTL, compute, wait_s=max(timeout, timeout_s)
            )
            return (json.loads(cached), True) if cached is not None else (jobs, False)
        except Exception as exc:
            logger.warning("Platform %s (%s) failed: %s", name, location, exc)
            warnings.append(f"{name} failed: {type(exc).__name__}")
            return [], False

    # A shared executor bounds synchronous work even after cancellation. The
    # overall budget keeps multi-role/city expansion within the activity deadline;
    # completed sources survive a timeout and missing sources are reported.
    pairs = [(name, location, q) for name in valid_names for location in locations for q in queries]
    tasks = [asyncio.create_task(run_one(n, loc, q)) for n, loc, q in pairs]
    try:
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=max(0, timeout_s))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if pending:
                warnings.append(
                    f"Search time budget reached; {len(pending)} source queries incomplete"
                )
        results = [task.result() if not task.cancelled() else ([], False) for task in tasks]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    per_source = [jobs for jobs, _ in results]

    # 3. Write-through so the next user's search finds these in the catalog.
    # write_through never raises; a failed catalog write must not fail the search.
    by_name: dict[str, list[dict]] = {}
    for (name, _, _), (group, from_cache) in zip(pairs, results, strict=True):
        if not from_cache:
            by_name.setdefault(name, []).extend(group)
    await asyncio.gather(
        *(write_through(g, n) for n, g in by_name.items() if g and n not in NO_CATALOG)
    )

    # Catalog rows go first so they keep their ids; a LinkedIn/Indeed copy of a
    # catalog role (different URL, same company+title+place) folds into it.
    from app.services.job_connectors import merge_duplicates

    jobs = merge_duplicates(catalog_jobs + [job for group in per_source for job in group])
    return [job for job in jobs if matches_query(job, query)], warnings
