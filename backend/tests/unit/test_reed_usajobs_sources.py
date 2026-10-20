"""Reed (UK) and USAJobs (US federal) keyed sources: fixture payloads, no network."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.services import job_connectors as connectors
from app.services.job_connectors import Source


def _capture(monkeypatch, payload):
    """Record every public_get call and answer with the JSON `payload`."""
    calls = []

    async def get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(connectors, "public_get", get)
    return calls


def _forbid_network(monkeypatch):
    async def get(url, **kwargs):
        raise AssertionError(f"keyless source must not fetch {url}")

    monkeypatch.setattr(connectors, "public_get", get)


REED_PAYLOAD = {
    "totalResults": 150,
    "results": [
        {
            "jobId": 41234,
            "employerName": "Acme Ltd",
            "jobTitle": "Python Developer",
            "locationName": "London",
            "minimumSalary": 50000,
            "maximumSalary": 65000,
            "currency": "GBP",
            "date": "03/10/2026",
            "expirationDate": "02/11/2026",
            "jobDescription": "<p>Build APIs.</p>",
            "jobUrl": "https://www.reed.co.uk/jobs/python-developer/41234",
        },
        {
            "jobId": 41235,
            "employerName": "Globex",
            "jobTitle": "Data Engineer",
            "locationName": "Remote",
            "minimumSalary": None,
            "date": "not a date",
            "jobUrl": "https://www.reed.co.uk/jobs/data-engineer/41235",
        },
    ],
}


@pytest.mark.asyncio
async def test_reed_is_inert_without_key(monkeypatch):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(connectors.settings, "REED_API_KEY", None)
    with pytest.raises(ValueError, match="not configured"):
        await connectors.fetch_page(Source(id="reed:uk", family="reed"), query="python")


@pytest.mark.asyncio
async def test_reed_maps_fields_and_pages_with_basic_auth(monkeypatch):
    monkeypatch.setattr(connectors.settings, "REED_API_KEY", "secret")
    calls = _capture(monkeypatch, REED_PAYLOAD)
    page = await connectors.fetch_page(Source(id="reed:uk", family="reed"), query="python")

    call = calls[0]
    assert call["url"].startswith("https://www.reed.co.uk/api/1.0/search?")
    assert "keywords=python" in call["url"] and "resultsToSkip=0" in call["url"]
    want_auth = "Basic " + base64.b64encode(b"secret:").decode()
    assert call["headers"]["Authorization"] == want_auth

    first, second = page.jobs
    assert first["title"] == "Python Developer" and first["company"] == "Acme Ltd"
    assert first["url"] == "https://www.reed.co.uk/jobs/python-developer/41234"
    assert first["location"] == "London" and first["source_job_id"] == "41234"
    assert first["posted_at"].startswith("2026-10-03")
    assert first["salary_text"] == "GBP 50000–65000"
    assert second["salary_text"] == "" and second["remote"] == "remote"
    assert second["posted_at"] is None  # unparseable date is dropped, not fatal
    # 150 results, 100 per page -> a second page exists; page 2 is the last
    assert page.next_cursor == "2"
    await connectors.fetch_page(Source(id="reed:uk", family="reed"), query="python", cursor="2")
    assert "resultsToSkip=100" in calls[1]["url"]


@pytest.mark.asyncio
async def test_reed_last_page_has_no_cursor(monkeypatch):
    monkeypatch.setattr(connectors.settings, "REED_API_KEY", "secret")
    _capture(monkeypatch, REED_PAYLOAD)
    page = await connectors.fetch_page(Source(id="reed:uk", family="reed"), cursor="2")
    assert page.next_cursor is None


USAJOBS_PAYLOAD = {
    "SearchResult": {
        "SearchResultCount": 2,
        "SearchResultCountAll": 2,
        "SearchResultItems": [
            {
                "MatchedObjectId": "801234500",
                "MatchedObjectDescriptor": {
                    "PositionTitle": "IT Specialist (Python)",
                    "OrganizationName": "Department of Commerce",
                    "PositionURI": "https://www.usajobs.gov/job/801234500",
                    "PositionLocationDisplay": "Washington, District of Columbia",
                    "PublicationStartDate": "2026-10-01",
                    "ApplicationCloseDate": "2026-10-31",
                    "PositionRemuneration": [
                        {
                            "MinimumRange": "80000",
                            "MaximumRange": "120000",
                            "RateIntervalCode": "PA",
                        }
                    ],
                    "UserArea": {"Details": {"JobSummary": "Maintain Python services."}},
                },
            },
            {
                "MatchedObjectId": "801234501",
                "MatchedObjectDescriptor": {
                    "PositionTitle": "Analyst",
                    "OrganizationName": "Treasury",
                    "PositionURI": "https://www.usajobs.gov/job/801234501",
                    "PositionLocationDisplay": "Remote",
                    "PublicationStartDate": "2026-10-02",
                    "PositionRemuneration": [],
                },
            },
        ],
    }
}


@pytest.mark.asyncio
async def test_usajobs_is_inert_without_key_or_user_agent(monkeypatch):
    _forbid_network(monkeypatch)
    source = Source(id="usajobs:us", family="usajobs")
    monkeypatch.setattr(connectors.settings, "USAJOBS_API_KEY", None)
    monkeypatch.setattr(connectors.settings, "USAJOBS_USER_AGENT", "ops@example.com")
    with pytest.raises(ValueError, match="not configured"):
        await connectors.fetch_page(source)
    # USAJobs rejects requests without a registered User-Agent, so the key alone is not enough.
    monkeypatch.setattr(connectors.settings, "USAJOBS_API_KEY", "secret")
    monkeypatch.setattr(connectors.settings, "USAJOBS_USER_AGENT", None)
    with pytest.raises(ValueError, match="not configured"):
        await connectors.fetch_page(source)


@pytest.mark.asyncio
async def test_usajobs_maps_fields_with_registered_headers(monkeypatch):
    monkeypatch.setattr(connectors.settings, "USAJOBS_API_KEY", "secret")
    monkeypatch.setattr(connectors.settings, "USAJOBS_USER_AGENT", "ops@example.com")
    calls = _capture(monkeypatch, USAJOBS_PAYLOAD)
    page = await connectors.fetch_page(Source(id="usajobs:us", family="usajobs"), query="python")

    call = calls[0]
    assert call["url"].startswith("https://data.usajobs.gov/api/search?")
    assert "Keyword=python" in call["url"] and "ResultsPerPage=500" in call["url"]
    assert call["headers"]["Authorization-Key"] == "secret"
    assert call["headers"]["User-Agent"] == "ops@example.com"
    assert call["headers"]["Host"] == "data.usajobs.gov"

    first, second = page.jobs
    assert first["title"] == "IT Specialist (Python)"
    assert first["company"] == "Department of Commerce"
    assert first["url"] == "https://www.usajobs.gov/job/801234500"
    assert first["location"] == "Washington, District of Columbia"
    assert first["source_job_id"] == "801234500"
    assert first["posted_at"].startswith("2026-10-01")
    assert first["salary_text"] == "$80000–120000 PA"
    assert second["salary_text"] == "" and second["remote"] == "remote"
    assert page.next_cursor is None  # 2 of 2 results on the only page


@pytest.mark.asyncio
async def test_usajobs_pages_by_count(monkeypatch):
    monkeypatch.setattr(connectors.settings, "USAJOBS_API_KEY", "secret")
    monkeypatch.setattr(connectors.settings, "USAJOBS_USER_AGENT", "ops@example.com")
    payload = json.loads(json.dumps(USAJOBS_PAYLOAD))
    payload["SearchResult"]["SearchResultCountAll"] = 1200
    _capture(monkeypatch, payload)
    page = await connectors.fetch_page(Source(id="usajobs:us", family="usajobs"))
    assert page.next_cursor == "2"


def test_reed_date_parses_day_first_and_rejects_junk():
    assert connectors._reed_date("03/10/2026").startswith("2026-10-03")
    assert connectors._reed_date("2026-10-03") is None
    assert connectors._reed_date(None) is None


def test_reed_and_usajobs_catalogued_and_registered():
    assert {"reed", "usajobs"} <= connectors.FAMILIES
    rows = json.loads(
        (Path(connectors.__file__).parent / "job_sources.json").read_text(encoding="utf-8")
    )
    by_id = {r["id"]: r for r in rows}
    for source_id, family in (("reed:uk", "reed"), ("usajobs:us", "usajobs")):
        assert by_id[source_id]["family"] == family
        assert by_id[source_id]["refresh_hours"] == 12 and by_id[source_id]["permitted"]
