"""Finds the original application page behind a job-board listing.

Aggregators and boards often link out to the employer's applicant tracking
system (Greenhouse, Lever, Workday and so on). Applying there is more
reliable than through the listing page, so before an application is handed to
the browser this follows the listing to that page when it can.

Boards that need the member's own login (LinkedIn, Naukri, Internshala, Indeed) are left
alone: they are applied through in the member's logged-in browser.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from app.services.job_connectors import ATS_HOSTS, is_ats_url
from app.services.public_http import public_get

logger = logging.getLogger(__name__)

# Applied to in the member's own logged-in browser session, never fetched here.
_LOGIN_BOARDS = (
    "linkedin.com",
    "naukri.com",
    "internshala.com",
    "indeed.",
    "foundit.",
    "instahyre.com",
    "glassdoor.",
)
_APPLY_TEXT = re.compile(r"\bapply\b", re.IGNORECASE)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (href, link text)
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
        if tag == "a":
            self._href = None


def ats_link_in(page_html: str, base_url: str) -> str | None:
    """The best employer-ATS link on a page: one labelled "apply" if there is
    one, else the first ATS link."""
    parser = _Links()
    parser.feed(page_html or "")
    candidates = []
    for href, text in parser.links:
        absolute = urljoin(base_url, href)
        if urlsplit(absolute).scheme == "https" and is_ats_url(absolute):
            candidates.append((absolute, text))
    for absolute, text in candidates:
        if _APPLY_TEXT.search(text):
            return absolute
    return candidates[0][0] if candidates else None


async def resolve_original_apply_url(url: str, fetch=public_get) -> str:
    """The employer's ATS page for this listing, or the listing itself when it
    already is one, needs the member's login, or cannot be followed."""
    host = (urlsplit(url).hostname or "").lower()
    if not host or is_ats_url(url) or any(board in host for board in _LOGIN_BOARDS):
        return url
    try:
        response = await fetch(url, max_bytes=1_500_000)
        if response.status_code != 200:
            return url
        return ats_link_in(response.text, url) or url
    except Exception as exc:  # unreachable, blocked or not public: keep the listing
        logger.info("Could not follow listing to its ATS: %s", type(exc).__name__)
        return url


__all__ = ["ATS_HOSTS", "ats_link_in", "resolve_original_apply_url"]
