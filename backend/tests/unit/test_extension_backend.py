"""Browser-extension backend: decision engine, fill plans, device tokens."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.config import settings
from app.services import decision_engine, extension_service

# ── Decision engine ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "configured,key,url,expected",
    [
        ("auto", "tk", "", "jev"),
        ("auto", "", "http://laya:8088", "laya"),
        ("auto", "tk", "http://laya:8088", "jev"),
        ("auto", "", "", "none"),
        ("laya", "tk", "http://laya:8088", "laya"),
    ],
)
def test_provider_selection(monkeypatch, configured, key, url, expected):
    monkeypatch.setattr(settings, "DECISION_ENGINE_PROVIDER", configured)
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", key)
    monkeypatch.setattr(settings, "LAYA_URL", url)
    assert decision_engine.provider() == expected


@pytest.mark.asyncio
async def test_jev_request_shape_and_answers(monkeypatch):
    monkeypatch.setattr(settings, "DECISION_ENGINE_PROVIDER", "jev")
    monkeypatch.setattr(settings, "TYPESAFE_API_KEY", "tk_test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, json={"answers": {"ok": {"type": "noul", "noul": 0.97}}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        decision_engine.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    result = await decision_engine.decide(
        "Thank you for applying", {"ok": {"type": "noul", "instructions": "Confirmed?"}}
    )

    assert result == {"provider": "jev", "answers": {"ok": {"type": "noul", "noul": 0.97}}}
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer tk_test"
    assert b'"model":"jev-latest"' in seen["body"].replace(b" ", b"")


@pytest.mark.asyncio
async def test_provider_outage_falls_back_to_heuristics(monkeypatch):
    monkeypatch.setattr(settings, "DECISION_ENGINE_PROVIDER", "laya")
    monkeypatch.setattr(settings, "LAYA_URL", "http://laya.invalid:8088")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        decision_engine.httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw
        ),
    )
    result = await decision_engine.decide("x", {"ok": {"type": "noul", "instructions": "?"}})
    assert result["provider"] == "heuristic"
    assert result["answers"]["ok"] == {"type": "noul", "noul": 0.5}  # "unknown", never acted on


def test_invalid_questions_are_rejected():
    with pytest.raises(ValueError):
        decision_engine.validate_questions(
            {"q": {"type": "choice", "instructions": "?", "criteria": {"a": "x"}}}
        )
    with pytest.raises(ValueError):
        decision_engine.validate_questions({"q": {"type": "generate", "instructions": "?"}})


@pytest.mark.asyncio
async def test_match_option_exact_and_threshold(monkeypatch):
    monkeypatch.setattr(settings, "DECISION_ENGINE_PROVIDER", "none")
    assert await decision_engine.match_option("No", ["Yes", "No"]) == ("No", 1.0)
    # Heuristic overlap for a clear match clears the threshold…
    option, _ = await decision_engine.match_option(
        "LinkedIn", ["Select…", "LinkedIn Jobs", "Referral"]
    )
    assert option == "LinkedIn Jobs"
    option, _ = await decision_engine.match_option("No", ["Yes, I will", "No, I will not"])
    assert option == "No, I will not"
    # …an unrelated value does not, so the user is asked instead.
    option, confidence = await decision_engine.match_option("Bengaluru", ["Yes", "No"])
    assert option is None and confidence < settings.DECISION_ENGINE_MIN_CONFIDENCE


# ── Fill plan rules ─────────────────────────────────────────────────────


def _raw(field_id, label, type_="text", **extra):
    return {
        "id": field_id,
        "name": extra.pop("name", field_id),
        "type": type_,
        "label": label,
        "group_label": extra.pop("group_label", ""),
        "value": "",
        "checked": False,
        "required": extra.pop("required", False),
        "options": extra.pop("options", []),
        "visible": True,
        "disabled": False,
        **extra,
    }


class _PlanDB:
    def __init__(self, user, saved_custom_keys=()):
        self.user = user
        self.saved_custom_keys = list(saved_custom_keys)

    async def get(self, model, key):
        return self.user

    async def execute(self, statement):
        result = MagicMock()
        result.scalars.return_value.all.return_value = self.saved_custom_keys
        return result


@pytest.mark.asyncio
async def test_plan_never_guesses_sensitive_or_consent_fields(monkeypatch):
    from app.applications.models import ResolvedAnswer

    async def resolve_fields(db, user_id, fields, **kwargs):
        # Pretend every field had a saved "Yes": the planner must still
        # refuse to tick consent on the user's behalf.
        return [
            (
                ResolvedAnswer(field_id=f.field_id, value=None, source="unresolved", confidence=0.0)
                if f.label.startswith("Will you")
                else ResolvedAnswer(field_id=f.field_id, value=True, source="user", confidence=1.0)
            )
            for f in fields
        ]

    monkeypatch.setattr(extension_service, "resolve_fields", resolve_fields)
    user = SimpleNamespace(
        email="priya@example.com", phone=None, full_name="Priya R", linkedin_url=None
    )
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={})
    raw = [
        _raw("consent", "I agree to the privacy policy", "checkbox", required=True),
        _raw(
            "yes",
            "Yes",
            "radio",
            name="sponsor",
            group_label="Will you require visa sponsorship?",
            required=True,
        ),
        _raw("no", "No", "radio", name="sponsor", group_label="Will you require visa sponsorship?"),
        _raw("resume", "Resume/CV", "file", required=True),
        _raw("cover", "Cover letter", "file"),
    ]
    plan = await extension_service.plan_fields(_PlanDB(user), task, raw)
    by_id = {f["field_id"]: f for f in plan["fields"]}

    assert by_id["consent"]["value"] is None and by_id["consent"]["source"] == "unresolved"
    assert by_id["sponsor"]["value"] is None
    assert by_id["resume"]["value"] == extension_service.RESUME_TOKEN
    assert by_id["cover"]["value"] is None
    assert set(plan["unresolved_required"]) == {"consent", "sponsor"}


@pytest.mark.asyncio
async def test_plan_falls_back_to_the_account_for_personal_details(monkeypatch):
    from app.applications.models import ResolvedAnswer

    async def resolve_fields(db, user_id, fields, **kwargs):
        return [
            ResolvedAnswer(field_id=f.field_id, value=None, source="unresolved", confidence=0.0)
            for f in fields
        ]

    monkeypatch.setattr(extension_service, "resolve_fields", resolve_fields)
    user = SimpleNamespace(
        email="priya@example.com",
        phone="+91 98765 43210",
        full_name="Priya Raghunathan",
        linkedin_url=None,
    )
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={})
    raw = [
        _raw("fn", "First Name"),
        _raw("ln", "Last Name"),
        _raw("em", "Email"),
        _raw("ph", "Phone"),
    ]
    plan = await extension_service.plan_fields(_PlanDB(user), task, raw)
    values = {f["field_id"]: f["value"] for f in plan["fields"]}

    assert values == {
        "fn": "Priya",
        "ln": "Raghunathan",
        "em": "priya@example.com",
        "ph": "+91 98765 43210",
    }


@pytest.mark.asyncio
async def test_saved_custom_answers_are_reused_but_new_questions_get_drafts(monkeypatch):
    captured = {}

    async def resolve_fields(db, user_id, fields, **kwargs):
        from app.applications.models import ResolvedAnswer

        captured.update({f.label: f.normalized_key for f in fields})
        return [
            ResolvedAnswer(field_id=f.field_id, value=None, source="unresolved", confidence=0.0)
            for f in fields
        ]

    monkeypatch.setattr(extension_service, "resolve_fields", resolve_fields)
    saved_key = extension_service.custom_question_key("Why do you want to work at Acme?")
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={})
    raw = [
        _raw("why", "Why do you want to work at Acme?", "textarea"),
        _raw("hobby", "What do you do for fun?", "textarea"),
    ]
    await extension_service.plan_fields(_PlanDB(None, [saved_key]), task, raw)

    assert captured == {
        "Why do you want to work at Acme?": saved_key,
        "What do you do for fun?": None,
    }


def test_answer_keys_prefer_canonical_rules():
    assert (
        extension_service.answer_key("Do you need visa sponsorship?")
        == "authorization.requires_sponsorship"
    )
    assert extension_service.answer_key("Why us?  (optional)") == "custom.why us optional"


# ── Device tokens ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pairing_stores_only_a_hash_and_authenticates_the_raw_token():
    added = []
    db = MagicMock()
    db.add = MagicMock(side_effect=added.append)
    db.commit = AsyncMock()

    device, token = await extension_service.pair_device(db, uuid.uuid4(), "  Work laptop  ")

    assert token.startswith("ccx_") and len(token) > 40
    assert device.token_hash == extension_service.hash_token(token) != token
    assert device.name == "Work laptop"

    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = device
    owner_lookup = MagicMock()
    owner_lookup.scalar_one_or_none.return_value = SimpleNamespace(
        policy_accepted_at=object(), deletion_scheduled_for=None
    )
    db.execute = AsyncMock(side_effect=[lookup, owner_lookup])
    db.flush = AsyncMock()
    assert await extension_service.authenticate(db, token) is device
    assert device.last_seen_at is not None
    # A Clerk JWT or anything else is never treated as a device token.
    assert await extension_service.authenticate(db, "eyJhbGciOiJSUzI1NiJ9.x.y") is None


@pytest.mark.asyncio
async def test_extension_can_revoke_its_own_token():
    from app.api.v1.extension import device_revoke_self

    device = SimpleNamespace(id=uuid.uuid4(), revoked_at=None)
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()

    assert await device_revoke_self(device=device, db=db) == {"status": "revoked"}
    assert device.revoked_at is not None
    # One UPDATE returns the device's claimed tasks to pending.
    sql = str(db.execute.await_args.args[0])
    assert "UPDATE extension_tasks" in sql and "device_id" in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_locks_device_row_first_and_flags_redelivery():
    device = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    task = SimpleNamespace(status="claimed", device_id=device.id, claimed_at=None)
    res = MagicMock()
    res.scalar_one_or_none.return_value = task
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(), res])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    assert await extension_service.claim_next_task(db, device) is task
    assert "FOR UPDATE" in str(db.execute.await_args_list[0].args[0])
    assert task.redelivered is True
