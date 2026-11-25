"""Hiring posts from X and LinkedIn: filtering, parsing and fetch wiring (no network)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import hiring_posts as hp
from app.services.job_connectors import Source, fetch_page


def x_post(text, post_id="1859000000000000001", author="11", urls=None):
    post = {
        "id": post_id,
        "text": text,
        "author_id": author,
        "created_at": "2026-11-23T10:00:00.000Z",
    }
    if urls:
        post["entities"] = {"urls": urls}
    return post


def x_page(*posts, users=None):
    return {
        "data": list(posts),
        "includes": {"users": users or [{"id": "11", "username": "jane_dev", "name": "Jane"}]},
        "meta": {"result_count": len(posts)},
    }


# ── What counts as a hiring post ────────────────────────────────


def test_genuine_post_becomes_a_job_with_ats_link_and_credit():
    page = x_page(
        x_post(
            "We're hiring a Senior Backend Engineer at Acme in Bengaluru! Apply: https://t.co/abc",
            urls=[
                {
                    "url": "https://t.co/abc",
                    "expanded_url": "https://boards.greenhouse.io/acme/jobs/123?gh_src=x",
                    "unwound_url": "https://boards.greenhouse.io/acme/jobs/123?gh_src=x",
                }
            ],
        )
    )
    [job] = hp.parse_x(page)
    assert job["title"] == "Senior Backend Engineer"
    assert job["company"] == "Acme"
    assert job["location"] == "Bengaluru"
    assert job["url"] == "https://boards.greenhouse.io/acme/jobs/123?gh_src=x"
    assert job["posted_at"] == "2026-11-23T10:00:00.000Z"
    assert job["description"].endswith(
        "Posted by @jane_dev on X: https://x.com/jane_dev/status/1859000000000000001"
    )


@pytest.mark.parametrize(
    "text",
    [
        # Job seekers, not employers.
        "#OpenToWork I'm looking for a job as a Data Analyst. "
        "We are hiring managers, please refer me!",
        "Seeking a new role as a Frontend Developer. Hiring managers, hire me!",
        # Scams.
        "We're hiring Python Developers! Registration fee Rs 499, join our Telegram t.me/jobs",
        "We are hiring Data Entry Executives, earn ₹5000 per day from home, WhatsApp me",
        "Hiring Software Engineers. Pay a small fee for training and get guaranteed placement",
        # Chatter that mentions a role but isn't hiring.
        "Software engineers are the best people to have at a party.",
        # Hiring, but no role we can name.
        "We're hiring! DM me.",
    ],
)
def test_non_jobs_are_dropped(text):
    assert hp.parse_x(x_page(x_post(text))) == []


def test_company_from_handle_and_poster_fallback():
    [handle] = hp.parse_x(x_page(x_post("@acmehq is hiring a Full Stack Developer, remote")))
    assert handle["company"] == "@acmehq"
    assert handle["location"] == "Remote"
    [fallback] = hp.parse_x(
        x_page(x_post("Hiring a Machine Learning Engineer for my team in Pune"))
    )
    assert fallback["company"] == "@jane_dev"
    assert fallback["title"] == "Machine Learning Engineer"


def test_remote_with_place_and_sde_titles():
    [job] = hp.parse_x(
        x_page(x_post("Razorpay is hiring SDE II engineers, remote within India. Join our team!"))
    )
    assert job["title"] == "SDE II"
    assert job["company"] == "Razorpay"
    assert job["location"] == "Remote / India"


def test_social_and_short_links_never_become_the_job_url():
    page = x_page(
        x_post(
            "We are hiring a Product Designer! Details: https://bit.ly/x https://lnkd.in/acme",
            urls=[{"expanded_url": "https://linkedin.com/jobs/view/1"}],
        )
    )
    [job] = hp.parse_x(page)
    assert job["url"] == "https://x.com/jane_dev/status/1859000000000000001"


def test_links_off_known_ats_hosts_never_become_the_job_url():
    # Any host could be a look-alike or phishing site: the post stays the link.
    for link in ("https://acme.example/careers/42", "https://acme-jobs.xyz/apply/form"):
        [job] = hp.parse_x(x_page(x_post(f"We're hiring a Data Engineer, apply at {link}")))
        assert job["url"] == "https://x.com/jane_dev/status/1859000000000000001"


def test_a_generic_ats_careers_page_is_not_a_posting():
    [job] = hp.parse_x(
        x_page(x_post("We're hiring a Data Engineer: https://boards.greenhouse.io/acme"))
    )
    assert job["url"] == "https://x.com/jane_dev/status/1859000000000000001"


def test_malformed_authors_and_ids_are_skipped():
    page = x_page(
        x_post("We're hiring a Backend Engineer", author="99"),
        x_post("We're hiring a Backend Engineer", post_id="../etc"),
        users=[{"id": "11", "username": "jane_dev"}, {"id": "99", "username": "bad handle!"}],
    )
    assert hp.parse_x(page) == []


# ── LinkedIn posts via SearXNG ──────────────────────────────────


def searx(*results):
    return {"query": "q", "results": list(results)}


def test_linkedin_posts_from_search_results():
    data = searx(
        {
            "url": "https://www.linkedin.com/posts/priya-sharma-123_hiring-dataanalyst-activity-7390000000000000000-abcd?utm_source=share",
            "title": "Priya Sharma on LinkedIn: We're hiring a Data Analyst at Zeta in Hyderabad",
            "content": "Join our team! 2+ years SQL. Apply via the link in comments. #hiring",
            "publishedDate": "2026-11-22T08:00:00",
        },
        # Same post again from a second engine.
        {
            "url": "https://www.linkedin.com/posts/priya-sharma-123_hiring-dataanalyst-activity-7390000000000000000-abcd",
            "title": "Priya Sharma on LinkedIn: We're hiring a Data Analyst at Zeta",
            "content": "",
        },
        # Not posts: job pages and profiles are left to other sources.
        {"url": "https://www.linkedin.com/jobs/view/123", "title": "Data Analyst - Zeta"},
        {"url": "https://in.linkedin.com/in/priya", "title": "Priya Sharma - Zeta"},
        {"url": "https://evil.example/posts/x", "title": "We're hiring a Data Analyst"},
    )
    [job] = hp.parse_searxng(data)
    assert job["title"] == "Data Analyst"
    assert job["company"] == "Zeta"
    assert job["location"] == "Hyderabad"
    assert job["url"].startswith("https://www.linkedin.com/posts/priya-sharma-123_")
    assert "?" not in job["url"]
    assert job["description"].endswith(f"Posted by Priya Sharma on LinkedIn: {job['url']}")


def test_linkedin_poster_from_url_when_title_has_none():
    [job] = hp.parse_searxng(
        searx(
            {
                "url": "https://www.linkedin.com/posts/rahul-verma_activity-7390000000000000001-xyz",
                "title": "Hiring now",
                "content": "We are hiring Frontend Developers in Noida",
            }
        )
    )
    assert job["company"] == "Rahul Verma"


# ── Fetch wiring ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sources_stay_inert_without_configuration(monkeypatch):
    monkeypatch.setattr(settings, "X_BEARER_TOKEN", "")
    monkeypatch.setattr(settings, "SEARXNG_URL", None)
    # Off is not a failure: nothing is requested and the source stays healthy.
    with patch.object(hp, "httpx") as client:
        assert await hp.fetch_x('"software engineer"') == []
        assert await hp.fetch_linkedin_posts('"software engineer"') == []
    client.AsyncClient.assert_not_called()


@pytest.mark.asyncio
async def test_x_request_is_one_bounded_page_with_bearer(monkeypatch):
    monkeypatch.setattr(settings, "X_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(settings, "X_HIRING_POSTS_PER_REFRESH", 500)
    response = MagicMock()
    response.json.return_value = x_page(x_post("We're hiring a Backend Engineer at Acme"))
    with patch("app.services.public_http.public_get", AsyncMock(return_value=response)) as get:
        jobs = await hp.fetch_x('"backend engineer"')
    url = get.await_args.args[0]
    assert url.startswith("https://api.x.com/2/tweets/search/recent?")
    assert "max_results=100" in url  # clamped
    assert "-is%3Aretweet" in url and "-is%3Areply" in url
    assert "next_token" not in url
    assert get.await_args.kwargs["headers"] == {"Authorization": "Bearer test-token"}
    assert [j["company"] for j in jobs] == ["Acme"]


@pytest.mark.asyncio
async def test_searxng_request_uses_json_and_recent_results(monkeypatch):
    monkeypatch.setattr(settings, "SEARXNG_URL", "http://127.0.0.1:18188/")
    import json as _json

    body = _json.dumps(searx()).encode()

    async def chunks():
        yield body

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.aiter_bytes = chunks
    stream = MagicMock()
    stream.__aenter__ = AsyncMock(return_value=response)
    stream.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch.object(hp.httpx, "AsyncClient", return_value=client):
        assert await hp.fetch_linkedin_posts('"data analyst"') == []
    assert client.stream.call_args.args[0] == "GET"
    url, kwargs = client.stream.call_args.args[1], client.stream.call_args.kwargs
    assert url == "http://127.0.0.1:18188/search"
    assert kwargs["params"]["format"] == "json"
    assert kwargs["params"]["time_range"] == "week"
    assert kwargs["params"]["q"].startswith('site:linkedin.com/posts ("data analyst")')


@pytest.mark.asyncio
async def test_catalog_fetch_normalizes_post_jobs():
    raw = {
        "id": "https://x.com/jane_dev/status/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote",
        "url": "https://x.com/jane_dev/status/1",
        "posted_at": "2026-11-23T10:00:00.000Z",
        "description": "We're hiring",
    }
    with patch.object(hp, "fetch_x", AsyncMock(return_value=[raw])) as fetch:
        page = await fetch_page(Source(id="x:software", family="x_hiring", url='"backend"'))
    fetch.assert_awaited_once_with('"backend"')
    [job] = page.jobs
    assert job["platform"] == "x_hiring"
    assert job["remote"] == "remote"
    assert page.next_cursor is None


def test_role_groups_are_registered_for_both_networks():
    from app.services.job_catalog import sources

    ids = {s.id: s for s in sources()}
    for group in ("software", "data-ai", "product-design", "freshers"):
        assert ids[f"x:{group}"].family == "x_hiring"
        assert ids[f"x:{group}"].refresh_hours == 24
        assert ids[f"linkedin-posts:{group}"].family == "linkedin_posts"


@pytest.mark.parametrize(
    "text",
    [
        "I'm looking for a Backend Engineer role in Bengaluru",
        "I am looking for a Data Analyst position in Pune. Freshers welcome to connect!",
        "Looking for a Senior Backend Engineer? I have 5 years of experience, DM me",
    ],
)
def test_job_seekers_naming_a_role_are_dropped(text):
    assert hp.post_to_job(text, "https://x.com/a/status/1", "@a", "X") is None


@pytest.mark.parametrize(
    "text",
    [
        "We're hiring a Backend Engineer. Application fee Rs 499.",
        "We are hiring a Python Developer! Apply via WhatsApp only.",
        "We're hiring a QA Engineer, WhatsApp-only applications",
        "Now hiring a Data Analyst - contact us on WhatsApp",
    ],
)
def test_fee_and_whatsapp_only_posts_are_dropped(text):
    assert hp.post_to_job(text, "https://x.com/a/status/1", "@a", "X") is None


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("#Hiring Backend Engineers at Acme", "Backend Engineer"),
        ("We're looking for a talented Frontend Developer to join us", "Frontend Developer"),
        ("We are hiring a Software Engineering Intern for summer", "Software Engineering Intern"),
    ],
)
def test_real_hiring_posts_are_kept(text, title):
    job = hp.post_to_job(text, "https://x.com/a/status/1", "@a", "X")
    assert job is not None and job["title"] == title


def test_malformed_and_trailing_punctuation_links():
    job = hp.post_to_job(
        "We're hiring a Backend Engineer: https://[broken then "
        "https://boards.greenhouse.io/acme/jobs/4123456.",
        "https://x.com/a/status/1",
        "@a",
        "X",
    )
    assert job["url"] == "https://boards.greenhouse.io/acme/jobs/4123456"


def test_a_fuller_result_for_the_same_linkedin_post_still_counts():
    url = "https://www.linkedin.com/posts/acme_hiring-activity-1"
    data = {
        "results": [
            {"url": url, "title": "Acme on LinkedIn", "content": "Acme is hiring"},
            {
                "url": url,
                "title": "Acme on LinkedIn",
                "content": "Acme is hiring a Backend Engineer",
            },
        ]
    }
    [job] = hp.parse_searxng(data)
    assert job["title"] == "Backend Engineer"


# ── Catalog refresh of hiring-post sources ──────────────────────


class _HealthDb:
    def __init__(self):
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        health = {"next_allowed_at": None, "cached_jobs": [], "warning": None, "failures": 0}
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: health))

    async def commit(self):
        return None


def _hiring_source(family="x_hiring"):
    from app.services.job_catalog import Source

    return Source(id=f"{family}:t", family=family, tenant="", url='"developer"')


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["x_hiring", "linkedin_posts"])
async def test_a_refresh_never_removes_earlier_hiring_posts(monkeypatch, family):
    from app.services import job_catalog
    from app.services.job_connectors import Page

    db = _HealthDb()

    async def fetch(source, query, cursor):
        return Page([], None)

    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(job_catalog, "fetch_page", fetch)
    monkeypatch.setattr(job_catalog, "upsert_jobs", AsyncMock())
    await job_catalog.refresh_source(_hiring_source(family))
    assert not any("DELETE FROM job_source_occurrences" in s for s in db.statements)


@pytest.mark.asyncio
async def test_an_x_timeout_is_not_retried(monkeypatch):
    from app.services import job_catalog

    calls = []

    async def fetch(source, query, cursor):
        calls.append(1)
        raise TimeoutError()

    monkeypatch.setattr(job_catalog, "AsyncSessionLocal", lambda: _HealthDb())
    monkeypatch.setattr(job_catalog, "fetch_page", fetch)
    await job_catalog.refresh_source(_hiring_source("x_hiring"))
    assert calls == [1]


def test_linkedin_poster_fallback_never_crashes_on_odd_paths():
    assert hp._linkedin_poster("", "https://www.linkedin.com/posts") == "a LinkedIn member"
    assert hp._linkedin_poster("", "https://www.linkedin.com/posts/priya-s_hiring-1") == "Priya S"


def test_posts_per_refresh_is_bounded_at_config_load():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(X_HIRING_POSTS_PER_REFRESH=500)
