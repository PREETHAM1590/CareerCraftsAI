"""Internshala jobs and internships over plain HTTP (no browser, no AI).

Internshala renders its listing cards in the server HTML, so one GET of its
keyword page returns up to ~40 cards. The connector identifies itself
honestly and requests only listing paths without a query string, which its
robots.txt allows (it disallows "/*?*", "/job/search/" and "/api/"). Live
results are cached by the job-search layer (search_cache), so repeated
searches do not re-fetch.

URL shapes (checked 2026-11-24):
  /jobs/keywords-<kw>/                         anywhere
  /jobs/keywords-<kw>/jobs-in-<city>/          one city
  /jobs/keywords-<kw>/work-from-home/          remote
  /internships/keywords-<kw>/internship-in-<city>/  (and the same variants)
An unknown city or keyword silently redirects to a generic page; those
results are dropped rather than shown as matches.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE = "https://internshala.com"
USER_AGENT = "CareerCraftBot/1.0 (+https://careercraftsai.me)"
TIMEOUT = 15
MAX_HTML = 3_000_000

# Internshala's own city slugs for names members commonly type.
CITY_ALIASES = {
    "bengaluru": "bangalore",
    "gurugram": "gurgaon",
    "new delhi": "delhi",
    "delhi ncr": "delhi",
    "bombay": "mumbai",
    "madras": "chennai",
    "calcutta": "kolkata",
    "trivandrum": "thiruvananthapuram",
}
INDIA_CITIES = {
    "india", "bangalore", "mumbai", "delhi", "hyderabad", "pune", "chennai",
    "kolkata", "gurgaon", "noida", "ahmedabad", "jaipur", "chandigarh", "kochi",
    "indore", "lucknow", "coimbatore", "nagpur", "bhubaneswar", "vadodara",
    "surat", "thiruvananthapuram", "mysore", "visakhapatnam", "bhopal", "patna",
    "ghaziabad", "faridabad", "navi mumbai", "thane", "mohali", "dehradun",
}  # fmt: skip
REMOTE_WORDS = {"remote", "work from home", "wfh", "anywhere"}


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60]


def place(location: str | None) -> str | None:
    """'remote', an Internshala city slug, '' for all of India, None if not India."""
    text = (location or "").strip().lower()
    if not text or text in {"india", "anywhere in india"}:
        return ""
    if text in REMOTE_WORDS or "work from home" in text:
        return "remote"
    city = CITY_ALIASES.get(text.split(",")[0].strip(), text.split(",")[0].strip())
    parts = {part.strip() for part in text.split(",")}
    if city in INDIA_CITIES or "india" in parts:
        return "" if city == "india" else slug(city)
    return None


def listing_url(query: str, where: str, internships: bool = False) -> str:
    kind = "internships" if internships else "jobs"
    path = f"/{kind}/keywords-{slug(query)}/"
    if where == "remote":
        path += "work-from-home/"
    elif where:
        path += f"{'internship' if internships else 'jobs'}-in-{where}/"
    return BASE + path


def _same_search(requested: str, final: str, where: str) -> bool:
    """Internshala redirects unknown keywords/cities to a generic page."""
    want, got = urlsplit(requested).path, urlsplit(final).path
    keyword = want.split("/")[2]
    if keyword not in got.split("/"):
        return False
    if where == "remote":
        return "work-from-home" in got
    if where:
        # Allow its own alias redirects (gurugram -> gurgaon) as long as a
        # city filter survives.
        return bool(re.search(r"/(jobs|internship)-in-[a-z0-9-]+/?$", got))
    return True


def posted_at(label: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now(UTC)
    text = (label or "").strip().lower()
    if text in {"just now", "today"} or "hour" in text or "minute" in text:
        return now.date().isoformat()
    match = re.match(r"(\d+|a|an)\s+(day|week|month)s?\s+ago", text)
    if not match:
        return None
    count = 1 if match.group(1) in {"a", "an"} else int(match.group(1))
    days = {"day": 1, "week": 7, "month": 30}[match.group(2)] * count
    return (now - timedelta(days=days)).date().isoformat()


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def parse_listing(html: str, internships: bool = False, now: datetime | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    jobs = []
    kind = "internship" if internships else "job"
    for card in soup.select("div.individual_internship[internshipid]"):
        if card.get("employment_type") != kind:
            continue  # "similar internships" padding on a jobs page, and vice versa
        link = card.select_one("a.job-title-href")
        href = (link.get("href") if link else None) or card.get("data-href") or ""
        url = urljoin(BASE, href)
        if not link or not re.match(r"https://internshala\.com/(job|internship)/detail/", url):
            continue  # ads, promos and external cards have no detail page here
        location = _text(card.select_one(".locations"))
        salary = _text(card.select_one(".stipend")) or _text(
            card.select_one(".row-1-item .desktop")
        )
        skills = [_text(s) for s in card.select(".job_skill")]
        about = _text(card.select_one(".about_job .text"))
        status = card.select_one(".color-labels [class^='status'] span")
        title = _text(link)
        jobs.append(
            {
                "title": f"{title} (Internship)" if internships else title,
                "company": _text(card.select_one(".company-name")),
                "location": location,
                "remote": "remote" if "work from home" in location.lower() else "",
                "salary_text": salary,
                "job_url": url.split("?")[0],
                "posted_at": posted_at(_text(status), now),
                "description": " ".join(
                    part
                    for part in (about, f"Skills: {', '.join(skills)}" if skills else "")
                    if part
                ),
            }
        )
    return jobs


def fetch(url: str) -> tuple[str, str]:
    with httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    ) as client:
        response = client.get(url)
    response.raise_for_status()
    if len(response.content) > MAX_HTML:
        raise ValueError("Internshala page too large")
    return str(response.url), response.text


def search(query: str, location: str, max_results: int) -> list[dict]:
    """Job-search adapter: sync, run in the adapter thread pool."""
    where = place(location)
    if where is None or not slug(query):
        return []  # Internshala lists Indian and remote roles only
    internships = bool(re.search(r"\bintern(ship)?s?\b", query, re.I))
    keyword = re.sub(r"\bintern(ship)?s?\b", " ", query, flags=re.I).strip() or query
    url = listing_url(keyword, where, internships)
    final, html = fetch(url)
    if not _same_search(url, final, where):
        logger.info("Internshala had no listing for %s; skipped", urlsplit(url).path)
        return []
    return parse_listing(html, internships)[:max_results]
