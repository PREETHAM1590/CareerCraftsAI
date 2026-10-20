"""Official/public source connector contract, normalization and deduplication."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from app.core.config import settings
from app.services.public_http import public_get

FAMILIES = {
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "smartrecruiters",
    "recruitee",
    "adzuna",
    "remotive",
    "remoteok",
    "arbeitnow",
    "himalayas",
    "workingnomads",
    "themuse",
    "careerjet",
    "jooble",
    "hn_hiring",
    "getro",
    "jobicy",
    "google_cse",
    "rss",
    "jsonld",
    "workday",
    "personio",
    "bamboohr",
    "breezy",
    "pinpoint",
    "rippling",
    "teamtailor",
    "jobscore",
    "trakstar",
    "hibob",
    "gem",
}


@dataclass(frozen=True)
class Source:
    id: str
    family: str
    tenant: str = ""
    url: str = ""
    permitted: bool = True
    # How often the catalog re-reads this source: busy boards every few
    # hours, quiet ones daily.
    refresh_hours: int = 1


@dataclass
class Page:
    jobs: list[dict]
    next_cursor: str | None = None


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Jobs require a HTTPS URL without credentials")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source", "trackingid"}
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), urlencode(sorted(query)), "")
    )


def posted(value):
    if not value:
        return None
    try:
        if isinstance(value, (float, int)):
            stamp = value / 1000 if value > 10**11 else value
            return datetime.fromtimestamp(stamp, UTC)
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def plain(value) -> str:
    return html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))[:16000].strip()


def normalize(raw: dict, source: Source) -> dict | None:
    try:
        url = canonical_url(str(raw.get("url") or ""))
    except ValueError:
        return None
    title, company = plain(raw.get("title")), plain(raw.get("company"))
    if not title or not company:
        return None
    location = plain(raw.get("location"))
    date = posted(raw.get("posted_at"))
    now = datetime.now(UTC)
    if date and date > now + timedelta(days=1):
        date = None
    expires = posted(raw.get("expires_at"))
    return {
        "job_id": hashlib.sha256(url.encode()).hexdigest()[:32],
        "source_id": source.id,
        "source_job_id": str(raw.get("id") or url),
        "title": title[:300],
        "company": company[:300],
        "location": location[:500],
        "remote": (
            "remote"
            if raw.get("remote") or "remote" in location.lower()
            else "hybrid" if "hybrid" in location.lower() else "unknown"
        ),
        "salary_text": plain(raw.get("salary_text"))[:300],
        "url": url,
        "platform": source.family,
        "posted_at": date.isoformat() if date else None,
        "first_seen_at": now.isoformat(),
        "last_seen_at": now.isoformat(),
        "expires_at": expires.isoformat() if expires else None,
        "description": plain(raw.get("description")),
        "occurrences": [{"source_id": source.id, "url": url}],
    }


# Hosts of applicant tracking systems. A posting on one of these is the
# original; the same role on an aggregator or board is a pointer to it.
ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "recruitee.com",
    "myworkdayjobs.com",
    "icims.com",
    "jobvite.com",
    "bamboohr.com",
    "myworkdaysite.com",
    "personio.com",
    "personio.de",
    "breezy.hr",
    "pinpointhq.com",
    "rippling.com",
    "teamtailor.com",
    "jobscore.com",
    "trakstar.com",
    "hibob.com",
    "gem.com",
)
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|technologies|technology|"
    r"solutions|labs|india)\b",
    re.IGNORECASE,
)


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def role_key(job: dict) -> str:
    """The same role listed on different boards, whatever its URL: company
    (without legal suffixes), exact title, and where (remote collapses)."""
    company = _squash(_COMPANY_SUFFIX.sub(" ", str(job.get("company") or "")))
    location = str(job.get("location") or "")
    remote = job.get("remote") == "remote" or "remote" in location.lower()
    title = _squash(str(job.get("title") or ""))
    return f"{company}|{title}|{'remote' if remote else _squash(location)}"


def is_ats_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ATS_HOSTS)


def _merge(old: dict, new: dict) -> dict:
    """One role seen twice: keep the ATS posting as the main entry, remember
    every place it was listed, and keep the earliest date and fullest text."""
    primary, other = (
        (new, old) if is_ats_url(new["url"]) and not is_ats_url(old["url"]) else (old, new)
    )
    merged = dict(primary)
    merged["occurrences"] = list(
        {
            item["source_id"]: item
            for item in old.get("occurrences", []) + new.get("occurrences", [])
        }.values()
    )
    dates = [d for d in (old.get("posted_at"), new.get("posted_at")) if d]
    merged["posted_at"] = min(dates) if dates else None
    if len(other.get("description") or "") > len(merged.get("description") or ""):
        merged["description"] = other["description"]
    return merged


def url_key(url: str) -> str:
    """`canonical_url`, or the lowercased URL when it can't be canonicalized
    (live scrapers emit http/blank URLs; those must not crash a merge)."""
    try:
        return canonical_url(url)
    except ValueError:
        return (url or "").strip().lower().rstrip("/")


def merge_duplicates(jobs: list[dict]) -> list[dict]:
    """Merge the same job seen twice: first by URL, then by role across boards
    (see `role_key`). Earlier entries keep their id unless only the later one
    is the ATS original."""
    by_url: dict[str, dict] = {}
    loose: list[dict] = []
    for job in jobs:
        key = url_key(job.get("url") or "")
        if not key:
            loose.append(dict(job))
        else:
            by_url[key] = _merge(by_url[key], job) if key in by_url else dict(job)
    by_role: dict[str, dict] = {}
    for i, job in enumerate([*by_url.values(), *loose]):
        # Without a company and title there is no role to match on.
        key = role_key(job) if job.get("company") and job.get("title") else f"#{i}"
        by_role[key] = _merge(by_role[key], job) if key in by_role else job
    return list(by_role.values())


def dedupe(jobs: list[dict], days=30, now=None) -> list[dict]:
    """Drop expired and old jobs, then `merge_duplicates`."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    fresh = []
    for job in jobs:
        date, expiry = posted(job.get("posted_at")), posted(job.get("expires_at"))
        if (date and date < cutoff) or (expiry and expiry <= now):
            continue
        fresh.append(job)
    return merge_duplicates(fresh)


class JobPostingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.active, self.parts = True, []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.blocks.append("".join(self.parts))
            self.active = False


def jsonld_jobs(text: str, url: str) -> list[dict]:
    parser = JobPostingParser()
    parser.feed(text)
    out = []

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if types == "JobPosting" or "JobPosting" in types:
                org = value.get("hiringOrganization") or {}
                locations = value.get("jobLocation") or []
                if isinstance(locations, dict):
                    locations = [locations]
                cities = [
                    str((loc.get("address") or {}).get("addressLocality") or "")
                    for loc in locations
                    if isinstance(loc, dict)
                ]
                out.append(
                    {
                        "id": str(value.get("identifier", "")),
                        "title": value.get("title"),
                        "company": org.get("name") if isinstance(org, dict) else org,
                        "location": ", ".join(cities),
                        "url": value.get("url") or url,
                        "posted_at": value.get("datePosted"),
                        "expires_at": value.get("validThrough"),
                        "description": value.get("description"),
                        "remote": value.get("jobLocationType") == "TELECOMMUTE",
                    }
                )
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)

    for block in parser.blocks:
        try:
            walk(json.loads(block))
        except (ValueError, TypeError):
            continue
    return out[:200]


# ── community and feed sources ────────────────────────────────────────────


def parse_hn_post(comment_html: str) -> dict | None:
    """A "Who is hiring?" post: the first line is `Company | Role | Location | ...`.

    Posts that do not follow that convention are skipped rather than guessed at.
    """
    first = re.split(r"<p>|\n", comment_html or "", maxsplit=1)[0]
    parts = [plain(part) for part in first.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    rest = parts[2:]
    return {
        "company": parts[0],
        "title": parts[1],
        "location": next((p for p in rest if p and "remote" not in p.lower()), ""),
        "remote": any("remote" in p.lower() for p in parts),
        "description": plain(comment_html),
    }


def parse_rss(xml_text: str, source_url: str) -> list[dict]:
    """Items of an RSS feed as raw jobs. Titles of the form `Company: Role`
    (We Work Remotely and similar) are split; otherwise the company is the
    feed's host."""
    from defusedxml import ElementTree

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        company, _, role = title.partition(": ")
        if not role:
            company, role = urlsplit(source_url).hostname or "", title
        pub = item.findtext("pubDate")
        try:
            from email.utils import parsedate_to_datetime

            posted_at = parsedate_to_datetime(pub).isoformat() if pub else None
        except (TypeError, ValueError):
            posted_at = None
        out.append(
            {
                "id": item.findtext("guid") or item.findtext("link"),
                "title": role,
                "company": company,
                "url": (item.findtext("link") or "").strip(),
                "location": item.findtext("region") or "",
                "remote": True,
                "description": item.findtext("description"),
                "posted_at": posted_at,
            }
        )
    return out


_getro_networks: dict[str, str] = {}
_GETRO_ID = re.compile(
    r'"network_id"\s*:\s*"?(\d+)|"network"\s*:\s*\{[^{}]{0,200}?"id"\s*:\s*"?(\d+)|collections/(\d+)'
)


def getro_network_id(page_html: str) -> str | None:
    """The numeric id of a Getro-powered board, from the board's own page."""
    match = _GETRO_ID.search(page_html or "")
    return next((g for g in match.groups() if g), None) if match else None


def parse_getro(data: dict, board_url: str) -> list[dict]:
    """Raw jobs from a Getro board search response. Getro hosts the portfolio
    job boards of many VC firms with one API shape."""
    results = data.get("results") if isinstance(data.get("results"), dict) else data
    out = []
    for job in results.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        org = job.get("organization") if isinstance(job.get("organization"), dict) else {}
        locations = job.get("locations") or job.get("searchable_locations") or []
        created = job.get("created_at")
        out.append(
            {
                "id": job.get("id"),
                "title": job.get("title"),
                "company": org.get("name") or job.get("company_name"),
                "url": job.get("url") or job.get("apply_url"),
                "location": ", ".join(str(x) for x in locations if x),
                "remote": str(job.get("work_mode") or "").lower() == "remote",
                "posted_at": (
                    datetime.fromtimestamp(created, UTC).isoformat()
                    if isinstance(created, int | float)
                    else created
                ),
            }
        )
    return out


async def _fetch_getro(source: Source, query: str, page: int) -> tuple[list[dict], str | None]:
    """One page of a VC portfolio board. The source is a numeric network id
    (`tenant`) or the board's address (`url`), whose network id is read from
    the board page once."""
    network = source.tenant if source.tenant.isdigit() else _getro_networks.get(source.id)
    board = canonical_url(source.url) if source.url else ""
    if not network:
        if not board:
            raise ValueError("Getro source needs a network id or board url")
        home = await public_get(board, max_bytes=2_000_000)
        home.raise_for_status()
        network = getro_network_id(home.text)
        if not network:
            raise ValueError("Could not find the Getro network id on the board page")
        _getro_networks[source.id] = network
    response = await public_get(
        f"https://api.getro.com/api/v2/collections/{network}/search/jobs",
        # Getro answers 406 to the default `Accept: */*`.
        headers={"Accept": "application/json"},
        json_body={"hitsPerPage": 100, "page": page - 1, "filters": {}, "query": query or ""},
    )
    response.raise_for_status()
    data = response.json()
    rows = parse_getro(data, board)
    results = data.get("results") if isinstance(data.get("results"), dict) else data
    total = int(results.get("total") or 0)
    return rows, str(page + 1) if page * 100 < total else None


_CSE_COMPANY = re.compile(r"^/(?:embed/)?(?P<tenant>[^/?#]+)", re.IGNORECASE)


def parse_google_cse(data: dict) -> list[dict]:
    """Postings found by a Google Programmable Search query such as
    `site:boards.greenhouse.io India engineer`. Results are links to the
    employer's own ATS page, the best place to apply."""
    out = []
    for item in data.get("items") or []:
        link = str(item.get("link") or "")
        parts = urlsplit(link)
        tenant = _CSE_COMPANY.match(parts.path or "")
        title = str(item.get("title") or "")
        role, _, tail = title.partition(" - ")
        company = ""
        if parts.hostname and any(h in parts.hostname for h in ("greenhouse", "lever", "ashby")):
            company = (tenant.group("tenant") if tenant else "").replace("-", " ").title()
        company = company or tail.split(" - ")[0].replace(" Careers", "").strip()
        if not role or not company:
            continue
        out.append(
            {
                "id": link,
                "title": role,
                "company": company,
                "url": link,
                "description": item.get("snippet"),
            }
        )
    return out


async def _fetch_google_cse(source: Source, query: str, page: int) -> tuple[list[dict], str | None]:
    if not settings.GOOGLE_CSE_API_KEY or not settings.GOOGLE_CSE_ID:
        raise ValueError("Google Programmable Search is not configured")
    q = " ".join(part for part in (source.url, query) if part).strip()
    if not q:
        raise ValueError("Google search source needs a query")
    response = await public_get(
        "https://www.googleapis.com/customsearch/v1?"
        + urlencode(
            {
                "key": settings.GOOGLE_CSE_API_KEY,
                "cx": settings.GOOGLE_CSE_ID,
                "q": q,
                "num": 10,
                "start": (page - 1) * 10 + 1,
                "dateRestrict": "w2",
            }
        )
    )
    response.raise_for_status()
    # Free quota is 100 queries a day, so one page per refresh.
    return parse_google_cse(response.json()), None


async def _fetch_hn_hiring(page: int) -> tuple[list[dict], str | None]:
    """The latest monthly "Ask HN: Who is hiring?" thread, one page of posts
    at a time, through HN's public Algolia search."""
    found = await public_get(
        "https://hn.algolia.com/api/v1/search_by_date?"
        + urlencode({"tags": "story,author_whoishiring", "query": "who is hiring"})
        + "&hitsPerPage=5"
    )
    found.raise_for_status()
    thread = next(
        (
            hit
            for hit in found.json().get("hits", [])
            if str(hit.get("title", "")).lower().startswith("ask hn: who is hiring")
        ),
        None,
    )
    if not thread:
        return [], None
    story = thread["objectID"]
    if not re.fullmatch(r"\d{1,12}", str(story)):
        return [], None
    response = await public_get(
        "https://hn.algolia.com/api/v1/search_by_date?"
        + urlencode({"tags": f"comment,story_{story}", "hitsPerPage": 100, "page": page - 1})
    )
    response.raise_for_status()
    data = response.json()
    rows = []
    for hit in data.get("hits", []):
        if str(hit.get("parent_id")) != str(story):
            continue  # a reply, not a posting
        job = parse_hn_post(hit.get("comment_text") or "")
        if job:
            job.update(
                {
                    "id": hit["objectID"],
                    "url": f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    "posted_at": hit.get("created_at"),
                }
            )
            rows.append(job)
    more = page < int(data.get("nbPages", 0))
    return rows, str(page + 1) if more else None


def _ashby_salary(job: dict) -> str:
    """`USD 120000–150000` from the Salary component of the first compensation tier."""
    for tier in (job.get("compensation") or {}).get("compensationTiers") or []:
        for c in tier.get("components") or []:
            kind = c.get("componentType") or c.get("type")
            if kind == "Salary" and (c.get("minValue") or c.get("maxValue")):
                lo, hi = (
                    f"{v:g}" if isinstance(v, (int, float)) else v
                    for v in (c.get("minValue"), c.get("maxValue"))
                )
                span = f"{lo}–{hi}" if lo and hi else f"{lo or hi}"
                return f"{c.get('currencyCode') or ''} {span}".strip()
    return ""


# ── keyless ATS boards (Workday, Personio, BambooHR, ...) ─────────────────

_WORKDAY_PAGE = 20
_WORKDAY_MAX_PAGES = 10  # bounds one source to 200 postings per refresh
_WORKDAY_HOST = re.compile(r"[a-z0-9-]+(?:\.wd\d+)?\.myworkday(?:jobs|site)\.com")
_WORKDAY_AGO = re.compile(r"(\d+)(\+?)\s*days?\s+ago")


def workday_posted(text) -> str | None:
    """`Posted 3 Days Ago` / `Posted Today` as an ISO date; `30+ Days Ago` and
    anything unrecognised are unknown rather than guessed."""
    value = str(text or "").lower()
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in value:
        return (today - timedelta(days=1)).isoformat()
    if "today" in value:
        return today.isoformat()
    match = _WORKDAY_AGO.search(value)
    if match and not match.group(2):
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    return None


async def _fetch_workday(source: Source, query: str, cursor: str | None) -> Page:
    """One page of a Workday CXS board. Tenant is `host|tenant|site`; cursor is
    the listing offset. Per-job detail requests are skipped (one request per
    posting is too costly for a refresh), so descriptions are empty."""
    try:
        host, tenant, site = source.tenant.split("|")
    except ValueError:
        raise ValueError("Workday tenant must be host|tenant|site") from None
    if not (
        _WORKDAY_HOST.fullmatch(host)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tenant)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", site)
    ):
        raise ValueError("Invalid Workday source")
    offset = int(cursor or "0")
    if offset < 0 or offset >= _WORKDAY_PAGE * _WORKDAY_MAX_PAGES:
        raise ValueError("Pagination limit exceeded")
    response = await public_get(
        f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
        json_body={
            "appliedFacets": {},
            "limit": _WORKDAY_PAGE,
            "offset": offset,
            "searchText": query or "",
        },
    )
    response.raise_for_status()
    data = response.json()
    postings = data.get("jobPostings") or []
    rows = [
        {
            "id": j.get("externalPath"),
            "title": j.get("title"),
            "company": tenant,
            "url": f"https://{host}/en-US/{site}{j.get('externalPath')}",
            "location": j.get("locationsText"),
            "posted_at": workday_posted(j.get("postedOn")),
        }
        for j in postings
        if str(j.get("externalPath") or "").startswith("/")
    ]
    nxt = offset + _WORKDAY_PAGE
    # Some tenants report `total` on the first page only (0 afterwards), so a
    # full page with no total keeps paging until an empty one.
    total = int(data.get("total") or 0)
    more = len(postings) == _WORKDAY_PAGE and (not total or nxt < total)
    more = more and nxt < _WORKDAY_PAGE * _WORKDAY_MAX_PAGES
    jobs = [job for raw in rows if (job := normalize(raw, source))]
    return Page(jobs, str(nxt) if more else None)


def _rfc822(value) -> str | None:
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).isoformat() if value else None
    except (TypeError, ValueError):
        return None


def _xml_root(text: str):
    from defusedxml import ElementTree

    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return None


def parse_personio(xml_text: str, tenant: str) -> list[dict]:
    root = _xml_root(xml_text)
    out = []
    for pos in root.iter("position") if root is not None else []:
        body = "".join(
            d.findtext("value") or "" for d in pos.findall("jobDescriptions/jobDescription")
        )
        out.append(
            {
                "id": pos.findtext("id"),
                "title": pos.findtext("name"),
                "company": tenant,
                "url": f"https://{tenant}.jobs.personio.com/job/{pos.findtext('id')}",
                "location": pos.findtext("office"),
                "description": body,
                "posted_at": pos.findtext("createdAt"),
            }
        )
    return out


def parse_teamtailor(xml_text: str, tenant: str) -> list[dict]:
    root = _xml_root(xml_text)
    ns = {"tt": "https://teamtailor.com/locations"}
    out = []
    for item in root.iter("item") if root is not None else []:
        places = [n.text for n in item.findall("tt:locations/tt:location/tt:name", ns) if n.text]
        out.append(
            {
                "id": item.findtext("guid") or item.findtext("link"),
                "title": item.findtext("title"),
                "company": tenant,
                "url": (item.findtext("link") or "").strip(),
                "location": ", ".join(places),
                "remote": (item.findtext("remoteStatus") or "").strip() == "fully",
                "description": item.findtext("description"),
                "posted_at": _rfc822(item.findtext("pubDate")),
            }
        )
    return out


def parse_trakstar(xml_text: str, tenant: str) -> list[dict]:
    root = _xml_root(xml_text)
    ns = "{https://recruiterbox.com/rss/job/}"
    out = []
    for item in root.iter("item") if root is not None else []:
        link = (item.findtext("link") or "").strip().replace("http://", "https://", 1)
        out.append(
            {
                "id": link.rstrip("/").rsplit("/", 1)[-1],
                "title": item.findtext("title"),
                "company": tenant,
                "url": link,
                "location": ", ".join(
                    v
                    for k in ("City", "State", "Country")
                    if (v := item.findtext(ns + "location" + k))
                ),
                "description": item.findtext("description"),
                "posted_at": _rfc822(item.findtext("pubDate")),
            }
        )
    return out


_KEYLESS_ATS = {
    "personio",
    "bamboohr",
    "breezy",
    "pinpoint",
    "rippling",
    "teamtailor",
    "jobscore",
    "trakstar",
    "hibob",
    "gem",
}
_GEM_LIST = (
    "query JobBoardList($boardId: String!) { oatsExternalJobPostings(boardId: $boardId) "
    "{ jobPostings { extId title locations { city isoCountry isRemote } job { locationType } } } }"
)


def _join(*parts) -> str:
    return ", ".join(str(p) for p in parts if p)


async def _fetch_ats_board(source: Source, tenant: str, page: int) -> Page:
    """Keyless one-request boards. Each returns the whole board (Teamtailor is
    paged by cursor). BambooHR, Breezy and Rippling list without descriptions:
    the body costs one more request per posting, which a refresh skips."""
    family = source.family
    headers: dict = {}
    body = None
    if family == "personio":
        url = f"https://{tenant}.jobs.personio.com/xml"
    elif family == "bamboohr":
        url = f"https://{tenant}.bamboohr.com/careers/list"
        headers = {"Accept": "application/json"}
    elif family == "breezy":
        url = f"https://{tenant}.breezy.hr/json"
    elif family == "pinpoint":
        url = f"https://{tenant}.pinpointhq.com/postings.json"
    elif family == "rippling":
        url = f"https://ats.rippling.com/api/v1/board/{tenant}/jobs"
    elif family == "teamtailor":
        url = f"https://{tenant}.teamtailor.com/jobs.rss?offset={(page - 1) * 100}&per_page=100"
    elif family == "jobscore":
        url = f"https://careers.jobscore.com/jobs/{tenant}/feed.json?sort=date&limit=200"
    elif family == "trakstar":
        url = f"https://{tenant}.hire.trakstar.com/jobfeeds/{tenant}"
    elif family == "hibob":
        url = f"https://{tenant}.careers.hibob.com/api/job-ad"
        headers = {"Referer": f"https://{tenant}.careers.hibob.com/jobs"}
    else:  # gem
        url = "https://jobs.gem.com/api/public/graphql"
        body = {
            "operationName": "JobBoardList",
            "query": _GEM_LIST,
            "variables": {"boardId": tenant},
        }
    response = await public_get(url, headers=headers, json_body=body)
    response.raise_for_status()
    next_cursor = None
    if family == "personio":
        rows = parse_personio(response.text, tenant)
        if any(not r["description"] for r in rows):
            # Posts published only in a non-default locale have an empty body; the
            # English feed usually has it. Best effort.
            try:
                en = await public_get(url + "?language=en")
                en.raise_for_status()
                bodies = {r["id"]: r["description"] for r in parse_personio(en.text, tenant)}
                for r in rows:
                    r["description"] = r["description"] or bodies.get(r["id"], "")
            except Exception:  # noqa: BLE001, S110 - the body is optional
                pass
    elif family == "teamtailor":
        rows = parse_teamtailor(response.text, tenant)
        next_cursor = str(page + 1) if len(rows) >= 100 else None
    elif family == "trakstar":
        rows = parse_trakstar(response.text, tenant)
    elif family == "bamboohr":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("jobOpeningName"),
                "company": tenant,
                "url": f"https://{tenant}.bamboohr.com/careers/{j.get('id')}",
                "location": _join(
                    (j.get("location") or {}).get("city"), (j.get("location") or {}).get("state")
                ),
                "remote": bool(j.get("isRemote")) or str(j.get("locationType")) == "1",
            }
            for j in response.json().get("result", [])
        ]
    elif family == "breezy":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("name"),
                "company": tenant,
                "url": j.get("url"),
                "location": _join(
                    (j.get("location") or {}).get("city"),
                    ((j.get("location") or {}).get("country") or {}).get("name"),
                ),
                "remote": (j.get("location") or {}).get("is_remote"),
                "posted_at": j.get("published_date"),
            }
            for j in response.json()
        ]
    elif family == "pinpoint":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": tenant,
                "url": j.get("url"),
                "location": _join(
                    (j.get("location") or {}).get("city"), (j.get("location") or {}).get("province")
                )
                or (j.get("location") or {}).get("name"),
                "remote": j.get("workplace_type") == "remote",
                "description": " ".join(
                    j.get(k) or ""
                    for k in (
                        "description",
                        "key_responsibilities",
                        "skills_knowledge_expertise",
                        "benefits",
                    )
                ),
            }
            for j in response.json().get("data", [])
        ]
    elif family == "rippling":
        data = response.json()
        rows = [
            {
                "id": j.get("uuid"),
                "title": j.get("name"),
                "company": tenant,
                "url": j.get("url"),
                "location": (j.get("workLocation") or {}).get("label"),
            }
            for j in (data if isinstance(data, list) else data.get("items", []))
        ]
    elif family == "jobscore":
        data = response.json()
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": data.get("company_name") or tenant,
                "url": j.get("detail_url"),
                "location": _join(j.get("city"), j.get("state"))
                or j.get("country")
                or j.get("location"),
                "remote": str(j.get("remote") or "").lower().startswith("yes")
                and "some" not in str(j.get("remote")).lower(),
                "description": j.get("description"),
                "posted_at": j.get("opened_date"),
            }
            for j in data.get("jobs", [])
        ]
    elif family == "hibob":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": tenant,
                "url": f"https://{tenant}.careers.hibob.com/jobs/{j.get('id')}",
                "location": _join(j.get("site"), j.get("country")),
                "remote": str(j.get("workspaceType") or "").lower() == "remote",
                # Bodies arrive entity-escaped, so unescape before tags are stripped.
                "description": html.unescape(
                    " ".join(
                        j.get(k) or ""
                        for k in ("description", "responsibilities", "requirements", "benefits")
                    )
                ),
                "posted_at": j.get("publishedAt"),
            }
            for j in response.json().get("jobAdDetails", [])
            if j.get("id")
        ]
    else:  # gem
        data = (response.json().get("data") or {}).get("oatsExternalJobPostings") or {}
        rows = []
        for j in data.get("jobPostings") or []:
            places = j.get("locations") or []
            rows.append(
                {
                    "id": j.get("extId"),
                    "title": j.get("title"),
                    "company": tenant,
                    "url": f"https://jobs.gem.com/{tenant}/{j.get('extId')}",
                    "location": _join(
                        *(v for p in places for v in (p.get("city"), p.get("isoCountry")))
                    ),
                    "remote": any(p.get("isRemote") for p in places)
                    or (j.get("job") or {}).get("locationType") == "REMOTE",
                }
            )
    return Page([job for raw in rows[:1000] if (job := normalize(raw, source))], next_cursor)


async def fetch_page(source: Source, query: str = "", cursor: str | None = None) -> Page:
    if source.family not in FAMILIES or not source.permitted:
        raise ValueError("Source is not permitted")
    if source.family == "workday":
        return await _fetch_workday(source, query, cursor)
    tenant = source.tenant
    lever_host = "api.lever.co"
    if source.family == "lever" and tenant.startswith("eu:"):
        # Lever EU boards live on a separate host: tenant "eu:board".
        tenant, lever_host = tenant[3:], "api.eu.lever.co"
    if tenant and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tenant):
        raise ValueError("Invalid source tenant")
    family = source.family
    page = int(cursor or "1")
    if page < 1 or page > 10:
        raise ValueError("Pagination limit exceeded")
    headers = {}
    if family in _KEYLESS_ATS:
        if not tenant:
            raise ValueError("Source needs a tenant")
        return await _fetch_ats_board(source, tenant, page)
    if family == "hn_hiring":
        rows, next_cursor = await _fetch_hn_hiring(page)
        return Page([job for raw in rows if (job := normalize(raw, source))], next_cursor)
    if family == "getro":
        rows, next_cursor = await _fetch_getro(source, query, page)
        return Page([job for raw in rows if (job := normalize(raw, source))], next_cursor)
    if family == "google_cse":
        rows, next_cursor = await _fetch_google_cse(source, query, page)
        return Page([job for raw in rows if (job := normalize(raw, source))], next_cursor)
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true",
        "lever": f"https://{lever_host}/v0/postings/{tenant}?mode=json&limit=100&skip={(page-1)*100}",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true",
        "workable": f"https://{tenant}.workable.com/spi/v3/jobs?state=published",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings?limit=100&offset={(page-1)*100}",
        "recruitee": f"https://{tenant}.recruitee.com/api/offers/",
        "remotive": "https://remotive.com/api/remote-jobs",
        "jobicy": "https://jobicy.com/api/v2/remote-jobs?"
        + urlencode({"count": 50, **({"tag": query} if query else {})}),
        "remoteok": "https://remoteok.com/api",
        "arbeitnow": f"https://www.arbeitnow.com/api/job-board-api?page={page}",
        "himalayas": f"https://himalayas.app/jobs/api?limit=100&offset={(page-1)*100}",
        "workingnomads": "https://www.workingnomads.com/api/exposed_jobs/",
        "themuse": f"https://www.themuse.com/api/public/jobs?page={page-1}",
    }
    if family == "workable":
        token = settings.WORKABLE_API_TOKENS.get(tenant)
        if not token:
            raise ValueError("Workable requires an authorized read-only API token")
        headers["Authorization"] = "Bearer " + token
    if family == "adzuna":
        if not settings.ADZUNA_APP_ID or not settings.ADZUNA_APP_KEY:
            raise ValueError("Adzuna credentials are not configured")
        url = f"https://api.adzuna.com/v1/api/jobs/{tenant or 'gb'}/search/{page}?" + urlencode(
            {
                "app_id": settings.ADZUNA_APP_ID,
                "app_key": settings.ADZUNA_APP_KEY,
                "what": "",
                "results_per_page": 100,
            }
        )
    elif family == "jsonld":
        url = canonical_url(source.url)
        parsed = urlsplit(url)
        robots_url = f"https://{parsed.netloc}/robots.txt"
        robots = await public_get(robots_url, max_bytes=200_000)
        if robots.status_code not in (200, 404):
            raise ValueError("Robots policy unavailable")
        if robots.status_code == 200:
            policy = RobotFileParser()
            policy.parse(robots.text.splitlines())
            if not policy.can_fetch("CareerCraftJobDiscovery", url):
                raise ValueError("Robots disallows access")
    elif family == "rss":
        url = canonical_url(source.url)
    elif family == "careerjet":
        if not settings.CAREERJET_API_KEY:
            raise ValueError("Careerjet credentials are not configured")
        url = "https://search.api.careerjet.net/v4/query?" + urlencode(
            {
                "locale_code": source.tenant or "en_IN",
                "keywords": query or "",
                "page": page,
                "page_size": 100,
                "user_ip": settings.CAREERJET_USER_IP,
                "user_agent": "CareerCraftJobDiscovery",
            }
        )
        headers["Authorization"] = (
            "Basic " + base64.b64encode(f"{settings.CAREERJET_API_KEY}:".encode()).decode()
        )
        # Careerjet rejects requests without a Referer (403 "Undeclared referrer").
        headers["Referer"] = settings.FRONTEND_URL.rstrip("/") + "/"
    elif family == "jooble":
        if not settings.JOOBLE_API_KEY:
            raise ValueError("Jooble credentials are not configured")
        url = f"https://jooble.org/api/{settings.JOOBLE_API_KEY}"
        jooble_body = {"keywords": query or "", "location": tenant, "page": page}
    else:
        url = urls[family]
    response = await public_get(
        url,
        headers=headers,
        json_body=jooble_body if family == "jooble" else None,
        # Greenhouse sends every description in one unpaginated page (~10 MB for large boards).
        max_bytes=20_000_000 if family == "greenhouse" else 4_000_000,
    )
    response.raise_for_status()
    data = response.json() if family not in {"jsonld", "rss"} else None
    rows, next_cursor = [], None
    if family == "greenhouse":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": tenant,
                "url": j.get("absolute_url"),
                "location": (j.get("location") or {}).get("name"),
                "description": j.get("content"),
                "posted_at": j.get("first_published"),
            }
            for j in data.get("jobs", [])
        ]
    elif family == "lever":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("text"),
                "company": tenant,
                "url": j.get("hostedUrl"),
                "location": (j.get("categories") or {}).get("location"),
                "description": j.get("descriptionPlain"),
                "posted_at": j.get("createdAt"),
            }
            for j in data
        ]
        next_cursor = str(page + 1) if len(data) == 100 else None
    elif family == "ashby":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": tenant,
                "url": j.get("jobUrl"),
                "location": j.get("location"),
                "remote": j.get("isRemote"),
                "description": j.get("descriptionPlain"),
                "posted_at": j.get("publishedAt"),
                "salary_text": _ashby_salary(j),
            }
            for j in data.get("jobs", [])
        ]
    elif family == "jobicy":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("jobTitle"),
                "company": j.get("companyName"),
                "url": j.get("url"),
                "location": j.get("jobGeo") or "Worldwide",
                "remote": True,
                "description": j.get("jobDescription") or j.get("jobExcerpt"),
                "posted_at": j.get("pubDate"),
                "salary_text": (
                    (
                        f"{j.get('salaryCurrency') or ''} "
                        f"{j.get('annualSalaryMin')}–{j.get('annualSalaryMax')}"
                    ).strip()
                    if j.get("annualSalaryMin") and j.get("annualSalaryMax")
                    else ""
                ),
            }
            for j in data.get("jobs", [])
        ]
    elif family == "smartrecruiters":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("name"),
                "company": tenant,
                "url": f"https://jobs.smartrecruiters.com/{tenant}/{j.get('id')}",
                "location": (j.get("location") or {}).get("city"),
                "posted_at": j.get("releasedDate"),
                "description": j.get("jobAd", {}).get("sections", {}),
            }
            for j in data.get("content", [])
        ]
        next_cursor = str(page + 1) if page * 100 < data.get("totalFound", 0) else None
    elif family in {"workable", "recruitee"}:
        items = data.get("results", data.get("jobs", data.get("offers", [])))
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": tenant,
                "url": j.get("url") or j.get("careers_url"),
                "location": (
                    j.get("location")
                    if isinstance(j.get("location"), str)
                    else str(j.get("location") or "")
                ),
                "description": j.get("description"),
                "posted_at": j.get("created_at") or j.get("published_at"),
            }
            for j in items
        ]
    elif family == "adzuna":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": (j.get("company") or {}).get("display_name"),
                "url": j.get("redirect_url"),
                "location": (j.get("location") or {}).get("display_name"),
                "description": j.get("description"),
                "posted_at": j.get("created"),
                "salary_text": f"{j.get('salary_min', '')}–{j.get('salary_max', '')}",
            }
            for j in data.get("results", [])
        ]
        next_cursor = str(page + 1) if page * 100 < data.get("count", 0) else None
    elif family == "remotive":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "url": j.get("url"),
                "location": j.get("candidate_required_location"),
                "remote": True,
                "description": j.get("description"),
                "posted_at": j.get("publication_date"),
                "salary_text": j.get("salary"),
            }
            for j in data.get("jobs", [])
        ]
    elif family == "remoteok":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("position"),
                "company": j.get("company"),
                "url": j.get("url"),
                "location": j.get("location"),
                "remote": True,
                "description": j.get("description"),
                "posted_at": j.get("date"),
            }
            for j in data
            if j.get("position")
        ]
    elif family == "arbeitnow":
        rows = [
            {
                "id": j.get("slug"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "url": j.get("url"),
                "location": j.get("location"),
                "remote": j.get("remote"),
                "description": j.get("description"),
                "posted_at": j.get("created_at"),
            }
            for j in data.get("data", [])
        ]
        next_cursor = str(page + 1) if (data.get("links") or {}).get("next") else None
    elif family == "jooble":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "company": j.get("company"),
                "url": j.get("link"),
                "location": j.get("location"),
                "description": j.get("snippet"),
                "posted_at": j.get("updated"),
                "salary_text": j.get("salary"),
            }
            for j in data.get("jobs", [])
        ]
        next_cursor = str(page + 1) if page * 20 < int(data.get("totalCount") or 0) else None
    elif family == "himalayas":
        rows = [
            {
                "id": j.get("guid") or j.get("applicationLink"),
                "title": j.get("title"),
                "company": j.get("companyName"),
                "url": j.get("applicationLink") or j.get("guid"),
                "location": ", ".join(j.get("locationRestrictions") or []) or "Worldwide",
                "remote": True,
                "description": j.get("description") or j.get("excerpt"),
                "posted_at": j.get("pubDate"),
                "expires_at": j.get("expiryDate"),
                "salary_text": (
                    f"{j.get('minSalary')}–{j.get('maxSalary')} {j.get('currency') or ''}".strip()
                    if j.get("minSalary")
                    else ""
                ),
            }
            for j in data.get("jobs", [])
        ]
        total = int(data.get("totalCount") or 0)
        next_cursor = str(page + 1) if page * 100 < total else None
    elif family == "workingnomads":
        rows = [
            {
                "id": j.get("url"),
                "title": j.get("title"),
                "company": j.get("company_name"),
                "url": j.get("url"),
                "location": j.get("location") or "Worldwide",
                "remote": True,
                "description": j.get("description"),
                "posted_at": j.get("pub_date"),
            }
            for j in (data if isinstance(data, list) else [])
        ]
    elif family == "themuse":
        rows = [
            {
                "id": j.get("id"),
                "title": j.get("name"),
                "company": (j.get("company") or {}).get("name"),
                "url": (j.get("refs") or {}).get("landing_page"),
                "location": ", ".join(
                    str(loc.get("name")) for loc in j.get("locations") or [] if loc.get("name")
                ),
                "description": j.get("contents"),
                "posted_at": j.get("publication_date"),
            }
            for j in data.get("results", [])
        ]
        next_cursor = str(page + 1) if page < int(data.get("page_count") or 0) else None
    elif family == "careerjet":
        rows = [
            {
                "id": j.get("url"),
                "title": j.get("title"),
                "company": j.get("company"),
                "url": j.get("url"),
                "location": j.get("locations"),
                "description": j.get("description"),
                "posted_at": j.get("date"),
                "salary_text": j.get("salary"),
            }
            for j in data.get("jobs", [])
        ]
        next_cursor = str(page + 1) if page < int(data.get("pages") or 0) else None
    elif family == "rss":
        rows = parse_rss(response.text, url)
    elif family == "jsonld":
        rows = jsonld_jobs(response.text, url)
    return Page([job for raw in rows[:1000] if (job := normalize(raw, source))], next_cursor)
