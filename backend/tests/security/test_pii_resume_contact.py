"""The resume agent's verified-contact block never reaches the provider.

The resume agent puts json.dumps(verified_contact) into its grounding source
and the contact line into the draft it asks the model to repair. Both go
through the gateway, which must send placeholders and put the real values
back into the reply, so the finished resume header still carries them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.pii_vault import KnownPii
from app.services import pii_profile
from app.services.pii_profile import PrivacyPolicy
from app.services.resume_facts import verified_contact
from app.services.resume_grounding import source_text
from tests.security.test_pii_gateway import (  # noqa: F401 — fixtures
    EMAIL,
    LINKEDIN,
    NAME,
    PHONE,
    _post,
    gateway_app,
    provider,
)

GITHUB = "github.com/preetham-v"
PORTFOLIO = "preetham.dev"


def _member():
    profile = SimpleNamespace(
        first_name="Preetham",
        last_name="Venkatesh",
        email=EMAIL,
        phone=PHONE,
        city="Bengaluru",
        state="Karnataka",
        country="India",
        postal_code="560038",
        linkedin_url="https://www.linkedin.com/in/preetham-venkatesh/",
        github_url="https://" + GITHUB,
        portfolio_url="https://" + PORTFOLIO,
    )
    user = SimpleNamespace(email=EMAIL, phone=None, linkedin_url=None, full_name=NAME)
    return profile, user


@pytest.fixture
def member_known(monkeypatch, gateway_app):  # noqa: F811 — imported fixture
    # After gateway_app, which installs its own known values.
    profile, user = _member()
    known: KnownPii = pii_profile.build_known_pii(user, profile, [])
    monkeypatch.setattr(
        pii_profile,
        "load_privacy_context",
        AsyncMock(return_value=(PrivacyPolicy(True, True, True), known)),
    )
    return profile, user


@pytest.mark.asyncio
async def test_grounding_payload_contact_block_is_placeholdered_and_restored(
    member_known, gateway_app, provider  # noqa: F811 — imported fixtures
):
    profile, user = member_known
    contact = verified_contact(profile, user, {})
    chunks = [
        f"{NAME}\n{EMAIL} | {PHONE} | {LINKEDIN}\nSenior Python engineer, Infosys (2019-2025)."
    ]
    source = source_text(chunks, {}, NAME, json.dumps(contact))
    header = " | ".join(
        v
        for v in (
            contact["email"],
            contact["phone"],
            contact["location"],
            contact["linkedin"],
            contact["github"],
            contact["portfolio"],
        )
        if v
    )
    draft = f"# {NAME}\n{header}\n\n## EXPERIENCE\nSenior Python engineer, Infosys"
    prompt = (
        "Your resume draft contains details that do not appear in the candidate's source "
        f"documents.\nUNSUPPORTED: Kubernetes\n\nDRAFT_RESUME_MARKDOWN:\n{draft}\n\n"
        f"CANDIDATE_SOURCE:\n{source[:12000]}"
    )
    response = await _post(
        gateway_app,
        {
            "messages": [
                {"role": "system", "content": "Return the resume as JSON."},
                {"role": "user", "content": prompt},
            ]
        },
    )
    assert response.status_code == 200
    sent = provider.outbound_text()
    for value in (
        EMAIL,
        "98765 43210",
        "preetham-venkatesh",
        GITHUB,
        PORTFOLIO,
        NAME,
        "Venkatesh",
        "560038",
    ):
        assert value.lower() not in sent.lower(), value
    assert "Bengaluru" in sent  # location is kept on purpose (job matching)
    # The stub echoes every placeholder, as a model copying the header would.
    reply = response.json()["choices"][0]["message"]["content"]
    for value in (EMAIL, PHONE, NAME):
        assert value in reply
    assert "[[" not in reply
