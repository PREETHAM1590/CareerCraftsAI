"""Workday, Personio and the other keyless ATS board families (fixture payloads, no network)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.services import job_connectors as connectors
from app.services.job_connectors import Source


def _capture(monkeypatch, payload=None, text=None):
    """Record every public_get call; answer JSON `payload` or raw `text`."""
    calls = []

    async def get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        request = httpx.Request("POST" if kwargs.get("json_body") else "GET", url)
        if text is not None:
            return httpx.Response(200, text=text, request=request)
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(connectors, "public_get", get)
    return calls


WD = "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite"


@pytest.mark.asyncio
async def test_workday_posts_listing_and_pages_by_offset(monkeypatch):
    calls = _capture(monkeypatch, {
        "total": 45,
        "jobPostings": [
            {"title": f"Engineer {i}", "externalPath": f"/job/Santa-Clara/Eng-{i}_R{i}",
             "locationsText": "US, CA, Santa Clara", "postedOn": "Posted 3 Days Ago"}
            for i in range(20)
        ],
    })
    source = Source(id="workday:nvidia", family="workday", tenant=WD)
    page = await connectors.fetch_page(source, query="python")
    assert calls[0]["url"] == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    assert calls[0]["json_body"] == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "python"}
    assert page.next_cursor == "20"
    job = page.jobs[0]
    assert job["url"] == "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Santa-Clara/Eng-0_R0"
    assert job["company"] == "nvidia" and job["location"] == "US, CA, Santa Clara"
    assert job["description"] == ""
    want = (datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=3)).isoformat()
    assert job["posted_at"] == want
    # last page: 45 total, offset 40 -> no further cursor
    last = await connectors.fetch_page(source, cursor="40")
    assert calls[1]["json_body"]["offset"] == 40
    assert last.next_cursor is None


@pytest.mark.asyncio
async def test_workday_rejects_bad_tenant_and_offset_cap(monkeypatch):
    _capture(monkeypatch, {"jobPostings": []})
    with pytest.raises(ValueError):
        await connectors.fetch_page(Source(id="w", family="workday", tenant="nvidia"))
    with pytest.raises(ValueError):
        await connectors.fetch_page(Source(id="w", family="workday", tenant="evil.example.com|a|b"))
    with pytest.raises(ValueError):
        await connectors.fetch_page(Source(id="w", family="workday", tenant=WD), cursor="200")


def test_workday_posted_text():
    assert connectors.workday_posted("Posted Today")[:10] == datetime.now(UTC).date().isoformat()
    assert connectors.workday_posted("Posted Yesterday")
    assert connectors.workday_posted("Posted 30+ Days Ago") is None
    assert connectors.workday_posted("whenever") is None


PERSONIO = """<?xml version="1.0"?><workzag-jobs>
<position><id>12</id><office>Berlin</office><name>Backend Engineer</name>
<jobDescriptions><jobDescription><name>A</name><value>&lt;p&gt;Build.&lt;/p&gt;</value></jobDescription>
<jobDescription><name>B</name><value>&lt;p&gt;Ship.&lt;/p&gt;</value></jobDescription></jobDescriptions>
<createdAt>2026-10-01T09:00:00+00:00</createdAt></position>
<position><id>13</id><office>Remote</office><name>Designer</name><createdAt>2026-10-02T09:00:00+00:00</createdAt></position>
</workzag-jobs>"""


@pytest.mark.asyncio
async def test_personio_maps_positions_and_falls_back_to_english_feed(monkeypatch):
    calls = _capture(monkeypatch, text=PERSONIO)
    page = await connectors.fetch_page(Source(id="personio:acme", family="personio", tenant="acme"))
    assert calls[0]["url"] == "https://acme.jobs.personio.com/xml"
    assert calls[1]["url"] == "https://acme.jobs.personio.com/xml?language=en"  # position 13 had no body
    job = page.jobs[0]
    assert job["url"] == "https://acme.jobs.personio.com/job/12"
    assert job["title"] == "Backend Engineer" and job["location"] == "Berlin"
    assert "Build." in job["description"] and "Ship." in job["description"]
    assert job["posted_at"].startswith("2026-10-01")
    assert page.jobs[1]["remote"] == "remote"


@pytest.mark.asyncio
async def test_bamboohr_lists_without_detail(monkeypatch):
    calls = _capture(monkeypatch, {"result": [
        {"id": "52", "jobOpeningName": "Designer", "isRemote": False,
         "location": {"city": "Yerevan", "state": None}},
        {"id": "53", "jobOpeningName": "Support", "locationType": "1"},
    ]})
    page = await connectors.fetch_page(Source(id="b", family="bamboohr", tenant="acme"))
    assert calls[0]["url"] == "https://acme.bamboohr.com/careers/list"
    assert page.jobs[0]["url"] == "https://acme.bamboohr.com/careers/52"
    assert page.jobs[0]["location"] == "Yerevan" and page.jobs[1]["remote"] == "remote"


@pytest.mark.asyncio
async def test_breezy_maps_list(monkeypatch):
    calls = _capture(monkeypatch, [{
        "id": "1", "name": "Engineer", "url": "https://acme.breezy.hr/p/1-engineer",
        "location": {"city": "Yerevan", "country": {"name": "Armenia"}, "is_remote": False},
        "published_date": "2025-01-14T00:20:37.073Z",
    }])
    page = await connectors.fetch_page(Source(id="b", family="breezy", tenant="acme"))
    assert calls[0]["url"] == "https://acme.breezy.hr/json"
    job = page.jobs[0]
    assert job["location"] == "Yerevan, Armenia" and job["posted_at"].startswith("2025-01-14")


@pytest.mark.asyncio
async def test_pinpoint_joins_description_sections(monkeypatch):
    calls = _capture(monkeypatch, {"data": [{
        "id": "9", "title": "Analyst", "url": "https://acme.pinpointhq.com/en/postings/9",
        "description": "<p>About</p>", "key_responsibilities": "<p>Do</p>",
        "workplace_type": "remote", "location": {"city": "Leeds", "province": "UK"},
    }]})
    page = await connectors.fetch_page(Source(id="p", family="pinpoint", tenant="acme"))
    assert calls[0]["url"] == "https://acme.pinpointhq.com/postings.json"
    job = page.jobs[0]
    assert "About" in job["description"] and "Do" in job["description"]
    assert job["remote"] == "remote" and job["location"] == "Leeds, UK"


@pytest.mark.asyncio
async def test_rippling_maps_board_jobs(monkeypatch):
    calls = _capture(monkeypatch, [
        {"uuid": "U1", "name": "SRE", "url": "https://ats.rippling.com/acme/jobs/U1",
         "workLocation": {"label": "London, United Kingdom"}},
    ])
    page = await connectors.fetch_page(Source(id="r", family="rippling", tenant="acme"))
    assert calls[0]["url"] == "https://ats.rippling.com/api/v1/board/acme/jobs"
    assert page.jobs[0]["location"] == "London, United Kingdom"


TT = """<?xml version="1.0"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
<item><title>QA Lead</title><description>&lt;p&gt;Test.&lt;/p&gt;</description>
<pubDate>Fri, 25 Sep 2026 13:07:00 +0300</pubDate>
<link>https://acme.teamtailor.com/jobs/1-qa-lead</link><remoteStatus>fully</remoteStatus>
<guid>g-1</guid><tt:locations><tt:location><tt:name>Kaunas</tt:name></tt:location></tt:locations></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_teamtailor_rss_with_locations_and_paging(monkeypatch):
    calls = _capture(monkeypatch, text=TT)
    page = await connectors.fetch_page(Source(id="t", family="teamtailor", tenant="acme"), cursor="2")
    assert calls[0]["url"] == "https://acme.teamtailor.com/jobs.rss?offset=100&per_page=100"
    job = page.jobs[0]
    assert job["title"] == "QA Lead" and job["location"] == "Kaunas" and job["remote"] == "remote"
    assert job["posted_at"].startswith("2026-09-25")
    assert page.next_cursor is None  # fewer than a full page


@pytest.mark.asyncio
async def test_jobscore_feed(monkeypatch):
    calls = _capture(monkeypatch, {"company_name": "Acme Inc", "jobs": [{
        "id": "j1", "title": "Accountant", "detail_url": "https://careers.jobscore.com/careers/acme/jobs/j1",
        "city": "Austin", "state": "TX", "remote": "Yes", "description": "<p>Count.</p>",
        "opened_date": "2026-10-03",
    }]})
    page = await connectors.fetch_page(Source(id="j", family="jobscore", tenant="acme"))
    assert calls[0]["url"].startswith("https://careers.jobscore.com/jobs/acme/feed.json")
    job = page.jobs[0]
    assert job["company"] == "Acme Inc" and job["location"] == "Austin, TX" and job["remote"] == "remote"


TRAK = """<?xml version="1.0"?><rss version="2.0" xmlns:rb="https://recruiterbox.com/rss/job/"><channel>
<item><title>Recruiter</title><link>http://acme.hire.trakstar.com/jobs/fk123/</link>
<pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate><description>&lt;p&gt;Hire.&lt;/p&gt;</description>
<rb:locationCity>Denver</rb:locationCity><rb:locationState>CO</rb:locationState></item></channel></rss>"""


@pytest.mark.asyncio
async def test_trakstar_feed_upgrades_http_links(monkeypatch):
    calls = _capture(monkeypatch, text=TRAK)
    page = await connectors.fetch_page(Source(id="k", family="trakstar", tenant="acme"))
    assert calls[0]["url"] == "https://acme.hire.trakstar.com/jobfeeds/acme"
    job = page.jobs[0]
    assert job["url"] == "https://acme.hire.trakstar.com/jobs/fk123"
    assert job["location"] == "Denver, CO" and job["source_job_id"] == "fk123"


@pytest.mark.asyncio
async def test_hibob_sends_referer_and_unescapes_body(monkeypatch):
    calls = _capture(monkeypatch, {"jobAdDetails": [{
        "id": "abc", "title": "PM", "site": "Berlin", "country": "Germany",
        "description": "&lt;p&gt;Lead.&lt;/p&gt;", "requirements": "&lt;li&gt;SQL&lt;/li&gt;",
        "publishedAt": "2026-10-04T08:00:00Z", "workspaceType": "Hybrid",
    }]})
    page = await connectors.fetch_page(Source(id="h", family="hibob", tenant="acme"))
    assert calls[0]["url"] == "https://acme.careers.hibob.com/api/job-ad"
    assert calls[0]["headers"] == {"Referer": "https://acme.careers.hibob.com/jobs"}
    job = page.jobs[0]
    assert job["url"] == "https://acme.careers.hibob.com/jobs/abc"
    assert "<" not in job["description"] and "SQL" in job["description"]
    assert job["location"] == "Berlin, Germany"


@pytest.mark.asyncio
async def test_gem_posts_graphql_list(monkeypatch):
    calls = _capture(monkeypatch, {"data": {"oatsExternalJobPostings": {"jobPostings": [{
        "extId": "E1", "title": "Data Scientist", "job": {"locationType": "REMOTE"},
        "locations": [{"city": "Paris", "isoCountry": "FR", "isRemote": False}],
    }]}}})
    page = await connectors.fetch_page(Source(id="g", family="gem", tenant="acme"))
    assert calls[0]["url"] == "https://jobs.gem.com/api/public/graphql"
    assert calls[0]["json_body"]["variables"] == {"boardId": "acme"}
    job = page.jobs[0]
    assert job["url"] == "https://jobs.gem.com/acme/E1" and job["remote"] == "remote"
    assert job["location"] == "Paris, FR"


def test_new_families_registered_and_catalogued():
    new = {"workday", "personio", "bamboohr", "breezy", "pinpoint", "rippling",
           "teamtailor", "jobscore", "trakstar", "hibob", "gem"}
    assert new <= connectors.FAMILIES
    for host in ("myworkdayjobs.com", "personio.com", "breezy.hr", "pinpointhq.com", "hibob.com"):
        assert connectors.is_ats_url(f"https://acme.{host}/x")
    rows = json.loads((Path(connectors.__file__).parent / "job_sources.json").read_text(encoding="utf-8"))
    added = [r for r in rows if r["family"] in new]
    # gem/trakstar have no known board; breezy/rippling's only known boards were empty
    # when checked live — board_harvest finds real ones.
    assert {r["family"] for r in added} >= new - {"gem", "trakstar", "breezy", "rippling"}
    assert all(r["refresh_hours"] == 12 and r["permitted"] for r in added)
