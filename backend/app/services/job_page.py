"""Full job descriptions from the job's own page, for tailoring and scoring.

Many sources only carry a snippet (hiring posts, Internshala, JobSpy). When
a member opens a saved job whose description is thin, this reads the job
page once:

1. Plain HTTPS through public_get (public addresses only, DNS pinned), after
   robots.txt allows it. The page's JSON-LD JobPosting description wins;
   otherwise its main text.
2. Only if that comes back thin (the page renders with JavaScript), the same
   URL is rendered in Obscura, a lightweight headless browser, when
   OBSCURA_CDP_URL is configured. Obscura runs in its own locked-down
   container and refuses private addresses itself.

Results (including "nothing better") are cached per URL in Redis, so a
popular job is fetched once for everyone. Login-walled or bot-blocking
sites are skipped instead of worked around.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import logging
import re
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from app.core.config import settings

logger = logging.getLogger(__name__)

THIN = 800  # characters: below this a description is a snippet
MAX_TEXT = 12_000
CACHE_DAYS = 7
MISS_HOURS = 24
ROBOTS_AGENT = "CareerCraftJobDiscovery"
# Login walls and sites that block automated readers: never worked around.
SKIP_HOSTS = (
    "linkedin.com", "x.com", "twitter.com", "indeed.com", "naukri.com",
    "glassdoor.com", "glassdoor.co.in", "foundit.in", "facebook.com", "instagram.com",
)  # fmt: skip
_NOISE = ("script", "style", "noscript", "nav", "header", "footer", "form", "svg", "aside")

RENDER_JS = r"""() => {
  const blocks = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s => s.textContent);
  const main = document.querySelector('main, article, [role=main]') || document.body;
  return {blocks, text: main ? main.innerText : ''};
}"""


def clean(text: str) -> str:
    text = html_lib.unescape(text or "")
    if "<" in text and ">" in text:  # JSON-LD descriptions are often HTML
        text = BeautifulSoup(text, "lxml").get_text("\n")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", text)[:MAX_TEXT]


def skipped(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SKIP_HOSTS)


def from_jsonld_blocks(blocks: list[str], url: str) -> str:
    from app.services.job_connectors import jsonld_jobs

    page = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    found = [clean(str(job.get("description") or "")) for job in jsonld_jobs(page, url)]
    return max(found, key=len, default="")


def from_html(page: str, url: str) -> str:
    from app.services.job_connectors import jsonld_jobs

    structured = max(
        (clean(str(job.get("description") or "")) for job in jsonld_jobs(page, url)),
        key=len,
        default="",
    )
    if len(structured) >= THIN:
        return structured
    soup = BeautifulSoup(page, "lxml")
    for tag in soup(list(_NOISE)):
        tag.decompose()
    main = soup.select_one("main, article, [role=main]") or soup.body or soup
    text = clean(main.get_text("\n"))
    return max((structured, text), key=len)


async def robots_allow(url: str) -> bool:
    from app.services.public_http import public_get

    parts = urlsplit(url)
    try:
        robots = await public_get(f"https://{parts.netloc}/robots.txt", max_bytes=200_000)
    except Exception:
        return False
    if robots.status_code == 404:
        return True
    if robots.status_code != 200:
        return False
    policy = RobotFileParser()
    policy.parse(robots.text.splitlines())
    return policy.can_fetch(ROBOTS_AGENT, url)


async def fetch_plain(url: str) -> str:
    from app.services.public_http import public_get

    response = await public_get(url, max_bytes=3_000_000)
    if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
        return ""
    return from_html(response.text, url)


async def fetch_rendered(url: str) -> str:
    """Render in Obscura over CDP. Off unless OBSCURA_CDP_URL is set."""
    if not settings.OBSCURA_CDP_URL:
        return ""
    from playwright.async_api import async_playwright

    headers = (
        {"Authorization": f"Bearer {settings.OBSCURA_CDP_TOKEN}"}
        if settings.OBSCURA_CDP_TOKEN
        else None
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(
            settings.OBSCURA_CDP_URL, headers=headers, timeout=10_000
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2500)
            result = await page.evaluate(RENDER_JS)
        finally:
            await browser.close()
    structured = from_jsonld_blocks(result.get("blocks") or [], url)
    if len(structured) >= THIN:
        return structured
    return max((structured, clean(result.get("text") or "")), key=len)


def _key(url: str) -> str:
    return "jobpage:" + hashlib.sha256(url.encode()).hexdigest()


async def full_description(url: str) -> str | None:
    """The job page's description, or None when nothing usable was found."""
    from app.core import redis_client
    from app.services.job_connectors import canonical_url

    try:
        url = canonical_url(url)
    except ValueError:
        return None
    if skipped(url):
        return None
    redis = redis_client.get_redis()
    cached = await redis.get(_key(url))
    if cached is not None:
        cached = cached.decode() if isinstance(cached, bytes) else cached
        return cached or None
    text = ""
    try:
        if await robots_allow(url):
            text = await fetch_plain(url)
            if len(text) < THIN:
                text = max(text, await fetch_rendered(url), key=len)
    except Exception as exc:
        # Never log the URL's query or the page: just what failed.
        logger.info("job_page_fetch_failed", extra={"error_type": type(exc).__name__})
    good = text if len(text) >= THIN else ""
    ttl = CACHE_DAYS * 86400 if good else MISS_HOURS * 3600
    await redis.set(_key(url), good, ex=ttl)
    return good or None
