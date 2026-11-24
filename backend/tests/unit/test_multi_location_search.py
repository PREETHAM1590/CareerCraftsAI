"""Every requested location counts, from the API request down to matching."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import jobs as jobs_module
from app.api.v1.jobs import JobSearchRequest
from app.services.job_catalog import location_matches, matches_query


def _context(monkeypatch, payload, prefs=None):
    async def execute(*a, **k):
        return SimpleNamespace(scalar_one_or_none=lambda: prefs)

    db = SimpleNamespace(execute=execute)
    monkeypatch.setattr(
        "app.services.search_basis.resolve_basis", AsyncMock(return_value=(None, None))
    )
    user = SimpleNamespace(id=uuid.uuid4(), headline=None)
    return asyncio.run(jobs_module._resolve_search_context(db, user, payload))


def _prefs(**kw):
    base = {
        "id": uuid.uuid4(),
        "target_roles": ["Backend Engineer"],
        "current_title": None,
        "years_experience": 4,
        "experience_level": "mid",
        "job_type": "full-time",
        "work_mode": "hybrid",
        "preferred_locations": ["Pune", "Mumbai", "Pune", "Delhi"],
    }
    return SimpleNamespace(**{**base, **kw})


def test_hybrid_search_uses_every_saved_city(monkeypatch):
    _, location, _, source = _context(monkeypatch, JobSearchRequest(), _prefs())
    assert location == "Pune"
    assert source["locations"] == ["Pune", "Mumbai", "Delhi"]


def test_cities_sent_with_the_request_all_count(monkeypatch):
    payload = JobSearchRequest(
        work_mode="onsite", location="Any", preferred_locations=["Berlin", "Munich", "Hamburg"]
    )
    _, location, _, source = _context(monkeypatch, payload)
    assert location == "Berlin"
    assert source["locations"] == ["Berlin", "Munich", "Hamburg"]


def test_a_typed_location_wins_and_remote_stays_remote(monkeypatch):
    typed = JobSearchRequest(location="Lisbon", work_mode="hybrid")
    assert _context(monkeypatch, typed, _prefs())[3]["locations"] == ["Lisbon"]
    remote = JobSearchRequest(work_mode="remote")
    assert _context(monkeypatch, remote, _prefs())[3]["locations"] == ["Remote"]


def test_search_start_carries_all_locations(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.workflows import starters
    from tests.unit.test_job_search_service import _override_auth

    app = _override_auth(monkeypatch)
    start = AsyncMock(return_value="job-search/x")
    monkeypatch.setattr(starters, "start_job_search", start)

    async def ctx(*a, **k):
        return ("Backend", "Pune", "hybrid", {"locations": ["Pune", "Mumbai", "Delhi"]})

    monkeypatch.setattr(jobs_module, "_resolve_search_context", ctx)

    async def post(body):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            return await client.post(
                "/api/v1/jobs/search", json=body, headers={"Authorization": "Bearer t"}
            )

    assert asyncio.run(post({})).status_code == 200
    assert start.await_args.args[2]["locations"] == ["Delhi", "Mumbai", "Pune"]

    assert asyncio.run(post({"locations": ["Oslo", "Bergen"]})).status_code == 200
    assert start.await_args.args[2]["locations"] == ["Bergen", "Oslo"]
    app.dependency_overrides.clear()


@pytest.mark.parametrize("city", ["Pune", "Mumbai", "Delhi"])
def test_a_job_in_any_requested_city_matches(city):
    job = {"title": "Backend Engineer", "location": f"{city}, India", "remote": "unknown"}
    query = {"titles": ["Backend Engineer"], "locations": ["Pune", "Mumbai", "Delhi"]}
    assert matches_query(job, query)
    assert location_matches(job, ["Pune", "Mumbai", "Delhi"])


def test_a_job_outside_every_requested_city_is_dropped():
    job = {"title": "Backend Engineer", "location": "Chennai, India", "remote": "unknown"}
    assert not matches_query(job, {"titles": ["Backend Engineer"], "locations": ["Pune", "Mumbai"]})


def test_ranking_bonus_counts_every_location():
    from app.services.job_matching import rule_score

    job = {"title": "Backend Engineer", "location": "Mumbai, India", "description": ""}
    one, _ = rule_score(job, "python", "Backend Engineer", "Pune")
    many, reasons = rule_score(job, "python", "Backend Engineer", ["Pune", "Mumbai"])
    assert many == one + 10 and "Matches preferred location" in reasons
