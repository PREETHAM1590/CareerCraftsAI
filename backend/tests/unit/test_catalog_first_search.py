"""W3a: catalog-first search with live gap fill, write-through and no result caps.

No DB, network or LLM: search_catalog / write_through / adapters / AsyncSessionLocal are mocked.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import job_catalog as cat
from app.services import job_search_service as svc


def _job(i: int, **extra) -> dict:
    return {
        "job_id": f"j{i}",
        "title": f"Backend Engineer {i}",
        "company": f"Co{i}",
        "location": "Remote",
        "remote": "remote",
        "url": f"https://jobs.example/{i}",
        "description": "python backend",
        "platform": "x",
        **extra,
    }


def _raw(i: int) -> dict:
    return {k: v for k, v in _job(i).items() if k != "job_id"}


async def _search(platforms, catalog, adapter, wt, **q):
    query = {"titles": ["Backend"], "locations": ["Remote"], "max_results": 10, **q}
    with (
        patch("app.services.job_catalog.search_catalog", catalog),
        patch("app.services.job_catalog.write_through", wt),
        patch.dict(svc._ADAPTERS, {"jobspy": adapter}, clear=True),
    ):
        return await svc.search_all_platforms(query, platforms)


@pytest.mark.asyncio
async def test_catalog_hit_with_enough_fresh_jobs_skips_live_fetch():
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    adapter, wt = MagicMock(return_value=[]), AsyncMock()
    catalog = AsyncMock(return_value=([_job(i, last_seen_at=now) for i in range(12)], []))
    jobs, _ = await _search(None, catalog, adapter, wt)  # default sources
    assert len(jobs) == 12
    adapter.assert_not_called()
    wt.assert_not_called()
    # live:<platform> rows other users stored are read from the catalog
    assert "jobspy" in catalog.await_args.kwargs["live_platforms"]


@pytest.mark.asyncio
async def test_stale_catalog_or_explicit_pick_still_runs_live():
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    now = datetime.now(UTC).isoformat()
    for platforms, seen in ((None, old), (["jobspy"], now)):
        adapter, wt = MagicMock(return_value=[_raw(500)]), AsyncMock()
        catalog = AsyncMock(return_value=([_job(i, last_seen_at=seen) for i in range(12)], []))
        jobs, _ = await _search(platforms, catalog, adapter, wt)
        adapter.assert_called()
        assert len(jobs) == 13


@pytest.mark.asyncio
async def test_shortfall_runs_live_and_writes_through():
    adapter = MagicMock(return_value=[_raw(i) for i in range(100, 103)])
    wt = AsyncMock()
    catalog = AsyncMock(return_value=([_job(i) for i in range(2)], []))
    jobs, _ = await _search(["jobspy"], catalog, adapter, wt)
    assert len(jobs) == 5
    adapter.assert_called_once()
    wt.assert_awaited_once()
    stored, platform = wt.await_args.args
    assert platform == "jobspy" and len(stored) == 3


@pytest.mark.asyncio
async def test_no_result_cap_on_live_results():
    # old cap: max_results(10) * 1 platform * 1 location = 10
    adapter = MagicMock(return_value=[_raw(i) for i in range(120)])
    jobs, _ = await _search(["jobspy"], AsyncMock(return_value=([], [])), adapter, AsyncMock())
    assert len(jobs) == 120


@pytest.mark.asyncio
async def test_catalog_write_failure_does_not_fail_search():
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    adapter = MagicMock(return_value=[_raw(i) for i in range(4)])
    query = {"titles": ["Backend"], "locations": ["Remote"], "max_results": 10}
    with (
        patch("app.services.job_catalog.search_catalog", AsyncMock(return_value=([], []))),
        patch.object(cat, "AsyncSessionLocal", lambda: Boom()),
        patch.dict(svc._ADAPTERS, {"jobspy": adapter}, clear=True),
    ):
        jobs, _ = await svc.search_all_platforms(query, ["jobspy"])
    assert len(jobs) == 4
    assert await cat.write_through([_raw(1)], "jobspy") == 0


@pytest.mark.asyncio
async def test_write_through_uses_live_source_and_shared_upsert():
    db = MagicMock()
    db.execute, db.commit = AsyncMock(), AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(cat, "AsyncSessionLocal", lambda: session),
        patch.object(cat, "upsert_jobs", AsyncMock()) as upsert,
    ):
        n = await cat.write_through([_raw(1), {"url": "", "title": "", "company": ""}], "jobspy")
    assert n == 1
    stored, source_id = upsert.await_args.args[1:]
    assert source_id == "live:jobspy" and stored[0]["description"] == "python backend"
    assert len(stored[0]["job_id"]) == 32  # same id scheme as public sources
    assert "job_source_health" in str(db.execute.await_args_list[0].args[0])
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_catalog_serves_fresh_rows_first_and_has_no_300_cap():
    rows = [
        {
            **_job(i),
            "url": f"https://co{i}.example/job",
            "company": f"Co{i}",
            "occurrences": [],
        }
        for i in range(400)
    ]
    for r in rows:
        r.update(posted_at=None, expires_at=None, first_seen_at="", last_seen_at="")
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    refresh = AsyncMock(return_value=([], None))
    with (
        patch.object(cat, "AsyncSessionLocal", lambda: session),
        patch.object(cat, "refresh_source", refresh),
    ):
        jobs, _ = await cat.search_catalog({"titles": ["Backend"], "max_results": 10}, ["remotive"])
    assert len(jobs) == 400
    refresh.assert_not_called()  # enough fresh rows: no gap fill
    assert cat.CATALOG_FRESH_DAYS == 14


@pytest.mark.asyncio
async def test_search_catalog_gap_fills_when_short_not_only_when_empty():
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    fresh = {**_job(1), "posted_at": None, "expires_at": None, "occurrences": []}
    refreshed = {**_job(2), "posted_at": None, "expires_at": None, "occurrences": []}
    # one stored row (< need) -> refresh runs and its jobs are merged
    result.scalars.return_value.all.return_value = [fresh]
    refresh = AsyncMock(return_value=([refreshed], None))
    with (
        patch.object(cat, "AsyncSessionLocal", lambda: session),
        patch.object(cat, "refresh_source", refresh),
    ):
        jobs, _ = await cat.search_catalog({"titles": ["Backend"], "max_results": 10}, ["remotive"])
    assert refresh.await_count >= 1
    assert {j["job_id"] for j in jobs} == {"j1", "j2"}


@pytest.mark.asyncio
async def test_rank_jobs_returns_all_matches_with_semantic_on_top_100_only():
    from app.services import job_matching as jm

    jobs = [_job(i) for i in range(120)]
    semantic = AsyncMock(return_value={})
    with (
        patch.object(jm, "basis_text", AsyncMock(return_value=("python backend", None))),
        patch.object(jm, "semantic_scores", semantic),
        patch("app.services.github_profile.get_profile", AsyncMock(return_value=None)),
        patch("app.services.funding_signals.funded_among", AsyncMock(return_value=set())),
    ):
        matches, _ = await jm.rank_jobs(
            str(uuid.uuid4()), {"titles": ["Backend"], "max_results": 10}, jobs
        )
    assert len(matches) == 120
    assert len(semantic.await_args.args[2]) == 100


# --- relevance + location (job search returned unrelated / foreign roles) ---


def test_location_ok_city_search_keeps_local_and_unrestricted_remote_only():
    from app.services.job_catalog import _location_ok

    want = ["Bengaluru"]
    assert _location_ok({"location": "Bengaluru, Karnataka, India"}, want)
    assert _location_ok({"location": "Bangalore"}, want)  # alias
    assert _location_ok({"location": "Remote", "remote": "remote"}, want)
    assert _location_ok({"location": "Remote - India", "remote": "remote"}, want)
    assert not _location_ok({"location": "Remote, United States", "remote": "remote"}, want)
    assert not _location_ok({"location": "Dublin"}, want)
    assert not _location_ok({"location": "Seattle, San Francisco"}, want)
    # Remote / anywhere searches accept every location.
    assert _location_ok({"location": "Dublin"}, ["Remote"])
    assert _location_ok({"location": "Dublin"}, [])


async def test_catalog_requires_every_query_word_in_title_and_the_location(monkeypatch):
    import app.services.job_catalog as catalog

    def job(title, location, desc="Work with software engineers on our platform."):
        return {"title": title, "location": location, "description": desc}

    rows = [
        job("Software Engineer", "Bengaluru, India"),
        job("Senior Software Engineer, Payments", "Remote"),
        job("Abuse Investigator", "Bengaluru"),  # 'software engineer' only in description
        job("Account Executive", "Dublin"),
        job("Software Engineer, Stripe Tax", "Barcelona"),  # wrong city
        job("Research Engineer", "Bengaluru"),  # 'software' missing from title
    ]
    monkeypatch.setattr(catalog, "dedupe", lambda r, days: r)
    monkeypatch.setattr(catalog, "sources", lambda: [])

    class Result:
        def scalars(self):
            return self

        def all(self):
            return rows

    class DB:
        async def execute(self, *a, **k):
            return Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(catalog, "AsyncSessionLocal", DB)
    jobs, _ = await catalog.search_catalog(
        {"titles": ["software engineer"], "locations": ["Bengaluru"], "max_results": 1},
        None,
        live_platforms=["jobspy"],
    )
    assert [j["title"] for j in jobs] == [
        "Software Engineer",
        "Senior Software Engineer, Payments",
    ]


def test_jobspy_is_a_default_source_with_a_budget_linkedin_fits_in():
    from app.services import job_search_service as svc

    assert "jobspy" in svc.DEFAULT_PLATFORMS
    assert svc._ADAPTER_TIMEOUT_SEC["jobspy"] >= 60


def test_dotted_country_and_city_coverage():
    from app.services.job_catalog import _location_ok, in_city

    assert not _location_ok({"location": "Remote U.S.", "remote": "remote"}, ["Bengaluru"])
    assert in_city({"location": "Bangalore, India"}, ["Bengaluru"])
    assert not in_city({"location": "Remote"}, ["Bengaluru"])
    assert not in_city({"location": "Bengaluru"}, ["Remote"])


@pytest.mark.asyncio
async def test_live_and_warm_results_share_role_city_and_country_rules():
    rows = [
        _job(1, title="Software Engineer", location="Bengaluru, India", remote="onsite"),
        _job(2, title="Finance Analyst", location="Bengaluru", remote="onsite"),
        _job(
            3,
            title="Software Engineer",
            location="New York, United States",
            remote="onsite",
        ),
        _job(4, title="Software Engineer", location="Remote U.S.", remote="remote"),
        _job(
            5,
            title="Senior Software Engineer",
            location="Remote India",
            remote="remote",
        ),
    ]
    query = {
        "titles": ["Software Engineer"],
        "locations": ["Bengaluru"],
        "max_results": 10,
    }
    cold, _ = await _search(
        ["jobspy"],
        AsyncMock(return_value=([], [])),
        MagicMock(return_value=rows),
        AsyncMock(),
        **query,
    )
    warm, _ = await _search(
        ["jobspy"],
        AsyncMock(return_value=(rows, [])),
        MagicMock(return_value=[]),
        AsyncMock(),
        **query,
    )
    assert {job["url"] for job in cold} == {rows[0]["url"], rows[4]["url"]}
    assert {job["url"] for job in warm} == {job["url"] for job in cold}


def test_shared_work_mode_treats_hybrid_separately_from_remote():
    row = _job(1, remote="hybrid", location="Bengaluru (hybrid/remote)")
    assert cat.matches_query(row, {"titles": ["Backend"], "remote": "hybrid"})
    assert not cat.matches_query(row, {"titles": ["Backend"], "remote": "remote"})


@pytest.mark.asyncio
async def test_live_search_fans_out_roles_and_cities_with_shared_predicate():
    seen = []

    def adapter(role, city, count):
        seen.append((role, city))
        return [_raw(len(seen)) | {"title": role, "location": city}]

    jobs, warnings = await _search(
        ["jobspy"],
        AsyncMock(return_value=([], [])),
        adapter,
        AsyncMock(),
        titles=["Backend Engineer", "Data Scientist"],
        locations=["Bengaluru", "Hyderabad"],
    )
    assert set(seen) == {
        (role, city)
        for role in ("Backend Engineer", "Data Scientist")
        for city in ("Bengaluru", "Hyderabad")
    }
    assert len(jobs) == 4 and not warnings


@pytest.mark.asyncio
async def test_overall_budget_preserves_completed_source_results(monkeypatch):
    import time

    def adapter(role, city, count):
        if city == "Hyderabad":
            time.sleep(0.15)
        return [_raw(1) | {"location": city}]

    monkeypatch.setattr(cat, "search_catalog", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(cat, "write_through", AsyncMock())
    monkeypatch.setattr(svc, "_ADAPTERS", {"jobspy": adapter})
    jobs, warnings = await svc.search_all_platforms(
        {"titles": ["Backend"], "locations": ["Bengaluru", "Hyderabad"]},
        ["jobspy"],
        timeout_s=0.05,
    )
    assert [j["location"] for j in jobs] == ["Bengaluru"]
    assert any("budget" in warning for warning in warnings)


def test_timed_out_workers_keep_capacity_across_event_loops(monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    started = threading.Event()
    calls = []

    def adapter(role, city, count):
        calls.append(city)
        started.set()
        assert release.wait(3)
        return [_raw(1) | {"location": city}]

    monkeypatch.setattr(cat, "search_catalog", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(cat, "write_through", AsyncMock())
    monkeypatch.setattr(svc, "_ADAPTERS", {"jobspy": adapter})
    monkeypatch.setattr(svc, "_ADAPTER_TIMEOUT_SEC", {"jobspy": 0.05})
    query = {"titles": ["Backend"], "locations": ["Bengaluru"]}
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(svc, "_ADAPTER_EXECUTOR", executor)
        try:
            jobs, warnings = asyncio.run(svc.search_all_platforms(query, ["jobspy"]))
            assert started.is_set()
            assert not jobs and any("TimeoutError" in w for w in warnings)
            # The first event loop is closed, but its worker still owns the thread, so
            # the second search only queues and times out.
            jobs, warnings = asyncio.run(svc.search_all_platforms(query, ["jobspy"], timeout_s=0.1))
            assert not jobs and warnings
            assert calls == ["Bengaluru"]
        finally:
            release.set()
        # Wait for completion/callback before checking recovery on a third loop.
        executor.submit(lambda: None).result(timeout=3)
        jobs, warnings = asyncio.run(svc.search_all_platforms(query, ["jobspy"]))
        assert len(jobs) == 1 and not warnings
        assert len(calls) == 2


@pytest.mark.parametrize("role", ["QA", "UX", "PM", "AI", "ML", "C#"])
def test_short_role_alternatives_never_match_unrelated_jobs(role):
    query = {"titles": [role, "Backend Engineer"]}
    assert cat.matches_query(_job(1, title=f"Senior {role} Engineer"), query)
    assert cat.matches_query(_job(2, title="Backend Engineer"), query)
    assert not cat.matches_query(_job(3, title="Financial Analyst"), query)
    assert not cat.matches_query(_job(4, title=f"prefix{role}suffix"), query)
    assert not cat.matches_query(
        _job(5, title="Software Engineer"), {"titles": [f"{role} Engineer"]}
    )


def test_stopword_only_role_does_not_disable_other_alternatives():
    query = {"titles": ["the job", "Backend Engineer"]}
    assert not cat.matches_query(_job(1, title="Financial Analyst"), query)
    assert cat.matches_query(_job(2, title="Backend Engineer"), query)


@pytest.mark.asyncio
async def test_catalog_sql_prefilter_keeps_short_role_alternatives():
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        _job(1, title="QA Analyst"),
        _job(2, title="Backend Engineer"),
    ]
    db.execute = AsyncMock(return_value=result)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)
    with patch.object(cat, "AsyncSessionLocal", lambda: session):
        jobs, _ = await cat.search_catalog(
            {"titles": ["QA", "Backend Engineer"]}, [], live_platforms=["jobspy"]
        )
    assert db.execute.await_args.args[1]["title_0_0"] == r"(?<!\w)qa(?!\w)"
    assert db.execute.await_args.args[1]["title_1_0"] == "backend"
    assert db.execute.await_args.args[1]["title_1_1"] == "engineer"
    assert {job["title"] for job in jobs} == {"QA Analyst", "Backend Engineer"}
