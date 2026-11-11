"""Careers-URL resolution plus the Workable (public), Oracle and iCIMS connectors."""

import httpx
import pytest

from app.services import job_connectors as connectors
from app.services.job_connectors import Source, resolve_careers_url


def _capture(monkeypatch, payload, text=None):
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        request = httpx.Request("GET", url)
        if text is not None:
            return httpx.Response(200, text=text, request=request)
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(connectors, "public_get", get)
    return calls


@pytest.mark.parametrize(
    ("url", "family", "tenant"),
    [
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External",
            "workday",
            "acme.wd5.myworkdayjobs.com|acme|External",
        ),
        (
            "https://acme.wd1.myworkdayjobs.com/Careers/job/NYC/Dev_R1",
            "workday",
            "acme.wd1.myworkdayjobs.com|acme|Careers",
        ),
        (
            "https://wd3.myworkdaysite.com/en-US/recruiting/acme/Site1",
            "workday",
            "wd3.myworkdaysite.com|acme|Site1",
        ),
        ("https://jobs.ashbyhq.com/acme", "ashby", "acme"),
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse", "acme"),
        ("https://job-boards.greenhouse.io/acme", "greenhouse", "acme"),
        ("https://boards.greenhouse.io/embed/job_board?for=acme", "greenhouse", "acme"),
        ("https://jobs.lever.co/acme/abc-123", "lever", "acme"),
        ("https://jobs.eu.lever.co/acme", "lever", "eu:acme"),
        ("https://jobs.smartrecruiters.com/BoschGroup/744", "smartrecruiters", "BoschGroup"),
        ("https://apply.workable.com/acme/", "workable", "acme"),
        ("https://acme.recruitee.com/o/dev", "recruitee", "acme"),
        ("https://acme.jobs.personio.de/job/1", "personio", "acme"),
        ("https://acme.bamboohr.com/careers", "bamboohr", "acme"),
        ("https://acme.teamtailor.com/jobs", "teamtailor", "acme"),
        ("careers-acme.icims.com/jobs/search", "icims", "careers-acme"),
        (
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
            "oracle",
            "jpmc.fa.oraclecloud.com|CX_1001",
        ),
    ],
)
def test_resolve_careers_url(url, family, tenant):
    source = resolve_careers_url(url)
    assert (source.family, source.tenant) == (family, tenant)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/careers",
        "https://acme.wd5.myworkdayjobs.com/",  # no site, so no board
        "https://www.recruitee.com",
        "https://jobs.lever.co/",
        "https://wd3.myworkdaysite.com/en-US/acme",
        "",
    ],
)
def test_resolve_careers_url_rejects_unknown_or_incomplete(url):
    assert resolve_careers_url(url) is None


def test_resolved_source_is_fetchable_family():
    assert resolve_careers_url("https://jobs.ashbyhq.com/acme").family in connectors.FAMILIES


@pytest.mark.asyncio
async def test_workable_without_token_uses_public_api(monkeypatch):
    payload = {
        "results": [
            {
                "shortcode": "AB12",
                "title": "ML Engineer",
                "remote": True,
                "location": {"city": "", "region": None, "country": "France"},
                "published": "2026-09-21T00:00:00.000Z",
            }
        ],
        "nextPage": "tok2",
    }
    calls = _capture(monkeypatch, payload)
    page = await connectors.fetch_page(
        Source(id="workable:acme", family="workable", tenant="acme"), "ml", None
    )
    url, kwargs = calls[0]
    assert url == "https://apply.workable.com/api/v3/accounts/acme/jobs"
    assert kwargs["json_body"]["query"] == "ml"
    job = page.jobs[0]
    assert job["url"] == "https://apply.workable.com/acme/j/AB12"
    assert job["remote"] == "remote" and job["location"] == "France"
    assert job["posted_at"].startswith("2026-09-21")
    assert page.next_cursor == "tok2"
    await connectors.fetch_page(
        Source(id="workable:acme", family="workable", tenant="acme"), "", "tok2"
    )
    assert calls[1][1]["json_body"]["token"] == "tok2"


@pytest.mark.asyncio
async def test_workable_rejects_malformed_cursor(monkeypatch):
    _capture(monkeypatch, {})
    with pytest.raises(ValueError):
        await connectors.fetch_page(
            Source(id="workable:acme", family="workable", tenant="acme"), "", "bad cursor!"
        )


@pytest.mark.asyncio
async def test_oracle_maps_requisitions_and_pages(monkeypatch):
    reqs = [
        {
            "Id": str(i),
            "Title": f"Engineer {i}",
            "PostedDate": "2026-11-11",
            "PrimaryLocation": "Hyderabad, India",
            "WorkplaceType": "Remote",
            "ShortDescriptionStr": "Build things",
        }
        for i in range(25)
    ]
    calls = _capture(monkeypatch, {"items": [{"TotalJobsCount": 60, "requisitionList": reqs}]})
    source = Source(id="oracle:jpmc", family="oracle", tenant="jpmc.fa.oraclecloud.com|CX_1001")
    page = await connectors.fetch_page(source, "data, engineer;")
    url = calls[0][0]
    assert url.startswith("https://jpmc.fa.oraclecloud.com/hcmRestApi/")
    assert "siteNumber=CX_1001" in url and "offset=0" in url
    assert "keyword=data++engineer" in url or "keyword=data+engineer" in url.replace("++", "+")
    assert ";" not in url.split("keyword=")[1]
    job = page.jobs[0]
    assert (
        job["url"]
        == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/0"
    )
    assert job["remote"] == "remote" and job["company"] == "jpmc"
    assert page.next_cursor == "25"
    last = await connectors.fetch_page(source, "", "50")
    assert "offset=50" in calls[1][0]
    assert last.next_cursor is None  # a full page but 75 >= total


@pytest.mark.asyncio
async def test_oracle_validates_tenant():
    for tenant in (
        "jpmc.fa.oraclecloud.com",
        "evil.example.com|CX_1",
        "jpmc.fa.oraclecloud.com|a/b",
    ):
        with pytest.raises(ValueError):
            await connectors.fetch_page(Source(id="oracle:x", family="oracle", tenant=tenant))


_ICIMS = """
<html><body>Page 1 of 3
<ul class="container-fluid iCIMS_JobsTable">
<li class="iCIMS_JobCardItem"><div class="row">
<div class="col-xs-6 header right"><span title="11/11/2026 12:41 PM">1 hour ago</span></div>
<div class="col-xs-12 title">
<a href="https://careers-acme.icims.com/jobs/80184/senior-engineer/job?in_iframe=1" class="iCIMS_Anchor">
<span class="sr-only field-label">Title</span>
<h3 >
Senior Engineer</h3></a></div>
<div class="col-xs-12 description">Build &amp; ship.<div class="col-xs-12 additionalFields">
<dl><div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField"><span class="sr-only field-label">Job Location</span></dt>
<dd class="iCIMS_JobHeaderData"><span >
US-CO-Boulder</span></dd></div>
<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Combined Salary Range</dt>
<dd class="iCIMS_JobHeaderData"><span >
USD $100 - USD $200 /Yr.</span></dd></div></dl></div></div></div></li>
</ul></body></html>
"""


def test_parse_icims_card():
    rows = connectors.parse_icims(_ICIMS, "careers-acme")
    assert rows == [
        {
            "id": "80184",
            "title": "Senior Engineer",
            "company": "careers-acme",
            "url": "https://careers-acme.icims.com/jobs/80184/senior-engineer/job",
            "location": "US-CO-Boulder",
            "description": "Build &amp; ship.",
            "salary_text": "USD $100 - USD $200 /Yr.",
            "posted_at": "2026-11-11T12:41:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_icims_fetch_pages_by_counter(monkeypatch):
    calls = _capture(monkeypatch, None, text=_ICIMS)
    source = Source(id="icims:careers-acme", family="icims", tenant="careers-acme")
    page = await connectors.fetch_page(source, "engineer")
    assert calls[0][0].startswith("https://careers-acme.icims.com/jobs/search?")
    assert "searchKeyword=engineer" in calls[0][0] and "pr=0" in calls[0][0]
    assert page.jobs[0]["title"] == "Senior Engineer"
    assert page.next_cursor == "2"


@pytest.mark.asyncio
async def test_recruitee_parses_utc_dates_and_real_location(monkeypatch):
    offer = {
        "id": 1,
        "title": "Agent",
        "careers_url": "https://careers.acme.com/o/agent",
        "location": "Remote job",
        "city": "Paris",
        "country": "France",
        "remote": True,
        "created_at": "2026-11-10 07:31:31 UTC",
        "published_at": "2026-11-10 07:45:05 UTC",
    }
    _capture(monkeypatch, {"offers": [offer]})
    page = await connectors.fetch_page(
        Source(id="recruitee:acme", family="recruitee", tenant="acme")
    )
    job = page.jobs[0]
    assert job["posted_at"] == "2026-11-10T07:45:05+00:00"
    assert job["location"] == "Paris, France" and job["remote"] == "remote"


@pytest.mark.asyncio
async def test_workday_sends_search_text_and_myworkdaysite_url(monkeypatch):
    posting = {"title": "Dev", "externalPath": "/job/NYC/Dev_R1", "locationsText": "NYC"}
    calls = _capture(monkeypatch, {"total": 1, "jobPostings": [posting]})
    source = Source(id="workday:a", family="workday", tenant="wd3.myworkdaysite.com|acme|Site1")
    page = await connectors.fetch_page(source, "python")
    assert calls[0][0] == "https://wd3.myworkdaysite.com/wday/cxs/acme/Site1/jobs"
    assert calls[0][1]["json_body"]["searchText"] == "python"
    assert page.jobs[0]["url"] == (
        "https://wd3.myworkdaysite.com/en-US/recruiting/acme/Site1/job/NYC/Dev_R1"
    )


@pytest.mark.asyncio
async def test_search_board_filters_client_side_for_full_board_apis(monkeypatch):
    rows = [
        {
            "id": "1",
            "title": "Python Engineer",
            "hostedUrl": "https://jobs.lever.co/a/1",
            "text": "Python Engineer",
        },
        {"id": "2", "title": "Chef", "hostedUrl": "https://jobs.lever.co/a/2", "text": "Chef"},
    ]
    _capture(monkeypatch, rows)
    jobs = await connectors.search_board(
        Source(id="lever:a", family="lever", tenant="a"), "python engineer"
    )
    assert [j["title"] for j in jobs] == ["Python Engineer"]


@pytest.mark.asyncio
async def test_search_board_trusts_server_side_filter_and_follows_cursor(monkeypatch):
    pages = iter(
        [
            {
                "total": 40,
                "jobPostings": [
                    {"title": f"Data {i}", "externalPath": f"/job/x/{i}"} for i in range(20)
                ],
            },
            {"total": 40, "jobPostings": [{"title": "Data tail", "externalPath": "/job/x/z"}]},
        ]
    )

    async def get(url, **kwargs):
        return httpx.Response(200, json=next(pages), request=httpx.Request("POST", url))

    monkeypatch.setattr(connectors, "public_get", get)
    source = Source(id="workday:a", family="workday", tenant="a.wd5.myworkdayjobs.com|a|Ext")
    jobs = await connectors.search_board(source, "scientist")
    assert len(jobs) == 21  # titles lack "scientist": the server already filtered
