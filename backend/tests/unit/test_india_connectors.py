"""Keka, Freshteam, Kula and Zoho Recruit boards, and India-scoped portal reads (no network)."""

import html
import json

import httpx
import pytest

from app.services import job_connectors as connectors
from app.services.job_connectors import Source, fetch_page, resolve_careers_url, role_key


def _serve(monkeypatch, routes):
    """Answer each request by the first route whose key is a substring of the URL."""
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs))
        for key, body in routes.items():
            if key in url:
                request = httpx.Request("GET", url)
                if isinstance(body, (dict, list)):
                    return httpx.Response(200, json=body, request=request)
                return httpx.Response(200, text=body, request=request)
        raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(connectors, "public_get", get)
    return calls


KEKA = [
    {
        "id": "146668",
        "title": "Backend Engineer",
        "description": "<p>Python &amp; Go</p>",
        "jobLocations": [{"city": "Mumbai", "countryCode": "IN", "countryName": "India"}],
        "publishedOn": "2026-11-06T10:44:53.947Z",
    },
    {"id": "../evil", "title": "Bad id"},
]


async def test_keka_board(monkeypatch):
    calls = _serve(monkeypatch, {"fynd.keka.com/careers/api/jobs/default/active": KEKA})
    source = Source(id="keka:fynd", family="keka", tenant="fynd", company="Fynd")
    page = await fetch_page(source)
    assert len(page.jobs) == 1
    job = page.jobs[0]
    assert job["url"] == "https://fynd.keka.com/careers/jobdetails/146668"
    assert job["company"] == "Fynd"
    assert job["location"] == "Mumbai, India"
    assert job["countries"] == ["IN"]
    assert job["description"] == "Python & Go"
    assert job["posted_at"].startswith("2026-11-06")
    assert calls[0][1]["headers"] == {"Accept": "application/json"}


FRESHTEAM_CLASSIC = """
<div class="job-list-info">
<a href="/jobs/Doy_p4CnkDuE/record-to-report-bangalore" class="job-title">Record to Report</a>
<a href="/jobs/Doy_p4CnkDuE/record-to-report-bangalore" class="job-desc text">...</a>
</div>
<div class="job-location">
<a href="/jobs/Doy_p4CnkDuE/record-to-report-bangalore" class="location-info">
Bangalore, India <br/>
Full Time
</a></div>
"""
FRESHTEAM_THEME = """
<a href="/jobs/OqG_9GjoYRX_/brand-strategist" class="heading" data-portal-title="x"
 data-portal-location="Bengaluru, India" data-portal-job-type="2"
 data-portal-remote-location=false>
  <div class="row"><div class="job-list-info">
  <div class="job-title">Brand Strategist &amp; Copywriter</div></div></div></a>
<a href="/jobs/AAAA1111/remote-sre" class="heading" data-portal-location="India"
 data-portal-remote-location=true><div class="job-title">SRE</div></a>
"""


@pytest.mark.parametrize(
    "page_html,expected",
    [
        (FRESHTEAM_CLASSIC, [("Record to Report", "Bangalore, India", "unknown")]),
        (
            FRESHTEAM_THEME,
            [
                ("Brand Strategist & Copywriter", "Bengaluru, India", "unknown"),
                ("SRE", "India", "remote"),
            ],
        ),
    ],
)
async def test_freshteam_both_themes(monkeypatch, page_html, expected):
    _serve(monkeypatch, {"ninjacart.freshteam.com/jobs": page_html})
    source = Source(id="freshteam:ninjacart", family="freshteam", tenant="ninjacart")
    jobs = (await fetch_page(source)).jobs
    assert [(j["title"], j["location"], j["remote"]) for j in jobs] == expected
    assert all(j["url"].startswith("https://ninjacart.freshteam.com/jobs/") for j in jobs)


def _kula_page(jobs):
    flight = '1:["$","div",null,{"careers":{"jobs":' + json.dumps(jobs) + "}}]"
    return f"<html><script>self.__next_f.push([1,{json.dumps(flight)}])</script></html>"


async def test_kula_board_reads_flight_payload(monkeypatch):
    jobs = [
        {
            "id": 4467,
            "title": "Security Engineer-2",
            "listed": True,
            "on_career_page": True,
            "launch_at": "2026-08-11T06:02:43.000Z",
            "end_at": None,
            "ats_job": {
                "workplace": "office",
                "offices": [
                    {"location": "Bellandur, Karnataka, India", "country_code": "IN"},
                ],
            },
        },
        {"id": 5, "title": "Hidden", "listed": False, "ats_job": {}},
        {"id": "x", "title": "Bad id"},
    ]
    _serve(monkeypatch, {"careers.kula.ai/cashfree": _kula_page(jobs)})
    page = await fetch_page(Source(id="kula:cashfree", family="kula", tenant="cashfree"))
    assert [j["title"] for j in page.jobs] == ["Security Engineer-2"]
    job = page.jobs[0]
    assert job["url"] == "https://careers.kula.ai/cashfree/4467"
    assert job["countries"] == ["IN"] and job["remote"] == "unknown"


def test_kula_page_without_jobs_is_empty():
    assert connectors.parse_kula("<html>no flight</html>", "x") == []
    assert connectors.parse_kula(_kula_page("not a list"), "x") == []


async def test_zohorecruit_board_reads_embedded_json(monkeypatch):
    listing = [
        {
            "id": "175309000000338019",
            "Posting_Title": "Sales executive",
            "City": "Mumbai",
            "State": "Maharashtra",
            "Country": "India",
            "Remote_Job": False,
            "Publish": True,
            "Date_Opened": "2026-11-01",
        },
        {"id": "1", "Posting_Title": "Draft", "Publish": False},
    ]
    page_html = f'<input type="hidden" value="{html.escape(json.dumps(listing))}" id="jobs">'
    calls = _serve(monkeypatch, {"hirewala.zohorecruit.in/jobs/Careers": page_html})
    source = Source(id="zohorecruit:in-hirewala", family="zohorecruit", tenant="in:hirewala")
    jobs = (await fetch_page(source)).jobs
    assert calls[0][0] == "https://hirewala.zohorecruit.in/jobs/Careers"
    assert [j["title"] for j in jobs] == ["Sales executive"]
    assert jobs[0]["url"] == "https://hirewala.zohorecruit.in/jobs/Careers/175309000000338019"
    assert jobs[0]["location"] == "Mumbai, Maharashtra, India"
    assert jobs[0]["countries"] == ["IN"]


async def test_zohorecruit_rejects_bad_tenant():
    with pytest.raises(ValueError):
        await fetch_page(Source(id="z", family="zohorecruit", tenant="in:a.b"))


@pytest.mark.parametrize(
    ("url", "family", "tenant"),
    [
        ("https://fynd.keka.com/careers/jobdetails/146668", "keka", "fynd"),
        ("https://ninjacart.freshteam.com/jobs", "freshteam", "ninjacart"),
        ("https://careers.kula.ai/cashfree/4467-security-engineer-2", "kula", "cashfree"),
        ("https://hirewala.zohorecruit.in/jobs/Careers", "zohorecruit", "in:hirewala"),
        ("https://acme.zohorecruit.com/jobs/Careers/1", "zohorecruit", "acme"),
    ],
)
def test_resolve_india_ats_urls(url, family, tenant):
    source = resolve_careers_url(url)
    assert (source.family, source.tenant) == (family, tenant)


def test_resolve_ignores_vendor_sites():
    assert resolve_careers_url("https://www.keka.com/careers") is None


WORKDAY_FACETS = [
    {
        "facetParameter": "locationMainGroup",
        "values": [
            {
                "facetParameter": "locationCountry",
                "values": [
                    {"descriptor": "United States", "id": "bc33aa3152ec42d4995f4791a106ed09"},
                    {"descriptor": "India", "id": "c4f78be1a8f14da0ab49ce1162348a5e"},
                ],
            },
            {
                "facetParameter": "locations",
                "values": [{"descriptor": "Bangalore, India", "id": "aaa"}],
            },
        ],
    }
]


def test_workday_country_facet_preferred_over_locations():
    applied = connectors.workday_country_facets(WORKDAY_FACETS, "IN")
    assert applied == {"locationCountry": ["c4f78be1a8f14da0ab49ce1162348a5e"]}


def test_workday_falls_back_to_india_locations_and_skips_indiana():
    facets = [
        {
            "facetParameter": "locations",
            "values": [
                {"descriptor": "Bangalore, India", "id": "a1"},
                {"descriptor": "Chennai, Tamil Nadu, India", "id": "a2"},
                {"descriptor": "Indianapolis, IN", "id": "us1"},
                {"descriptor": "Indiana, United States", "id": "us2"},
            ],
        },
        {"facetParameter": "Location_Region", "values": [{"descriptor": "Indiana", "id": "x"}]},
    ]
    assert connectors.workday_country_facets(facets, "IN") == {"locations": ["a1", "a2"]}
    assert connectors.workday_country_facets([], "IN") == {}


async def test_workday_india_board_applies_facet_and_tags_country(monkeypatch):
    connectors._workday_facets.clear()
    bodies = []

    async def get(url, json_body=None, **kwargs):
        bodies.append(json_body)
        request = httpx.Request("POST", url)
        if json_body["limit"] == 1:
            return httpx.Response(200, json={"facets": WORKDAY_FACETS}, request=request)
        posting = {"title": "SDE", "externalPath": "/job/Bangalore/SDE_1", "postedOn": "Today"}
        return httpx.Response(200, json={"total": 1, "jobPostings": [posting]}, request=request)

    monkeypatch.setattr(connectors, "public_get", get)
    source = Source(
        id="workday:acme-ext-india",
        family="workday",
        tenant="acme.wd5.myworkdayjobs.com|acme|Ext",
        country="IN",
    )
    page = await fetch_page(source)
    assert bodies[1]["appliedFacets"] == {"locationCountry": ["c4f78be1a8f14da0ab49ce1162348a5e"]}
    assert page.jobs[0]["countries"] == ["IN"]
    await fetch_page(source)  # facets are cached: one discovery request in total
    assert sum(1 for b in bodies if b["limit"] == 1) == 1


async def test_workday_board_without_india_jobs_is_empty(monkeypatch):
    connectors._workday_facets.clear()
    _serve(monkeypatch, {"/wday/cxs/": {"facets": []}})
    source = Source(
        id="w", family="workday", tenant="acme.wd5.myworkdayjobs.com|acme|Ext", country="IN"
    )
    assert (await fetch_page(source)).jobs == []


async def test_smartrecruiters_country_filter_and_structured_country(monkeypatch):
    posting = {
        "id": "1",
        "name": "Data Analyst",
        "location": {"city": "Deogarh", "country": "in"},
        "releasedDate": "2026-11-01T00:00:00Z",
    }
    calls = _serve(monkeypatch, {"api.smartrecruiters.com": {"content": [posting]}})
    source = Source(id="sr", family="smartrecruiters", tenant="swiggy", country="IN")
    page = await fetch_page(source)
    assert "country=in" in calls[0][0]
    assert page.jobs[0]["countries"] == ["IN"]


async def test_himalayas_country_search(monkeypatch):
    job = {
        "title": "Engineer",
        "companyName": "Drivetrain",
        "applicationLink": "https://himalayas.app/companies/drivetrain/jobs/engineer",
        "locationRestrictions": ["India", "United States"],
        "pubDate": 1792198336,
    }
    calls = _serve(
        monkeypatch, {"himalayas.app/jobs/api/search": {"jobs": [job], "totalCount": 45}}
    )
    source = Source(id="himalayas:india", family="himalayas", country="IN")
    page = await fetch_page(source)
    assert "country=india" in calls[0][0].lower()
    assert page.jobs[0]["countries"] == ["IN", "united states"]
    assert page.next_cursor == "2"  # 20 a page on search


async def test_lever_and_ashby_structured_countries(monkeypatch):
    lever = [
        {
            "id": "1",
            "text": "SDE",
            "hostedUrl": "https://jobs.lever.co/cred/1",
            "categories": {"location": "Bangalore"},
            "country": "IN",
            "workplaceType": "remote",
        }
    ]
    ashby = {
        "jobs": [
            {
                "id": "a",
                "title": "ML Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/sarvam/a",
                "location": "Bengaluru",
                "address": {"postalAddress": {"addressCountry": "India"}},
                "secondaryLocations": [
                    {"address": {"postalAddress": {"addressCountry": "United States"}}}
                ],
            }
        ]
    }
    _serve(monkeypatch, {"api.lever.co": lever, "api.ashbyhq.com": ashby})
    lever_job = (await fetch_page(Source(id="l", family="lever", tenant="cred"))).jobs[0]
    assert lever_job["countries"] == ["IN"] and lever_job["remote"] == "remote"
    ashby_job = (await fetch_page(Source(id="a", family="ashby", tenant="sarvam"))).jobs[0]
    assert ashby_job["countries"] == ["IN", "united states"]


def test_source_company_and_hq_tag_normalized_jobs():
    source = Source(
        id="greenhouse:razorpaysoftwareprivatelimited",
        family="greenhouse",
        tenant="razorpaysoftwareprivatelimited",
        company="Razorpay",
        company_country="IN",
    )
    raw = {"title": "SDE", "company": "razorpaysoftwareprivatelimited", "url": "https://x.io/1"}
    job = connectors.normalize(raw, source)
    assert job["company"] == "Razorpay" and job["company_country"] == "IN"
    assert "countries" not in job


def test_role_key_folds_indian_city_spellings():
    a = {"company": "Razorpay", "title": "SDE 2", "location": "Bangalore, Karnataka"}
    b = {"company": "Razorpay Software Private Limited", "title": "SDE 2", "location": "Bengaluru"}
    assert role_key(a) == role_key(b)
    merged = connectors.merge_duplicates(
        [
            {**a, "url": "https://www.linkedin.com/jobs/view/1", "job_id": "l"},
            {**b, "url": "https://boards.greenhouse.io/razorpay/jobs/1", "job_id": "g"},
        ]
    )
    assert len(merged) == 1 and merged[0]["url"].startswith("https://boards.greenhouse.io")
