"""Jobs that people announce in posts on X and LinkedIn.

Two catalog source families, each fetched once per role group for everyone
(job_sources.json), never per member search:

* ``x_hiring``: X's official API v2 recent search (pay per post read,
  X_BEARER_TOKEN). One page per refresh bounds the cost.
* ``linkedin_posts``: public linkedin.com/posts pages as indexed by search
  engines, through our self-hosted SearXNG (SEARXNG_URL). LinkedIn itself
  is never contacted and no member's LinkedIn account is used.

Every post is untrusted text. It is turned into a job only when it reads as
an employer hiring (not someone looking for work) and does not look like a
scam (fees, deposits, Telegram/WhatsApp-only contact, crypto, "earn per
day"). Links are never followed; an employer careers/ATS link from the post
becomes the job URL, otherwise the post itself is the link. Each job credits
the post it came from.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

import httpx

from app.core.config import settings

X_SEARCH = "https://api.x.com/2/tweets/search/recent"
X_HIRING_TERMS = (
    '"we\'re hiring" OR "we are hiring" OR "is hiring" OR "now hiring" OR "join our team"'
    ' OR "hiring for" OR #hiring'
)
LINKEDIN_HIRING_TERMS = 'hiring OR "we\'re hiring" OR "join our team"'

HIRING = re.compile(
    r"(?:#hiring\b|\b(?:we(?:'|’)?re hiring|we are hiring|(?:is|are|now) hiring"
    r"|hiring (?:for|an?|our)\b|join (?:our|my|the) (?:\w+ )?team|open (?:roles?|positions?)"
    r"|job openings?|vacanc(?:y|ies)))",
    re.I,
)
# "Looking for a ..." alone reads the same from an employer and a job seeker:
# it counts as hiring only next to "we"/"our"/"team".
LOOKING = re.compile(r"\blooking for (?:an?|experienced|talented|passionate) ", re.I)
EMPLOYER_VOICE = re.compile(r"\b(?:we|we(?:'|’)re|our|us|my team|the team)\b", re.I)
SEEKER = re.compile(
    r"(#?open ?to ?work|\bi(?:'|’)?m (?:actively |currently )?(?:looking|searching|seeking)\b"
    r"|\bi am (?:actively |currently )?(?:looking|searching|seeking)\b"
    r"|\bi (?:have|bring) \d+\+? years?\b|seeking (?:a |new )?(?:job|role|opportunit)"
    r"|looking for (?:new )?(?:job )?opportunities|hire me\b|please refer me|need a referral)",
    re.I,
)
SCAM = re.compile(
    r"(registration fee|processing fee|joining fee|training fee|application fee|security deposit"
    r"|refundable deposit|pay (?:a |the )?(?:small )?fee|telegram|t\.me/|wa\.me/"
    r"|whatsapp(?:[- ]only| me| number| us)|(?:apply|contact|message|reach|dm)(?: us| me)?"
    r"(?: only)? (?:via|on|through) whatsapp|crypto|usdt|bitcoin|forex"
    r"|earn (?:rs\.?|₹|\$|inr)?\s?\d[\d,]*\s?(?:/|per |a )(?:day|hour|week)"
    r"|no interview|guaranteed (?:job|placement|income))",
    re.I,
)
ROLE = re.compile(
    r"\b((?:senior|sr\.?|junior|jr\.?|lead|staff|principal|associate|entry[- ]level)\s+)?"
    r"((?:software|backend|back[- ]end|frontend|front[- ]end|full[- ]?stack|data|machine learning"
    r"|ml|ai|devops|cloud|mobile|android|ios|flutter|react|python|java|golang|node(?:\.js)?|qa"
    r"|test|security|platform|site reliability|product|ux|ui|ux/ui|ui/ux|graphic|marketing|sales"
    r"|business|hr|content)\s+)"
    r"(engineers?|developers?|scientists?|analysts?|designers?|managers?|interns?|architects?"
    r"|executives?|writers?|specialists?)\b"
    r"|\b(sdes?(?:[- ](?:i{1,3}|[123]))?"
    r"|(?:software|data|ml|ai|product|design)(?: engineering| science)? interns?)\b",
    re.I,
)
_UPPER = {"ml": "ML", "ai": "AI", "ui": "UI", "ux": "UX", "qa": "QA", "ios": "iOS", "hr": "HR",
          "ux/ui": "UX/UI", "ui/ux": "UI/UX", "sde": "SDE", "node.js": "Node.js"}  # fmt: skip
_NOT_COMPANY = {
    "we", "our", "i", "my", "the", "team", "hiring", "remote", "now", "this", "you", "us",
    "join", "apply", "dm", "hey", "hi", "hello", "excited", "big", "great", "urgent", "we're",
}  # fmt: skip
_COMPANY = [
    re.compile(r"(?:^|[\s(])@([A-Za-z0-9_]{2,30})\s+(?:is|are)\s+(?:now\s+)?hiring"),
    re.compile(r"\b([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})\s+(?:is|are)\s+(?:now\s+)?hiring"),
    re.compile(r"\bhiring\b[^.\n]{0,80}?\b(?:at|@)\s+@?([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})"),
    re.compile(r"\bjoin\s+(?:us\s+at\s+)?@?([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})"),
]
_PLACES = {
    "bangalore", "bengaluru", "mumbai", "delhi", "new delhi", "hyderabad", "pune", "chennai",
    "kolkata", "gurgaon", "gurugram", "noida", "ahmedabad", "jaipur", "kochi", "india",
    "san francisco", "new york", "seattle", "austin", "boston", "london", "berlin",
    "toronto", "singapore", "dubai", "amsterdam", "paris", "sydney", "united states",
    "usa", "uk", "canada", "germany", "europe",
}  # fmt: skip
_SOCIAL_HOSTS = (
    "x.com", "twitter.com", "t.co", "linkedin.com", "lnkd.in", "t.me", "wa.me",
    "whatsapp.com", "telegram.me", "bit.ly", "tinyurl.com", "instagram.com", "facebook.com",
)  # fmt: skip
_URL = re.compile(r"https://[^\s<>\"')\]]+")
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


def _title(match: re.Match) -> str:
    words = re.sub(r"\s+", " ", match.group(0).strip()).split(" ")
    out = []
    for i, word in enumerate(words):
        low = word.lower().rstrip(".")
        if i == len(words) - 1 and low.endswith("s") and low not in {"ios", "sales"}:
            low = low[:-1]
        roman = re.fullmatch(r"i{1,3}", low)
        upper = low.startswith("sde") or roman
        out.append(_UPPER.get(low) or (low.upper() if upper else low.capitalize()))
    return " ".join(out)


def role(text: str) -> str | None:
    for match in ROLE.finditer(text):
        title = _title(match)
        if len(title.split()) >= 2 or title.startswith("SDE"):
            return title
    return None


def company(text: str) -> str | None:
    for pattern in _COMPANY:
        for match in pattern.finditer(text):
            name = match.group(1).strip(" .-")
            if name.split()[0].lower().strip("'’") not in _NOT_COMPANY and len(name) >= 2:
                return ("@" + name) if pattern is _COMPANY[0] else name
    return None


def location(text: str) -> str:
    low = text.lower()
    remote = bool(re.search(r"\b(remote|work from home|wfh)\b", low))
    place = next(
        (
            p
            for p in sorted(_PLACES, key=len, reverse=True)
            if re.search(rf"\b{re.escape(p)}\b", low)
        ),
        None,
    )
    if place:
        name = place.upper() if place in {"usa", "uk"} else place.title()
        return f"Remote / {name}" if remote else name
    return "Remote" if remote else ""


# A specific posting on an ATS has an id in its path (/jobs/4123456,
# /acme/1b2c3d4e-...); a bare /careers page is shared by every role.
_POSTING_ID = re.compile(r"\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}", re.I)


def apply_link(links: list[str]) -> str | None:
    """A specific posting on a known ATS from the post, or None.

    Any other host is untrusted (a post can link a look-alike or phishing
    site), and a generic careers page would give different roles one URL,
    so both fall back to the post itself."""
    from app.services.job_connectors import ATS_HOSTS

    for link in links:
        try:
            parts = urlsplit(link)
            host = (parts.hostname or "").lower()
        except ValueError:
            continue  # malformed link in untrusted text
        if parts.scheme != "https" or not host or parts.username or parts.password:
            continue
        if len(link) > 2000:
            continue  # never cut a URL: a truncated one is broken
        if any(host == h or host.endswith("." + h) for h in ATS_HOSTS) and _POSTING_ID.search(
            parts.path
        ):
            return link
    return None


def post_to_job(
    text: str,
    post_url: str,
    poster: str,
    network: str,
    posted_at: str | None = None,
    links: list[str] | None = None,
) -> dict | None:
    """One job from one post, or None when it isn't a genuine hiring post."""
    text = re.sub(r"\s+", " ", text or "").strip()[:5000]
    hiring = HIRING.search(text) or (LOOKING.search(text) and EMPLOYER_VOICE.search(text))
    if not text or not hiring or SEEKER.search(text) or SCAM.search(text):
        return None
    title = role(text)
    if not title:
        return None
    links = [*(links or []), *(u.rstrip(".,;:!?") for u in _URL.findall(text))]
    employer = company(text)
    credit = f"Posted by {poster} on {network}: {post_url}"
    return {
        "id": post_url,
        "title": title,
        "company": employer or poster,
        "location": location(text),
        "url": apply_link(links) or post_url,
        "posted_at": posted_at,
        "description": f"{text}\n\n{credit}",
    }


# ── X ───────────────────────────────────────────────────────────


def x_query(roles: str) -> str:
    return f"({roles}) ({X_HIRING_TERMS}) -is:retweet -is:reply lang:en"


def parse_x(data: dict) -> list[dict]:
    users = {u.get("id"): u for u in (data.get("includes") or {}).get("users") or []}
    jobs = []
    for post in data.get("data") or []:
        user = users.get(post.get("author_id")) or {}
        handle = str(user.get("username") or "")
        post_id = str(post.get("id") or "")
        if not _HANDLE.fullmatch(handle) or not post_id.isdigit():
            continue
        links = [
            str(u.get("unwound_url") or u.get("expanded_url") or "")
            for u in (post.get("entities") or {}).get("urls") or []
        ]
        job = post_to_job(
            str(post.get("text") or ""),
            f"https://x.com/{handle}/status/{post_id}",
            f"@{handle}",
            "X",
            post.get("created_at"),
            [link for link in links if link],
        )
        if job:
            jobs.append(job)
    return jobs


async def fetch_x(roles: str) -> list[dict]:
    from app.services.public_http import public_get

    if not settings.X_BEARER_TOKEN:
        return []  # optional source, off: not a failure
    if not roles:
        raise ValueError("X hiring source needs role terms")
    params = {
        "query": x_query(roles),
        "max_results": max(10, min(100, settings.X_HIRING_POSTS_PER_REFRESH)),
        "tweet.fields": "created_at,entities,author_id,lang",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    response = await public_get(
        f"{X_SEARCH}?{urlencode(params)}",
        headers={"Authorization": f"Bearer {settings.X_BEARER_TOKEN}"},
    )
    response.raise_for_status()
    # One page per refresh: X bills every post returned.
    return parse_x(response.json())


# ── LinkedIn posts through SearXNG ──────────────────────────────

_LINKEDIN_POST = re.compile(r"^https://(?:www\.|[a-z]{2}\.)?linkedin\.com/posts/[A-Za-z0-9_%\-]+")


def linkedin_query(roles: str) -> str:
    return f"site:linkedin.com/posts ({roles}) ({LINKEDIN_HIRING_TERMS})"


def _linkedin_poster(title: str, url: str) -> str:
    match = re.match(r"^(.{2,80}?) on LinkedIn\b", title or "")
    if match:
        return match.group(1).strip()
    slug = urlsplit(url).path.split("/")[2].split("_")[0]
    return slug.replace("-", " ").title()[:80] or "a LinkedIn member"


def parse_searxng(data: dict) -> list[dict]:
    jobs, seen = [], set()
    for item in (data.get("results") or [])[:60]:
        url = str(item.get("url") or "").split("?")[0]
        if not _LINKEDIN_POST.match(url) or url in seen:
            continue
        title = str(item.get("title") or "")
        posted = item.get("publishedDate")
        job = post_to_job(
            f"{re.sub(r'^.{2,80}? on LinkedIn:?', '', title)} {item.get('content') or ''}",
            url,
            _linkedin_poster(title, url),
            "LinkedIn",
            posted if isinstance(posted, str) else None,
        )
        if job:
            seen.add(url)  # a later, fuller result for the same post still counts
            jobs.append(job)
    return jobs


async def fetch_linkedin_posts(roles: str) -> list[dict]:
    if not settings.SEARXNG_URL:
        return []  # optional source, off: not a failure
    if not roles:
        raise ValueError("LinkedIn posts source needs role terms")
    # Our own SearXNG on loopback: a trusted, configured address, so not
    # public_get (which refuses private addresses by design).
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        response = await client.get(
            settings.SEARXNG_URL.rstrip("/") + "/search",
            params={
                "q": linkedin_query(roles),
                "format": "json",
                "time_range": "week",
                "language": "en",
                "safesearch": 1,
            },
        )
    response.raise_for_status()
    return parse_searxng(response.json())
