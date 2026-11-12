"""Members' "hide from the AI model" switches and the server override."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import users
from app.core.config import settings
from app.models.db import UserPreferences
from app.models.schemas import UserPreferencesResponse, UserPreferencesSchema
from app.services import pii_profile

MIGRATIONS = Path(__file__).resolve().parents[3] / "supabase" / "migrations"


def test_columns_default_on():
    columns = UserPreferences.__table__.columns
    for name in ("llm_hide_personal_info", "llm_hide_name"):
        assert columns[name].default.arg is True
        assert columns[name].server_default.arg == "true"
        assert not columns[name].nullable


def test_migration_adds_both_columns_idempotently():
    migration = next(MIGRATIONS.glob("*_llm_privacy_preferences.sql")).read_text()
    for name in ("llm_hide_personal_info", "llm_hide_name"):
        assert f"ADD COLUMN IF NOT EXISTS {name} boolean NOT NULL DEFAULT true" in migration


@pytest.mark.asyncio
async def test_patch_changes_only_the_privacy_switch_sent():
    prefs = UserPreferences(
        user_id=uuid.uuid4(), llm_hide_personal_info=True, llm_hide_name=True, notify_email=True
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = prefs
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    payload = UserPreferencesSchema.model_validate({"llm_hide_name": False})
    out = await users.upsert_preferences(
        payload, db=db, current_user=SimpleNamespace(id=prefs.user_id)
    )
    assert out.llm_hide_name is False
    assert out.llm_hide_personal_info is True and out.notify_email is True


def test_response_exposes_the_switches():
    prefs = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        llm_hide_personal_info=False,
        llm_hide_name=True,
        target_roles=[],
        preferred_locations=[],
        prefer_live_browser=False,
    )
    body = UserPreferencesResponse.model_validate(prefs).model_dump()
    assert body["llm_hide_personal_info"] is False and body["llm_hide_name"] is True


def test_member_opt_out_is_honoured_unless_the_server_forces_redaction(monkeypatch):
    opted_out = SimpleNamespace(llm_hide_personal_info=False, llm_hide_name=False)
    monkeypatch.setattr(settings, "PII_REDACTION_FORCED", False)
    assert not pii_profile.policy_for(opted_out).enabled
    monkeypatch.setattr(settings, "PII_REDACTION_FORCED", True)
    forced = pii_profile.policy_for(opted_out)
    assert forced.enabled and forced.detect_patterns
    assert not forced.redact_names  # the name switch stays the member's
    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", False)
    assert not pii_profile.policy_for(opted_out).enabled  # master switch still wins


@pytest.mark.parametrize(
    "key,kind",
    [
        ("personal.date_of_birth", "DOB"),
        ("location.postal_code", "POSTAL_CODE"),
        ("address.street", "ADDRESS"),
        ("identity.aadhaar", "AADHAAR"),
        ("identity.pan", "PAN"),
        ("identity.passport", "PASSPORT"),
        ("personal.middle_name", "NAME"),
        ("personal.phone_country_code", None),
    ],
)
def test_saved_answers_under_new_identity_keys_are_hidden(key, kind):
    assert pii_profile.key_type(key) == kind


@pytest.mark.asyncio
async def test_india_focus_preference_roundtrips_without_changing_other_preferences():
    prefs = UserPreferences(user_id=uuid.uuid4(), india_focus=True, notify_email=True)
    result = MagicMock()
    result.scalar_one_or_none.return_value = prefs
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    user = SimpleNamespace(id=prefs.user_id)
    out = await users.upsert_preferences(
        UserPreferencesSchema(india_focus=False), db=db, current_user=user
    )
    assert out.india_focus is False and out.notify_email is True
    read = await users.get_preferences(db=db, current_user=user)
    assert read.india_focus is False


def test_india_focus_defaults_and_migration():
    column = UserPreferences.__table__.columns.india_focus
    assert column.default.arg is True and not column.nullable
    migration = next(MIGRATIONS.glob("*_india_focus_preference.sql")).read_text()
    assert "ADD COLUMN IF NOT EXISTS india_focus boolean NOT NULL DEFAULT true" in migration
