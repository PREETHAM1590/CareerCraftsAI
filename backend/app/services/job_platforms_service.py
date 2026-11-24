"""
job_platforms_service.py — LinkedIn and Indeed search via JobSpy.

Indian job boards (Naukri, Foundit, Instahyre, ...) have no public listing API;
see indian_platforms_service.py for why they are reported as unavailable.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

from app.services.india_focus import is_indian_place
from app.services.job_connectors import safe_url

logger = logging.getLogger(__name__)


@dataclass
class JobListing:
    title: str
    company: str
    location: str
    description: str
    job_url: str
    platform: str
    date_posted: str | None = None
    salary: str | None = None


# Sites read by default. JobSpy also supports glassdoor, google, zip_recruiter,
# bayt, naukri and bdjobs; those either block this network (403/406), scrape a
# search engine, or (Naukri) forbid automated access in their terms, so they
# are never read implicitly.
JOBSPY_PLATFORMS = ["linkedin", "indeed"]


# Map common location strings to JobSpy's `country_indeed` enum.  Default is
# "usa" because Indeed defaults to US; any Indian place resolves to "india".
_COUNTRY_MAP = {
    "usa": "usa",
    "us": "usa",
    "united states": "usa",
    "new york": "usa",
    "san francisco": "usa",
    "seattle": "usa",
    "austin": "usa",
    "boston": "usa",
    "india": "india",
    "in": "india",
    "uk": "uk",
    "united kingdom": "uk",
    "london": "uk",
    "manchester": "uk",
    "germany": "germany",
    "berlin": "germany",
    "munich": "germany",
    "france": "france",
    "paris": "france",
    "canada": "canada",
    "toronto": "canada",
    "vancouver": "canada",
    "australia": "australia",
    "sydney": "australia",
    "melbourne": "australia",
    "singapore": "singapore",
    "dubai": "uae",
    "uae": "uae",
}


def _country_from_location(location: str | None) -> str:
    """Map a free-form location string to JobSpy's `country_indeed` enum.

    Uses word-boundary matching so that "Berlin" doesn't match "in" in the
    India key (substring collision).
    """
    if not location:
        return "usa"
    if is_indian_place(location):
        return "india"
    needle = location.lower().strip()
    # Longer keys ("united kingdom") match before shorter ones ("uk").
    for key in sorted(_COUNTRY_MAP.keys(), key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", needle):
            return _COUNTRY_MAP[key]
    return "usa"


def _salary(row) -> str | None:
    """`INR 1200000–1800000 yearly` from JobSpy's amount/currency/interval columns."""
    low, high = row.get("min_amount"), row.get("max_amount")
    if not low and not high:
        return None

    def amount(value):
        try:
            number = float(value)
            return f"{number:.0f}" if number == number else ""  # NaN check
        except (TypeError, ValueError):
            return ""

    span = "–".join(a for a in (amount(low), amount(high)) if a)
    if not span:
        return None
    currency = row.get("currency")
    interval = row.get("interval")
    parts = [str(currency) if currency == currency and currency else "", span]
    parts.append(str(interval) if interval == interval and interval else "")
    return " ".join(p for p in parts if p)


def scrape_jobs(
    search_term: str,
    location: str = "Remote",
    results_wanted: int = 20,
    hours_old: int = 72,
    platforms: list[str] | None = None,
    country: str = "India",
    is_remote: bool = False,
) -> list[JobListing]:
    """Search JobSpy-supported platforms.

    Args:
        search_term: Job title/keywords to search
        location: Location filter; "Remote India" searches India for remote roles
        results_wanted: Max results per platform
        hours_old: Only jobs posted within this many hours
        platforms: Platforms to read (default: JOBSPY_PLATFORMS)
        country: Country for Indeed localization (e.g. "india" -> in.indeed.com)
        is_remote: Only remote roles

    Returns:
        List of JobListing objects from all platforms combined.
    """
    from jobspy import scrape_jobs as jobspy_scrape

    target_platforms = platforms or JOBSPY_PLATFORMS
    if location.strip().lower() == "remote india":
        location, is_remote, country = "India", True, "india"
    try:
        df = jobspy_scrape(
            site_name=target_platforms,
            search_term=search_term,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            country_indeed=country,
            is_remote=is_remote,
            linkedin_fetch_description=True,
        )

        if df is None or df.empty:
            logger.info("JobSpy returned no results for '%s' in '%s'", search_term, location)
            return []

        jobs: list[JobListing] = []
        for _, row in df.iterrows():
            jobs.append(
                JobListing(
                    title=str(row.get("title", "")),
                    company=str(row.get("company", "")),
                    location=str(row.get("location", location)),
                    description=str(row.get("description", ""))[:2000],
                    job_url=safe_url(str(row.get("job_url", ""))),
                    platform=str(row.get("site", "unknown")),
                    date_posted=str(row.get("date_posted", "")) if row.get("date_posted") else None,
                    salary=_salary(row),
                )
            )

        logger.info(
            "JobSpy found %d jobs across %s for '%s'", len(jobs), target_platforms, search_term
        )
        return jobs

    except Exception as exc:
        logger.error("JobSpy scraping failed for '%s': %s", search_term, exc)
        return []


async def scrape_all_platforms(
    search_term: str,
    location: str = "Bangalore",
    results_wanted: int = 20,
    hours_old: int = 72,
    include_indian: bool = True,
    indian_platforms: list[str] | None = None,
    llm=None,
    user_id: str | None = None,
    warnings: list[str] | None = None,
) -> list[JobListing]:
    """LinkedIn/Indeed via JobSpy, plus the Indian-board status.

    Indian boards return no jobs (no public API); with ``include_indian`` their
    unavailability is appended to ``warnings`` instead of passing silently.
    """
    loop = asyncio.get_running_loop()
    country_indeed = _country_from_location(location)
    jobspy_jobs = await loop.run_in_executor(
        None, scrape_jobs, search_term, location, results_wanted, hours_old, None, country_indeed
    )
    if include_indian:
        from app.services.indian_platforms_service import unavailable_notice

        notice = unavailable_notice(indian_platforms)
        logger.info(notice)
        if warnings is not None:
            warnings.append(notice)
    logger.info("Total jobs found: %d (JobSpy)", len(jobspy_jobs))
    return jobspy_jobs
