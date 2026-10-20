"""Task 3 slice 3a: schema extraction, answer resolution, validation."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.applications import answer_resolver, profile_service
from app.applications.models import ApplicationField
from app.applications.question_normalizer import normalize_question
from app.applications.schema_extractor import extract_fields
from app.applications.validator import validate_fields

# --- question_normalizer ---


@pytest.mark.parametrize(
    "label",
    [
        "Will you require visa sponsorship?",
        "Do you need employer sponsorship?",
        "Will your employment require sponsorship?",
    ],
)
def test_sponsorship_variants_map_to_same_key(label):
    assert normalize_question(label) == "authorization.requires_sponsorship"


def test_unmapped_label_returns_none():
    assert normalize_question("Why do you want to work here?") is None


# --- schema_extractor ---


def test_radio_group_collapses_to_one_field_with_selected_value():
    raw = [
        {
            "name": "sponsor",
            "id": "",
            "type": "radio",
            "label": "Yes",
            "value": "yes",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
        {
            "name": "sponsor",
            "id": "",
            "type": "radio",
            "label": "No",
            "value": "no",
            "checked": True,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
    ]
    fields = extract_fields(raw)
    assert len(fields) == 1
    field = fields[0]
    assert field.input_type == "radio"
    assert field.options == ["Yes", "No"]
    assert field.value == "No"
    assert field.required is True


def test_unselected_radio_group_has_none_value():
    raw = [
        {
            "name": "sponsor",
            "id": "",
            "type": "radio",
            "label": "Yes",
            "value": "yes",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
        {
            "name": "sponsor",
            "id": "",
            "type": "radio",
            "label": "No",
            "value": "no",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
    ]
    fields = extract_fields(raw)
    assert fields[0].value is None


def test_single_required_checkbox_unchecked_value_is_false_not_truthy():
    """Regression: the bug this task fixes — an unchecked checkbox's raw
    HTML `value` attribute is non-empty, but the semantic value must be
    False so validation correctly flags it as missing."""
    raw = [
        {
            "name": "consent",
            "id": "consent",
            "type": "checkbox",
            "label": "I agree",
            "value": "on",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        }
    ]
    fields = extract_fields(raw)
    assert fields[0].input_type == "checkbox"
    assert fields[0].value is False


def test_select_field_carries_options():
    raw = [
        {
            "name": "country",
            "id": "country",
            "type": "select",
            "label": "Country",
            "value": "",
            "checked": False,
            "required": True,
            "options": ["Select...", "USA", "India"],
            "visible": True,
            "disabled": False,
        }
    ]
    fields = extract_fields(raw)
    assert fields[0].input_type == "select"
    assert fields[0].options == ["Select...", "USA", "India"]
    assert fields[0].value is None  # empty string normalized to None


def test_text_email_textarea_number_date_file_map_correctly():
    raw = [
        {
            "name": "email",
            "id": "email",
            "type": "email",
            "label": "Email",
            "value": "a@b.com",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
        {
            "name": "why",
            "id": "why",
            "type": "textarea",
            "label": "Why us?",
            "value": "",
            "checked": False,
            "required": False,
            "options": [],
            "visible": True,
            "disabled": False,
        },
        {
            "name": "years",
            "id": "years",
            "type": "number",
            "label": "Years",
            "value": "5",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
        {
            "name": "start",
            "id": "start",
            "type": "date",
            "label": "Start date",
            "value": "",
            "checked": False,
            "required": False,
            "options": [],
            "visible": True,
            "disabled": False,
        },
        {
            "name": "resume",
            "id": "resume",
            "type": "file",
            "label": "Resume",
            "value": "",
            "checked": False,
            "required": True,
            "options": [],
            "visible": True,
            "disabled": False,
        },
    ]
    fields = {f.field_id: f for f in extract_fields(raw)}
    assert fields["email"].input_type == "text"
    assert fields["why"].input_type == "textarea"
    assert fields["years"].input_type == "number"
    assert fields["start"].input_type == "date"
    assert fields["resume"].input_type == "file"


# --- validator ---


def test_required_unchecked_checkbox_is_flagged():
    field = ApplicationField(
        field_id="c1", label="I agree", input_type="checkbox", required=True, value=False
    )
    issues = validate_fields([field])
    assert len(issues) == 1
    assert issues[0].field_id == "c1"


def test_required_unselected_radio_is_flagged():
    field = ApplicationField(
        field_id="r1",
        label="Sponsorship",
        input_type="radio",
        required=True,
        options=["Yes", "No"],
        value=None,
    )
    issues = validate_fields([field])
    assert len(issues) == 1


def test_required_select_placeholder_is_flagged():
    field = ApplicationField(
        field_id="s1",
        label="Country",
        input_type="select",
        required=True,
        options=["Select...", "USA"],
        value=None,
    )
    issues = validate_fields([field])
    assert len(issues) == 1


def test_checked_checkbox_and_selected_radio_pass():
    fields = [
        ApplicationField(
            field_id="c1", label="I agree", input_type="checkbox", required=True, value=True
        ),
        ApplicationField(
            field_id="r1",
            label="Sponsorship",
            input_type="radio",
            required=True,
            options=["Yes", "No"],
            value="No",
        ),
    ]
    assert validate_fields(fields) == []


def test_invalid_email_format_is_flagged():
    field = ApplicationField(
        field_id="email",
        label="Email",
        input_type="text",
        normalized_key="personal.email",
        required=True,
        value="not-an-email",
    )
    issues = validate_fields([field])
    assert any("email" in i.message.lower() for i in issues)


def test_invisible_required_field_is_skipped():
    field = ApplicationField(
        field_id="hidden1",
        label="Hidden",
        input_type="text",
        required=True,
        value=None,
        visible=False,
    )
    assert validate_fields([field]) == []


def test_disabled_required_field_is_skipped():
    field = ApplicationField(
        field_id="d1", label="Disabled", input_type="text", required=True, value=None, disabled=True
    )
    assert validate_fields([field]) == []


# --- answer_resolver ---


class _FakeAnswer:
    def __init__(self, value):
        self.answer = {"value": value}


class _FakeDB:
    def __init__(self, saved=None, profile=None):
        self._saved = saved
        self._profile = profile

    async def execute(self, statement, *a, **k):
        compiled = str(statement).lower()

        class _R:
            def __init__(self, v):
                self._v = v

            def scalar_one_or_none(self):
                return self._v

        if "candidate_answers" in compiled:
            return _R(self._saved)
        return _R(self._profile)


@pytest.mark.asyncio
async def test_saved_answer_wins_over_profile_and_resume():
    field = ApplicationField(
        field_id="f1",
        label="Will you require visa sponsorship?",
        input_type="radio",
        required=True,
        options=["Yes", "No"],
    )
    db = _FakeDB(saved=_FakeAnswer("No"))
    resume_resolver = AsyncMock(return_value=("Yes", 0.8, ["resume"]))
    result = await answer_resolver.resolve_field(
        db, uuid.uuid4(), field, resume_resolver=resume_resolver
    )
    assert result.source == "user"
    assert result.value == "No"
    assert result.confidence == 1.0
    resume_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_sensitive_field_never_uses_resume_or_generated_fallback():
    field = ApplicationField(
        field_id="f1",
        label="Will you require visa sponsorship?",
        input_type="radio",
        required=True,
        options=["Yes", "No"],
    )
    db = _FakeDB(saved=None, profile=None)
    resume_resolver = AsyncMock(return_value=("Yes", 0.8, ["resume"]))
    narrative = AsyncMock(return_value="Yes I will")
    result = await answer_resolver.resolve_field(
        db,
        uuid.uuid4(),
        field,
        resume_resolver=resume_resolver,
        narrative_generator=narrative,
    )
    assert result.source == "unresolved"
    assert result.requires_review is True
    resume_resolver.assert_not_awaited()
    narrative.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmapped_generative_question_uses_narrative_generator_with_review_flag():
    field = ApplicationField(
        field_id="f1", label="Why do you want to work here?", input_type="textarea", required=False
    )
    db = _FakeDB(saved=None, profile=None)
    narrative = AsyncMock(return_value="Because I love the mission.")
    result = await answer_resolver.resolve_field(
        db, uuid.uuid4(), field, narrative_generator=narrative
    )
    assert result.source == "generated"
    assert result.requires_review is True
    assert result.value == "Because I love the mission."


@pytest.mark.asyncio
async def test_nothing_available_returns_unresolved():
    field = ApplicationField(
        field_id="f1", label="What's your favorite color?", input_type="text", required=False
    )
    db = _FakeDB(saved=None, profile=None)
    result = await answer_resolver.resolve_field(db, uuid.uuid4(), field)
    assert result.source == "unresolved"
    assert result.missing_reason


# --- profile_service ---


def test_structured_profile_value_composes_full_name():
    from app.models.db import CandidateProfile

    profile = CandidateProfile(user_id=uuid.uuid4(), first_name="Ada", last_name="Lovelace")
    assert profile_service.structured_profile_value(profile, "personal.full_name") == "Ada Lovelace"


def test_structured_profile_value_returns_none_for_unmapped_key():
    from app.models.db import CandidateProfile

    profile = CandidateProfile(user_id=uuid.uuid4())
    assert profile_service.structured_profile_value(profile, "not.a.real.key") is None


@pytest.mark.asyncio
async def test_save_approved_answer_updates_existing_row_in_place():
    from app.models.db import CandidateAnswer

    existing = CandidateAnswer(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        question_key="authorization.requires_sponsorship",
        answer={"value": "Yes"},
        approved_by_user=True,
    )

    class _Db:
        async def execute(self, *a, **k):
            class _R:
                def scalar_one_or_none(self):
                    return existing

            return _R()

        def add(self, obj):
            raise AssertionError("must update existing row, not insert a new one")

    row = await profile_service.save_approved_answer(
        _Db(),
        existing.user_id,
        "authorization.requires_sponsorship",
        "Will you require sponsorship?",
        "No",
    )
    assert row is existing
    assert row.answer == {"value": "No"}
    assert row.approved_by_user is True


# --- candidate_profile API ---


@pytest.mark.asyncio
async def test_candidate_profile_api_upsert_then_get(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.core.database import get_db
    from app.api.v1.deps import get_current_user
    from app.main import app
    from app.models.db import User

    payload = {"sub": "00000000-0000-0000-0000-000000000001", "email": "a@b.com"}
    monkeypatch.setattr("app.core.clerk_auth.verify_token", lambda token: payload)

    user_id = uuid.uuid4()
    store: dict = {}

    class _FakeDB:
        async def execute(self, *a, **k):
            class _R:
                def scalar_one_or_none(self_inner):
                    return store.get("profile")

            return _R()

        def add(self, obj):
            store["profile"] = obj

        async def flush(self):
            # A real AsyncSession.flush() runs the INSERT and populates
            # Python-side column defaults; this fake must do the same.
            profile = store.get("profile")
            if profile is not None and profile.version is None:
                profile.version = 1

    async def _fake_db():
        return _FakeDB()

    async def _fake_user():
        return User(id=user_id, email="a@b.com")

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user] = _fake_user
    headers = {"Authorization": "Bearer test-token"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/api/v1/candidate-profile/",
                json={
                    "first_name": "Ada",
                    "work_authorization": "US Citizen",
                    "requires_sponsorship": False,
                },
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["first_name"] == "Ada"
            assert resp.json()["version"] == 1

            resp2 = await client.get("/api/v1/candidate-profile/", headers=headers)
            assert resp2.status_code == 200
            assert resp2.json()["work_authorization"] == "US Citizen"
    finally:
        app.dependency_overrides.clear()
