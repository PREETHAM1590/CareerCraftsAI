"""Identity fields on application forms are filled from stored data only.

The member's name, contact details, address, IDs, links, pay and notice are
never drafted by a model: the normalizer gives them a canonical key, the
resolver answers them from the profile or saved answers (or leaves them for
the member), and a recording stub model must never be called for them. For a
genuine free-text question that is drafted, the prompt carries no contact
detail, and after the gateway none of the member's values at all.
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from app.applications import answer_resolver, profile_service
from app.applications.models import ApplicationField
from app.applications.question_normalizer import (
    asks_for_identity,
    is_identity_key,
    normalize_question,
)
from app.services import extension_service
from tests.security.test_pii_gateway import (  # noqa: F401 — fixtures
    AADHAAR,
    EMAIL,
    LINKEDIN,
    NAME,
    PAN,
    PHONE,
    _leaks,
    _post,
    gateway_app,
    provider,
)

FIRST, LAST = "personal.first_name", "personal.last_name"
FULL, EMAIL_KEY, PHONE_KEY = "personal.full_name", "personal.email", "personal.phone"
CODE, STREET, CITY = "personal.phone_country_code", "address.street", "location.city"
STATE, PIN, COUNTRY = "location.state", "location.postal_code", "location.country"
CURRENT_CTC, EXPECTED_CTC = "compensation.current_salary", "compensation.expected_salary"
NOTICE = "experience.notice_period_days"

# (label as a form or ATS presents it, expected canonical key)
LABELS = [
    ("First Name", FIRST),
    ("First name*", FIRST),
    ("legalNameSection_firstName", FIRST),
    ("job_application[first_name]", FIRST),
    ("Given name(s)", FIRST),
    ("Middle Name", "personal.middle_name"),
    ("legalNameSection_middleName", "personal.middle_name"),
    ("Last Name", LAST),
    ("Surname", LAST),
    ("Family name", LAST),
    ("legalNameSection_lastName", LAST),
    ("job_application[last_name]", LAST),
    ("Full Name", FULL),
    ("Name", FULL),
    ("Name*", FULL),
    ("_systemfield_name", FULL),
    ("Legal name", FULL),
    ("What is your full name?", FULL),
    ("Candidate Name", FULL),
    ("Preferred Name", "personal.preferred_name"),
    ("preferredName", "personal.preferred_name"),
    ("Email", EMAIL_KEY),
    ("Email Address*", EMAIL_KEY),
    ("E-mail ID", EMAIL_KEY),
    ("job_application[email]", EMAIL_KEY),
    ("_systemfield_email", EMAIL_KEY),
    ("emailAddress", EMAIL_KEY),
    ("What is your email address?", EMAIL_KEY),
    ("Phone", PHONE_KEY),
    ("Phone Number", PHONE_KEY),
    ("phone-number", PHONE_KEY),
    ("_systemfield_phone", PHONE_KEY),
    ("job_application[phone]", PHONE_KEY),
    ("Mobile", PHONE_KEY),
    ("Mobile Number*", PHONE_KEY),
    ("Mobile No.", PHONE_KEY),
    ("Alternate mobile number", PHONE_KEY),
    ("WhatsApp number", PHONE_KEY),
    ("Contact Number", PHONE_KEY),
    ("Telephone", PHONE_KEY),
    ("Cell phone", PHONE_KEY),
    ("Country Phone Code", CODE),
    ("countryPhoneCode", CODE),
    ("Phone country code", CODE),
    ("ISD Code", CODE),
    ("Country code", CODE),
    ("Phone Device Type", "personal.phone_type"),
    ("Phone Extension", "personal.phone_extension"),
    ("Address Line 1", STREET),
    ("addressSection_addressLine1", STREET),
    ("Address", STREET),
    ("Street Address", STREET),
    ("Residential Address", STREET),
    ("Permanent address (as per Aadhaar)", STREET),
    ("Current Address", STREET),
    ("Locality", STREET),
    ("City", CITY),
    ("addressSection_city", CITY),
    ("Current City", CITY),
    ("State", STATE),
    ("State/Province", STATE),
    ("addressSection_region", STATE),
    ("Province", STATE),
    ("PIN Code", PIN),
    ("Pincode", PIN),
    ("PIN", PIN),
    ("Postal Code", PIN),
    ("ZIP", PIN),
    ("Zip/Postal Code", PIN),
    ("addressSection_postalCode", PIN),
    ("Country", COUNTRY),
    ("addressSection_countryRegion", COUNTRY),
    ("Country/Region", COUNTRY),
    ("Country of residence", COUNTRY),
    ("Location", "location.current"),
    ("Current Location", "location.current"),
    ("_systemfield_location", "location.current"),
    ("Where are you currently based?", "location.current"),
    ("Date of Birth", "personal.date_of_birth"),
    ("Date of Birth (DD/MM/YYYY)", "personal.date_of_birth"),
    ("DOB", "personal.date_of_birth"),
    ("dateOfBirth", "personal.date_of_birth"),
    ("Aadhaar Number", "identity.aadhaar"),
    ("Aadhar Card No", "identity.aadhaar"),
    ("PAN", "identity.pan"),
    ("PAN Number", "identity.pan"),
    ("PAN Card No.", "identity.pan"),
    ("Permanent Account Number", "identity.pan"),
    ("Passport Number", "identity.passport"),
    ("Passport No", "identity.passport"),
    ("Voter ID", "identity.other_id"),
    ("Driving Licence Number", "identity.other_id"),
    ("Social Security Number", "identity.other_id"),
    ("Nationality", "personal.nationality"),
    ("Citizenship", "personal.nationality"),
    ("Father's Name", "personal.guardian_name"),
    ("Gender", "disclosure.gender"),
    ("Pronouns", "disclosure.pronouns"),
    ("Marital Status", "disclosure.marital_status"),
    ("Religion", "disclosure.religion"),
    ("Caste", "disclosure.caste_category"),
    ("Race/Ethnicity", "disclosure.race_ethnicity"),
    ("Veteran Status", "disclosure.veteran_status"),
    ("LinkedIn", "links.linkedin_url"),
    ("LinkedIn Profile URL", "links.linkedin_url"),
    ("urls[LinkedIn]", "links.linkedin_url"),
    ("GitHub", "links.github_url"),
    ("GitHub URL (optional)", "links.github_url"),
    ("urls[GitHub]", "links.github_url"),
    ("Portfolio", "links.portfolio_url"),
    ("Website", "links.portfolio_url"),
    ("Personal website", "links.portfolio_url"),
    ("urls[Portfolio]", "links.portfolio_url"),
    ("Current CTC", CURRENT_CTC),
    ("Current CTC (in LPA)", CURRENT_CTC),
    ("CTC", CURRENT_CTC),
    ("Current Salary", CURRENT_CTC),
    ("Last drawn salary", CURRENT_CTC),
    ("Expected CTC", EXPECTED_CTC),
    ("ECTC", EXPECTED_CTC),
    ("Expected Salary (LPA)", EXPECTED_CTC),
    ("Salary expectations", EXPECTED_CTC),
    ("Notice Period", NOTICE),
    ("Notice period (in days)", NOTICE),
    ("Are you currently serving notice?", NOTICE),
]

# No canonical key, but plainly asking for a personal detail.
UNKEYED_IDENTITY = [
    "Do you have a valid passport?",
    "Before we continue, could you please tell us your current residential address "
    "and the PIN code where you live?",
    "For background verification we need your date of birth exactly as on your documents",
    "Please paste a link to your LinkedIn profile so the hiring team can review it",
]

# Genuine free-text questions: no key, drafting allowed.
FREE_TEXT = [
    "Why do you want to work at Acme?",
    "Describe a challenging project you led and what you learned from it.",
    "Tell us about a time you wrote an email to a difficult customer.",
    "What would you address first in your first 90 days in this role?",
    "How do you handle state management in React applications?",
    "What is your experience with GitHub Actions?",
    "Describe your experience building mobile applications",
    "Tell us about a website you built",
]


@pytest.mark.parametrize("label,key", LABELS)
def test_identity_labels_get_canonical_keys(label, key):
    assert normalize_question(label) == key
    assert asks_for_identity(label)


@pytest.mark.parametrize("label", UNKEYED_IDENTITY)
def test_unkeyed_identity_questions_are_still_recognised(label):
    assert asks_for_identity(label)


@pytest.mark.parametrize("label", FREE_TEXT)
def test_free_text_questions_stay_draftable(label):
    assert normalize_question(label) is None
    assert not asks_for_identity(label)


def _no_db(monkeypatch, *, profile=None, saved=None):
    monkeypatch.setattr(profile_service, "get_saved_answer", AsyncMock(return_value=saved))
    monkeypatch.setattr(profile_service, "get_profile", AsyncMock(return_value=profile))


def _field(label: str, input_type: str = "textarea") -> ApplicationField:
    return ApplicationField(field_id="f", label=label, input_type=input_type, required=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [label for label, _ in LABELS] + UNKEYED_IDENTITY)
async def test_identity_fields_never_reach_a_model(monkeypatch, label):
    _no_db(monkeypatch)
    drafted = AsyncMock(return_value="model text")
    answer = await answer_resolver.resolve_field(
        None, uuid.uuid4(), _field(label), narrative_generator=drafted
    )
    drafted.assert_not_called()
    assert answer.source == "unresolved" and answer.value is None


def _profile(**kw):
    base = dict.fromkeys(
        ("first_name", "last_name", "email", "phone", "city", "state", "country", "postal_code")
    )
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,value",
    [
        ("legalNameSection_firstName", "Preetham"),
        ("Full Name", NAME),
        ("Preferred Name", "Preetham"),
        ("Email Address*", EMAIL),
        ("Mobile Number*", PHONE),
        ("State/Province", "Karnataka"),
        ("PIN Code", "560038"),
        ("Country", "India"),
        ("Current Location", "Bengaluru, Karnataka, India"),
    ],
)
async def test_identity_fields_are_filled_from_the_profile(monkeypatch, label, value):
    _no_db(
        monkeypatch,
        profile=_profile(
            first_name="Preetham",
            last_name="Venkatesh",
            email=EMAIL,
            phone=PHONE,
            city="Bengaluru",
            state="Karnataka",
            country="India",
            postal_code="560038",
        ),
    )
    drafted = AsyncMock()
    answer = await answer_resolver.resolve_field(
        None, uuid.uuid4(), _field(label, "text"), narrative_generator=drafted
    )
    drafted.assert_not_called()
    assert (answer.value, answer.source) == (value, "profile")


@pytest.mark.asyncio
async def test_saved_identity_answer_is_used_verbatim(monkeypatch):
    saved = SimpleNamespace(answer={"value": "1998-04-21"})
    _no_db(monkeypatch, saved=saved)
    answer = await answer_resolver.resolve_field(
        None, uuid.uuid4(), _field("Date of Birth", "date"), narrative_generator=AsyncMock()
    )
    assert (answer.value, answer.source) == ("1998-04-21", "user")


def test_identity_keys_are_classified():
    assert is_identity_key("identity.pan") and is_identity_key("location.postal_code")
    assert not is_identity_key("custom.why acme") and not is_identity_key(None)


# ── a genuine free-text question that is drafted ─────────────────────────────

RESUME_TEXT = (
    f"{NAME.upper()}\n"
    f"Email: {EMAIL} | Phone: {PHONE} | {LINKEDIN}\n"
    f"Aadhaar {AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]} · PAN {PAN}\n"
    "Bengaluru, India\n\n"
    "EXPERIENCE\nSenior Python engineer, Infosys (2019-2025): FastAPI, PostgreSQL."
)
CONTACT_VALUES = [EMAIL, "98765 43210", "9876543210", LINKEDIN, AADHAAR[:4] + " ", PAN]


class RecordingModel:
    def __init__(self):
        self.prompts: list[list] = []

    def invoke(self, messages, **_):
        self.prompts.append(messages)
        return AIMessage(content="Because Acme builds what I love.")


@pytest.mark.asyncio
async def test_drafting_prompt_has_no_contact_details_and_gateway_hides_the_rest(
    monkeypatch, gateway_app, provider  # noqa: F811 — imported fixtures
):
    model = RecordingModel()
    document = SimpleNamespace(raw_text=RESUME_TEXT)
    result = MagicMock()
    result.scalar_one_or_none.return_value = document
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    @asynccontextmanager
    async def session():
        yield db

    import app.core.database as database
    import app.core.model_router as model_router

    monkeypatch.setattr(database, "AsyncSessionLocal", session)
    monkeypatch.setattr(model_router, "get_llm", AsyncMock(return_value=model))
    task = SimpleNamespace(
        user_id=uuid.uuid4(),
        run_id=None,
        payload={"resume_document_id": str(uuid.uuid4()), "role": "Engineer", "company": "Acme"},
    )
    generate = extension_service._narrative_generator(task.user_id, task, [3])
    answer = await generate(_field("Why do you want to work at Acme?"))
    assert answer == "Because Acme builds what I love."

    sent = "\n".join(str(m.content) for m in model.prompts[0])
    squashed = re.sub(r"[\s()+-]", "", sent)
    assert [v for v in CONTACT_VALUES if v in sent] == []
    assert "9876543210" not in squashed
    assert "Infosys" in sent and "FastAPI" in sent  # the facts the answer needs remain

    # The same prompt through the gateway: the name is hidden there too.
    roles = {"system": "system", "human": "user"}
    response = await _post(
        gateway_app,
        {"messages": [{"role": roles[m.type], "content": m.content} for m in model.prompts[0]]},
    )
    assert response.status_code == 200
    assert _leaks(provider.outbound_text()) == []
