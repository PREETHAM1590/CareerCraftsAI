"""Rules for moving an application from an inbox reply."""

import pytest

from app.models.db import JobApplication
from app.services.application_status_service import (
    match_application,
    next_status,
    normalize_company,
)


def _app(company, status="applied"):
    return JobApplication(company=company, role="Engineer", status=status)


@pytest.mark.parametrize(
    "current,category,expected",
    [
        ("applied", "VIEWED", "viewed"),
        ("applied", "INTERVIEW", "interview"),
        ("viewed", "SHORTLISTED", None),  # same level, not forward
        ("interview", "VIEWED", None),  # never backwards
        ("interview", "REJECTED", "rejected"),
        ("applied", "REJECTED", "rejected"),
        ("offer", "REJECTED", None),  # an offer is never overwritten
        ("rejected", "INTERVIEW", None),  # nor a rejection
        ("saved", "INTERVIEW", None),  # never applied to
        ("applied", "RECRUITER_MESSAGE", None),  # recorded, no status change
    ],
)
def test_status_only_moves_forward(current, category, expected):
    assert next_status(current, category) == expected


def test_company_names_match_across_suffixes_and_case():
    assert normalize_company("Acme Technologies Pvt. Ltd.") == "acme"
    assert normalize_company("ACME, Inc.") == "acme"


def test_matches_the_single_application_the_email_is_about():
    acme, other = _app("Acme Technologies Pvt Ltd"), _app("Globex")
    update = {"company": "UNKNOWN", "sender": "Acme Careers <jobs@acme.com>", "subject": "Hi"}
    assert match_application([acme, other], update) is acme


def test_two_open_applications_at_one_company_are_ambiguous():
    update = {"company": "Acme", "sender": "", "subject": "Interview invitation"}
    assert match_application([_app("Acme"), _app("Acme Inc")], update) is None


def test_a_short_name_never_matches_inside_a_longer_word():
    update = {"company": "UNKNOWN", "sender": "Facebook Jobs", "subject": "Application update"}
    assert match_application([_app("Ace")], update) is None


@pytest.mark.parametrize(
    "sender,trusted",
    [
        ("Acme Careers <jobs@acme.com>", True),
        ("jobs@careers.acme.co.uk", True),
        ("no-reply@acme-careers.com", True),
        ("Acme <no-reply@us.greenhouse-mail.io>", True),
        ("notifications@myworkdayjobs.com", True),
        ("Acme HR <hr.acme@gmail.com>", False),  # display text and local part prove nothing
        ("offers@acme.attacker.com", False),  # the company only as a subdomain
        ("hr@acme-offers.xyz", False),
        ("hr@acme.com.evil.io", False),
        ("Acme Careers", False),
        ("", False),
    ],
)
def test_only_company_or_platform_senders_are_trusted(sender, trusted):
    from app.services.application_status_service import sender_is_trusted

    assert sender_is_trusted(_app("Acme Technologies Pvt Ltd"), sender) is trusted


def test_attacker_offer_email_does_not_change_status(monkeypatch):
    import asyncio
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.services import application_status_service as svc

    application = _app("Acme")
    application.id = uuid.uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [application]
    result.scalar.return_value = uuid.uuid4()  # event inserted: not seen before
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: db)
    notify = AsyncMock()
    monkeypatch.setattr("app.workflows.starters.start_notification", notify)

    def update(sender, message_id):
        return {
            "message_id": message_id,
            "category": "INTERVIEW",
            "company": "Acme",
            "sender": sender,
            "subject": "Acme: we are pleased to offer you an interview",
        }

    changes = asyncio.run(svc.apply_inbox_updates(str(uuid.uuid4()), [update("x@evil.io", "m1")]))
    assert changes == [] and application.status == "applied"
    assert notify.await_count == 1
    assert SimpleNamespace(**notify.await_args.kwargs).link == "/applications"

    changes = asyncio.run(
        svc.apply_inbox_updates(str(uuid.uuid4()), [update("Acme <jobs@acme.com>", "m2")])
    )
    assert [c["to_status"] for c in changes] == ["interview"]
    assert application.status == "interview" and notify.await_count == 1
