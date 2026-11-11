"""Every graph agent, driven through the real orchestrator with a stub LLM.

No provider keys exist in CI, so this pins down what each agent does when the
model misbehaves: garbage text, an empty reply, or a provider error must end
in a failed run, never in a "completed" or approval-ready result built from
nothing. It also covers the reachability fixes found in the agent audit.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage


class StubLLM:
    def __init__(self, mode: str):
        self.mode = mode
        self.calls = 0

    def invoke(self, messages, *args, **kwargs):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("provider down")
        return AIMessage(content="" if self.mode == "empty" else "Sure! {not json")

    async def ainvoke(self, messages, *args, **kwargs):
        return self.invoke(messages)


MODEL = SimpleNamespace(
    user_id=str(uuid.uuid4()), provider="openai", model_name="m", api_key_enc=None, ollama_url=None
)
DOCS = [Document(page_content="Python engineer, 6 years, FastAPI and PostgreSQL")]

CASES = {
    "resume_optimize": {"jd_text": "Senior Python Engineer, FastAPI"},
    "cover_letter": {"jd_text": "Senior Python Engineer, FastAPI", "tone": "formal"},
    "linkedin_optimize": {"target_role": "Python Engineer"},
    "email": {"company": "Acme", "role": "Engineer", "recipient_email": "r@acme.example"},
    "interview_prep": {"role": "Engineer", "company": "Acme"},
    "interview_coach": {"role": "Engineer", "company": "Acme"},
    "salary_intelligence": {"role": "Python Engineer", "location": "Remote"},
}


def _patches(llm) -> list:
    agents = "app.agents"
    found = AsyncMock(return_value=[{"url": "https://example.com", "text": "salary"}])
    percentiles = {"p25": 100000, "p50": 120000, "p75": 140000, "currency": "USD"}
    targets = [
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
        patch("app.core.sync_db.fetch_user_full_name", return_value="Test User"),
        patch("app.core.sync_db.fetch_user_profile_text", return_value="Python engineer"),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=({}, {})),
    ]
    return targets


async def _run(task_type: str, llm, context: dict | None = None) -> dict:
    from app.agents.orchestrator import orchestrator

    state = {
        "user_id": MODEL.user_id,
        "run_id": str(uuid.uuid4()),
        "task_type": task_type,
        "messages": [],
        "context": {**(context if context is not None else CASES[task_type]), "_durable": True},
        "status": "running",
        "pending_action": None,
        "result": None,
        "error": None,
        "model_settings": MODEL,
    }
    with ExitStack() as stack:
        for item in _patches(llm):
            stack.enter_context(item)
        return await orchestrator.ainvoke(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["garbage", "empty", "raise"])
@pytest.mark.parametrize("task_type", sorted(CASES))
async def test_bad_model_output_fails_the_run(task_type, mode):
    llm = StubLLM(mode)
    result = await _run(task_type, llm)
    assert llm.calls >= 1, "the agent must actually reach the model"
    assert result["status"] == "failed"
    assert not result.get("pending_action")
    assert result.get("error")


# ── Fixes from the audit ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_harness_reports_unrouted_task_as_failed(monkeypatch):
    from app.agents import harness as module
    from app.agents.harness import AgentHarness

    agent = AgentHarness("postgresql://unused", "redis://unused")
    agent._memory = MagicMock()
    agent._semantic_memory = MagicMock()
    monkeypatch.setattr("app.core.sync_db.fetch_model_settings", lambda *a: None)
    result = await agent.run(str(uuid.uuid4()), "no_such_task", {}, {})
    assert result["status"] == "failed"
    assert "no_such_task" in result["error"]
    assert module.TASK_TO_AGENT.get("no_such_task") is None


@pytest.mark.asyncio
async def test_harness_surfaces_user_facing_value_errors(monkeypatch):
    from app.agents import harness as module
    from app.agents.harness import AgentHarness

    async def invoke(state):
        raise ValueError("Enter a company name to research")

    agent = AgentHarness("postgresql://unused", "redis://unused")
    agent._memory = MagicMock()
    agent._semantic_memory = MagicMock()
    monkeypatch.setattr("app.core.sync_db.fetch_model_settings", lambda *a: None)
    monkeypatch.setattr(module.orchestrator, "ainvoke", invoke)
    result = await agent.run(str(uuid.uuid4()), "company_research", {}, {})
    assert result == {**result, "status": "failed", "error": "Enter a company name to research"}


@pytest.mark.asyncio
async def test_company_research_without_a_name_is_a_clear_error():
    from app.agents.company_research_agent import company_research_node

    with pytest.raises(ValueError, match="company name"):
        await company_research_node(
            {"user_id": MODEL.user_id, "run_id": "r", "context": {}, "messages": []}
        )


def test_email_monitor_reports_failure_when_model_cannot_classify(monkeypatch):
    from app.agents import email_monitor_agent as monitor

    gmail = MagicMock()
    gmail.search_threads.return_value = [{"id": "m1"}, {"id": "m2"}]
    gmail.get_message_summary.return_value = {
        "subject": "Hello",
        "body": "Quick question about your availability",
        "from": "x@y.example",
    }
    applied = AsyncMock(return_value=[])
    monkeypatch.setattr(monitor, "GmailMCPClient", lambda user_id: gmail)
    monkeypatch.setattr(monitor, "build_agent_llm", lambda settings: StubLLM("raise"))
    monkeypatch.setattr(monitor, "processed_message_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(monitor, "apply_inbox_updates", applied)

    result = monitor.email_monitor_node(
        {"user_id": MODEL.user_id, "run_id": "r", "context": {}, "model_settings": MODEL}
    )
    assert result["status"] == "failed"
    assert "classify" in result["error"]
    applied.assert_not_awaited()


def test_email_monitor_still_completes_when_nothing_needs_the_model(monkeypatch):
    from app.agents import email_monitor_agent as monitor

    gmail = MagicMock()
    gmail.search_threads.return_value = [{"id": "m1"}]
    gmail.get_message_summary.return_value = {
        "subject": "Interview scheduled at Acme.",
        "body": "",
        "from": "a@acme.example",
    }
    llm = StubLLM("raise")
    monkeypatch.setattr(monitor, "GmailMCPClient", lambda user_id: gmail)
    monkeypatch.setattr(monitor, "build_agent_llm", lambda settings: llm)
    monkeypatch.setattr(monitor, "processed_message_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(monitor, "apply_inbox_updates", AsyncMock(return_value=[]))

    result = monitor.email_monitor_node(
        {"user_id": MODEL.user_id, "run_id": "r", "context": {}, "model_settings": MODEL}
    )
    assert result["status"] == "completed"
    assert result["result"]["updates"][0]["category"] == "INTERVIEW"
    assert llm.calls == 0


def test_linkedin_outreach_works_without_proxycurl_from_supplied_contacts():
    from app.agents.linkedin_outreach_agent import linkedin_outreach_agent_node

    async def persist(user_id, company, drafts):
        return [{**draft, "queue_id": str(uuid.uuid4())} for draft in drafts]

    proxycurl = MagicMock()
    proxycurl.find_contacts = AsyncMock(return_value=[])
    resume = "Jane Doe\njane@example.com | +1 555 010 7788\nBuilt payment APIs"
    with (
        patch("app.agents.linkedin_outreach_agent.ProxycurlService", return_value=proxycurl),
        patch("app.agents.linkedin_outreach_agent.fetch_user_profile_text", return_value=resume),
        patch("app.agents.linkedin_outreach_agent._persist_queue_items", new=persist),
    ):
        result = linkedin_outreach_agent_node(
            {
                "user_id": MODEL.user_id,
                "run_id": "r",
                "messages": [],
                "context": {
                    "company_name": "Acme",
                    "contacts": [
                        {
                            "name": "Sam Lee",
                            "title": "Recruiter",
                            "linkedin_url": "https://www.linkedin.com/in/samlee",
                        }
                    ],
                },
            }
        )
    proxycurl.find_contacts.assert_not_awaited()
    assert result["status"] == "awaiting_approval"
    message = result["pending_action"]["messages"][0]["message"]
    assert message.startswith("Hi Sam Lee")
    assert "jane@example.com" not in message and "555" not in message


def test_linkedin_edits_never_request_retired_browser_automation(mock_llm):
    import json

    from app.agents.linkedin_agent import linkedin_agent_node

    mock_llm.responses = [
        "INCLUDE: x",
        json.dumps({"headline": "Engineer", "about": "About", "experiences": []}),
    ]
    with (
        patch("app.agents.linkedin_agent.retrieve", return_value=DOCS),
        patch("app.agents.linkedin_agent.build_agent_llm", return_value=mock_llm),
    ):
        result = linkedin_agent_node(
            {
                "user_id": MODEL.user_id,
                "run_id": "r",
                "messages": [],
                "context": {"target_role": "Engineer", "live_browser": True},
                "model_settings": MODEL,
            }
        )
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["live_browser"] is False
