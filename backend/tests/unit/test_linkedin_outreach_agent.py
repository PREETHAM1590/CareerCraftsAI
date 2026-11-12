import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.state import AgentState


def make_state() -> AgentState:
    return AgentState(
        user_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        task_type="linkedin_outreach",
        messages=[HumanMessage(content="Find LinkedIn contacts")],
        context={"company_name": "Acme Corp", "role_context": "backend platform"},
        status="running",
        pending_action=None,
        result=None,
        error=None,
    )


async def _fake_persist(user_id: str, company: str, drafts: list[dict]):
    return [{**draft, "queue_id": str(uuid.uuid4())} for draft in drafts]


def test_linkedin_outreach_agent_finds_contacts_and_pauses_for_approval():
    from app.agents.linkedin_outreach_agent import linkedin_outreach_agent_node

    proxycurl = MagicMock()
    proxycurl.find_contacts = AsyncMock(
        return_value=[
            {
                "name": "Sarah Chen",
                "title": "Senior Technical Recruiter",
                "linkedin_url": "https://linkedin.com/in/sarah",
            },
            {
                "name": "Pat Lee",
                "title": "Product Manager",
                "linkedin_url": "https://linkedin.com/in/pat",
            },
        ]
    )

    with (
        patch("app.agents.linkedin_outreach_agent.lookup_configured", return_value=True),
        patch("app.agents.linkedin_outreach_agent.ProxycurlService", return_value=proxycurl),
        patch(
            "app.agents.linkedin_outreach_agent.fetch_user_profile_text",
            return_value="Python FastAPI platform work",
        ),
        patch("app.agents.linkedin_outreach_agent._persist_queue_items", new=_fake_persist),
    ):
        result = linkedin_outreach_agent_node(make_state())

    assert result["status"] == "awaiting_approval"
    pending = result["pending_action"]
    assert pending["type"] == "linkedin_outreach"
    assert pending["company"] == "Acme Corp"
    assert len(pending["contacts"]) == 1
    assert pending["contacts"][0]["name"] == "Sarah Chen"
    assert pending["messages"][0]["queue_id"]
    assert len(pending["messages"][0]["message"]) <= 300


def test_linkedin_outreach_agent_gracefully_handles_no_contacts():
    from app.agents.linkedin_outreach_agent import linkedin_outreach_agent_node

    proxycurl = MagicMock()
    proxycurl.find_contacts = AsyncMock(return_value=[])

    with (
        patch("app.agents.linkedin_outreach_agent.lookup_configured", return_value=True),
        patch("app.agents.linkedin_outreach_agent.ProxycurlService", return_value=proxycurl),
    ):
        result = linkedin_outreach_agent_node(make_state())

    assert result["status"] == "completed"
    assert result["result"]["contacts"] == []
    assert "No recruiter" in result["result"]["notice"]


def test_draft_handles_member_typed_contacts_without_a_title():
    from app.services.linkedin_outreach_service import draft_outreach_message

    msg = draft_outreach_message("  Priya\n Rao ", "", "Python services")
    assert msg.startswith("Hi Priya Rao, I came across your profile.")
    assert "role as ." not in msg and len(msg) <= 300


def test_outreach_without_lookup_asks_for_contacts_and_skips_proxycurl():
    from app.agents.linkedin_outreach_agent import linkedin_outreach_agent_node
    from app.services.linkedin_outreach_service import NO_LOOKUP_NOTICE

    proxycurl = MagicMock()
    proxycurl.find_contacts = AsyncMock(return_value=[])

    with (
        patch("app.agents.linkedin_outreach_agent.lookup_configured", return_value=False),
        patch("app.agents.linkedin_outreach_agent.ProxycurlService", return_value=proxycurl),
    ):
        result = linkedin_outreach_agent_node(make_state())

    assert result["result"]["notice"] == NO_LOOKUP_NOTICE
    proxycurl.find_contacts.assert_not_awaited()


def test_identify_request_validates_supplied_contacts():
    from pydantic import ValidationError

    from app.api.v1.linkedin import OutreachIdentifyRequest

    good = {
        "name": "Sarah Chen",
        "title": "Recruiter",
        "linkedin_url": "https://www.linkedin.com/in/sc",
    }
    body = OutreachIdentifyRequest(company_name="Acme", contacts=[good])
    assert body.contacts[0].name == "Sarah Chen"
    assert OutreachIdentifyRequest(company_name="Acme").contacts == []

    for bad in (
        {**good, "linkedin_url": "http://www.linkedin.com/in/sc"},
        {**good, "linkedin_url": "https://evil.example/linkedin.com"},
        {**good, "linkedin_url": "https://notlinkedin.com/in/sc"},
        {**good, "name": ""},
        {**good, "title": "x" * 151},
    ):
        with pytest.raises(ValidationError):
            OutreachIdentifyRequest(company_name="Acme", contacts=[bad])
    with pytest.raises(ValidationError):
        OutreachIdentifyRequest(company_name="Acme", contacts=[good] * 6)
