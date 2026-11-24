import asyncio

import httpx

from app.services.apply_url import ats_link_in, resolve_original_apply_url
from app.workflows.extension_activities import detect_platform

LISTING = """
<a href="/about">About</a>
<a href="https://boards.greenhouse.io/acme/jobs/1">See company jobs</a>
<a href="https://jobs.lever.co/acme/abc/apply">Apply now</a>
<a href="http://insecure.myworkdayjobs.com/x">Apply</a>
"""


def _fetch(body, status=200):
    async def fetch(url, **kwargs):
        return httpx.Response(status, text=body, request=httpx.Request("GET", url))

    return fetch


def test_the_apply_link_to_an_ats_wins_and_insecure_links_are_ignored():
    assert (
        ats_link_in(LISTING, "https://board.example/job/1")
        == "https://jobs.lever.co/acme/abc/apply"
    )
    assert ats_link_in('<a href="http://x.greenhouse.io/1">Apply</a>', "https://b.example") is None


def test_listing_resolves_to_the_employers_ats_page():
    url = asyncio.run(resolve_original_apply_url("https://remotive.com/jobs/7", _fetch(LISTING)))
    assert url == "https://jobs.lever.co/acme/abc/apply"


def test_ats_pages_and_login_boards_are_never_fetched():
    async def boom(url, **kwargs):
        raise AssertionError("must not fetch")

    for url in (
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://www.linkedin.com/jobs/view/1",
        "https://www.naukri.com/job-listings-x",
        "https://internshala.com/internship/detail/python-internship-at-acme123",
    ):
        assert asyncio.run(resolve_original_apply_url(url, boom)) == url


def test_failures_keep_the_listing_url():
    url = "https://remotive.com/jobs/7"
    assert asyncio.run(resolve_original_apply_url(url, _fetch("", 404))) == url

    async def down(url, **kwargs):
        raise ValueError("Host must resolve only to public addresses")

    assert asyncio.run(resolve_original_apply_url(url, down)) == url


def test_more_ats_platforms_are_recognised():
    assert detect_platform("https://jobs.smartrecruiters.com/Acme/1") == "smartrecruiters"
    assert detect_platform("https://apply.workable.com/acme/j/1") == "workable"
    assert detect_platform("https://acme.myworkdayjobs.com/en/x") == "workday"
    assert detect_platform("https://internshala.com/job/detail/python-developer-job-at-acme1") == (
        "internshala"
    )
