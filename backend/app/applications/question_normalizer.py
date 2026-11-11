"""Deterministic label -> canonical question_key mapping.

Rule-based, not LLM-based: the same wording variants of a question ("Will
you require visa sponsorship?", "Do you need employer sponsorship?") must
always resolve to the same key so a saved answer is reused, and so this
stays testable and auditable. Order matters — first match wins.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"visa|sponsorship", re.I), "authorization.requires_sponsorship"),
    (
        re.compile(
            r"authori[sz](e|ed|ation)\s+to\s+work|legally\s+(authori[sz]ed|entitled)"
            r"|(right|eligible|eligibility)\s+to\s+work",
            re.I,
        ),
        "authorization.work_authorized",
    ),
    # Voluntary disclosures: only ever answered from an answer the member saved.
    (re.compile(r"veteran", re.I), "disclosure.veteran_status"),
    (re.compile(r"disabilit|disabled", re.I), "disclosure.disability_status"),
    (re.compile(r"\brace\b|ethnic|hispanic|latin[oax]", re.I), "disclosure.race_ethnicity"),
    (re.compile(r"gender|\bsex\b", re.I), "disclosure.gender"),
    (
        re.compile(r"willing\s+to\s+relocate|open\s+to\s+relocat", re.I),
        "location.willing_to_relocate",
    ),
    (
        re.compile(
            r"(expected|desired|target)\s+(salary|compensation|ctc|pay)"
            r"|(salary|pay|compensation)\s+expectations?|\bectc\b",
            re.I,
        ),
        "compensation.expected_salary",
    ),
    (re.compile(r"current\s+(salary|compensation|ctc|pay)\b", re.I), "compensation.current_salary"),
    (re.compile(r"notice\s+period", re.I), "experience.notice_period_days"),
    (
        re.compile(
            r"when\s+can\s+you\s+(start|join)|start\s+date|available\s+to\s+(start|join)", re.I
        ),
        "availability.start_date",
    ),
    (re.compile(r"years?\s+of\s+experience", re.I), "experience.years_experience"),
    (re.compile(r"first\s*name", re.I), "personal.first_name"),
    (re.compile(r"last\s*name|surname", re.I), "personal.last_name"),
    (re.compile(r"full\s*name|^name$", re.I), "personal.full_name"),
    (re.compile(r"e-?mail", re.I), "personal.email"),
    (re.compile(r"phone|mobile", re.I), "personal.phone"),
    (re.compile(r"linkedin", re.I), "links.linkedin_url"),
    (re.compile(r"github", re.I), "links.github_url"),
    (re.compile(r"portfolio|website|personal\s+site", re.I), "links.portfolio_url"),
    (re.compile(r"current\s+(company|employer)", re.I), "experience.current_company"),
    (re.compile(r"current\s+title|current\s+role", re.I), "experience.current_title"),
    (re.compile(r"\bcity\b", re.I), "location.city"),
    (re.compile(r"remote|hybrid|on-?site", re.I), "work.remote_preference"),
]


def normalize_question(label: str) -> str | None:
    """Return the canonical question_key for a label, or None if the label
    does not match a known factual question (e.g. a free-form "why this
    role?" question, which the answer resolver treats as generative)."""
    for pattern, key in _RULES:
        if pattern.search(label):
            return key
    return None
