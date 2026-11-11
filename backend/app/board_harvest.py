"""Operator-run discovery of company ATS job boards. Never imported by the app or scheduler.

    python -m app.board_harvest probe --family greenhouse --slugs slugs.txt --out candidates.json
    python -m app.board_harvest detect --websites sites.txt --out candidates.json
    python -m app.board_harvest health [--prune] [--report report.json]

It writes a candidates file only; a human reviews it and merges entries into
services/job_sources.json by hand. It makes many outbound requests (throttled
per host), so run it rarely. Idea modeled on strelov1/freehire (MIT).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.services import job_connectors
from app.services.job_connectors import Source, fetch_page
from app.services.public_http import public_get

SOURCES = Path(__file__).parent / "services" / "job_sources.json"
S = r"([A-Za-z0-9][A-Za-z0-9_-]*)"
# (family, regex). Group 1 is the slug; lever EU / workday are handled in detect_boards.
PATTERNS = [
    ("greenhouse", rf"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?{S}"),
    ("lever", rf"jobs\.lever\.co/{S}"),
    ("ashby", rf"jobs\.ashbyhq\.com/{S}"),
    ("workable", rf"apply\.workable\.com/{S}"),
    ("recruitee", rf"{S}\.recruitee\.com"),
    ("personio", rf"{S}\.jobs\.personio\.(?:com|de)"),
    ("bamboohr", rf"{S}\.bamboohr\.com"),
    ("teamtailor", rf"{S}\.teamtailor\.com"),
    ("breezy", rf"{S}\.breezy\.hr"),
    ("smartrecruiters", rf"jobs\.smartrecruiters\.com/{S}"),
    ("icims", rf"{S}\.icims\.com"),
]
LEVER_EU = re.compile(rf"jobs\.eu\.lever\.co/{S}", re.I)
WORKDAY = re.compile(
    r"https://(([A-Za-z0-9-]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
)
NOISE = {
    "www",
    "api",
    "app",
    "careers",
    "jobs",
    "embed",
    "static",
    "cdn",
    "assets",
    "support",
    "help",
}


def parse_slugs(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in text.splitlines():
        if slug := line.strip().lower():
            seen.setdefault(slug)
    return list(seen)


def detect_boards(html: str, base_url: str = "") -> set[tuple[str, str]]:
    """Return {(family, tenant)} for ATS links in html, restricted to FAMILIES known at runtime."""
    found: set[tuple[str, str]] = set()
    for family, pattern in PATTERNS:
        for m in re.finditer(pattern, html, re.I):
            if m.group(1).lower() not in NOISE:
                found.add((family, m.group(1).lower()))
    found |= {("lever", f"eu:{m.group(1).lower()}") for m in LEVER_EU.finditer(html)}
    for m in WORKDAY.finditer(html):
        found.add(("workday", f"{m.group(1).lower()}|{m.group(2).lower()}|{m.group(3)}"))
    known = {f for f, _ in found if f in job_connectors.FAMILIES}
    for family in sorted({f for f, _ in found} - known):
        print(f"note: family {family!r} not in FAMILIES, skipped ({base_url})", file=sys.stderr)
    return {(f, t) for f, t in found if f in known}


def to_entry(source: Source, jobs: list[dict]) -> dict:
    return {
        "id": source.id,
        "family": source.family,
        "tenant": source.tenant,
        "permitted": True,
        "refresh_hours": source.refresh_hours,
        "sample_title": jobs[0].get("title", ""),
        "job_count": len(jobs),
    }


class Throttle:
    def __init__(self, delay=0.5):
        self.delay, self.locks, self.last = delay, {}, {}

    async def wait(self, key: str):
        async with self.locks.setdefault(key, asyncio.Lock()):
            await asyncio.sleep(max(0.0, self.last.get(key, 0) + self.delay - time.monotonic()))
            self.last[key] = time.monotonic()


async def probe(targets, concurrency=4, delay=0.5, timeout=20, log=sys.stderr):
    """targets: iterable of (family, tenant). Returns (entries, stats)."""
    sem, throttle, entries = asyncio.Semaphore(concurrency), Throttle(delay), []
    stats = {"probed": 0, "kept": 0, "failed": 0}
    existing = (
        {e["id"] for e in json.loads(SOURCES.read_text("utf-8"))} if SOURCES.exists() else set()
    )

    async def one(family, tenant):
        source = Source(id=f"{family}:{tenant}", family=family, tenant=tenant, refresh_hours=12)
        if source.id in existing:
            return
        async with sem:
            await throttle.wait(family)
            stats["probed"] += 1
            try:
                page = await asyncio.wait_for(fetch_page(source, "", None), timeout)
            except Exception:
                stats["failed"] += 1
                return
        if page.jobs:
            stats["kept"] += 1
            entries.append(to_entry(source, page.jobs))
        print(
            f"\rprobed {stats['probed']} kept {stats['kept']} failed {stats['failed']}",
            end="",
            file=log,
        )

    await asyncio.gather(*(one(f, t) for f, t in sorted(set(targets))))
    print(file=log)
    return sorted(entries, key=lambda e: e["id"]), stats


async def detect(sites, concurrency=4, delay=0.5, timeout=20):
    sem, throttle, found = asyncio.Semaphore(concurrency), Throttle(delay), set()

    async def one(url):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            return
        for path in ("", "/careers", "/jobs"):
            async with sem:
                await throttle.wait(parts.hostname)
                try:
                    r = await asyncio.wait_for(
                        public_get(f"https://{parts.hostname}{path or parts.path}"), timeout
                    )
                except Exception:  # noqa: S112 - an unreachable page is just skipped
                    continue
            if r.status_code == 200 and (hit := detect_boards(r.text, url)):
                found.update(hit)
                return

    await asyncio.gather(*(one(u) for u in sites))
    return found



DEAD_STATUSES = {404, 410}
# Aggregators and keyed APIs have no per-board identity to check without credentials.
NOT_BOARDS = {"adzuna", "careerjet", "jooble", "reed", "usajobs", "google_cse", "getro", "rss"}


async def check_source(source: Source, timeout=20) -> str:
    """ok (has jobs), empty (answered, zero jobs), dead (404/410) or error (anything else)."""
    try:
        page = await asyncio.wait_for(fetch_page(source, "", None), timeout)
    except httpx.HTTPStatusError as exc:
        return "dead" if exc.response.status_code in DEAD_STATUSES else "error"
    except Exception:
        return "error"
    if page.jobs:
        return "ok"
    if source.family == "smartrecruiters" and await _smartrecruiters_unknown(source.tenant):
        return "dead"
    return "empty"


async def _smartrecruiters_unknown(tenant: str) -> bool:
    """The postings API answers 200/empty for any id; the careers page sends an
    unknown company back to the jobs.smartrecruiters.com home page."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", tenant):
        return False
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            reply = await client.get(f"https://careers.smartrecruiters.com/{tenant}")
    except httpx.HTTPError:
        return False
    return reply.status_code in (301, 302) and reply.headers.get("location", "").rstrip(
        "/"
    ) == "https://jobs.smartrecruiters.com"


async def health(rows, concurrency=4, delay=0.3, timeout=20, log=sys.stderr):
    """Probe every board entry politely. Returns {source_id: status}."""
    sem, throttle, result = asyncio.Semaphore(concurrency), Throttle(delay), {}

    async def one(row):
        keys = ("id", "family", "tenant", "permitted", "url", "refresh_hours")
        source = Source(**{k: row[k] for k in keys if k in row})
        async with sem:
            await throttle.wait(source.family)
            result[source.id] = await check_source(source, timeout)
        print(f"\rchecked {len(result)}/{len(rows)}", end="", file=log)

    await asyncio.gather(*(one(r) for r in rows if r["family"] not in NOT_BOARDS))
    print(file=log)
    return result


def health_counts(rows, statuses) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        if row["id"] in statuses:
            family = counts.setdefault(row["family"], dict.fromkeys(("ok", "empty", "dead", "error"), 0))
            family[statuses[row["id"]]] += 1
    return counts


def run_health(args) -> dict[str, dict[str, int]]:
    rows = json.loads(SOURCES.read_text("utf-8"))
    statuses = asyncio.run(health(rows, args.concurrency))
    counts = health_counts(rows, statuses)
    if args.report:
        Path(args.report).write_text(json.dumps(statuses, indent=2, sort_keys=True) + "\n", "utf-8")
    if args.prune:
        kept = [r for r in rows if statuses.get(r["id"]) != "dead"]
        SOURCES.write_text(json.dumps(kept, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps(counts, indent=2, sort_keys=True))
    return counts


def main(argv=None):
    ap = argparse.ArgumentParser(prog="app.board_harvest", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--family", required=True)
    p.add_argument("--slugs", required=True)
    d = sub.add_parser("detect")
    d.add_argument("--websites", required=True)
    h = sub.add_parser("health")
    h.add_argument("--prune", action="store_true", help="drop dead (404/410) entries")
    h.add_argument("--report", help="write per-source statuses as JSON")
    for s in (p, d):
        s.add_argument("--out", required=True)
    for s in (p, d, h):
        s.add_argument("--concurrency", type=int, default=4, choices=range(1, 9), metavar="1-8")
    a = ap.parse_args(argv)
    if a.mode == "health":
        run_health(a)
        return
    text = Path(a.slugs if a.mode == "probe" else a.websites).read_text("utf-8")
    if a.mode == "probe":
        if a.family not in job_connectors.FAMILIES:
            ap.error(f"unknown family {a.family!r}")
        targets = [(a.family, s) for s in parse_slugs(text)]
    else:
        targets = asyncio.run(
            detect([u.strip() for u in text.splitlines() if u.strip()], a.concurrency)
        )
    entries, stats = asyncio.run(probe(targets, a.concurrency))
    Path(a.out).write_text(json.dumps(entries, indent=2) + "\n", "utf-8")
    print(f"done: {stats} -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
