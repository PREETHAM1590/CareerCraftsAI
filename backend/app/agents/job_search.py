import logging

import httpx
from langchain_core.messages import AIMessage

from app.agents.state import AgentState
from app.core.config import settings as app_settings
from app.core.event_bus import emit
from app.core.sync_db import run_coro_sync
from app.services.job_search_service import search_all_platforms

logger = logging.getLogger(__name__)
REMOTEOK_API_URL = "https://remoteok.com/api"

SCORE_PROMPT = """Rate how well this job matches the candidate profile. Return ONLY a number 0-100.

Candidate profile:
{profile}

Job: {title} at {company}
Description: {description}

Score (0-100):"""


def _job_listings_to_dicts(job_listings) -> list[dict]:
    return [
        {
            "title": j.title,
            "company": j.company,
            "location": j.location,
            "description": j.description,
            "job_url": j.job_url,
            "platform": j.platform,
        }
        for j in job_listings
    ]


def _domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc.lower().removeprefix("www.")
        return host.split(":")[0] or "Unknown"
    except Exception:
        return "Unknown"


def _search_searxng_jobs(query: str, location: str, max_results: int) -> list[dict]:
    """Query a self-hosted SearXNG meta-search for real jobs (JSON, no CAPTCHA).

    SearXNG aggregates Google/Bing/DuckDuckGo/Brave results and accepts dork
    operators (site:, OR, -, filetype:). Off unless SEARXNG_URL is configured.
    """
    base = (app_settings.SEARXNG_URL or "").rstrip("/")
    if not base:
        return []
    try:
        resp = httpx.get(
            f"{base}/search",
            params={"q": f"{query} jobs {location}", "format": "json"},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get("results", []) or []
    except Exception as exc:
        logger.warning("SearXNG search failed: %s", exc)
        return []
    jobs: list[dict] = []
    for item in results:
        url = (item or {}).get("url")
        title = (item or {}).get("title")
        if not url or not title:
            continue
        jobs.append(
            {
                "title": title,
                "company": _domain_of(url),
                "location": location,
                "description": (item.get("content") or "")[:2000],
                "job_url": url,
                "platform": "searxng",
            }
        )
        if len(jobs) >= max_results:
            break
    return jobs


def _search_remoteok_jobs(query: str, max_results: int) -> list[dict]:
    terms = {term.lower() for term in query.split() if len(term) > 2}
    response = httpx.get(
        REMOTEOK_API_URL,
        headers={"User-Agent": "CareerCraftAI/1.0"},
        timeout=15,
    )
    response.raise_for_status()
    rows = response.json()
    jobs: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("position"):
            continue
        haystack = " ".join(
            str(row.get(key, "")) for key in ("position", "company", "description", "tags")
        ).lower()
        if terms and not any(term in haystack for term in terms):
            continue
        jobs.append(
            {
                "title": row.get("position") or "Unknown",
                "company": row.get("company") or "Unknown",
                "location": row.get("location") or "Remote",
                "description": row.get("description") or "",
                "job_url": row.get("url") or row.get("apply_url"),
                "platform": "remoteok",
            }
        )
        if len(jobs) >= max_results:
            break
    return jobs


def _query_terms(query: str) -> set[str]:
    stop = {
        "and",
        "for",
        "the",
        "with",
        "remote",
        "hybrid",
        "onsite",
        "entry",
        "level",
        "senior",
        "junior",
        "lead",
        "full",
        "time",
    }
    return {
        term.lower().strip(".,:;()[]")
        for term in query.split()
        if len(term.strip(".,:;()[]")) > 2 and term.lower() not in stop
    }


def _matches_query(job: dict, query: str) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    haystack = " ".join(
        str(job.get(key, "")) for key in ("title", "company", "location", "description", "platform")
    ).lower()
    return any(term in haystack for term in terms)


def _dedupe_jobs(jobs: list[dict], max_results: int) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for job in jobs:
        key = (job.get("job_url") or f"{job.get('company')}::{job.get('title')}").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(job)
        if len(unique) >= max_results:
            break
    return unique


def _strip_html(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


# ---------------------------------------------------------------------------
# Tier-1 free job-board API wrappers (added 2026-06-05)
# All keyless, all JSON, all reliable. Real `job_url` apply links.
# ---------------------------------------------------------------------------


def _search_himalayas_jobs(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    """Himalayas.app — curated remote jobs, no key, JSON API.

    API shape: {"jobs": [...], "totalCount": N, "updatedAt": ..., "comments": ...}
    Job fields: title, excerpt, companyName, companySlug, employmentType,
                locationRestrictions, categories, applicationUrl, etc.
    """
    try:
        r = client.get("https://himalayas.app/jobs/api", params={"limit": max_results * 2})
        payload = r.json() or {}
        items = payload.get("jobs") or []
    except Exception as exc:
        logger.warning("Himalayas API failed: %s", exc)
        return []
    jobs: list[dict] = []
    for j in items:
        title = j.get("title", "")
        company = j.get("companyName", "") or j.get("companySlug", "")
        # description is in 'excerpt' (short) — Himalayas jobs link directly to
        # the company ATS so the apply URL is the canonical link.
        excerpt = j.get("excerpt", "") or ""
        if not _matches_query({"title": title, "description": excerpt[:300]}, query):
            continue
        # Build a sensible location from locationRestrictions
        locs = j.get("locationRestrictions") or []
        location = ", ".join(locs) if locs else "Remote"
        jobs.append(
            {
                "title": title,
                "company": company,
                "location": location,
                "description": _strip_html(excerpt)[:600],
                "job_url": j.get("applicationUrl") or j.get("url", ""),
                "platform": "himalayas",
            }
        )
        if len(jobs) >= max_results:
            break
    return jobs


def _search_workingnomads_jobs(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    """WorkingNomads — remote jobs aggregator, no key.

    Note: their `/api/v2/jobs` endpoint returns 404 as of 2026; we try several
    known path variations and silently skip if none work.  WorkingNomads is
    a low-priority source — Himalayas + 4dayweek + The Muse cover the same
    remote-job market.
    """
    for path in (
        "/api/exposed_jobs/",
        "/api/jobs/",
        "/api/v3/jobs/",
    ):
        try:
            r = client.get(
                f"https://www.workingnomads.com{path}",
                params={"query": query, "limit": max_results * 2},
                timeout=6,
            )
            if r.status_code != 200 or not r.text or r.text.lstrip().startswith("<"):
                continue
            data = r.json()
            items = (
                data if isinstance(data, list) else (data.get("jobs") or data.get("results") or [])
            )
            if not items:
                continue
            jobs: list[dict] = []
            for j in items:
                jobs.append(
                    {
                        "title": j.get("title", ""),
                        "company": j.get("company_name", "")
                        or j.get("companyName", "")
                        or (j.get("company") or {}).get("name", ""),
                        "location": j.get("location", "") or "Remote",
                        "description": _strip_html(j.get("description", ""))[:600],
                        "job_url": j.get("url", "") or j.get("apply_url", ""),
                        "platform": "workingnomads",
                    }
                )
                if len(jobs) >= max_results:
                    break
            if jobs:
                return jobs
        except Exception as exc:
            logger.debug("WorkingNomads %s failed: %s", path, exc)
            continue
    return []


def _search_themuse_jobs(
    client: httpx.Client, query: str, location: str, max_results: int
) -> list[dict]:
    """The Muse — curated professional jobs (US/UK/EU strong). No key for low volume."""
    try:
        params = {"page": 0, "descending": "true"}
        if query:
            params["category"] = query
        r = client.get("https://www.themuse.com/api/public/jobs", params=params)
        items = (r.json() or {}).get("results", []) or []
    except Exception as exc:
        logger.warning("The Muse API failed: %s", exc)
        return []
    jobs: list[dict] = []
    for j in items:
        locs = j.get("locations") or []
        loc_str = ", ".join(locs) if locs else (location or "")
        title = j.get("name", "")
        if not _matches_query({"title": title}, query):
            continue
        jobs.append(
            {
                "title": title,
                "company": (j.get("company") or {}).get("name", ""),
                "location": loc_str,
                "description": _strip_html(j.get("contents", ""))[:600],
                "job_url": j.get("refs", {}).get("landing_page", ""),
                "platform": "themuse",
            }
        )
        if len(jobs) >= max_results:
            break
    return jobs


def _search_authentic_jobs(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    """Authentic Jobs — design / dev / creative, RSS-only but we treat as JSON-friendly."""
    try:
        # Authentic Jobs is RSS. Use hardened parsing for provider responses.
        from defusedxml import ElementTree as ET

        r = client.get(
            "https://authenticjobs.com/api/",
            params={
                "api_key": "",
                "method": "aj.jobs.search",
                "keywords": query,
                "count": max_results * 2,
            },
        )
        root = ET.fromstring(r.text)
    except Exception as exc:
        logger.debug("Authentic Jobs API failed (RSS): %s", exc)
        return []
    jobs: list[dict] = []
    for job in root.iter("item") or root.iter("job") or []:
        title = (job.findtext("title") or job.findtext("jobtitle") or "").strip()
        link = (job.findtext("link") or job.findtext("url") or "").strip()
        company = (job.findtext("company") or "").strip()
        loc = (job.findtext("location") or "").strip()
        desc = (job.findtext("description") or job.findtext("body") or "").strip()
        if not title or not link:
            continue
        jobs.append(
            {
                "title": title,
                "company": company,
                "location": loc or "Remote",
                "description": _strip_html(desc)[:600],
                "job_url": link,
                "platform": "authenticjobs",
            }
        )
        if len(jobs) >= max_results:
            break
    return jobs


def _search_4dayweek_jobs(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    """4dayweek.io — 4-day-workweek jobs. JSON, no key.

    API shape: {"jobs": [{"id", "title", "slug", "company_name", "company_id",
    "work_arrangement", "locations": [{"city", "state", "country"}], ...}]}
    """
    try:
        r = client.get(
            "https://4dayweek.io/api/jobs", params={"q": query, "limit": max_results * 2}
        )
        payload = r.json() or {}
        items = payload.get("jobs") or (payload if isinstance(payload, list) else [])
    except Exception as exc:
        logger.warning("4dayweek API failed: %s", exc)
        return []
    jobs: list[dict] = []
    for j in items:
        title = j.get("title", "")
        if not _matches_query({"title": title}, query):
            continue
        # Build a real location string from the locations array
        locs = j.get("locations") or []
        if locs:
            first = locs[0] or {}
            parts = [first.get("city"), first.get("state"), first.get("country")]
            location = ", ".join(p for p in parts if p) or "Remote"
        else:
            location = j.get("location", "") or "Remote"
        jobs.append(
            {
                "title": title,
                "company": j.get("company_name", "") or j.get("company", ""),
                "location": location,
                "description": _strip_html(j.get("description", ""))[:600],
                "job_url": j.get("url", "") or f"https://4dayweek.io/jobs/{j.get('slug', '')}",
                "platform": "4dayweek",
            }
        )
        if len(jobs) >= max_results:
            break
    return jobs


# ---------------------------------------------------------------------------
# Tier-3 general web search — Google/Bing/DDG via 5 providers (added 2026-06-05)
# Falls back across providers; whichever works first wins.  No key required for
# DuckDuckGo, Firecrawl; Tavily/Brave/SerpAPI are gated on keys.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tier-3.5 strategy search — Google dork library + company career pages
# (added 2026-06-05). Runs the pre-built dork queries from
# ``app.services.search_presets`` against the 7 web-search providers and the
# 16 direct company career pages (10 Indian IT + 6 global AI labs).
# Falls back gracefully — no key required for the keyless providers.
# ---------------------------------------------------------------------------


def _search_via_search_presets(query: str, location: str, max_results: int) -> list[dict]:
    """Build ready-to-fetch URLs from ``search_presets.SEARCH_PRESETS``.

    For each preset whose method is ``fetch`` (RemoteOK, WeWorkRemotely,
    AI-Jobs.net, Turing, Hugging Face), we issue an HTTP GET. For
    ``browser_use`` presets we just return the URL as a lead (the caller
    can choose to scrape it with browser-use). For ``jobspy`` presets we
    return the URL as a lead (JobSpy scrapes it natively).
    """
    from app.services.search_presets import SEARCH_PRESETS, build_url

    out: list[dict] = []
    for preset in SEARCH_PRESETS:
        if len(out) >= max_results:
            break
        url = build_url(preset, q=query, loc=location)
        # Direct-fetch presets: just hit the URL.
        if preset.get("method") == "fetch":
            try:
                with httpx.Client(
                    timeout=10,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (CareerCraft)"},
                ) as c:
                    r = c.get(url)
                r.raise_for_status()
                # RSS / JSON: parse if obvious.
                ctype = r.headers.get("content-type", "").lower()
                if "xml" in ctype or url.endswith(".rss"):
                    # Quick RSS title-link extraction.
                    import re

                    for m in re.finditer(
                        r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>.*?"
                        r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>",
                        r.text,
                        flags=re.S,
                    ):
                        title, link = m.group(1).strip(), m.group(2).strip()
                        if not link or "weworkremotely" not in link and "remoteok" not in link:
                            continue
                        out.append(
                            {
                                "title": title,
                                "company": _domain_of(link),
                                "location": "Remote",
                                "description": "",
                                "job_url": link,
                                "platform": preset["name"].split(" — ")[0].lower(),
                            }
                        )
                        if len(out) >= max_results:
                            break
            except Exception as exc:
                logger.debug("Preset %s failed: %s", preset["name"], exc)
        else:
            # browser_use / jobspy: return the URL as a lead.
            out.append(
                {
                    "title": preset["name"],
                    "company": preset["name"].split(" — ")[0],
                    "location": location or preset.get("region", ""),
                    "description": preset.get("notes", ""),
                    "job_url": url,
                    "platform": preset.get("method", "preset"),
                }
            )
    return out


def _search_open_job_apis(
    query: str, location: str, max_results: int, keyless_only: bool = False
) -> list[dict]:
    """Real job listings from free, key-less JSON APIs (Remotive, Arbeitnow, Jobicy).

    Every returned job carries a real `job_url` apply link. Reliable and fast —
    no scraping, no rate-limited search engines. This is the primary source.

    Set ``keyless_only=True`` to unconditionally skip the keyed RapidAPI/Adzuna
    branches below even when those credentials are configured — used by the
    public, unauthenticated demo endpoint so anonymous traffic can never
    consume the operator's paid third-party API quota.
    """
    jobs: list[dict] = []
    q = (query or "").strip()
    first_term = q.split()[0] if q else "developer"
    terms = [t.lower() for t in q.split() if len(t) > 2]

    def _relevant(text: str) -> bool:
        if not terms:
            return True
        blob = text.lower()
        return any(t in blob for t in terms)

    with httpx.Client(
        timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (CareerCraft)"}
    ) as client:
        # 0a) JSearch (RapidAPI) — aggregates Google for Jobs / LinkedIn / Indeed /
        # Naukri. Best coverage incl. India. Real apply links. Used when key is set.
        if not keyless_only and app_settings.RAPIDAPI_KEY:
            try:
                jq = q or first_term
                if location and location.lower() != "any":
                    jq = f"{jq} in {location}"
                r = client.get(
                    "https://jsearch.p.rapidapi.com/search",
                    params={"query": jq, "page": "1", "num_pages": "1", "date_posted": "month"},
                    headers={
                        "X-RapidAPI-Key": app_settings.RAPIDAPI_KEY,
                        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
                    },
                )
                for j in (r.json().get("data") or [])[: max_results * 2]:
                    loc = ", ".join(
                        p
                        for p in (j.get("job_city"), j.get("job_state"), j.get("job_country"))
                        if p
                    )
                    jobs.append(
                        {
                            "title": j.get("job_title", ""),
                            "company": j.get("employer_name", ""),
                            "location": loc or "—",
                            "description": _strip_html(j.get("job_description", ""))[:600],
                            "job_url": j.get("job_apply_link", ""),
                            "platform": "jsearch",
                        }
                    )
            except Exception as exc:
                logger.warning("JSearch API failed: %s", exc)

        # 0b) Adzuna — free key, strong India coverage. Country code in path.
        if not keyless_only and app_settings.ADZUNA_APP_ID and app_settings.ADZUNA_APP_KEY:
            try:
                country = "in" if (location or "").lower() in ("", "any", "india") else "gb"
                r = client.get(
                    f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                    params={
                        "app_id": app_settings.ADZUNA_APP_ID,
                        "app_key": app_settings.ADZUNA_APP_KEY,
                        "results_per_page": max_results,
                        "what": q or first_term,
                        "content-type": "application/json",
                    },
                )
                for j in r.json().get("results") or []:
                    jobs.append(
                        {
                            "title": j.get("title", ""),
                            "company": (j.get("company") or {}).get("display_name", ""),
                            "location": (j.get("location") or {}).get("display_name", ""),
                            "description": _strip_html(j.get("description", ""))[:600],
                            "job_url": j.get("redirect_url", ""),
                            "platform": "adzuna",
                        }
                    )
            except Exception as exc:
                logger.warning("Adzuna API failed: %s", exc)

        # 1) Remotive — supports server-side search.
        try:
            r = client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": q or first_term, "limit": max_results},
            )
            for j in (r.json().get("jobs") or [])[: max_results * 2]:
                jobs.append(
                    {
                        "title": j.get("title", ""),
                        "company": j.get("company_name", ""),
                        "location": j.get("candidate_required_location") or "Remote",
                        "description": _strip_html(j.get("description", ""))[:600],
                        "job_url": j.get("url", ""),
                        "platform": "remotive",
                    }
                )
        except Exception as exc:
            logger.warning("Remotive API failed: %s", exc)

        # 2) Arbeitnow — recent ATS-sourced board; filter client-side by query.
        try:
            r = client.get("https://www.arbeitnow.com/api/job-board-api")
            for j in r.json().get("data") or []:
                title = j.get("title", "")
                tags = " ".join(j.get("tags") or [])
                if not _relevant(f"{title} {tags} {j.get('description', '')[:300]}"):
                    continue
                jobs.append(
                    {
                        "title": title,
                        "company": j.get("company_name", ""),
                        "location": j.get("location") or ("Remote" if j.get("remote") else ""),
                        "description": _strip_html(j.get("description", ""))[:600],
                        "job_url": j.get("url", ""),
                        "platform": "arbeitnow",
                    }
                )
        except Exception as exc:
            logger.warning("Arbeitnow API failed: %s", exc)

        # 3) Jobicy — remote jobs feed, tag-filtered.
        try:
            r = client.get(
                "https://jobicy.com/api/v2/remote-jobs",
                params={"count": max_results, "tag": first_term},
            )
            for j in (r.json() or {}).get("jobs", []) or []:
                jobs.append(
                    {
                        "title": j.get("jobTitle", ""),
                        "company": j.get("companyName", ""),
                        "location": j.get("jobGeo") or "Remote",
                        "description": _strip_html(j.get("jobExcerpt", ""))[:600],
                        "job_url": j.get("url", ""),
                        "platform": "jobicy",
                    }
                )
        except Exception as exc:
            logger.warning("Jobicy API failed: %s", exc)

        # 4) Himalayas — curated remote jobs (no key, JSON).
        try:
            for j in _search_himalayas_jobs(client, q or first_term, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.warning("Himalayas wrapper failed: %s", exc)

        # 5) Working Nomads — remote jobs aggregator (no key).
        try:
            for j in _search_workingnomads_jobs(client, q or first_term, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.warning("WorkingNomads wrapper failed: %s", exc)

        # 6) The Muse — curated professional jobs (US/UK/EU strong).
        try:
            for j in _search_themuse_jobs(client, q or first_term, location, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.warning("The Muse wrapper failed: %s", exc)

        # 7) 4dayweek.io — 4-day-workweek jobs (no key).
        try:
            for j in _search_4dayweek_jobs(client, q or first_term, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.warning("4dayweek wrapper failed: %s", exc)

        # 8) Authentic Jobs — design/dev/creative (RSS, no key).
        try:
            for j in _search_authentic_jobs(client, q or first_term, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.debug("Authentic Jobs wrapper failed: %s", exc)

        # 9) AI-Jobs.net — pure-AI/ML job board, no key, HTML scraping
        try:
            for j in _search_ai_jobs_net(client, q or first_term, location, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.debug("AI-Jobs.net wrapper failed: %s", exc)

        # 10) Turing — remote developer jobs, JSON API, no key for public list
        try:
            for j in _search_turing_jobs(client, q or first_term, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.debug("Turing wrapper failed: %s", exc)

        # 11) Hugging Face Jobs — AI/ML community board, JSON API, no key
        try:
            for j in _search_huggingface_jobs(client, q or first_term, max_results):
                jobs.append(j)
        except Exception as exc:
            logger.debug("Hugging Face Jobs wrapper failed: %s", exc)

    # Keep only jobs with a real apply link, then dedupe.
    jobs = [j for j in jobs if j.get("job_url") and j.get("title")]
    return _dedupe_jobs(jobs, max_results)


def _search_ai_jobs_net(
    client: httpx.Client, query: str, location: str, max_results: int
) -> list[dict]:
    """AI-Jobs.net — pure-AI/ML job board, no key required.

    Public HTML at https://ai-jobs.net/jobs/?q={q}.  Parses the JSON-LD block
    embedded in each job page; falls back to the public job listing HTML.
    """
    out: list[dict] = []
    try:
        r = client.get(
            "https://ai-jobs.net/jobs/",
            params={"q": query, "country": location} if location else {"q": query},
            headers={"User-Agent": "CareerCraftAI/1.0", "Accept": "text/html"},
            timeout=15,
        )
        if r.status_code != 200:
            return out
    except Exception as exc:
        logger.debug("AI-Jobs.net GET failed: %s", exc)
        return out

    # Each job card on ai-jobs.net is a <article id="job-{id}">; the URL is
    # inside the <a class="job-title"> link.
    import re

    pattern = re.compile(
        r'<a[^>]+class="[^"]*job-title[^"]*"[^>]+href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(r.text):
        url, title = m.group(1), m.group(2).strip()
        if not title or len(title) < 3:
            continue
        out.append(
            {
                "title": title,
                "company": "—",  # ai-jobs.net shows company on detail page only
                "location": location or "Remote",
                "description": "",
                "job_url": f"https://ai-jobs.net{url}" if url.startswith("/") else url,
                "platform": "ai-jobs.net",
            }
        )
        if len(out) >= max_results:
            break
    return out


def _search_turing_jobs(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    """Turing.com — public jobs page, HTML scraping (no key required for list)."""
    out: list[dict] = []
    try:
        r = client.get(
            "https://www.turing.com/jobs/search",
            params={"q": query},
            headers={"User-Agent": "CareerCraftAI/1.0"},
            timeout=15,
        )
        if r.status_code != 200:
            return out
    except Exception as exc:
        logger.debug("Turing GET failed: %s", exc)
        return out

    import re

    # Turing public search results are in <a href="/jobs/{slug}"> tags
    pattern = re.compile(
        r'<a[^>]+href="(/jobs/[^"]+)"[^>]*>\s*<[^>]*>\s*([^<]{4,150}?)\s*<',
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(r.text):
        url, title = m.group(1), m.group(2).strip()
        if not title or "turing" in title.lower():
            continue
        out.append(
            {
                "title": title,
                "company": "Turing client",
                "location": "Remote",
                "description": "",
                "job_url": f"https://www.turing.com{url}",
                "platform": "turing",
            }
        )
        if len(out) >= max_results:
            break
    return out


def _search_huggingface_jobs(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    """Hugging Face Jobs board — public JSON at https://huggingface.co/api/jobs.

    No key required. Returns a list of community-posted job listings.
    """
    out: list[dict] = []
    try:
        r = client.get(
            "https://huggingface.co/api/jobs",
            params={"full": "true"},
            headers={"User-Agent": "CareerCraftAI/1.0"},
            timeout=15,
        )
        if r.status_code != 200:
            return out
        items = r.json() or []
    except Exception as exc:
        logger.debug("Hugging Face Jobs GET failed: %s", exc)
        return out

    terms = [t.lower() for t in query.split() if len(t) > 2]
    for j in items:
        if not isinstance(j, dict):
            continue
        title = j.get("title", "") or ""
        company = (j.get("company") or {}).get("name", "—")
        loc = (j.get("location") or {}).get("name", "Remote")
        if terms:
            blob = f"{title} {company} {loc}".lower()
            if not any(t in blob for t in terms):
                continue
        out.append(
            {
                "title": title,
                "company": company,
                "location": loc,
                "description": (j.get("description") or "")[:600],
                "job_url": f"https://huggingface.co/jobs/{j.get('id', '')}",
                "platform": "huggingface",
            }
        )
        if len(out) >= max_results:
            break
    return out


REQUIRED_CTX = ["titles"]
SCORE_BATCH_SIZE = 20
SAVE_MIN_SCORE = 50

# Legacy job-board names accepted by the API mapped onto search adapters.
# JobSpy aggregates linkedin/indeed/glassdoor/naukri-style boards; unknown
# names are dropped with a warning by the service layer.
LEGACY_PLATFORM_MAP = {
    "linkedin": "jobspy",
    "indeed": "jobspy",
    "glassdoor": "jobspy",
    "naukri": "jobspy",
    "shine": "open_apis",
    "freshersworld": "open_apis",
    # The retired multi-ATS fan-out now reads the same catalog sources.
    "ats": ("greenhouse", "lever", "ashby", "smartrecruiters", "workday", "bamboohr"),
}


def _normalize_platforms(platforms: list[str] | None) -> list[str] | None:
    if not platforms:
        return None
    mapped: list[str] = []
    for name in platforms:
        key = (name or "").strip().lower()
        if not key:
            continue
        target = LEGACY_PLATFORM_MAP.get(key, key)
        mapped.extend(target if isinstance(target, tuple) else (target,))
    seen = list(dict.fromkeys(mapped))
    return seen or None


def _persist_saved_jobs(user_id: str, scored: list[dict]) -> int:
    """Persist matches with score >= SAVE_MIN_SCORE as saved applications.

    Idempotent per user on the canonical job URL *and* on the role (company +
    title + place, see `role_key`), so the same job reached through a tracking
    URL or another board is not saved twice. Soft-deleted rows count, so a
    removed job is not re-added. Returns the number of newly created rows.
    Failures propagate to the caller, which degrades to a warning (search
    results are still returned).
    """
    from sqlalchemy import select

    from app.core.sync_db import _get_sync_factory, _to_uuid
    from app.models.db import JobApplication
    from app.services.job_connectors import posted, role_key, url_key

    candidates = [
        j for j in scored if (j.get("match_score") or 0) >= SAVE_MIN_SCORE and j.get("url")
    ]
    if not candidates:
        return 0
    factory = _get_sync_factory()
    saved = 0
    with factory() as session:
        # ponytail: check-then-insert, no DB unique index — two concurrent runs
        # for one user can still race; add a partial unique index once existing
        # duplicates are cleaned up.
        rows = session.execute(
            select(
                JobApplication.job_url,
                JobApplication.company,
                JobApplication.role,
                JobApplication.location,
            )
            .where(JobApplication.user_id == _to_uuid(user_id))
            .execution_options(include_deleted=True)
        ).all()
        seen_urls = {url_key(r.job_url) for r in rows if r.job_url}
        seen_roles = {
            role_key({"company": r.company, "title": r.role, "location": r.location or ""})
            for r in rows
            if r.company not in (None, "", "Unknown") and r.role not in (None, "", "Unknown")
        }
        for job in candidates:
            url = url_key(job["url"])
            role = role_key(job) if job.get("company") and job.get("title") else None
            if url in seen_urls or (role and role in seen_roles):
                continue
            seen_urls.add(url)
            if role:
                seen_roles.add(role)
            red_flags = job.get("red_flags") or []
            session.add(
                JobApplication(
                    user_id=_to_uuid(user_id),
                    company=job.get("company", "") or "Unknown",
                    role=job.get("title", "") or "Unknown",
                    location=job.get("location"),
                    job_url=job["url"],
                    jd_text=(job.get("description") or "")[:4000],
                    match_score=job.get("match_score"),
                    status="saved",
                    source=job.get("platform"),
                    posted_at=posted(job.get("posted_at")),
                    notes=("; ".join(red_flags[:3])) if red_flags else None,
                )
            )
            saved += 1
        session.commit()
    return saved


def job_search_agent_node(state: AgentState) -> AgentState:
    """Public discovery and selected-resume matching, logged by the existing harness."""
    from app.services.job_matching import rank_jobs

    ctx = state.get("context", {})
    if not ctx.get("titles") and not ctx.get("search_query"):
        return {**state, "status": "failed", "error": "missing: titles (or search_query)"}
    user_id, run_id = state["user_id"], state["run_id"]
    query = {
        **ctx,
        "titles": ctx.get("titles") or [ctx.get("search_query", "software engineer")],
        "locations": ctx.get("locations") or [ctx.get("location", "Remote")],
        "max_results": int(ctx.get("max_results", 10)),
    }
    try:
        emit(run_id, "thinking", {"step": "search", "message": "Searching public job sources"})
        jobs, warnings = run_coro_sync(
            search_all_platforms(query, _normalize_platforms(ctx.get("platforms")))
        )
        matches, ranking_warnings = run_coro_sync(rank_jobs(user_id, query, jobs))
        warnings.extend(ranking_warnings)
        try:
            saved = _persist_saved_jobs(user_id, matches)
        except Exception as exc:
            logger.warning("job_persistence_failed: %s", type(exc).__name__)
            saved = 0
            warnings.append("Matches found, but saved applications are unavailable")
        result = {
            "matches": matches,
            "top_pick_id": matches[0]["job_id"] if matches else None,
            "total_found": len(jobs),
            "saved_count": saved,
            "warnings": warnings,
        }
        emit(run_id, "complete", {"result": {"total_found": len(jobs), "saved_count": saved}})
        return {
            **state,
            "status": "completed",
            "result": result,
            "messages": state.get("messages", []) + [AIMessage(content=f"Found {len(jobs)} jobs.")],
        }  # noqa: E501
    except Exception as exc:
        logger.warning(
            "job_search_failed: %s",
            type(exc).__name__,
            extra={"run_id": run_id, "error_type": type(exc).__name__},
        )  # noqa: E501
        return {
            **state,
            "status": "failed",
            "error": "Job search unavailable or selected resume no longer exists",
        }  # noqa: E501
