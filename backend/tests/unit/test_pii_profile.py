"""Which stored values the gateway treats as the member's personal details."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import pii_profile


def _user(**kw):
    base = {"email": "login@example.org", "phone": None, "linkedin_url": None, "full_name": None}
    return SimpleNamespace(**{**base, **kw})


def _profile(**kw):
    base = dict.fromkeys(
        (
            "first_name",
            "last_name",
            "email",
            "phone",
            "postal_code",
            "linkedin_url",
            "github_url",
            "portfolio_url",
        )
    )
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(
    "key,kind",
    [
        ("personal.email", "EMAIL"),
        ("custom.alternate email address", "EMAIL"),
        ("personal.phone", "PHONE"),
        ("custom.whatsapp number", "PHONE"),
        ("custom.date of birth", "DOB"),
        ("custom.aadhaar number", "AADHAAR"),
        ("custom.pan card number", "PAN"),
        ("custom.passport number", "PASSPORT"),
        ("custom.upi id", "UPI"),
        ("custom.pin code", "POSTAL_CODE"),
        ("custom.address line 1", "ADDRESS"),
        ("links.github_url", "URL"),
        ("personal.first_name", "FIRST_NAME"),
        ("custom.why do you want to join us", None),
        ("compensation.expected_salary", None),
    ],
)
def test_question_keys_map_to_types(key, kind):
    assert pii_profile.key_type(key) == kind


def test_known_values_come_from_every_member_source():
    answers = [
        (
            "resume.facts",
            {"contact": {"email": "resume@example.org", "github": "github.com/pv"}},
        ),
        ("custom.date of birth", {"value": "14/02/1996"}),
        ("custom.do you have a passport", {"value": "Yes"}),
        ("custom.address line 1", {"value": "12, MG Road"}),
        ("custom.why us", {"value": "Because I like it"}),
    ]
    known = pii_profile.build_known_pii(
        _user(full_name="Preetham Venkatesh", phone="+91 98765 43210"),
        _profile(first_name="Preetham", last_name="V", postal_code="560038"),
        answers,
    ).values
    assert known["EMAIL"] == {"login@example.org", "resume@example.org"}
    assert known["PHONE"] == {"+91 98765 43210"}
    assert known["NAME"] == {"Preetham Venkatesh", "Preetham V"}
    assert known["FIRST_NAME"] == {"Preetham"}
    assert known["LAST_NAME"] == {"Venkatesh"}  # "V" is too short to match safely
    assert known["DOB"] == {"14/02/1996"}
    assert known["ADDRESS"] == {"12, MG Road"}
    assert known["POSTAL_CODE"] == {"560038"}
    assert known["URL"] == {"github.com/pv"}
    assert "PASSPORT" not in known  # "Yes" is not a passport number


def test_names_can_be_left_out():
    known = pii_profile.build_known_pii(
        _user(full_name="Preetham Venkatesh"),
        _profile(first_name="Preetham"),
        [("personal.full_name", {"value": "Preetham Venkatesh"})],
        include_names=False,
    ).values
    assert not {"NAME", "FIRST_NAME", "LAST_NAME"} & set(known)
    assert known["EMAIL"] == {"login@example.org"}


def test_member_preference_overrides_server_defaults(monkeypatch):
    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", True)
    monkeypatch.setattr(settings, "PII_REDACT_NAMES", True)
    on = pii_profile.policy_for(None)
    assert on.enabled and on.redact_names and on.detect_patterns
    off = pii_profile.policy_for(SimpleNamespace(llm_hide_personal_info=False))
    assert not off.enabled and not off.redact_names and not off.detect_patterns
    names_off = pii_profile.policy_for(SimpleNamespace(llm_hide_name=False))
    assert names_off.enabled and not names_off.redact_names
    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", False)
    assert not pii_profile.policy_for(SimpleNamespace(llm_hide_personal_info=True)).enabled


@pytest.mark.asyncio
async def test_sessions_without_a_member_uuid_get_pattern_only_redaction():
    policy, known = await pii_profile.load_privacy_context("user_test_1")
    assert policy.enabled and not known
