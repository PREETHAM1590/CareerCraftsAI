"""Ashby compensation, Lever EU boards, Jobicy, and the WeWorkRemotely feed entry."""

import json
from pathlib import Path

import httpx
import pytest

from app.services import job_connectors as connectors
from app.services.job_connectors import Source


def _capture(monkeypatch, payload):
    urls = []

    async def get(url, **kwargs):
        urls.append(url)
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(connectors, "public_get", get)
    return urls


@pytest.mark.asyncio
async def test_ashby_requests_and_maps_salary(monkeypatch):
    job = {
        "id": "a1", "title": "Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
        "location": "Remote", "descriptionPlain": "Build.",
        "compensation": {"compensationTiers": [{"components": [
            {"componentType": "Equity", "minValue": 1, "maxValue": 2},
            {"componentType": "Salary", "currencyCode": "USD",
             "minValue": 120000, "maxValue": 150000},
        ]}]},
    }
    unpaid = {**job, "id": "a2", "jobUrl": "https://jobs.ashbyhq.com/acme/a2", "compensation": None}
    urls = _capture(monkeypatch, {"jobs": [job, unpaid]})
    page = await connectors.fetch_page(Source(id="ashby:acme", family="ashby", tenant="acme"))
    assert urls[0].endswith("/acme?includeCompensation=true")
    assert page.jobs[0]["salary_text"] == "USD 120000–150000"
    assert not page.jobs[1]["salary_text"]


@pytest.mark.asyncio
async def test_lever_eu_prefix_uses_eu_host_and_us_is_default(monkeypatch):
    row = [{"id": "1", "text": "Dev", "hostedUrl": "https://jobs.eu.lever.co/acme/1"}]
    urls = _capture(monkeypatch, row)
    page = await connectors.fetch_page(Source(id="lever:eu", family="lever", tenant="eu:acme"))
    assert urls[0].startswith("https://api.eu.lever.co/v0/postings/acme?")
    assert page.jobs[0]["company"] == "acme"
    await connectors.fetch_page(Source(id="lever:us", family="lever", tenant="acme"))
    assert urls[1].startswith("https://api.lever.co/v0/postings/acme?")


@pytest.mark.asyncio
async def test_jobicy_maps_jobs_and_passes_tag(monkeypatch):
    urls = _capture(monkeypatch, {"jobs": [{
        "id": 7, "url": "https://jobicy.com/jobs/7", "jobTitle": "Python Dev",
        "companyName": "Acme", "jobGeo": "Europe", "jobExcerpt": "Short",
        "pubDate": "2026-10-01 10:00:00", "annualSalaryMin": 50000,
        "annualSalaryMax": 70000, "salaryCurrency": "EUR",
    }]})
    page = await connectors.fetch_page(Source(id="jobicy", family="jobicy"), query="python")
    assert urls[0] == "https://jobicy.com/api/v2/remote-jobs?count=50&tag=python"
    job = page.jobs[0]
    assert job["title"] == "Python Dev" and job["company"] == "Acme"
    assert job["remote"] and job["salary_text"] == "EUR 50000–70000"


def test_catalog_has_jobicy_and_weworkremotely():
    sources = {s["id"]: s for s in json.loads(
        (Path(connectors.__file__).parent / "job_sources.json").read_text(encoding="utf-8"))}
    assert sources["jobicy"]["refresh_hours"] == 12 and sources["jobicy"]["permitted"]
    assert sources["rss:weworkremotely"]["url"] == "https://weworkremotely.com/remote-jobs.rss"
