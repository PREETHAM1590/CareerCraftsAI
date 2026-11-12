"""India-first classification, filtering, ranking and salary parsing (no network)."""

import pytest

from app.services import india_focus as india


@pytest.mark.parametrize(
    "location,fit",
    [
        ("Bangalore", "india"),
        ("Bengaluru, Karnataka, India", "india"),
        ("Bengaluru, IN", "india"),
        ("TS, IN", "india"),
        ("KA, IN", "india"),
        ("Atlanta, GA", "foreign"),
        ("Gurgaon, Haryana", "india"),
        ("Hyderabad", "india"),
        ("Mumbai / Bombay", "india"),
        ("Chennai (Madras)", "india"),
        ("Delhi NCR", "india"),
        ("Noida, Uttar Pradesh", "india"),
        ("India - Bangalore", "india"),
        ("IN", "india"),
        ("Pan India", "india"),
        ("London, UK; Bengaluru, India", "india"),
        ("Chennai, Tamil Nadu, India; Austin, TX", "india"),
        ("Remote - India", "india_remote"),
        ("Remote (IN)", "india_remote"),
        ("India (Remote)", "india_remote"),
        ("Virtual India", "india_remote"),
        ("Remote (US or India)", "india_remote"),
        ("Remote, Europe or India", "india_remote"),
        ("APAC", "india_remote"),
        ("Remote - Asia Pacific", "india_remote"),
        ("Remote", "remote_global"),
        ("Worldwide", "remote_global"),
        ("Anywhere", "remote_global"),
        ("Remote - Worldwide, US preferred", "remote_global"),
        ("Remote, US only", "foreign"),
        ("Remote - US", "foreign"),
        ("Remote (Anywhere in the US)", "foreign"),
        ("Remote - EMEA", "foreign"),
        ("Remote, Americas", "foreign"),
        ("Singapore / APAC", "foreign"),
        ("San Francisco, CA", "foreign"),
        ("Indianapolis, IN 46229", "foreign"),
        ("Indiana, United States of America (Virtual)", "foreign"),
        ("Hyderabad, Sindh, Pakistan", "foreign"),
        ("Lahore, Punjab, Pakistan", "foreign"),
        ("Berlin", "foreign"),
        ("Washington, DC", "foreign"),
        ("Toronto, ON, Canada", "foreign"),
        ("Dubai, UAE", "foreign"),
        ("", "unknown"),
    ],
)
def test_location_text_classification(location, fit):
    assert india.classify({"location": location})[0] == fit, location


def test_structured_countries_win_over_text():
    # SmartRecruiters "Deogarh, IN" names no known city; the country code decides.
    assert india.classify({"location": "Deogarh", "countries": ["IN"]})[0] == "india"
    assert india.classify({"location": "Bengaluru", "countries": ["US"]})[0] == "foreign"
    remote_in = {"location": "Remote", "remote": True, "countries": ["IN"]}
    assert india.classify(remote_in)[0] == "india_remote"
    us_remote = {"location": "Remote", "remote": True, "countries": ["US"]}
    assert india.classify(us_remote)[0] == "foreign"
    worldwide = {"location": "Remote - Worldwide", "remote": True, "countries": ["US"]}
    assert india.classify(worldwide)[0] == "remote_global"
    assert india.classify({"location": "x", "countries": ["India"]})[0] == "india"


def test_remote_posting_body_residency_closes_it():
    job = {"location": "Remote", "description": "You must be based in the US to apply."}
    assert india.classify(job)[0] == "foreign"
    job = {"location": "Remote", "description": "Must be authorized to work in the United States"}
    assert india.classify(job)[0] == "foreign"
    assert india.classify({"location": "Remote", "description": "Python, AWS"})[0] == (
        "remote_global"
    )


@pytest.mark.parametrize(
    "query,expected",
    [
        ({}, True),
        ({"locations": ["Remote"]}, True),
        ({"locations": ["Bangalore", "Remote"]}, True),
        ({"locations": ["India"]}, True),
        ({"location": "Gurgaon"}, True),
        ({"locations": ["Berlin"]}, False),
        ({"locations": ["Austin"]}, False),
        ({"locations": ["Remote US"]}, False),
        ({"locations": ["Bangalore", "London"]}, False),
        ({"locations": ["Berlin"], "india_focus": True}, True),
        ({"locations": ["Bangalore"], "india_focus": False}, False),
        ({"india_focus": "off"}, False),
    ],
)
def test_india_focus_default_and_override(query, expected):
    assert india.wants_india(query) is expected


def test_split_hides_foreign_only_roles_with_reason():
    jobs = [
        {"title": "a", "location": "Bengaluru"},
        {"title": "b", "location": "Remote, US only"},
        {"title": "c", "location": "Remote"},
    ]
    kept, hidden = india.split(jobs, {"locations": ["Remote"]})
    assert [j["title"] for j in kept] == ["a", "c"]
    assert hidden[0]["india_reason"].startswith("Remote only for")
    kept, hidden = india.split(jobs, {"locations": ["Remote"], "india_focus": False})
    assert len(kept) == 3 and not hidden


def test_rank_bonus_orders_india_then_remote_then_foreign():
    def bonus(**job):
        return india.rank_bonus(job)[0]

    in_india = bonus(location="Pune", company_country="IN")
    assert in_india > bonus(location="Pune") > bonus(location="Remote - India")
    assert bonus(location="Remote - India") > bonus(location="Remote") > bonus(location="Berlin")
    assert india.rank_bonus({"location": "Berlin"}, focus=False) == (0, [])
    assert "Indian company" in india.rank_bonus({"location": "", "company_country": "IN"})[1]


def test_city_aliases_and_ncr():
    assert india.canonical_city("Bangalore Urban") == "bengaluru"
    assert india.canonical_city("Gurgaon") == "gurugram"
    assert india.canonical_city("Calcutta") == "kolkata"
    assert india.search_cities("Delhi NCR") == set(india.NCR)
    assert india.search_cities("Pune") == {"pune"}
    assert india.search_cities("Berlin") == set()


def test_eligible_gate_for_auto_apply():
    assert india.eligible("Bengaluru")
    assert india.eligible("Remote")
    assert not india.eligible("Remote, US only")
    assert not india.eligible("New York, NY")


def test_eligible_listings_for_auto_apply_candidates():
    from types import SimpleNamespace as Row

    rows = [
        Row(location="Pune", description=""),
        Row(location="Remote", description="Must be based in the US"),
        Row(location="Austin, TX", jd_text=""),
        Row(location="Remote - India", description=None, jd_text="Python"),
    ]
    kept, hidden = india.eligible_listings(rows, "Remote")
    assert [r.location for r in kept] == ["Pune", "Remote - India"] and hidden == 2
    assert india.eligible_listings(rows, "Austin")[1] == 0
    assert india.eligible_listings(rows, "Remote", india_focus=False)[1] == 0


@pytest.mark.parametrize(
    "text,low,high",
    [
        ("₹12-18 LPA", 1_200_000, 1_800_000),
        ("INR 6,00,000 - 12,00,000", 600_000, 1_200_000),
        ("CTC 25L", 2_500_000, 2_500_000),
        ("1.2 Cr per annum", 12_000_000, 12_000_000),
        ("₹50k/month", 600_000, 600_000),
        ("Rs. 40,000 - 60,000 per month", 480_000, 720_000),
        ("8 to 12 Lakhs", 800_000, 1_200_000),
    ],
)
def test_inr_salary(text, low, high):
    parsed = india.parse_inr_salary(text)
    assert parsed == {"currency": "INR", "min": low, "max": high, "period": "year"}


def test_non_inr_salary_is_not_guessed():
    assert india.parse_inr_salary("USD 120000–150000") is None
    assert india.parse_inr_salary("") is None
