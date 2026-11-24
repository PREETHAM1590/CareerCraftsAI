"""Adversarial checks: tailored text that invents or moves facts must be flagged."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import indian_resume as candidate  # noqa: E402

from app.services.resume_grounding import (  # noqa: E402
    date_issues,
    omitted_entries,
    unsupported_claims,
)

SRC = candidate.SOURCE_MARKDOWN
JD = candidate.JD


def _flagged(markdown: str) -> list[str]:
    return unsupported_claims(markdown, SRC, JD)


def test_the_candidates_own_resume_is_clean():
    assert _flagged(SRC) == []
    assert date_issues(SRC) == []


def test_legitimate_reordering_and_rewording_is_clean():
    reworded = SRC.replace(
        "Led migration of 14 microservices to Kubernetes, cutting deployment time by 40%.",
        "Cut deployment time by 40% by migrating 14 services to Kubernetes.",
    )
    assert _flagged(reworded) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("Flipkart Internet Pvt Ltd", "Zomato Media Pvt Ltd", "Zomato"),
        (
            "Senior Software Engineer | Razorpay",
            "Principal Software Engineer | Razorpay",
            "Principal",
        ),
        ("Jul 2018 - Dec 2020", "Jul 2017 - Dec 2020", "2017"),
        ("Jan 2021", "Mar 2021", "Mar 2021 at Razorpay"),  # month the source never gives
        ("14 microservices", "40 microservices", "40"),  # 40 exists, but only as 40%
        ("by 40%", "by 35%", "35%"),
        ("6 years", "9 years", "9 years"),
        ("8.7/10", "9.4/10", "9.4"),
        ("B.Tech in Computer Science", "M.Tech in Computer Science", "M.Tech"),
        ("Go, FastAPI", "Java, FastAPI", "Java"),  # Java is not a fragment of JavaScript
        ("AWS, Docker", "AWS, Docker, Terraform, GraphQL", "Terraform"),
        ("AWS, Docker", "AWS, Docker, terraform", "terraform"),  # lowercase JD requirement
        ("using Kafka and PostgreSQL", "using kafka and terraform", "terraform"),
        ("Associate, 2022", "Professional, 2022", "AWS Certified Solutions Architect Professional"),
    ],
)
def test_fabrications_are_flagged(old, new, expected):
    assert old in SRC
    flagged = _flagged(SRC.replace(old, new))
    assert any(expected in item for item in flagged), flagged


def test_dates_moved_between_employers_are_flagged():
    swapped = SRC.replace("Jan 2021 - Present", "TMP").replace(
        "Jul 2018 - Dec 2020", "Jan 2021 - Present"
    )
    swapped = swapped.replace("TMP", "Jul 2018 - Dec 2020")
    flagged = _flagged(swapped)
    assert any("Razorpay" in item for item in flagged), flagged
    assert any("Flipkart" in item for item in flagged), flagged


def test_substring_of_a_source_word_is_not_support():
    source = "Built tools in JavaScript."
    assert unsupported_claims("## Skills\nJava, JavaScript", source) == ["Java"]


def test_non_ascii_names_are_checked_too():
    source = "Worked at Škoda Auto."
    assert unsupported_claims("- Worked at Škoda Auto.", source) == []
    assert unsupported_claims("- Worked at Łukasiewicz Institute.", source)


def test_end_before_start_and_newest_first_are_reported():
    reversed_dates = SRC.replace("Jan 2021 - Present", "Dec 2022 - Jan 2021")
    assert any("end before they start" in p for p in date_issues(reversed_dates))
    flipped = SRC.replace("Jan 2021 - Present", "Jan 2015 - Present")
    assert any("most recent role first" in p for p in date_issues(flipped))


def test_a_dropped_job_is_reported_not_silent():
    cut = (
        SRC.split("### Software Engineer | Flipkart")[0]
        + "## Education"
        + SRC.split("## Education")[1]
    )
    assert omitted_entries(cut, SRC) == ["Flipkart Internet Pvt Ltd"]
    assert omitted_entries(SRC, SRC) == []
    facts = {"experience": [{"role": "Intern", "employer": "Swiggy"}]}
    assert omitted_entries(SRC, SRC, facts) == ["Swiggy"]
