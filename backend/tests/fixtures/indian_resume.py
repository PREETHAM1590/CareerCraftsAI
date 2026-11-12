# ruff: noqa: E501
"""A realistic multi-page Indian candidate used by resume-pipeline tests."""

from __future__ import annotations

from app.core.pii_vault import _VERHOEFF_D, _VERHOEFF_INV, _VERHOEFF_P


def verhoeff_check_digit(number: str) -> str:
    """Check digit that makes ``number`` a valid Aadhaar-style Verhoeff string."""
    checksum = 0
    for i, ch in enumerate(reversed(number)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[(i + 1) % 8][int(ch)]]
    return str(_VERHOEFF_INV[checksum])


NAME = "Aditi Venkataraman Śrīnivāsan"
EMAIL = "aditi.srinivasan@example.in"
PHONE = "+91 98765 43210"
LONG_URL = (
    "https://www.linkedin.com/in/aditi-venkataraman-srinivasan-"
    "senior-backend-engineer-bengaluru-" + "x" * 90
)

PROJECTS = "\n\n".join(
    f"### Project {i} | Open source | 2022\n"
    f"- Built service {i} handling {1000 + i} requests per second on AWS with PostgreSQL."
    for i in range(1, 13)
)

SOURCE_MARKDOWN = f"""# {NAME}
Senior Backend Engineer
{EMAIL} | {PHONE} | Bengaluru, Karnataka | {LONG_URL}

## Summary
Backend engineer with 6 years of experience building payment and logistics platforms in Python and Go.

## Experience
### Senior Software Engineer | Razorpay Software Pvt Ltd | Bengaluru, Karnataka | Jan 2021 - Present
- Led migration of 14 microservices to Kubernetes, cutting deployment time by 40%.
- Designed UPI reconciliation pipeline processing 2.5 million transactions daily using Kafka and PostgreSQL.
- Mentored 4 engineers; current CTC 28 LPA, notice period 60 days.

### Software Engineer | Flipkart Internet Pvt Ltd | Bengaluru, Karnataka | Jul 2018 - Dec 2020
- Built inventory APIs in Python and FastAPI serving 1200 requests per second.
- Reduced p99 latency from 800 ms to 350 ms by adding Redis caching.

## Projects
{PROJECTS}

## Education
### B.Tech in Computer Science | NIT Trichy | Tiruchirappalli, Tamil Nadu | 2014 - 2018
- CGPA: 8.7/10

### Class XII CBSE | Delhi Public School | Chennai, Tamil Nadu | 2013 - 2014
- 94.2%

## Skills
Python, Go, FastAPI, Django, PostgreSQL, Redis, Kafka, Kubernetes, AWS, Docker

## Certifications
- AWS Certified Solutions Architect Associate, 2022

## Languages
हिन्दी, English, தமிழ்
"""

JD = """Senior Backend Engineer
We need Python, FastAPI, Kubernetes, PostgreSQL, Kafka, Terraform and GraphQL experience.
Remote within India. Experience with microservices and AWS required.
"""
