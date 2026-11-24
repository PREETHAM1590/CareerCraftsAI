"""Internshala connector: URL shapes, redirects, card parsing (no network)."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.services import internshala

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def card(kind, href, title, company, location, pay, posted, skills=("Python",), pay_class=None):
    pay_html = (
        f'<span class="stipend">{pay}</span>'
        if pay_class == "stipend"
        else f'<span class="desktop"> {pay} </span><span class="mobile">{pay} /year</span>'
    )
    skill_html = "".join(f'<div class="job_skill">{s}</div>' for s in skills)
    return f"""
<div class="container-fluid individual_internship" internshipid="1" employment_type="{kind}"
     data-href="{href}">
  <h2 class="job-internship-name"><a class="job-title-href" href="{href}">{title}</a></h2>
  <p class="company-name"> {company} </p>
  <div class="detail-row-1">
    <p class="row-1-item locations"><span><a>{location}</a></span></p>
    <div class="row-1-item">{pay_html}</div>
  </div>
  <div class="about_job"><div class="text"> Build things with the team </div></div>
  <div class="job_skills">{skill_html}</div>
  <div class="color-labels"><div class="status-success"><span>{posted}</span></div></div>
</div>"""


PAGE = (
    "<html><body>"
    + card(
        "job",
        "/job/detail/python-developer-job-in-bangalore-at-acme123?utm_source=x",
        "Python Developer",
        "Acme Labs",
        "Bangalore",
        "₹ 3,00,000 - 4,50,000",
        "3 weeks ago",
        skills=("Python", "Django"),
    )
    + card(
        "job",
        "/job/detail/remote-qa-job-at-beta456",
        "QA Engineer",
        "Beta",
        "Work from home",
        "₹ 2,00,000 - 3,00,000",
        "Today",
    )
    # Padding Internshala adds to a short jobs page.
    + card(
        "internship",
        "/internship/detail/sql-internship-at-gamma789",
        "SQL Developer",
        "Gamma",
        "Work from home",
        "Unpaid",
        "1 day ago",
        pay_class="stipend",
    )
    # A promo card without a detail page.
    + card("job", "https://partner.example/offer", "Course offer", "Promo", "", "", "")
    + "</body></html>"
)


def test_parses_job_cards_and_drops_padding_and_promos():
    jobs = internshala.parse_listing(PAGE, now=NOW)
    assert [j["title"] for j in jobs] == ["Python Developer", "QA Engineer"]
    first, second = jobs
    assert first == {
        "title": "Python Developer",
        "company": "Acme Labs",
        "location": "Bangalore",
        "remote": "",
        "salary_text": "₹ 3,00,000 - 4,50,000",
        "job_url": "https://internshala.com/job/detail/python-developer-job-in-bangalore-at-acme123",
        "posted_at": "2026-11-03",
        "description": "Build things with the team Skills: Python, Django",
    }
    assert second["remote"] == "remote"
    assert second["posted_at"] == "2026-11-24"


def test_internship_mode_keeps_only_internships():
    jobs = internshala.parse_listing(PAGE, internships=True, now=NOW)
    assert [j["title"] for j in jobs] == ["SQL Developer (Internship)"]
    assert jobs[0]["salary_text"] == "Unpaid"


@pytest.mark.parametrize(
    ("location", "where"),
    [
        ("Bengaluru, Karnataka", "bangalore"),
        ("Gurugram", "gurgaon"),
        ("Pune, Maharashtra, India", "pune"),
        # Job search's default when no location is given: not India.
        ("Remote", None),
        ("Remote, India", "remote"),
        ("Work from home", "remote"),
        ("WFH", "remote"),
        ("India", ""),
        ("", ""),
        ("Navi Mumbai", "navi-mumbai"),
        ("Berlin", None),
        ("San Jose, CA", None),
        ("London, UK", None),
    ],
)
def test_place(location, where):
    assert internshala.place(location) == where


def test_listing_urls():
    assert internshala.listing_url("Python Developer", "pune") == (
        "https://internshala.com/jobs/keywords-python-developer/jobs-in-pune/"
    )
    assert internshala.listing_url("data analyst", "remote") == (
        "https://internshala.com/jobs/keywords-data-analyst/work-from-home/"
    )
    assert internshala.listing_url("ML", "", internships=True) == (
        "https://internshala.com/internships/keywords-ml/"
    )
    assert internshala.listing_url("C++ / Qt", "delhi", internships=True) == (
        "https://internshala.com/internships/keywords-c-qt/internship-in-delhi/"
    )


def test_listing_urls_never_carry_a_query_string():
    # robots.txt disallows "/*?*"; keyword pages are path-only.
    url = internshala.listing_url("python?x=1&y=2", "pune")
    assert "?" not in url and "&" not in url


@pytest.mark.parametrize(
    ("final", "ok"),
    [
        ("https://internshala.com/jobs/keywords-python/jobs-in-pune/", True),
        # Its own alias redirect keeps a city filter.
        ("https://internshala.com/jobs/keywords-python/jobs-in-gurgaon/", True),
        # Unknown city: generic keyword page, no city.
        ("https://internshala.com/jobs/keywords-python/", False),
        # Unknown keyword: generic city page.
        ("https://internshala.com/jobs/jobs-in-pune/", False),
        ("https://internshala.com/jobs/", False),
    ],
)
def test_redirects_to_generic_pages_are_rejected(final, ok):
    requested = "https://internshala.com/jobs/keywords-python/jobs-in-pune/"
    assert internshala._same_search(requested, final, "pune") is ok


def test_remote_redirect_must_keep_work_from_home():
    requested = "https://internshala.com/jobs/keywords-python/work-from-home/"
    assert internshala._same_search(requested, requested, "remote")
    assert not internshala._same_search(
        requested, "https://internshala.com/jobs/keywords-python/", "remote"
    )


@pytest.mark.parametrize(
    ("label", "date"),
    [
        ("Just now", "2026-11-24"),
        ("Few hours ago", "2026-11-24"),
        ("1 day ago", "2026-11-23"),
        ("2 weeks ago", "2026-11-10"),
        ("a month ago", "2026-10-25"),
        ("Be an early applicant", None),
        ("", None),
    ],
)
def test_posted_at(label, date):
    assert internshala.posted_at(label, NOW) == date


def test_outside_india_makes_no_request():
    with patch.object(internshala, "fetch") as fetch:
        assert internshala.search("python developer", "Berlin, Germany", 20) == []
    fetch.assert_not_called()


def test_generic_redirect_returns_nothing():
    with patch.object(
        internshala, "fetch", return_value=("https://internshala.com/jobs/jobs-in-pune/", PAGE)
    ):
        assert internshala.search("zzqx", "Pune", 20) == []


def test_intern_queries_search_internships_without_the_word_intern():
    url = "https://internshala.com/internships/keywords-machine-learning/internship-in-pune/"
    with patch.object(internshala, "fetch", return_value=(url, PAGE)) as fetch:
        jobs = internshala.search("Machine Learning Intern", "Pune", 20)
    assert fetch.call_args.args[0] == url
    assert [j["title"] for j in jobs] == ["SQL Developer (Internship)"]


def test_results_are_capped():
    url = "https://internshala.com/jobs/keywords-python/jobs-in-bangalore/"
    with patch.object(internshala, "fetch", return_value=(url, PAGE)):
        assert len(internshala.search("python", "Bangalore", 1)) == 1


def test_registered_as_default_live_source():
    from app.services import job_search_service as svc

    assert "internshala" in svc.DEFAULT_PLATFORMS
    raw = internshala.parse_listing(PAGE, now=NOW)[0]
    job = svc._normalize(raw, "internshala")
    assert job["url"].startswith("https://internshala.com/job/detail/")
    assert job["salary_text"] == "₹ 3,00,000 - 4,50,000"
    assert job["platform"] == "internshala"


def test_search_without_a_location_makes_no_request():
    # job_search_service passes "Remote" when the member gave no location.
    with patch.object(internshala, "fetch") as fetch:
        assert internshala.search("python developer", "Remote", 20) == []
    fetch.assert_not_called()
