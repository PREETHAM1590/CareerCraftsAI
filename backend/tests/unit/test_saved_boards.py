"""Direct company-portal search and saved boards (service layer and HTTP routes)."""

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services import saved_boards as sb
from app.services.job_connectors import Source


def _job(title, location="Remote", company="Acme", posted="2026-01-02T00:00:00+00:00", slug=None):
    slug = slug or title.lower().replace(" ", "-")
    return {
        "job_id": slug,
        "title": title,
        "company": company,
        "location": location,
        "remote": "unknown",
        "url": f"https://boards.greenhouse.io/acme/jobs/{slug}",
        "platform": "greenhouse",
        "posted_at": posted,
        "description": "",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/jobs",
        "http://169.254.169.254/latest/meta-data",
        "https://boards.greenhouse.io.evil.com/acme",
        "https://acme.myworkdayjobs.com",
        "not a url at all",
    ],
)
def test_unrecognised_links_are_rejected_before_any_fetch(url, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not fetch")

    monkeypatch.setattr(sb, "search_board", boom)
    with pytest.raises(sb.PortalError) as err:
        sb.resolve_board(url)
    assert err.value.status_code == 422
    assert "Workday" in err.value.detail


def test_known_portals_resolve():
    assert sb.resolve_board("https://boards.greenhouse.io/acme").id == "greenhouse:acme"
    assert sb.resolve_board("jobs.lever.co/acme/").family == "lever"
    workday = sb.resolve_board(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
    )
    assert workday.family == "workday"


def test_search_portal_keeps_every_requested_location_newest_first(monkeypatch):
    jobs = [
        _job("Old Berlin", "Berlin, Germany", posted="2025-01-01T00:00:00+00:00"),
        _job("London One", "London, UK", posted="2026-03-01T00:00:00+00:00"),
        _job("Paris One", "Paris, France", posted="2026-04-01T00:00:00+00:00"),
        _job("New Berlin", "Berlin, Germany", posted="2026-05-01T00:00:00+00:00"),
    ]

    async def fake(source, query, pages):
        assert (query, pages) == ("engineer", 5)
        return list(jobs)

    monkeypatch.setattr(sb, "search_board", fake)
    source = Source(id="greenhouse:acme", family="greenhouse", tenant="acme")
    found, total = asyncio.run(sb.search_portal(source, "engineer", ["Berlin", "London"], 2))
    assert [j["title"] for j in found] == ["New Berlin", "London One"]
    assert total == 3


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (httpx.HTTPStatusError("x", request=None, response=httpx.Response(404)), 404),
        (httpx.HTTPStatusError("x", request=None, response=httpx.Response(422)), 404),
        (httpx.HTTPStatusError("x", request=None, response=httpx.Response(429)), 429),
        (TimeoutError(), 504),
        (ValueError("secret detail"), 502),
    ],
)
def test_search_portal_failures_become_clear_errors(monkeypatch, error, status):
    async def fake(*a, **k):
        raise error

    monkeypatch.setattr(sb, "search_board", fake)
    source = Source(id="greenhouse:acme", family="greenhouse", tenant="acme")
    with pytest.raises(sb.PortalError) as err:
        asyncio.run(sb.search_portal(source, "", [], 10))
    assert err.value.status_code == status
    assert "secret" not in err.value.detail


class FakeDb:
    def __init__(self, rows=()):
        self.rows, self.added, self.commits = list(rows), [], 0

    async def execute(self, *a, **k):
        return SimpleNamespace(all=lambda: self.rows)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1


def _row(url, company="Other", role="Role", deleted=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        job_url=url,
        company=company,
        role=role,
        location="",
        deleted_at=deleted,
    )


def test_save_jobs_skips_duplicates_and_removed_jobs():
    mine = _row("https://boards.greenhouse.io/acme/jobs/have")
    gone = _row("https://boards.greenhouse.io/acme/jobs/gone", deleted=datetime.now(UTC))
    db = FakeDb([mine, gone])
    jobs = [_job("Have", slug="have"), _job("Gone", slug="gone"), _job("Fresh", slug="fresh")]
    ids, created = asyncio.run(sb.save_jobs(db, uuid.uuid4(), jobs))
    assert created == 1
    assert len(db.added) == 1 and db.added[0].role == "Fresh"
    assert ids == [mine.id, db.added[0].id]
    assert db.added[0].status == "saved" and db.added[0].source == "greenhouse"
    assert db.commits == 1


def test_save_jobs_does_not_save_the_same_role_twice_in_one_batch():
    db = FakeDb()
    twin = {**_job("Same", slug="a"), "url": "https://jobs.lever.co/acme/b"}
    ids, created = asyncio.run(sb.save_jobs(db, uuid.uuid4(), [_job("Same", slug="a"), twin]))
    assert created == 1 and len(ids) == 1


def test_save_jobs_keeps_distinct_openings_on_one_board():
    db = FakeDb()
    other = {**_job("Same", slug="a"), "url": "https://boards.greenhouse.io/acme/jobs/b"}
    ids, created = asyncio.run(sb.save_jobs(db, uuid.uuid4(), [_job("Same", slug="a"), other]))
    assert created == 2 and len(ids) == 2


def test_save_jobs_merges_tracking_variants_of_one_posting():
    db = FakeDb()
    first = _job("Same", slug="a")
    variant = {**first, "url": first["url"] + "?gh_src=abc123"}
    ids, created = asyncio.run(sb.save_jobs(db, uuid.uuid4(), [first, variant]))
    assert created == 1 and len(ids) == 1


def test_save_jobs_never_persists_non_https_urls():
    db = FakeDb()
    bad = [{**_job("X", slug="x"), "url": u} for u in ("javascript:alert(1)", "data:text/html,x")]
    ids, created = asyncio.run(sb.save_jobs(db, uuid.uuid4(), bad))
    assert created == 0 and not db.added


def _board(url, label="Acme"):
    return sb.SavedBoard(uuid.uuid4(), "x", "x", label, url, datetime.now(UTC))


class _Session:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


def _use_boards(monkeypatch, boards, search):
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", lambda: _Session())

    async def listing(db, user_id):
        return boards

    monkeypatch.setattr(sb, "list_boards", listing)
    monkeypatch.setattr(sb, "search_board", search)


def test_saved_boards_search_each_role_server_side_and_filter_locally(monkeypatch):
    calls = []

    async def search(source, query, pages):
        calls.append((source.family, query))
        return [_job("Backend Engineer", "Berlin"), _job("Chef", "Berlin")]

    boards = [
        _board("https://boards.greenhouse.io/acme"),
        _board("https://nvidia.wd5.myworkdayjobs.com/en-US/Site", "Nv"),
    ]
    _use_boards(monkeypatch, boards, search)
    query = {"titles": ["Backend Engineer", "Platform Engineer"], "locations": ["Berlin"]}
    jobs, warnings = asyncio.run(sb.search_saved_boards(str(uuid.uuid4()), query))
    assert sorted(calls) == [
        ("greenhouse", ""),
        ("workday", "Backend Engineer"),
        ("workday", "Platform Engineer"),
    ]
    assert [j["title"] for j in jobs] == ["Backend Engineer"]
    assert warnings == []


def test_saved_boards_failures_and_stale_links_only_warn(monkeypatch):
    async def search(source, query, pages):
        raise httpx.HTTPStatusError("x", request=None, response=httpx.Response(404))

    boards = [
        _board("https://boards.greenhouse.io/acme", "Acme"),
        _board("https://evil.example.com/jobs", "Evil"),
    ]
    _use_boards(monkeypatch, boards, search)
    jobs, warnings = asyncio.run(sb.search_saved_boards(str(uuid.uuid4()), {"titles": ["x"]}))
    assert jobs == []
    assert any(w.startswith("Acme:") for w in warnings)
    assert any("Evil" in w and "no longer supported" in w for w in warnings)


def test_agent_adds_saved_board_jobs_unless_a_source_was_picked(monkeypatch):
    from app.agents import job_search as agent

    seen = []

    async def boards(user_id, query):
        seen.append(user_id)
        return [_job("Board Job", slug="board")], ["Acme: slow"]

    monkeypatch.setattr(sb, "search_saved_boards", boards)
    warnings = []
    merged = agent._with_saved_boards(
        "u1", {"titles": ["x"]}, [_job("Cat Job", slug="cat")], warnings
    )
    assert {j["title"] for j in merged} == {"Cat Job", "Board Job"}
    assert warnings == ["Acme: slow"]

    async def broken(user_id, query):
        raise RuntimeError("db down")

    monkeypatch.setattr(sb, "search_saved_boards", broken)
    warnings = []
    assert agent._with_saved_boards("u1", {}, [_job("Cat Job")], warnings)[0]["title"] == "Cat Job"
    assert warnings == ["Saved company boards are unavailable"]


# --- HTTP routes ---------------------------------------------------------------------------


def _client(monkeypatch, db=None):
    from httpx import ASGITransport, AsyncClient

    from app.api.v1.deps import get_current_user, get_db
    from app.main import app

    payload = {"sub": "00000000-0000-0000-0000-000000000001", "email": "t@e.com"}
    monkeypatch.setattr("app.core.clerk_auth.verify_token", lambda token: payload)
    monkeypatch.setattr("app.api.v1.deps.verify_auth_jwt", lambda token: payload)
    user = SimpleNamespace(id=uuid.UUID(payload["sub"]))

    async def fake_user():
        return user

    async def fake_db():
        return db

    monkeypatch.setitem(app.dependency_overrides, get_current_user, fake_user)
    monkeypatch.setitem(app.dependency_overrides, get_db, fake_db)

    def call(method, path, **kw):
        async def go():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.request(
                    method, "/api/v1/jobs" + path, headers={"Authorization": "Bearer t"}, **kw
                )

        try:
            return asyncio.run(go())
        finally:
            app.dependency_overrides.clear()

    return call


def test_portal_search_rejects_unsupported_link_with_a_clear_message(monkeypatch):
    call = _client(monkeypatch)
    resp = call("POST", "/portal-search", json={"url": "https://example.com/careers"})
    assert resp.status_code == 422
    assert "not a careers page we can read" in resp.json()["detail"]


def test_portal_search_returns_saved_jobs_and_optionally_saves_the_board(monkeypatch):
    from app.api.v1 import jobs as jobs_module

    first, second = uuid.uuid4(), uuid.uuid4()
    saved_board = uuid.uuid4()
    added = []

    async def fake_search(source, query, locations, max_results):
        assert (source.id, query, locations, max_results) == (
            "greenhouse:acme",
            "backend",
            ["Berlin", "London"],
            5,
        )
        return [_job("A", "Berlin"), _job("B", "London", company="Acme Inc")], 7

    async def fake_save(db, user_id, jobs):
        return [first, second], 1

    async def fake_add(db, user_id, source, url, label):
        added.append((source.id, url, label))
        return SimpleNamespace(id=saved_board)

    async def fake_tracking(db, user_id, rows):
        return [
            jobs_module.ApplicationResponse(
                id=r.id,
                company="Acme",
                role=f"R{i}",
                location=None,
                job_url=None,
                match_score=None,
                status="saved",
                applied_at=None,
            )
            for i, r in enumerate(rows)
        ]

    rows = [SimpleNamespace(id=first), SimpleNamespace(id=second)]
    db = SimpleNamespace()

    async def execute(*a, **k):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    db.execute = execute
    monkeypatch.setattr(sb, "search_portal", fake_search)
    monkeypatch.setattr(sb, "save_jobs", fake_save)
    monkeypatch.setattr(sb, "add_board", fake_add)
    monkeypatch.setattr(jobs_module, "_with_tracking", fake_tracking)
    call = _client(monkeypatch, db)
    resp = call(
        "POST",
        "/portal-search",
        json={
            "url": "https://boards.greenhouse.io/acme",
            "query": "backend",
            "locations": ["Berlin", " London "],
            "max_results": 5,
            "save_board": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_found"] == 7 and body["saved_count"] == 1
    assert [j["id"] for j in body["jobs"]] == [str(first), str(second)]
    assert body["board"]["saved_board_id"] == str(saved_board)
    assert body["board"]["label"] == "Acme"
    assert added == [("greenhouse:acme", "https://boards.greenhouse.io/acme", "Acme")]


def test_portal_search_maps_upstream_errors(monkeypatch):
    async def fake_search(*a, **k):
        raise sb.PortalError(404, "That portal has no job board at this address.")

    monkeypatch.setattr(sb, "search_portal", fake_search)
    call = _client(monkeypatch)
    resp = call("POST", "/portal-search", json={"url": "https://boards.greenhouse.io/nope"})
    assert resp.status_code == 404


def test_saved_board_crud_routes(monkeypatch):
    board = sb.SavedBoard(
        uuid.uuid4(), "lever:acme", "lever", "Acme", "https://jobs.lever.co/acme", datetime.now(UTC)
    )
    removed = []

    async def listing(db, user_id):
        return [board]

    async def add(db, user_id, source, url, label):
        assert (source.id, label) == ("lever:acme", "Acme")
        return board

    async def remove(db, user_id, board_id):
        removed.append(board_id)
        return board_id == board.id

    monkeypatch.setattr(sb, "list_boards", listing)
    monkeypatch.setattr(sb, "add_board", add)
    monkeypatch.setattr(sb, "remove_board", remove)

    assert _client(monkeypatch)("GET", "/boards").json()[0]["source_id"] == "lever:acme"
    created = _client(monkeypatch)("POST", "/boards", json={"url": "jobs.lever.co/acme"})
    assert created.status_code == 201 and created.json()["label"] == "Acme"
    assert (
        _client(monkeypatch)("POST", "/boards", json={"url": "https://x.example"}).status_code
        == 422
    )
    assert _client(monkeypatch)("DELETE", f"/boards/{board.id}").status_code == 204
    assert _client(monkeypatch)("DELETE", f"/boards/{uuid.uuid4()}").status_code == 404
