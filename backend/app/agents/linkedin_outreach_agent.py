"""LinkedIn outreach agent.

Finds recruiter/talent contacts for a company, drafts short connection notes,
stores them in the approval queue, and returns a human-review checkpoint.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langchain_core.messages import AIMessage

from app.agents.state import AgentState
from app.core.database import AsyncSessionLocal
from app.core.sync_db import fetch_user_profile_text, run_coro_sync
from app.models.db import LinkedInOutreachQueue
from app.services.linkedin_outreach_service import (
    draft_outreach_message,
    filter_contacts_by_title,
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


async def _build_outreach(state: AgentState) -> AgentState:
    user_id = state["user_id"]
    ctx = state["context"]
    company = (ctx.get("company_name") or ctx.get("company") or "").strip()
    role_context = (ctx.get("role_context") or ctx.get("target_role") or "").strip()

    if not company:
        return {**state, "status": "failed", "error": "company_name is required"}

    contacts = await ProxycurlService().find_contacts(company, role_context or None)
    relevant_contacts = filter_contacts_by_title(contacts) or contacts
    relevant_contacts = [
        c for c in relevant_contacts[:10]
        if c.get("name") and c.get("linkedin_url")
    ][:5]

    if not relevant_contacts:
        return {
            **state,
            "status": "completed",
            "result": {
                "type": "linkedin_outreach",
                "company": company,
                "contacts": [],
                "messages": [],
                "notice": "No recruiter or hiring contacts found. Check Proxycurl configuration or try another company.",
            },
            "messages": state["messages"] + [
                AIMessage(content=f"No LinkedIn outreach contacts found for {company}.")
            ],
        }

    try:
        profile_text = fetch_user_profile_text(user_id)
    except Exception as exc:
        logger.warning("LinkedIn outreach profile fetch failed for user %s: %s", user_id, exc)
        profile_text = ""

    user_experience = (profile_text or role_context or "software engineering and product delivery").strip()
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
        "messages": state["messages"] + [
            AIMessage(content=f"LinkedIn outreach drafts ready for {company}.")
        ],
    }


def linkedin_outreach_agent_node(state: AgentState) -> AgentState:
    """Synchronous LangGraph node wrapper for async outreach workflow."""
    try:
        return run_coro_sync(_build_outreach(state))
    except Exception as exc:
        logger.error("LinkedIn outreach failed for user %s: %s", state.get("user_id"), exc)
        return {**state, "status": "failed", "error": "Agent failed"}
