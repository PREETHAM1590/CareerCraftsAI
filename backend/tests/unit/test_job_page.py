"""Full job descriptions from job pages: plain HTTP first, Obscura only when needed."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import job_page

LONG = "Build reliable systems. " * 60  # ~1,400 chars
JSONLD_PAGE = f"""<html><head><script type="application/ld+json">{
    json.dumps(
        {"@type": "JobPosting", "title": "Backend Engineer", "description": f"<p>{LONG}</p>"}
    )
}</script></head><body><nav>Menu</nav><main>Short teaser</main></body></html>"""
TEXT_PAGE = f"""<html><body><header>Site header</header><script>var x=1;</script>
<main><h1>Data Analyst</h1><p>{LONG}</p></main><footer>Footer links</footer></body></html>"""


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttl = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex


@pytest.fixture
def redis():
    fake = FakeRedis()
    with patch("app.core.redis_client.get_redis", return_value=fake):
        yield fake


def test_jsonld_description_wins_and_is_cleaned():
    text = job_page.from_html(JSONLD_PAGE, "https://jobs.example.com/1")
    assert text.startswith("Build reliable systems.")
    assert "<p>" not in text and "Menu" not in text


def test_main_text_without_page_chrome():
    text = job_page.from_html(TEXT_PAGE, "https://jobs.example.com/2")
    assert text.startswith("Data Analyst")
    assert "Site header" not in text and "Footer" not in text and "var x" not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/1",
        "https://in.indeed.com/viewjob?jk=1",
        "https://x.com/acme/status/1",
        "https://www.naukri.com/job-listings-1",
    ],
)
def test_login_walled_and_blocking_sites_are_skipped(url):
    assert job_page.skipped(url)


@pytest.mark.asyncio
async def test_plain_http_is_enough_for_most_pages(redis):
    with (
        patch.object(job_page, "robots_allow", AsyncMock(return_value=True)),
        patch.object(job_page, "fetch_plain", AsyncMock(return_value=LONG)),
        patch.object(job_page, "fetch_rendered", AsyncMock()) as render,
    ):
        assert await job_page.full_description("https://jobs.example.com/1?utm_source=x") == LONG
    render.assert_not_called()
    [(key, value)] = redis.data.items()
    assert value == LONG and redis.ttl[key] == 7 * 86400


@pytest.mark.asyncio
async def test_javascript_pages_are_rendered_in_obscura(redis):
    with (
        patch.object(job_page, "robots_allow", AsyncMock(return_value=True)),
        patch.object(job_page, "fetch_plain", AsyncMock(return_value="Loading…")),
        patch.object(job_page, "fetch_rendered", AsyncMock(return_value=LONG)) as render,
    ):
        assert await job_page.full_description("https://acme.wd5.myworkdayjobs.com/job/1") == LONG
    render.assert_awaited_once()


@pytest.mark.asyncio
async def test_robots_refusal_reads_nothing(redis):
    with (
        patch.object(job_page, "robots_allow", AsyncMock(return_value=False)),
        patch.object(job_page, "fetch_plain", AsyncMock()) as plain,
        patch.object(job_page, "fetch_rendered", AsyncMock()) as render,
    ):
        assert await job_page.full_description("https://jobs.example.com/1") is None
    plain.assert_not_called()
    render.assert_not_called()


@pytest.mark.asyncio
async def test_misses_are_cached_briefly_and_failures_are_quiet(redis):
    with (
        patch.object(job_page, "robots_allow", AsyncMock(return_value=True)),
        patch.object(job_page, "fetch_plain", AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        assert await job_page.full_description("https://jobs.example.com/1") is None
    [(key, value)] = redis.data.items()
    assert value == "" and redis.ttl[key] == 24 * 3600
    with patch.object(job_page, "robots_allow", AsyncMock()) as robots:
        assert await job_page.full_description("https://jobs.example.com/1") is None
    robots.assert_not_called()  # served from the cached miss


@pytest.mark.asyncio
async def test_non_https_and_skipped_urls_make_no_request(redis):
    with patch.object(job_page, "robots_allow", AsyncMock()) as robots:
        assert await job_page.full_description("http://jobs.example.com/1") is None
        assert await job_page.full_description("https://www.linkedin.com/jobs/view/1") is None
    robots.assert_not_called()


@pytest.mark.asyncio
async def test_rendering_is_off_without_obscura(monkeypatch):
    monkeypatch.setattr(job_page.settings, "OBSCURA_CDP_URL", "")
    assert await job_page.fetch_rendered("https://jobs.example.com/1") == ""


# ── /applications/{id}/jd ───────────────────────────────────────


def jd_db(app, catalog=""):
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalar_one_or_none.return_value = catalog
    db.commit = AsyncMock()
    return db


def saved_app(jd_text):
    return SimpleNamespace(
        id=uuid.uuid4(),
        jd_text=jd_text,
        job_url="https://jobs.example.com/1",
        role="R",
        company="C",
    )


@pytest.mark.asyncio
async def test_thin_saved_description_is_replaced_by_the_job_page():
    from app.api.v1 import jobs

    app = saved_app("We're hiring a Backend Engineer")
    db = jd_db(app)
    with (
        patch.object(jobs, "_owned_applications", AsyncMock(return_value=[app])),
        patch.object(jobs.job_page, "full_description", AsyncMock(return_value=LONG)),
    ):
        result = await jobs.get_application_jd(app.id, db=db, current_user=SimpleNamespace(id=1))
    assert result.source == "job_page" and result.jd_text == LONG
    assert app.jd_text == LONG
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_saved_description_never_fetches():
    from app.api.v1 import jobs

    app = saved_app(LONG)
    with (
        patch.object(jobs, "_owned_applications", AsyncMock(return_value=[app])),
        patch.object(jobs.job_page, "full_description", AsyncMock()) as fetch,
    ):
        result = await jobs.get_application_jd(
            app.id, db=jd_db(app), current_user=SimpleNamespace(id=1)
        )
    assert result.source == "application"
    fetch.assert_not_called()
