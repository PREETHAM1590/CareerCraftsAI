from unittest.mock import AsyncMock

import pytest

from app.services import decision_engine, extension_service


@pytest.mark.parametrize(
    "key",
    [
        "personal.first_name",
        "email",
        "phone",
        "disclosure.gender",
        "authorization.sponsorship",
        "authorization.work_authorization",
    ],
)
def test_identity_and_sensitive_fields_use_local_matching(key):
    assert extension_service._local_match_only(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected", [("Female", "Female (woman)"), ("unrelated", None)])
async def test_local_only_match_never_calls_external_service(monkeypatch, value, expected):
    remote = AsyncMock(side_effect=AssertionError("sensitive answer left server"))
    monkeypatch.setattr(decision_engine, "decide", remote)
    option, _ = await decision_engine.match_option(
        value, ["Male (man)", "Female (woman)"], "Gender", local_only=True
    )
    assert option == expected
    remote.assert_not_awaited()
