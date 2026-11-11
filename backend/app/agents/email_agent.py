import logging

from langchain_core.messages import AIMessage

from app.agents._llm_json import call_llm_json
from app.agents.prompts import unvetted_destinations
from app.agents.prompts.email_prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from app.agents.state import AgentState
from app.agents.thinking import think_and_select
from app.core.model_router import build_agent_llm
from app.core.sync_db import fetch_model_settings
from app.services.gmail_service import GmailMCPClient

logger = logging.getLogger(__name__)


def _query_term(value) -> str:
    """A value safe to quote inside a Gmail search operator."""
    return " ".join(str(value or "").replace('"', " ").replace("\\", " ").split())[:200]


def email_agent_node(state: AgentState) -> AgentState:
    try:
        user_id = state["user_id"]
        ctx = state["context"]
        company = ctx.get("company", "")
        role = ctx.get("role", "")
        recipient = ctx.get("recipient_email", "")

        model_settings = state.get("model_settings") or fetch_model_settings(user_id)
        if not model_settings:
            raise ValueError("No active model settings configured for user")

        try:
            gmail = GmailMCPClient(user_id)
            threads = gmail.search_threads(
                f'from:"{_query_term(recipient)}" OR subject:"{_query_term(company)}"',
                max_results=3,
            )
        except Exception as exc:
            logger.warning("Email thread lookup failed for user %s: %s", user_id, exc)
            threads = []
        thread_context = (
            "\n".join(str(t) for t in threads[:2]) if threads else "No prior threads found."
        )

        llm = build_agent_llm(model_settings)

        # ── Think: What angle to take, what to emphasize ─────────────
        thinking = think_and_select(
            llm=llm,
            task_description=f"Write outreach email to {recipient} about {role} at {company}",
            user_context=f"Prior email threads: {thread_context}",
            target_context=f"Company: {company}, Role: {role}",
            selection_criteria=(
                "What hook will get a response? What's unique about this candidate for this role?"
            ),
        )

        draft = call_llm_json(
            llm,
            SYSTEM_PROMPT,
            build_user_prompt(
                {
                    "company": company,
                    "role": role,
                    "recipient": recipient,
                    "thread": thread_context,
                    "purpose": thinking,
                }
            ),
            OUTPUT_SCHEMA,
        )

        subject = ctx.get("subject") if ctx.get("subject") is not None else draft.subject
        body = ctx.get("body") if ctx.get("body") is not None else draft.body
        pending = {
            "type": "send_email",
            # Always the caller's recipient: model output never chooses where mail goes.
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "thinking": draft.intent_detected,
        }
        unvetted = unvetted_destinations(
            f"{subject}\n{body}",
            [recipient, company, role, ctx.get("subject"), ctx.get("body")],
        )
        if unvetted:
            pending["warnings"] = [
                "The draft contains links or addresses you did not provide "
                f"({', '.join(unvetted[:5])}). Check them before approving."
            ]

        return {
            **state,
            "status": "awaiting_approval",
            "pending_action": pending,
            "messages": state["messages"]
            + [AIMessage(content=f"Email draft ready for {recipient}. Review before sending.")],
        }
    except Exception as exc:
        logger.exception("Email agent failed for user %s", state.get("user_id"))
        return {
            **state,
            "status": "failed",
            "error": f"Email drafting failed: {str(exc)[:200]}",
            "messages": state["messages"]
            + [AIMessage(content="Email drafting failed; no fabricated message was created.")],
        }
