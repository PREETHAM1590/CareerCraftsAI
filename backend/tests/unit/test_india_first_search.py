"""India-first search end to end: matching, hidden-role notes, zero-result reasons,
JobSpy India parameters, the Indian-board status and legacy platform mapping (no network)."""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import patch

import pandas as pd
import pytest

from app.services import job_catalog as catalog
from app.services import job_search_service as svc


def _job(i, location, **extra):
    return {
        "job_id": f"j{i}",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": location,
        "remote": "",
        "url": f"https://example.com/{i}",
        "description": "",
        **extra,
    }


@pytest.mark.parametrize(
    "location,query_places,india_focus,expected",
    [
        ("Bengaluru, India", ["Bangalore"], None, True),
        ("Gurgaon, Haryana", ["Delhi NCR"], None, True),
        ("Noida", ["Delhi"], None, True),
        ("Pune", ["Bangalore"], None, False),
        ("Remote - India", ["Bangalore"], None, True),
        ("Remote, US only", ["Bangalore"], None, False),
        ("Remote, US only", ["Remote"], None, False),
        ("Remote, US only", ["Remote"], False, True),
        ("San Francisco, CA", [], None, False),
        ("San Francisco, CA", ["San Francisco"], None, True),
        ("Remote", ["Remote"], None, True),
        ("Hyderabad", ["India"], None, True),
        ("London, UK", ["India"], None, False),
    ],
)
def test_matches_query_india_rules(location, query_places, india_focus, expected):
    query = {"titles": ["backend engineer"], "locations": query_places}
    if india_focus is not None:
        query["india_focus"] = india_focus
    assert catalog.matches_query(_job(1, location), query) is expected


@pytest.mark.parametrize(
    "role,title",
    [
        ("frontend developer", "Front-End Developer"),
        ("frontend developer", "Front End Developer (React)"),
        ("front end developer", "Senior Frontend Developer"),
        ("back-end engineer", "Backend Engineer"),
        ("full stack developer", "Full-Stack Developer"),
    ],
)
def test_compound_title_words_match_every_spelling(role, title):
    job = {**_job(1, "Pune"), "title": title}
    assert catalog.matches_query(job, {"titles": [role], "locations": ["Pune"]})


def test_matches_query_india_flag_off_checks_everything_else():
    query = {"titles": ["backend engineer"], "locations": ["Remote"]}
    job = _job(1, "Remote, US only")
    assert catalog.matches_query(job, query) is False
    assert catalog.matches_query(job, query, india=False) is True


def _run(adapter_jobs, query):
    def fake(q, location, n):
        return adapter_jobs

    async def go():
        with patch.dict(svc._ADAPTERS, {"open_apis": fake}, clear=True):
            return await svc.search_all_platforms(query, ["open_apis"])

    return asyncio.run(go())


def test_hidden_foreign_roles_are_counted_in_a_note():
    jobs, notes = _run(
        [_job(1, "Bengaluru"), _job(2, "Remote, US only"), _job(3, "New York, NY")],
        {"titles": ["Backend"], "locations": ["Remote"], "max_results": 10},
    )
    assert [j["location"] for j in jobs] == ["Bengaluru"]
    assert jobs[0]["india_fit"] == "india"
    assert any(n.startswith("Hid 2 matching roles open only outside India") for n in notes)


def test_zero_results_explain_why():
    jobs, notes = _run(
        [_job(1, "Berlin")],
        {"titles": ["Backend"], "locations": ["Pune"], "max_results": 10},
    )
    assert jobs == []
    assert any(n.startswith("No open roles matched every word of 'Backend' in Pune") for n in notes)


def test_india_focus_off_keeps_foreign_roles_without_notes():
    jobs, notes = _run(
        [_job(1, "Remote, US only")],
        {"titles": ["Backend"], "locations": ["Remote"], "india_focus": False, "max_results": 1},
    )
    assert len(jobs) == 1 and not any(n.startswith("Hid") for n in notes)


@pytest.mark.parametrize(
    "location,query,expected",
    [
        ("Remote", {}, "Remote India"),
        ("", {}, "Remote India"),
        ("Remote", {"india_focus": False}, "Remote"),
        ("Bengaluru", {}, "Bengaluru"),
    ],
)
def test_live_place_scopes_jobspy_to_india(location, query, expected):
    assert svc._live_place("jobspy", location, query) == expected
    assert svc._live_place("remoteok", location, query) == location


def _fake_jobspy(monkeypatch, frame):
    seen = {}

    def scrape(**kwargs):
        seen.update(kwargs)
        return frame

    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=scrape))
    return seen


def test_jobspy_remote_india_reads_linkedin_indeed_india_remote_only(monkeypatch):
    from app.services import job_platforms_service as platforms

    row = {
        "title": "Data Analyst",
        "company": "Acme",
        "location": "Bengaluru, Karnataka, India",
        "description": "SQL",
        "job_url": "https://in.indeed.com/viewjob?jk=1",
        "site": "indeed",
        "date_posted": "2026-11-10",
        "min_amount": 600000.0,
        "max_amount": 1200000.0,
        "currency": "INR",
        "interval": "yearly",
    }
    seen = _fake_jobspy(monkeypatch, pd.DataFrame([row]))
    jobs = platforms.scrape_jobs("data analyst", "Remote India", 10, 72, None, "usa")
    assert seen["site_name"] == ["linkedin", "indeed"]
    assert seen["location"] == "India"
    assert seen["country_indeed"] == "india" and seen["is_remote"] is True
    assert jobs[0].salary == "INR 600000–1200000 yearly"


def test_jobspy_default_sites_never_include_naukri_or_google():
    from app.services.job_platforms_service import JOBSPY_PLATFORMS

    assert JOBSPY_PLATFORMS == ["linkedin", "indeed"]


@pytest.mark.parametrize(
    "location,country",
    [("Gurugram", "india"), ("Kochi", "india"), ("Remote", "usa"), ("Berlin", "germany")],
)
def test_indeed_country_from_location(location, country):
    from app.services.job_platforms_service import _country_from_location

    assert _country_from_location(location) == country


async def test_indian_boards_report_unavailable_instead_of_empty():
    from app.services import indian_platforms_service as indian

    with pytest.raises(indian.PlatformUnavailable, match="Naukri is not searched"):
        await indian.scrape_indian_platform(platform="naukri")
    with pytest.raises(indian.PlatformUnavailable, match="Foundit"):
        await indian.scrape_all_indian_platforms()
    with pytest.raises(indian.PlatformUnavailable, match="Google Jobs"):
        await indian.search_google_jobs()
    assert all(not p["available"] and p["reason"] for p in indian.platform_status())


async def test_scrape_all_platforms_surfaces_indian_board_notice(monkeypatch):
    from app.services import job_platforms_service as platforms

    monkeypatch.setattr(platforms, "scrape_jobs", lambda *a, **k: [])
    warnings: list[str] = []
    jobs = await platforms.scrape_all_platforms("sre", "Pune", warnings=warnings)
    assert jobs == []
    assert warnings and "Naukri" in warnings[0] and "no public API" in warnings[0]


def test_legacy_ats_platform_covers_every_ats_family():
    from app.agents.job_search import _normalize_platforms
    from app.services.job_connectors import FAMILIES

    ats = set(_normalize_platforms(["ats"]))
    for family in ("jobvite", "jazzhr", "dover", "paylocity", "successfactors", "taleo"):
        assert family in ats
    for family in ("keka", "freshteam", "zohorecruit", "kula"):
        assert family in ats
    assert ats <= FAMILIES


def test_rule_score_understands_city_aliases():
    from app.services.job_matching import rule_score

    job = {"title": "Backend Engineer", "location": "Bengaluru, India", "description": ""}
    _, reasons = rule_score(job, "", "backend engineer", ["Bangalore"])
    assert "Matches preferred location" in reasons


def test_india_catalog_entries_are_tagged_and_unique():
    rows = catalog.sources()
    ids = [s.id for s in rows]
    assert len(ids) == len(set(ids))
    india = [s for s in rows if s.country == "IN" or s.company_country == "IN"]
    assert len(india) >= 60
    assert all(s.country in {"", "IN"} and s.company_country in {"", "IN"} for s in rows)
    assert catalog.PREFERRED_INDIA_SOURCES <= set(ids)
