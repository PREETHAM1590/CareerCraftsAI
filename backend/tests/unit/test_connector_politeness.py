"""Host concurrency cap and 429 backoff, keyword refresh for Workday, source health check."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from app import board_harvest
from app.services import job_catalog
from app.services import job_connectors as connectors
from app.services.job_connectors import FAMILIES, Page, Source


def _response(status, headers=None):
    return httpx.Response(status, headers=headers or {}, json={})


def test_429_is_retried_with_the_retry_after_delay(monkeypatch):
    answers = [_response(429, {"retry-after": "3"}), _response(503), _response(200)]
    slept = []

    async def get(url, **kw):
        return answers.pop(0)

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(connectors, "public_get", get)
    monkeypatch.setattr(connectors.asyncio, "sleep", sleep)
    reply = asyncio.run(connectors._polite_get("https://boards-api.greenhouse.io/x"))
    assert reply.status_code == 200
    assert slept == [3.0, 2.0]


def test_a_persistent_429_is_returned_not_raised(monkeypatch):
    calls = []

    async def get(url, **kw):
        calls.append(url)
        return _response(429, {"retry-after": "86400"})

    async def sleep(seconds):
        assert seconds <= connectors._MAX_BACKOFF_S

    monkeypatch.setattr(connectors, "public_get", get)
    monkeypatch.setattr(connectors.asyncio, "sleep", sleep)
    reply = asyncio.run(connectors._polite_get("https://api.lever.co/v0/postings/x"))
    assert reply.status_code == 429
    assert len(calls) == connectors._MAX_RETRIES + 1


def test_requests_to_one_host_are_capped_but_other_hosts_are_not(monkeypatch):
    live = {"now": 0, "peak": 0}

    async def get(url, **kw):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return _response(200)

    monkeypatch.setattr(connectors, "public_get", get)

    async def run(urls):
        await asyncio.gather(*(connectors._polite_get(u) for u in urls))

    asyncio.run(run([f"https://api.lever.co/v0/postings/{i}" for i in range(20)]))
    assert live["peak"] == connectors.HOST_CONCURRENCY

    live["peak"] = 0
    asyncio.run(run([f"https://host{i}.example/x" for i in range(6)]))
    assert live["peak"] == 6


# --- keyword refresh ---------------------------------------------------------------------


class _Db:
    def __init__(self, health=None):
        self.health = health
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: self.health))

    async def commit(self):
        return None


def _workday():
    return Source(id="workday:x", family="workday", tenant="x.wd5.myworkdayjobs.com|x|Site")


def test_keyword_refresh_passes_each_role_to_the_server_and_stores_matches(monkeypatch):
    db, asked, stored = _Db(), [], []

    async def fetch(source, query, cursor):
        asked.append(query)
        return Page([{"job_id": query, "title": query}], None)

    async def upsert(session, jobs, source_id):
        stored.extend(jobs)

    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(job_catalog, "fetch_page", fetch)
    monkeypatch.setattr(job_catalog, "upsert_jobs", upsert)
    jobs, warning = asyncio.run(
        job_catalog.refresh_for_query(_workday(), ["data engineer", "ml engineer"])
    )
    assert asked == ["data engineer", "ml engineer"]
    assert len(jobs) == len(stored) == 2 and warning is None
    assert not any("DELETE" in s for s in db.statements)


def test_keyword_refresh_leaves_a_backing_off_board_alone(monkeypatch):
    from datetime import UTC, datetime, timedelta

    health = {
        "status": "degraded",
        "next_allowed_at": datetime.now(UTC) + timedelta(hours=1),
        "cached_jobs": [{"job_id": "c"}],
    }

    async def fetch(*a):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", lambda: _Db(health))
    monkeypatch.setattr(job_catalog, "fetch_page", fetch)
    jobs, warning = asyncio.run(job_catalog.refresh_for_query(_workday(), ["x"]))
    assert jobs == [{"job_id": "c"}] and "backing off" in warning


def test_gap_fill_uses_keyword_refresh_only_for_server_side_search_families(monkeypatch):
    keyword, plain = [], []

    async def by_query(source, roles):
        keyword.append((source.id, roles))
        return [], None

    async def by_source(source):
        plain.append(source.id)
        return [], None

    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", lambda: _Db())
    monkeypatch.setattr(job_catalog, "refresh_for_query", by_query)
    monkeypatch.setattr(job_catalog, "refresh_source", by_source)

    class Rows:
        def scalars(self):
            return self

        def all(self):
            return []

    async def execute(self, *a, **k):
        return Rows()

    monkeypatch.setattr(_Db, "execute", execute)
    public = [_workday(), Source(id="greenhouse:a", family="greenhouse", tenant="a")]
    query = {"titles": ["Senior Data Engineer"], "locations": [], "max_results": 5}
    asyncio.run(job_catalog._search_catalog_db(query, public, [], lambda rows: []))
    assert keyword == [("workday:x", ["Senior Data Engineer"])]
    assert plain == ["greenhouse:a"]


# --- health check --------------------------------------------------------------------------


def _status_error(code):
    return httpx.HTTPStatusError("x", request=None, response=httpx.Response(code))


def _classify(monkeypatch, outcome, family="greenhouse", unknown_sr=False):
    async def fetch(source, query, cursor):
        if isinstance(outcome, Exception):
            raise outcome
        return Page(outcome, None)

    async def unknown(tenant):
        return unknown_sr

    monkeypatch.setattr(board_harvest, "fetch_page", fetch)
    monkeypatch.setattr(board_harvest, "_smartrecruiters_unknown", unknown)
    source = Source(id=f"{family}:x", family=family, tenant="x")
    return asyncio.run(board_harvest.check_source(source))


def test_health_check_classifies_boards(monkeypatch):
    assert _classify(monkeypatch, [{"title": "t"}]) == "ok"
    assert _classify(monkeypatch, []) == "empty"
    assert _classify(monkeypatch, _status_error(404)) == "dead"
    assert _classify(monkeypatch, _status_error(429)) == "error"
    assert _classify(monkeypatch, _status_error(500)) == "error"
    assert _classify(monkeypatch, TimeoutError()) == "error"
    assert _classify(monkeypatch, _status_error(422), family="workday") == "dead"
    assert _classify(monkeypatch, _status_error(422)) == "error"
    # SmartRecruiters answers 200/empty for any id; only the careers page tells them apart.
    assert _classify(monkeypatch, [], family="smartrecruiters") == "empty"
    assert _classify(monkeypatch, [], family="smartrecruiters", unknown_sr=True) == "dead"


def test_health_counts_group_by_family():
    rows = [
        {"id": "a", "family": "lever"},
        {"id": "b", "family": "lever"},
        {"id": "c", "family": "ashby"},
        {"id": "skipped", "family": "adzuna"},
    ]
    counts = board_harvest.health_counts(rows, {"a": "ok", "b": "dead", "c": "empty"})
    assert counts == {
        "lever": {"ok": 1, "empty": 0, "dead": 1, "error": 0},
        "ashby": {"ok": 0, "empty": 1, "dead": 0, "error": 0},
    }


def test_health_prune_drops_only_dead_entries(monkeypatch, tmp_path):
    path = tmp_path / "job_sources.json"
    rows = [
        {"id": "lever:alive", "family": "lever", "tenant": "alive"},
        {"id": "lever:dead", "family": "lever", "tenant": "dead"},
        {"id": "ashby:empty", "family": "ashby", "tenant": "empty"},
    ]
    path.write_text(json.dumps(rows))
    monkeypatch.setattr(board_harvest, "SOURCES", path)

    async def fake_health(rows, concurrency):
        return {"lever:alive": "ok", "lever:dead": "dead", "ashby:empty": "empty"}

    monkeypatch.setattr(board_harvest, "health", fake_health)
    board_harvest.main(["health", "--prune", "--report", str(tmp_path / "r.json")])
    kept = [r["id"] for r in json.loads(path.read_text())]
    assert kept == ["lever:alive", "ashby:empty"]
    assert json.loads((tmp_path / "r.json").read_text())["lever:dead"] == "dead"


# --- catalog file -----------------------------------------------------------------------------


def test_catalog_entries_are_unique_known_families_and_resolvable():
    rows = json.loads(Path(connectors.__file__).with_name("job_sources.json").read_text())
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert {row["family"] for row in rows} <= FAMILIES
    for row in rows:
        if row["family"] == "workday":
            host, tenant, site = row["tenant"].split("|")
            assert connectors._WORKDAY_HOST.fullmatch(host) and tenant and site


def test_dead_boards_found_by_the_health_check_stay_out_of_the_catalog():
    ids = {
        r["id"]
        for r in json.loads(Path(connectors.__file__).with_name("job_sources.json").read_text())
    }
    assert not ids & {"greenhouse:hubspot", "smartrecruiters:bosch", "smartrecruiters:visa-x"}
    assert not any(i.startswith("lever:netflix") or i == "recruitee:mollie" for i in ids)
    assert "lever:netflix" not in job_catalog.PREFERRED_SOURCES
