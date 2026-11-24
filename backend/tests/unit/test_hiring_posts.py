"""Hiring posts from X and LinkedIn: filtering, parsing and fetch wiring (no network)."""

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


def test_careers_page_link_is_used():
    [job] = hp.parse_x(
        x_page(x_post("We're hiring a Data Engineer, apply at https://acme.example/careers/42"))
    )
    assert job["url"] == "https://acme.example/careers/42"


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
    with pytest.raises(ValueError):
        await hp.fetch_x('"software engineer"')
    with pytest.raises(ValueError):
        await hp.fetch_linkedin_posts('"software engineer"')


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
    response = MagicMock()
    response.json.return_value = searx()
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch.object(hp.httpx, "AsyncClient", return_value=client):
        assert await hp.fetch_linkedin_posts('"data analyst"') == []
    url, kwargs = client.get.await_args.args[0], client.get.await_args.kwargs
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
