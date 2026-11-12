"""
indian_platforms_service.py — status of Indian job boards that cannot be read.

Naukri, Foundit, Instahyre, Cutshort, Shine, Internshala and the rest offer no
public listing API. Reading them means scraping behind bot protection or a
login, which their terms forbid, and server-side browser automation is retired
(see browser_control_service). Instead of returning a silent empty list, every
entry point here reports the board as unavailable with the reason, and points
to what does cover Indian employers: the public catalog (Greenhouse, Lever,
Ashby, SmartRecruiters, Workday India boards, Keka, Freshteam, Kula, Zoho
Recruit), Himalayas' India feed, and LinkedIn/Indeed India via JobSpy.
"""

from __future__ import annotations

import logging

from app.services.job_platforms_service import JobListing

logger = logging.getLogger(__name__)

# key -> (display name, home page, why it is not read)
_NO_API = "no public listing API; terms forbid automated access"
INDIAN_PLATFORMS: dict[str, dict[str, str]] = {
    "naukri": {"name": "Naukri", "url": "https://www.naukri.com", "reason": _NO_API},
    "foundit": {
        "name": "Foundit (Monster India)",
        "url": "https://www.foundit.in",
        "reason": _NO_API + "; bot protection (HTTP 403)",
    },
    "instahyre": {
        "name": "Instahyre",
        "url": "https://www.instahyre.com",
        "reason": "only an undocumented internal API; site behind a bot challenge",
    },
    "cutshort": {"name": "Cutshort", "url": "https://cutshort.io", "reason": _NO_API},
    "hirect": {"name": "Hirect", "url": "https://www.hirect.in", "reason": "app-only; " + _NO_API},
    "internshala": {
        "name": "Internshala",
        "url": "https://internshala.com",
        "reason": "robots.txt disallows search and /api/; " + _NO_API,
    },
    "shine": {"name": "Shine", "url": "https://www.shine.com", "reason": _NO_API},
    "iimjobs": {"name": "iimjobs", "url": "https://www.iimjobs.com", "reason": _NO_API},
    "hirist": {"name": "Hirist", "url": "https://www.hirist.tech", "reason": _NO_API},
    "timesjobs": {
        "name": "TimesJobs",
        "url": "https://www.timesjobs.com",
        "reason": "unreachable from the server; " + _NO_API,
    },
    "apna": {"name": "Apna", "url": "https://apna.co", "reason": _NO_API},
    "techgig": {"name": "TechGig", "url": "https://www.techgig.com", "reason": _NO_API},
    "unstop": {"name": "Unstop", "url": "https://unstop.com", "reason": _NO_API},
    "ambitionbox": {
        "name": "AmbitionBox Jobs",
        "url": "https://www.ambitionbox.com",
        "reason": _NO_API,
    },
    "freshersworld": {
        "name": "Freshersworld",
        "url": "https://www.freshersworld.com",
        "reason": _NO_API,
    },
}
COVERAGE = (
    "Indian employers are searched through their public careers boards and "
    "LinkedIn/Indeed India instead"
)


class PlatformUnavailable(RuntimeError):
    """The board cannot be read lawfully from the server."""


def unavailable_notice(platforms: list[str] | None = None) -> str:
    names = [
        INDIAN_PLATFORMS[p]["name"] for p in platforms or INDIAN_PLATFORMS if p in INDIAN_PLATFORMS
    ]
    return f"{', '.join(names)} not searched (no public API). {COVERAGE}."


def platform_status() -> list[dict[str, str | bool]]:
    """Per-board availability, for status pages and the source health API."""
    return [
        {"key": key, "name": p["name"], "url": p["url"], "available": False, "reason": p["reason"]}
        for key, p in INDIAN_PLATFORMS.items()
    ]


async def scrape_indian_platform(
    llm=None,
    user_id: str | None = None,
    platform: str = "naukri",
    search_term: str = "",
    location: str = "",
    results_wanted: int = 10,
) -> list[JobListing]:
    """Always raises PlatformUnavailable with the reason the board is not read."""
    info = INDIAN_PLATFORMS.get(platform)
    if info is None:
        raise PlatformUnavailable(f"Unknown platform: {platform}")
    raise PlatformUnavailable(f"{info['name']} is not searched: {info['reason']}")


async def scrape_all_indian_platforms(
    llm=None,
    user_id: str | None = None,
    search_term: str = "",
    location: str = "",
    results_wanted: int = 10,
    platforms: list[str] | None = None,
) -> list[JobListing]:
    """Raises PlatformUnavailable naming every requested board (none can be read)."""
    raise PlatformUnavailable(unavailable_notice(platforms))


async def search_google_jobs(
    llm=None,
    user_id: str | None = None,
    search_term: str = "",
    location: str = "India",
    results_wanted: int = 15,
    live_browser: bool = False,
    run_id: str | None = None,
) -> list[JobListing]:
    """Google Jobs has no public API and Google's terms forbid automated queries,
    so the former headless-browser scrape is gone. Raises PlatformUnavailable."""
    raise PlatformUnavailable(
        "Google Jobs is not searched (no public API; automated queries are not permitted). "
        + COVERAGE
        + "."
    )
