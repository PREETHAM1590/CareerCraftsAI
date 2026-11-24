"""Jobvite, JazzHR, Dover, Paylocity, SAP SuccessFactors and Taleo connectors (no network)."""

# ruff: noqa: E501

import json

import httpx
import pytest

from app.services import job_connectors as connectors
from app.services.job_connectors import Source, resolve_careers_url


def _serve(monkeypatch, routes):
    """Answer each GET/POST by the first route whose key is a substring of the URL."""
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


@pytest.mark.parametrize(
    ("url", "family", "tenant"),
    [
        ("https://jobs.jobvite.com/tylertech/jobs", "jobvite", "tylertech"),
        ("https://jobs.jobvite.com/tylertech/job/o0qHAfwr", "jobvite", "tylertech"),
        ("https://acme.applytojob.com/apply/KxtgmvDrXc/Engineer", "jazzhr", "acme"),
        ("https://app.dover.com/jobs/needletailai?embed=1", "dover", "needletailai"),
        (
            "https://app.dover.com/apply/Dover/577fcef4-b129-48b8-8aee-0faae7c7cca5",
            "dover",
            "Dover",
        ),
        ("https://app.dover.com/Acme/careers/abc", "dover", "Acme"),
        (
            "https://recruiting.paylocity.com/recruiting/jobs/All/abc-123/Acme",
            "paylocity",
            "abc-123",
        ),
        (
            "https://2000recruiting.paylocity.com/Recruiting/Jobs/All/e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c",
            "paylocity",
            "e2bcef5a-b6e5-4c5a-8fdd-c4da179dd98c",
        ),
        ("https://jobs.zf.com/search/?q=", "successfactors", "jobs.zf.com"),
        (
            "https://valero.taleo.net/careersection/2/jobsearch.ftl?lang=en",
            "taleo",
            "valero.taleo.net|2",
        ),
        (
            "https://hdr.taleo.net/careersection/hdr_mobile/jobdetail.ftl?job=1",
            "taleo",
            "hdr.taleo.net|hdr_mobile",
        ),
    ],
)
def test_resolve_direct_portal_urls(url, family, tenant):
    source = resolve_careers_url(url)
    assert (source.family, source.tenant) == (family, tenant)
    assert source.family in connectors.FAMILIES


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.jobvite.com/",
        "https://jobs.jobvite.com/api/job",
        "https://applytojob.com/apply",
        "https://www.applytojob.com/apply",
        "https://app.dover.com/",
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4646713",
        "https://jobs.example.com/search/",  # branded SuccessFactors hosts must be allow-listed
        "https://valero.taleo.net/",
        "https://valero.taleo.net/careersection/rest/jobboard/searchjobs",
        "https://tbe.taleo.net/ats/servlet/Rss?org=PHF",
    ],
)
def test_resolve_direct_portal_urls_rejects_incomplete_or_unknown(url):
    assert resolve_careers_url(url) is None


_JOBVITE = """<html><head><title>Tyler Technologies Careers</title></head><body>
<h3 class="h2">Administration</h3><table class="jv-job-list"><tbody>
<tr><td class="jv-job-list-name"><a href="/tylertech/job/o0qHAfwr">Office Administrator</a></td>
<td class="jv-job-list-location">

    Ahmedabad,
    Gujarat

</td></tr>
<tr><td class="jv-job-list-name"><a href="/tylertech/job/odCGAfwP">Receptionist</a></td>
<td class="jv-job-list-location"><div class="jv-meta">2 Locations</div></td></tr>
</tbody></table></body></html>"""


@pytest.mark.asyncio
async def test_jobvite_parses_listing(monkeypatch):
    calls = _serve(monkeypatch, {"jobs.jobvite.com/tylertech/jobs": _JOBVITE})
    source = Source(id="jobvite:tylertech", family="jobvite", tenant="tylertech")
    page = await connectors.fetch_page(source)
    assert calls[0][0] == "https://jobs.jobvite.com/tylertech/jobs"
    assert [(j["title"], j["company"], j["location"]) for j in page.jobs] == [
        ("Office Administrator", "Tyler Technologies", "Ahmedabad, Gujarat"),
        ("Receptionist", "Tyler Technologies", "2 Locations"),
    ]
    assert page.jobs[0]["url"] == "https://jobs.jobvite.com/tylertech/job/o0qHAfwr"
    assert page.jobs[0]["platform"] == "jobvite" and page.next_cursor is None


_JAZZ = """<html><head><title>BELAY - Corporate - Career Page</title></head><body>
<ul class='list-group'>
<li class="list-group-item">
  <h3 class='list-group-item-heading'>
    <a href="https://belay.applytojob.com/apply/eUEGaNpWat/Client-Success-Consultant">
      Client Success Consultant &amp; Lead </a>
  </h3>
  <ul class='list-inline list-group-item-text'>
    <li><i class='fa fa-map-marker'></i>Remote</li>
    <li><i class='fa fa-sitemap'></i>Client Relations</li>
  </ul>
</li>
<li class="list-group-item"><h3 class='list-group-item-heading'>no link here</h3></li>
<li class="list-group-item">
  <h3 class='list-group-item-heading'><a href="https://belay.applytojob.com/apply/0imc2cBtIp/Controller">Controller</a></h3>
  <ul class='list-inline list-group-item-text'><li><i class='fa fa-map-marker'></i>Austin, TX</li></ul>
</li>
</ul></body></html>"""


@pytest.mark.asyncio
async def test_jazzhr_parses_listing_and_skips_linkless_items(monkeypatch):
    _serve(monkeypatch, {"belay.applytojob.com/apply": _JAZZ})
    source = Source(id="jazzhr:belay", family="jazzhr", tenant="belay")
    page = await connectors.fetch_page(source)
    assert [(j["title"], j["company"], j["location"]) for j in page.jobs] == [
        ("Client Success Consultant & Lead", "BELAY - Corporate", "Remote"),
        ("Controller", "BELAY - Corporate", "Austin, TX"),
    ]
    assert page.jobs[0]["remote"] == "remote"


@pytest.mark.parametrize(
    "fn", [connectors.parse_jobvite, connectors.parse_jazzhr, connectors.parse_paylocity]
)
@pytest.mark.parametrize("text", ["", "<html></html>", "not html at all \x00"])
def test_html_parsers_tolerate_malformed_pages(fn, text):
    assert fn(text, "acme") == []


_DOVER_META = {"id": "72c2afbf-36e4-4eec-8d78-5d2600498018", "slug": "qompyl", "name": "Qompyl"}
_DOVER_JOBS = {
    "count": 4,
    "next": None,
    "results": [
        {
            "id": "8fbc227b-462b-4481-a98a-35d4af62c140",
            "title": "Product Owner",
            "locations": [{"name": "International"}, {"name": "Japan"}],
            "workplace_type": "REMOTE",
            "is_published": True,
            "is_sample": False,
        },
        {"id": "sample1", "title": "Sample", "is_published": True, "is_sample": True},
        {"id": "draft1", "title": "Draft", "is_published": False, "is_sample": False},
        {"title": "No id"},
    ],
}


@pytest.mark.asyncio
async def test_dover_resolves_client_then_lists_published_jobs(monkeypatch):
    calls = _serve(
        monkeypatch,
        {
            "careers-page-slug/qompyl": _DOVER_META,
            "careers-page/72c2afbf-36e4-4eec-8d78-5d2600498018/jobs": _DOVER_JOBS,
        },
    )
    source = Source(id="dover:qompyl", family="dover", tenant="qompyl")
    page = await connectors.fetch_page(source)
    assert "limit=100" in calls[1][0] and "offset=0" in calls[1][0]
    assert [(j["title"], j["company"], j["location"], j["remote"]) for j in page.jobs] == [
        ("Product Owner", "Qompyl", "International, Japan", "remote")
    ]
    assert (
        page.jobs[0]["url"]
        == "https://app.dover.com/apply/qompyl/8fbc227b-462b-4481-a98a-35d4af62c140"
    )
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_dover_follows_next_by_offset_and_rejects_unknown_board(monkeypatch):
    calls = _serve(
        monkeypatch,
        {
            "careers-page-slug/qompyl": _DOVER_META,
            "careers-page/72c2afbf-36e4-4eec-8d78-5d2600498018/jobs": {
                **_DOVER_JOBS,
                "next": "https://evil.example/x",
            },
        },
    )
    source = Source(id="dover:qompyl", family="dover", tenant="qompyl")
    page = await connectors.fetch_page(source, cursor="2")
    assert "offset=100" in calls[1][0] and page.next_cursor == "3"
    assert all(url.startswith("https://app.dover.com/") for url, _ in calls)

    _serve(monkeypatch, {"careers-page-slug/ghost": {"detail": "Not found."}})
    with pytest.raises(ValueError, match="not found"):
        await connectors.fetch_page(Source(id="dover:ghost", family="dover", tenant="ghost"))


_PAYLOCITY = (
    '<script>window.pageData = {"Culture":"en","Jobs":[{"JobId":4646713,"JobTitle":"Senior Ads Strategist",'
    '"LocationName":"","PublishedDate":"2026-11-06T06:43:33-06:00","Description":"About {the} company [x]",'
    '"IsInternal":false,"JobLocation":{"City":"Austin","State":"TX"},"IsRemote":true},'
    '{"JobId":1,"JobTitle":"Internal only","IsInternal":true},'
    '{"JobId":4638710,"JobTitle":"Forklift Operator","LocationName":"Denver","PublishedDate":"2026-10-30T10:00:00-06:00",'
    '"IsInternal":false,"JobLocation":{},"IsRemote":false}],"Other":[1]};</script>'
)


@pytest.mark.asyncio
async def test_paylocity_reads_embedded_jobs_array(monkeypatch):
    calls = _serve(monkeypatch, {"Recruiting/Jobs/All/acme-board": _PAYLOCITY})
    source = Source(id="paylocity:acme-board", family="paylocity", tenant="acme-board")
    page = await connectors.fetch_page(source)
    assert calls[0][0] == "https://recruiting.paylocity.com/Recruiting/Jobs/All/acme-board"
    assert [(j["title"], j["location"], j["remote"]) for j in page.jobs] == [
        ("Senior Ads Strategist", "Austin, TX", "remote"),
        ("Forklift Operator", "Denver", "unknown"),
    ]
    assert page.jobs[0]["url"].endswith("/Recruiting/Jobs/Details/4646713")
    assert page.jobs[0]["posted_at"] == "2026-11-06T12:43:33+00:00"
    assert page.jobs[0]["description"] == "About {the} company [x]"


def test_parse_paylocity_survives_truncated_json():
    assert connectors.parse_paylocity('"Jobs":[{"JobId":1,"JobTitle":"x"', "acme") == []
    assert connectors.parse_paylocity('"Jobs":{"a":1}', "acme") == []


def _tile(job_id, path, title, fields=""):
    return (
        f'<li class="job-tile job-id-{job_id} job-row-index-1" data-url="{path}"><div>'
        f'<a class="jobTitle-link fontcolorabc" data-focus-tile=".job-id-{job_id}" href="{path}">\n {title}\n</a>'
        f"{fields}</div></li>"
    )


_SF = (
    _tile(
        "1396295733",
        "/job/Shanghai-Facility-Supervisor-SH-201814/1396295733/",
        "Facility Supervisor",
    )
    + _tile(
        "1393071733",
        "/Electrify_America/job/Contract-Solution-Architect-SAP/1393071733/",
        "Contract Solution Architect - SAP",
        '<span id="job-1393071733-desktop-section-location-value">US</span>'
        '<span id="job-1393071733-desktop-section-date-value">Nov 12, 2026</span>',
    )
    + '<li class="job-tile job-id-5"><span>no title link</span></li>'
)


@pytest.mark.asyncio
async def test_successfactors_reads_tiles_and_pages_by_row(monkeypatch):
    calls = _serve(monkeypatch, {"jobs.zf.com/tile-search-results": _SF})
    source = Source(id="successfactors:jobs.zf.com", family="successfactors", tenant="jobs.zf.com")
    page = await connectors.fetch_page(source)
    assert "startrow=0" in calls[0][0]
    first, second = page.jobs
    assert (first["title"], first["location"], first["posted_at"]) == (
        "Facility Supervisor",
        "Shanghai",
        None,
    )
    assert (
        first["url"] == "https://jobs.zf.com/job/Shanghai-Facility-Supervisor-SH-201814/1396295733"
    )
    assert (second["location"], second["posted_at"]) == ("US", "2026-11-12T00:00:00+00:00")
    assert page.next_cursor == "3"
    await connectors.fetch_page(source, cursor="3")
    assert "startrow=3" in calls[1][0]


@pytest.mark.asyncio
async def test_successfactors_ends_on_empty_page_and_rejects_foreign_hosts(monkeypatch):
    _serve(monkeypatch, {"jobs.zf.com/tile-search-results": ""})
    source = Source(id="successfactors:jobs.zf.com", family="successfactors", tenant="jobs.zf.com")
    assert (await connectors.fetch_page(source)).next_cursor is None
    for tenant in ("internal.example.com", "jobs.zf.com@evil.example", "169.254.169.254"):
        with pytest.raises(ValueError, match="Invalid source tenant"):
            await connectors.fetch_page(Source(id="x", family="successfactors", tenant=tenant))
    with pytest.raises(ValueError, match="Pagination"):
        await connectors.fetch_page(source, cursor="5000")


_TALEO_SECTION = "<script>var o = { portalNo: '101430233', lang: 'en' };</script>"


def _taleo_page(total, size=2):
    return {
        "requisitionList": [
            {
                "jobId": "314340",
                "contestNo": "26001GF",
                "column": ["Mechanic II (Millwright II)", '["US-LA-Norco"]', "Nov 10, 2026"],
                "locationsColumns": [1],
            },
            {
                "jobId": "2",
                "contestNo": "26002",
                "column": ["Engineer", '["US-TX-Houston","US-OK-Tulsa"]'],
                "locationsColumns": [1],
            },
            {"jobId": "3", "column": ["No contest number"]},
        ],
        "pagingData": {"currentPageNo": 1, "pageSize": size, "totalCount": total},
    }


@pytest.mark.asyncio
async def test_taleo_opens_section_then_posts_search_and_cursors_by_page(monkeypatch):
    calls = _serve(
        monkeypatch,
        {
            "careersection/2/jobsearch.ftl": _TALEO_SECTION,
            "jobboard/searchjobs": _taleo_page(total=5),
        },
    )
    source = Source(id="taleo:valero-2", family="taleo", tenant="valero.taleo.net|2")
    page = await connectors.fetch_page(source)
    assert (
        calls[1][0]
        == "https://valero.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=101430233"
    )
    assert calls[1][1]["json_body"]["pageNo"] == 1
    assert [(j["title"], j["location"], j["posted_at"]) for j in page.jobs] == [
        ("Mechanic II (Millwright II)", "US-LA-Norco", "2026-11-10T00:00:00+00:00"),
        ("Engineer", "US-TX-Houston; US-OK-Tulsa", None),
    ]
    assert (
        page.jobs[0]["url"]
        == "https://valero.taleo.net/careersection/2/jobdetail.ftl?job=26001GF&lang=en"
    )
    assert page.next_cursor == "101430233:2"

    last = await connectors.fetch_page(source, cursor="101430233:3")
    assert calls[-1][1]["json_body"]["pageNo"] == 3 and last.next_cursor is None
    assert len(calls) == 3  # later pages skip the section fetch


@pytest.mark.asyncio
async def test_taleo_rejects_bad_tenant_cursor_and_private_section(monkeypatch):
    _serve(monkeypatch, {"jobsearch.ftl": "<html>login required</html>"})
    for tenant in (
        "evil.example.com|2",
        "valero.taleo.net|../x",
        "valero.taleo.net",
        "valero.taleo.net.evil.com|2",
    ):
        with pytest.raises(ValueError, match="Invalid source tenant"):
            await connectors.fetch_page(Source(id="x", family="taleo", tenant=tenant))
    source = Source(id="taleo:valero-2", family="taleo", tenant="valero.taleo.net|2")
    with pytest.raises(ValueError, match="not public"):
        await connectors.fetch_page(source)
    for cursor in ("abc:2", "123:99", "123:1", "123"):
        with pytest.raises(ValueError, match="Pagination"):
            await connectors.fetch_page(source, cursor=cursor)


def test_parse_taleo_tolerates_odd_payloads():
    assert connectors.parse_taleo({}, "a.taleo.net", "2") == []
    assert (
        connectors.parse_taleo({"requisitionList": [None, {"column": []}, "x"]}, "a.taleo.net", "2")
        == []
    )
    row = connectors.parse_taleo(
        {
            "requisitionList": [
                {"contestNo": "1", "column": ["T", "not json"], "locationsColumns": [1, 9]}
            ]
        },
        "a.taleo.net",
        "2",
    )[0]
    assert row["location"] == "" and row["posted_at"] is None


@pytest.mark.asyncio
async def test_search_board_filters_taleo_client_side(monkeypatch):
    _serve(
        monkeypatch,
        {
            "jobsearch.ftl": _TALEO_SECTION,
            "jobboard/searchjobs": json.loads(json.dumps(_taleo_page(total=2))),
        },
    )
    source = Source(id="taleo:valero-2", family="taleo", tenant="valero.taleo.net|2")
    jobs = await connectors.search_board(source, "engineer")
    assert [j["title"] for j in jobs] == ["Engineer"]


def test_new_families_are_registered_and_hosts_are_ats():
    for family in ("jobvite", "jazzhr", "dover", "paylocity", "successfactors", "taleo"):
        assert family in connectors.FAMILIES
    assert connectors.is_ats_url("https://app.dover.com/apply/x/y")
    assert connectors.is_ats_url("https://recruiting.paylocity.com/Recruiting/Jobs/All/x")
    assert not connectors.is_ats_url("https://www.dover.com/")
