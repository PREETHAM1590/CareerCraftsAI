"""Personal details never leave the LLM gateway; replies come back whole.

A stub provider records exactly what the gateway would send to the model.
Agents are driven through a real ChatOpenAI client wired to the gateway app
in-process, so the check covers OpenAI-wire serialisation, the gateway's
redaction and its rehydration of the reply.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.core import llm_gateway as gateway
from app.core import model_router
from app.core.config import settings
from app.core.pii_vault import KnownPii, verhoeff_check_digit
from app.services import pii_profile
from app.services.pii_profile import PrivacyPolicy

USER_ID = str(uuid.uuid4())
NAME = "Preetham Venkatesh"
EMAIL = "preetham.v@gmail.com"
PHONE = "+91 98765 43210"
LINKEDIN = "linkedin.com/in/preetham-venkatesh"
AADHAAR = "23456789012" + verhoeff_check_digit("23456789012")
PAN = "ABCPV1234K"
RECRUITER_EMAIL = "ananya.rao@acme.in"
RECRUITER_PHONE = "+91 99887 76655"

RESUME = (
    f"{NAME.upper()}\n{EMAIL} | {PHONE} | {LINKEDIN}\n"
    f"Aadhaar {AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]} · PAN {PAN}\n"
    "Senior Python engineer, 6 years, FastAPI and PostgreSQL at Infosys (2019-2025)."
)
JD = (
    "Senior Python Engineer, FastAPI, Bengaluru (hybrid). Questions? "
    f"Write to {RECRUITER_EMAIL} or call {RECRUITER_PHONE}. Apply via careers@acme.in."
)
# Strings that must never appear in anything sent to a provider.
SECRETS = [
    NAME,
    "Preetham",
    "Venkatesh",
    EMAIL,
    "98765 43210",
    "9876543210",
    "preetham-venkatesh",
    AADHAAR,
    AADHAAR[:4] + " " + AADHAAR[4:8],
    PAN,
    RECRUITER_EMAIL,
    "99887 76655",
]


def _known() -> KnownPii:
    known = KnownPii()
    for kind, value in (
        ("NAME", NAME),
        ("FIRST_NAME", "Preetham"),
        ("LAST_NAME", "Venkatesh"),
        ("EMAIL", EMAIL),
        ("PHONE", PHONE),
        ("URL", LINKEDIN),
        ("AADHAAR", AADHAAR),
        ("PAN", PAN),
    ):
        known.add(kind, value)
    return known


def _leaks(text: str) -> list[str]:
    squashed = re.sub(r"[\s()+-]", "", text)
    found = [s for s in SECRETS if s.lower() in text.lower()]
    found += [d for d in ("919876543210", "9876543210") if d in squashed]
    return found


class RecordingProvider:
    """Stands in for the model: records the messages it receives and echoes
    back every placeholder it saw, as a model writing a draft would."""

    def __init__(self, tool_call: bool = False):
        self.sent: list[list] = []
        self.tool_call = tool_call
        self.callbacks = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.sent.append(messages)
        tokens = sorted(set(re.findall(r"\[\[[A-Z_]+_\d+\]\]", str(messages[-1].content))))
        if self.tool_call:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "fill", "args": {"values": tokens, "note": "x"}, "id": "call_1"}
                ],
            )
        return AIMessage(content="Draft: " + " ".join(tokens))

    def outbound_text(self) -> str:
        return "\n".join(str(m.content) for call in self.sent for m in call)


@pytest.fixture
def provider(monkeypatch):
    return RecordingProvider()


@pytest.fixture
def gateway_app(monkeypatch, provider):
    session = {
        "user_id": USER_ID,
        "provider": "openai",
        "model_name": "m",
        "api_key_enc": None,
        "allow_tools": True,
    }
    monkeypatch.setattr(gateway, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(model_router, "_make_llm", lambda *args: provider)
    monkeypatch.setattr(
        pii_profile,
        "load_privacy_context",
        AsyncMock(return_value=(PrivacyPolicy(True, True, True), _known())),
    )
    app = FastAPI()
    app.include_router(gateway.router)
    return app


async def _post(app, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/llm-gateway/v1/chat/completions",
            headers={"Authorization": "Bearer session"},
            json={"model": "m", **body},
        )


# ── gateway ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbound_request_has_no_pii_and_reply_is_rehydrated(gateway_app, provider, caplog):
    caplog.set_level(logging.DEBUG)
    response = await _post(
        gateway_app,
        {
            "messages": [
                {"role": "system", "content": "Write a cover letter."},
                {"role": "user", "content": f"RESUME:\n{RESUME}\n\nJD:\n{JD}"},
            ]
        },
    )
    assert response.status_code == 200
    sent = provider.outbound_text()
    assert _leaks(sent) == []
    assert "[[NAME_1]]" in sent and "[[EMAIL_1]]" in sent and "[[PHONE_1]]" in sent
    assert "careers@acme.in" in sent  # shared mailboxes stay readable
    system = provider.sent[0][0].content
    assert system.startswith("UNTRUSTED CONTENT") and "PRIVATE VALUES" in system
    reply = response.json()["choices"][0]["message"]["content"]
    for value in (NAME, EMAIL, PHONE, RECRUITER_EMAIL, PAN):
        assert value in reply
    assert "[[" not in reply
    assert _leaks(caplog.text) == []


@pytest.mark.asyncio
async def test_tool_call_arguments_are_rehydrated(gateway_app, monkeypatch):
    tool_provider = RecordingProvider(tool_call=True)
    monkeypatch.setattr(model_router, "_make_llm", lambda *args: tool_provider)
    response = await _post(
        gateway_app,
        {
            "messages": [
                {"role": "user", "content": f"Fill the form for {NAME}, {EMAIL}, {PHONE}"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_0",
                            "type": "function",
                            "function": {"name": "fill", "arguments": json.dumps({"e": EMAIL})},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_0", "content": f"Typed {EMAIL}"},
            ],
            "tools": [{"type": "function", "function": {"name": "fill", "parameters": {}}}],
        },
    )
    assert response.status_code == 200
    assert _leaks(tool_provider.outbound_text()) == []
    forwarded_call = tool_provider.sent[0][2].tool_calls[0]["args"]
    assert forwarded_call == {"e": "[[EMAIL_1]]"}
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    # Tokens echoed from the tool message (the provider sees the last message).
    assert json.loads(call["function"]["arguments"])["values"] == [EMAIL]


@pytest.mark.asyncio
async def test_strict_mode_fails_closed_when_redaction_cannot_run(gateway_app, provider, monkeypatch):
    monkeypatch.setattr(
        pii_profile, "load_privacy_context", AsyncMock(side_effect=RuntimeError(EMAIL))
    )
    monkeypatch.setattr(settings, "PII_REDACTION_STRICT", True)
    response = await _post(gateway_app, {"messages": [{"role": "user", "content": EMAIL}]})
    assert response.status_code == 503
    assert provider.sent == []
    assert EMAIL not in response.text


@pytest.mark.asyncio
async def test_strict_mode_fails_closed_when_redaction_itself_errors(
    gateway_app, provider, monkeypatch
):
    def broken(self, messages):
        raise ValueError("boom")

    monkeypatch.setattr(gateway.PiiVault, "redact_messages", broken)
    response = await _post(gateway_app, {"messages": [{"role": "user", "content": EMAIL}]})
    assert response.status_code == 503
    assert provider.sent == []


@pytest.mark.asyncio
async def test_lenient_mode_sends_unredacted_when_loading_fails(gateway_app, provider, monkeypatch):
    monkeypatch.setattr(pii_profile, "load_privacy_context", AsyncMock(side_effect=RuntimeError()))
    monkeypatch.setattr(settings, "PII_REDACTION_STRICT", False)
    response = await _post(gateway_app, {"messages": [{"role": "user", "content": EMAIL}]})
    assert response.status_code == 200
    assert EMAIL in provider.outbound_text()


@pytest.mark.asyncio
async def test_member_who_opted_out_is_sent_as_is(gateway_app, provider, monkeypatch):
    monkeypatch.setattr(
        pii_profile,
        "load_privacy_context",
        AsyncMock(return_value=(PrivacyPolicy(False, False, False), KnownPii())),
    )
    response = await _post(gateway_app, {"messages": [{"role": "user", "content": EMAIL}]})
    assert response.status_code == 200
    assert EMAIL in provider.outbound_text()
    assert "PRIVATE VALUES" not in provider.outbound_text()


@pytest.mark.asyncio
async def test_requests_without_pii_are_unchanged(gateway_app, provider):
    response = await _post(gateway_app, {"messages": [{"role": "user", "content": "Hello"}]})
    assert response.status_code == 200
    assert "PRIVATE VALUES" not in provider.sent[0][0].content
    assert provider.sent[0][-1].content == "Hello"


# ── every agent, through the gateway ────────────────────────────────────────


def _gateway_chat_model(app):
    from langchain_openai import ChatOpenAI
    from starlette.testclient import TestClient

    return ChatOpenAI(
        model="m",
        api_key="session",
        base_url="http://testserver/llm-gateway/v1",
        http_client=TestClient(app),
        http_async_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ),
        max_retries=0,
        timeout=30,
    )


MODEL = SimpleNamespace(
    user_id=USER_ID, provider="openai", model_name="m", api_key_enc=None, ollama_url=None
)
DOCS = [Document(page_content=RESUME)]
CASES = {
    "resume_optimize": {"jd_text": JD},
    "cover_letter": {"jd_text": JD, "tone": "formal"},
    "linkedin_optimize": {"target_role": "Python Engineer"},
    "email": {"company": "Acme", "role": "Engineer", "recipient_email": RECRUITER_EMAIL},
    "interview_prep": {"role": "Engineer", "company": "Acme", "jd_text": JD},
    "interview_coach": {"role": "Engineer", "company": "Acme"},
    "salary_intelligence": {"role": "Python Engineer", "location": "Bengaluru"},
}


def _agent_patches(llm) -> list:
    agents = "app.agents"
    found = AsyncMock(return_value=[{"url": "https://example.com", "text": "salary"}])
    percentiles = {"p25": 1000000, "p50": 1500000, "p75": 2000000, "currency": "INR"}
    contact = {"email": EMAIL, "phone": PHONE, "linkedin": LINKEDIN}
    return [
        *(
            patch(f"{agents}.{m}.build_agent_llm", return_value=llm)
            for m in (
                "email_agent",
                "linkedin_agent",
                "interview_prep_agent",
                "interview_coach_agent",
                "salary_agent",
                "cover_letter_agent",
            )
        ),
        patch("app.core.llm_gateway.build_gateway_llm", return_value=llm),
        *(
            patch(f"{agents}.{m}.retrieve", return_value=DOCS)
            for m in (
                "linkedin_agent",
                "interview_prep_agent",
                "interview_coach_agent",
                "cover_letter_agent",
                "resume_agent",
            )
        ),
        patch(f"{agents}.email_agent.GmailMCPClient"),
        patch(f"{agents}.salary_agent._search_salary_sources", found),
        patch(f"{agents}.salary_agent._extract_percentiles", return_value=percentiles),
        patch(f"{agents}.salary_agent._log_agent_run", return_value=None),
        patch(f"{agents}.interview_coach_agent._log_agent_run", return_value=None),
        patch(f"{agents}.interview_coach_agent._save_interview_session", return_value=None),
        patch(f"{agents}.resume_agent.generate_resume_pdf", return_value=b"%PDF"),
        patch(f"{agents}.resume_agent._persist_resume_document", return_value=None),
        patch(f"{agents}.orchestrator.emit"),
        patch("app.core.event_bus.emit"),
        patch("app.core.sync_db.fetch_user_full_name", return_value=NAME),
        patch("app.core.sync_db.fetch_user_profile_text", return_value=RESUME),
        patch(
            "app.services.resume_facts.fetch_resume_facts_sync",
            return_value=(contact, {"contact": contact}),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", sorted(CASES))
async def test_agent_prompts_reach_the_provider_without_pii(
    task_type, gateway_app, provider, caplog
):
    from app.agents.orchestrator import orchestrator

    caplog.set_level(logging.DEBUG)
    llm = _gateway_chat_model(gateway_app)
    state = {
        "user_id": USER_ID,
        "run_id": str(uuid.uuid4()),
        "task_type": task_type,
        "messages": [],
        "context": {**CASES[task_type], "_durable": True},
        "status": "running",
        "pending_action": None,
        "result": None,
        "error": None,
        "model_settings": MODEL,
    }
    with ExitStack() as stack:
        for item in _agent_patches(llm):
            stack.enter_context(item)
        await orchestrator.ainvoke(state)
    assert provider.sent, f"{task_type} never reached the model"
    assert _leaks(provider.outbound_text()) == []
    privacy_logs = "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(("app.core.llm_gateway", "app.core.pii", "app.services.pii"))
    )
    assert _leaks(privacy_logs) == []


def test_email_monitor_sends_third_party_mail_without_contact_details(gateway_app, provider):
    from app.agents import email_monitor_agent as monitor

    gmail = MagicMock()
    gmail.search_threads.return_value = [{"id": "m1"}]
    gmail.get_message_summary.return_value = {
        "subject": f"Your application, {NAME}",
        "body": f"Hi {NAME}, thanks for applying. Reach me at {RECRUITER_PHONE}.",
        "from": f"Ananya Rao <{RECRUITER_EMAIL}>",
    }
    llm = _gateway_chat_model(gateway_app)
    with (
        patch.object(monitor, "GmailMCPClient", lambda user_id: gmail),
        patch.object(monitor, "build_agent_llm", lambda settings: llm),
        patch.object(monitor, "processed_message_ids", AsyncMock(return_value=set())),
        patch.object(monitor, "apply_inbox_updates", AsyncMock(return_value=[])),
    ):
        monitor.email_monitor_node(
            {"user_id": USER_ID, "run_id": "r", "context": {}, "model_settings": MODEL}
        )
    assert provider.sent
    assert _leaks(provider.outbound_text()) == []
