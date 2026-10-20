"""Unit tests for the job_search pass and its Temporal-started API route.

Search I/O is NEVER called here — every test mocks
services/job_search_service.search_all_platforms() (or the service adapters).
The LLM only scores via prompts/job_search_prompt in batches of 20.
"""

from __future__ import annotations

from types import SimpleNamespace

import uuid
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.prompts.job_search_prompt import JobMatch, JobSearchOutput
from app.agents.state import AgentState


def make_state(**ctx) -> AgentState:
    context = {"titles": ["Backend Engineer"], "locations": ["Bengaluru"], "max_results": 10}
    context.update(ctx)
    return AgentState(
        user_id="usr_test123",
        run_id=str(uuid.uuid4()),
        task_type="job_search",
        messages=[HumanMessage(content="find backend jobs")],
        context=context,
        status="running",
        pending_action=None,
        result=None,
        error=None,
    )


def _job(i: int) -> dict:
    return {
        "job_id": f"j{i}",
        "title": f"Backend Engineer {i}",
        "company": "Acme",
        "location": "Bengaluru",
        "remote": "onsite",
        "salary_text": "",
        "url": f"https://example.com/j/{i}",
        "platform": "open_apis",
        "posted_at": None,
        "description": "Python APIs",
    }


def _scored_output(jobs: list[dict], score: int = 80) -> JobSearchOutput:
    return JobSearchOutput(
        matches=[
            JobMatch(
                job_id=j["job_id"], score=score, reasons=["fit"], red_flags=[], missing_skills=[]
            )
            for j in jobs
        ],
        top_pick_id=jobs[0]["job_id"] if jobs else None,
    )


def _node_patches(**overrides):
    from app.agents import job_search as js

    defaults = dict(
        fetch_model_settings=MagicMock(provider="openai"),
        fetch_user_profile_text="profile",
        search_all_platforms=([], []),
        call_llm_json=None,
        _persist_saved_jobs=0,
        emit=None,
    )
    defaults.update(overrides)
    return [
        patch.object(js, "fetch_model_settings", return_value=defaults["fetch_model_settings"]),
        patch.object(
            js, "fetch_user_profile_text", return_value=defaults["fetch_user_profile_text"]
        ),
        patch.object(js, "search_all_platforms", return_value=defaults["search_all_platforms"]),
        patch.object(js, "emit"),
    ]


def test_search_returns_empty_complete_with_warnings_and_no_llm_call():
    from app.agents import job_search as js

    patches = _node_patches(search_all_platforms=([], ["open_apis failed: Timeout"]))
    from unittest.mock import AsyncMock

    with patches[0], patches[1], patches[2], patches[3]:
        with patch("app.services.job_matching.rank_jobs", AsyncMock(return_value=([], []))):
            with patch.object(js, "call_llm_json") as mock_score:
                result = js.job_search_agent_node(make_state())
                mock_score.assert_not_called()
    assert result["status"] == "completed"
    assert result["result"]["matches"] == []
    assert result["result"]["top_pick_id"] is None
    assert result["result"]["warnings"]


def test_45_postings_all_scored_then_ranked_before_truncation():
    from unittest.mock import AsyncMock

    from app.agents import job_search as js

    jobs = [_job(i) for i in range(45)]
    matches = [{**jobs[40], "match_score": 99}] + [{**j, "match_score": 50} for j in jobs[:24]]
    rank = AsyncMock(return_value=(matches, []))
    patches = _node_patches(search_all_platforms=(jobs, []))
    with patches[0], patches[1], patches[2], patches[3]:
        with (
            patch("app.services.job_matching.rank_jobs", rank),
            patch.object(js, "_persist_saved_jobs", return_value=25),
        ):
            result = js.job_search_agent_node(make_state(max_results=25))
    assert len(rank.await_args.args[2]) == 45
    assert len(result["result"]["matches"]) == 25
    assert result["result"]["matches"][0]["job_id"] == "j40"
    assert result["result"]["total_found"] == 45


def test_missing_titles_returns_failed_shape():
    from app.agents import job_search as js

    result = js.job_search_agent_node(make_state(titles=[], search_query=None))
    assert result["status"] == "failed"
    assert "titles" in result["error"]


def test_one_platform_failure_still_returns_others():
    import asyncio

    from app.services import job_search_service as svc

    good = [_job(0)]

    async def go():
        # NOTE: run_one resolves adapters via the _ADAPTERS registry, so
        # patch the registry entries — patching module attrs would miss.
        with patch.dict(
            svc._ADAPTERS,
            {
                "open_apis": MagicMock(side_effect=Exception("boom")),
                "jobspy": MagicMock(return_value=[{**_job(0), "platform": "x"}]),
                "ats": MagicMock(return_value=[]),
                "remoteok": MagicMock(return_value=[]),
            },
        ):
            return await svc.search_all_platforms(
                {"titles": ["Backend"], "locations": [], "max_results": 10},
                ["open_apis", "jobspy", "ats"],
            )

    jobs, warnings = asyncio.run(go())
    assert len(jobs) == 1
    assert any("open_apis failed" in w for w in warnings)


def test_search_all_platforms_fans_out_over_every_location():
    """A multi-location request must hit every location, not just the first."""
    import asyncio

    from app.services import job_search_service as svc

    seen_locations: list[str] = []

    def fake_adapter(query, location, max_results):
        seen_locations.append(location)
        return [{**_job(len(seen_locations)), "location": location}]

    async def go():
        with patch.dict(svc._ADAPTERS, {"open_apis": fake_adapter}, clear=True):
            return await svc.search_all_platforms(
                {"titles": ["Backend"], "locations": ["Bengaluru", "Remote"], "max_results": 10},
                ["open_apis"],
            )

    jobs, warnings = asyncio.run(go())
    assert sorted(seen_locations) == ["Bengaluru", "Remote"]
    assert {j["location"] for j in jobs} == {"Bengaluru", "Remote"}


def test_search_all_platforms_applies_remote_filter():
    """query['remote'] must filter out non-matching jobs post-fetch, even
    though no adapter accepts a remote/work-mode parameter."""
    import asyncio

    from app.services import job_search_service as svc

    def fake_adapter(query, location, max_results):
        return [
            {**_job(1), "location": "Remote", "url": "https://x/1"},
            {**_job(2), "location": "Bengaluru, India", "url": "https://x/2"},
        ]

    async def go():
        query = {
            "titles": ["Backend"],
            "locations": ["Remote"],
            "max_results": 10,
            "remote": "remote",
        }
        with patch.dict(svc._ADAPTERS, {"open_apis": fake_adapter}, clear=True):
            return await svc.search_all_platforms(query, ["open_apis"])

    jobs, warnings = asyncio.run(go())
    assert len(jobs) == 1
    assert jobs[0]["location"] == "Remote"


def test_make_job_search_id_is_deterministic():
    from app.api.v1.jobs import make_job_search_id

    a = make_job_search_id("u1", "python", "Remote", 10)
    assert a == make_job_search_id("u1", "python", "Remote", 10)
    assert a.startswith("u1:job_search:")
    assert make_job_search_id("u1", "java", "Remote", 10) != a


def _override_auth(monkeypatch):
    """Authenticated app client without network.

    Patches the two JWT verify entry points (middleware in app.main and
    deps.get_current_user), the DB dependency, and search-context resolution.
    Returns the FastAPI app. Caller must clear dependency_overrides after.
    """
    from app.api.v1 import jobs as jobs_module
    from app.core.database import get_db
    from app.main import app

    payload = {"sub": "00000000-0000-0000-0000-000000000001", "email": "t@e.com"}
    monkeypatch.setattr("app.main.verify_token", lambda token: payload)
    monkeypatch.setattr("app.api.v1.deps.verify_auth_jwt", lambda token: payload)

    model_row = MagicMock()
    # get_or_provision_user() runs _repair_placeholder_profile() on whatever
    # this mock stands in for (both the user row and the model-settings row
    # share this same object here) — give it a real, non-placeholder email
    # and clerk_user_id so that guard short-circuits instead of calling out
    # to the real Clerk API with a MagicMock as the subject.
    model_row.email = "t@e.com"
    model_row.deletion_scheduled_for = None
    model_row.clerk_user_id = "00000000-0000-0000-0000-000000000001"
    db = MagicMock()
    db.add = MagicMock()
    calls = {"n": 0}

    async def _flush():
        return None

    async def _commit():
        return None

    async def _exec(*a, **k):
        calls["n"] += 1
        if "FROM users" in str(a[0]) or "FROM user_model_settings" in str(a[0]):
            # Query 1: get_current_user's own user-by-subject lookup.
            # Query 2: search_jobs()'s active-model-settings check.
            # Both must resolve truthy for the route to reach the handler body.
            return MagicMock(
                scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=model_row))),
                scalar_one_or_none=MagicMock(return_value=model_row),
            )
        # Later queries (the duplicate-job-search lookup): no existing run.
        return MagicMock(
            scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None))),
            scalar_one_or_none=MagicMock(return_value=None),
        )

    db.flush = MagicMock(side_effect=lambda: _flush())
    db.commit = MagicMock(side_effect=lambda: _commit())
    db.execute = MagicMock(side_effect=lambda *a, **k: _exec(*a, **k))

    async def _fake_db():
        return db

    async def _ctx_unpack(*a, **k):
        return ("Backend Engineer", "Bengaluru", "", {"role_source": "test"})

    async def _no_browser(*a, **k):
        return False

    monkeypatch.setitem(app.dependency_overrides, get_db, _fake_db)
    monkeypatch.setattr(jobs_module, "_resolve_search_context", _ctx_unpack)
    monkeypatch.setattr(jobs_module, "_resolve_live_browser", _no_browser)
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.services.search_basis.resolve_basis", AsyncMock(return_value=(None, None))
    )
    return app


def test_search_starts_a_job_search_workflow(monkeypatch):
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    from app.workflows import starters

    app = _override_auth(monkeypatch)
    start = AsyncMock(return_value="job-search/x")
    monkeypatch.setattr(starters, "start_job_search", start)

    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/v1/jobs/search",
                json={
                    "titles": ["Backend Engineer"],
                    "locations": ["Bengaluru"],
                    "platforms": ["indeed"],
                    "max_results": 5,
                },
                headers={"Authorization": "Bearer test-token"},
            )

    import asyncio as _aio

    resp = _aio.run(go())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queued"] is True
    run_id, _user_id, params = start.await_args.args
    assert run_id == body["run_id"]
    assert params["search_query"] == "Backend Engineer"
    assert params["platforms"] == ["indeed"]

    app.dependency_overrides.clear()


def test_search_returns_503_when_temporal_is_unreachable(monkeypatch):
    """No inline fallback and no run left "running" forever: the caller is
    told the search did not start."""
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    from app.workflows import starters

    app = _override_auth(monkeypatch)
    monkeypatch.setattr(
        starters,
        "start_job_search",
        AsyncMock(side_effect=starters.WorkflowUnavailable("down")),
    )

    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/v1/jobs/search",
                json={
                    "titles": ["Backend Engineer"],
                    "locations": ["Bengaluru"],
                    "max_results": 5,
                },
                headers={"Authorization": "Bearer test-token"},
            )

    import asyncio as _aio

    resp = _aio.run(go())
    assert resp.status_code == 503

    app.dependency_overrides.clear()


def test_persist_only_above_threshold_and_idempotent():
    from app.agents import job_search as js

    added: list = []

    class FakeSession:
        def __init__(self, store):
            self.store = store

        def execute(self, *a, **k):
            class R:
                def all(inner):
                    return (
                        [SimpleNamespace(job_url=_job(0)["url"], company="x", role="y", location="")]
                        if self.store.get("seen")
                        else []
                    )

            return R()

        def add(self, row):
            added.append(row)

        def commit(self):
            self.store["seen"] = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    store: dict = {}
    # NOTE: _persist_saved_jobs imports _get_sync_factory locally from
    # app.core.sync_db at call time — patch it there, not in job_search.
    with patch("app.core.sync_db._get_sync_factory", return_value=lambda: FakeSession(store)):
        from app.agents.job_search import _persist_saved_jobs

        jobs = [{**_job(0), "match_score": 90}, {**_job(1), "match_score": 10}]
        assert _persist_saved_jobs("u1", jobs) == 1  # only score>=50, has url
        assert _persist_saved_jobs("u1", jobs) == 0  # second run: idempotent
