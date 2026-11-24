"""LinkedIn outreach agent.

Drafts short connection notes for contacts the user supplies (or a configured
lookup finds), stores them in the approval queue, and returns a human-review checkpoint.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from langchain_core.messages import AIMessage

from app.agents.state import AgentState
from app.core.database import AsyncSessionLocal
from app.core.sync_db import fetch_user_profile_text, run_coro_sync
from app.models.db import LinkedInOutreachQueue
from app.services.linkedin_outreach_service import (
    NO_LOOKUP_NOTICE,
    draft_outreach_message,
    filter_contacts_by_title,
    lookup_configured,
)
from app.services.proxycurl_service import ProxycurlService

logger = logging.getLogger(__name__)


async def _persist_queue_items(
    user_id: str,
    company: str,
    drafts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist drafted outreach messages to linkedin_outreach_queue."""
    queued: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        for draft in drafts:
            row = LinkedInOutreachQueue(
                user_id=uuid.UUID(user_id),
                company=company,
                contact_name=draft["contact_name"],
                contact_title=draft.get("contact_title"),
                contact_linkedin_url=draft.get("profile_url"),
                message=draft["message"],
                status="pending_approval",
            )
            db.add(row)
            await db.flush()
            queued.append({**draft, "queue_id": str(row.id)})
        await db.commit()
    return queued


_CONTACT_DETAIL = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://\S+|www\.\S+|\+?\d[\d\s().-]{6,}\d"
)


def _without_contact_details(resume_text: str) -> str:
    """Resume text minus its header line and any email, phone or link.

    A resume starts with the candidate's name and contact line, which must not
    end up in a note to a stranger.
    """
    lines = [line for line in (resume_text or "").splitlines() if line.strip()]
    body = " ".join(lines[1:] if len(lines) > 1 else lines)
    return " ".join(_CONTACT_DETAIL.sub(" ", body).split())


def _supplied_contacts(value: Any) -> list[dict[str, Any]]:
    """Contacts the user typed, kept only with a name and a LinkedIn profile URL."""
    contacts: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:100]
        title = " ".join(str(item.get("title") or "").split())[:150]
        url = str(item.get("linkedin_url") or "").strip()
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            not name
            or parsed.scheme != "https"
            or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
        ):
            continue
        contacts.append({"name": name, "title": title, "linkedin_url": url[:300]})
    return contacts[:5]


async def _build_outreach(state: AgentState) -> AgentState:
    user_id = state["user_id"]
    ctx = state["context"]
    company = (ctx.get("company_name") or ctx.get("company") or "").strip()
    role_context = (ctx.get("role_context") or ctx.get("target_role") or "").strip()

    if not company:
        return {**state, "status": "failed", "error": "company_name is required"}

    supplied = _supplied_contacts(ctx.get("contacts"))
    if supplied:
        relevant_contacts = supplied
    elif lookup_configured():
        contacts = await ProxycurlService().find_contacts(company, role_context or None)
        relevant_contacts = filter_contacts_by_title(contacts) or contacts
        relevant_contacts = [
            c for c in relevant_contacts[:10] if c.get("name") and c.get("linkedin_url")
        ][:5]
    else:
        relevant_contacts = []

    if not relevant_contacts:
        return {
            **state,
            "status": "completed",
            "result": {
                "type": "linkedin_outreach",
                "company": company,
                "contacts": [],
                "messages": [],
                "notice": (
                    NO_LOOKUP_NOTICE
                    if not lookup_configured()
                    else "No recruiter or hiring contacts found. Add the people you want to "
                    "reach (name, title and LinkedIn profile URL) and run it again."
                ),
            },
            "messages": state["messages"]
            + [AIMessage(content=f"No LinkedIn outreach contacts found for {company}.")],
        }

    try:
        profile_text = fetch_user_profile_text(user_id)
    except Exception as exc:
        logger.warning("LinkedIn outreach profile fetch failed for user %s: %s", user_id, exc)
        profile_text = ""

    user_experience = (
        role_context
        or _without_contact_details(profile_text)
        or "software engineering and product delivery"
    )
    drafts = []
    for contact in relevant_contacts:
        message = draft_outreach_message(
            contact_name=contact.get("name", "there"),
            contact_title=contact.get("title", "talent leader"),
            user_experience=user_experience,
            company_intel=role_context or company,
        )
        drafts.append(
            {
                "contact_name": contact.get("name", "Unknown"),
                "contact_title": contact.get("title", ""),
                "profile_url": contact.get("linkedin_url", ""),
                "message": message,
            }
        )

    queued = await _persist_queue_items(user_id, company, drafts)
    contacts_payload = [
        {
            "name": item["contact_name"],
            "title": item.get("contact_title", ""),
            "linkedin_url": item.get("profile_url", ""),
        }
        for item in queued
    ]

    return {
        **state,
        "status": "awaiting_approval",
        "pending_action": {
            "type": "linkedin_outreach",
            "company": company,
            "role_context": role_context,
            "contacts": contacts_payload,
            "messages": queued,
            "queue_ids": [item["queue_id"] for item in queued],
        },
        "messages": state["messages"]
        + [AIMessage(content=f"LinkedIn outreach drafts ready for {company}.")],
    }


def linkedin_outreach_agent_node(state: AgentState) -> AgentState:
    """Synchronous LangGraph node wrapper for async outreach workflow."""
    try:
        return run_coro_sync(_build_outreach(state))
    except Exception as exc:
        logger.error("LinkedIn outreach failed for user %s: %s", state.get("user_id"), exc)
        return {**state, "status": "failed", "error": "Agent failed"}
