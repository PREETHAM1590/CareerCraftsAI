import logging

from langchain_core.messages import AIMessage

from app.agents._llm_json import call_llm_json
from app.agents.prompts.interview_prep_prompt import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from app.agents.state import AgentState
from app.core.model_router import build_agent_llm
from app.core.sync_db import fetch_model_settings
from app.services.rag_service import retrieval_unavailable, retrieve

logger = logging.getLogger(__name__)


def interview_prep_agent_node(state: AgentState) -> AgentState:
    target_role = "software engineer"
    company = "the company"
    try:
        user_id = state["user_id"]
        ctx = state["context"]
        target_role = ctx.get("target_role", ctx.get("role", "software engineer"))
        company = ctx.get("company", "the company")

        model_settings = state.get("model_settings") or fetch_model_settings(user_id)
        if not model_settings:
            raise ValueError("No active model settings configured for user")

        chunks = retrieve(user_id, "resume", target_role, model_settings, k=5)
        context_text = (
            "\n".join(c.page_content for c in chunks) if chunks else "No resume context available."
        )
        if not chunks:
            context_warning = (
                "No resume text could be retrieved (no resume uploaded, or document search "
                "was unavailable), so this prep is generic."
            )
        elif retrieval_unavailable(chunks):
            context_warning = "Document search was unavailable, so your saved profile text was used."
        else:
            context_warning = None

        llm = build_agent_llm(model_settings)

        # ── Think: What are the candidate's strengths/gaps for this role ──
        from app.agents.thinking import think_and_select

        thinking = think_and_select(
            llm=llm,
            task_description=f"Prepare interview for {target_role} at {company}",
            user_context=context_text,
            target_context=f"Role: {target_role}, Company: {company}",
            selection_criteria="What are the candidate's strongest stories? Where are the gaps they'll be questioned on?",
        )

        prep_data = call_llm_json(
            llm,
            SYSTEM_PROMPT,
            build_user_prompt(
                {
                    "role": target_role,
                    "company": company,
                    "research_notes": thinking,
                },
                [context_text],
            ),
            OUTPUT_SCHEMA,
        ).model_dump()

        pending = {
            "type": "interview_prep",
            "target_role": target_role,
            "company": company,
            **prep_data,
            **({"context_warning": context_warning} if context_warning else {}),
        }
        return {
            **state,
            "status": "awaiting_approval",
            "pending_action": pending,
            "result": None,
            "messages": state["messages"]
            + [AIMessage(content=f"Interview prep ready for {target_role} at {company}.")],
        }
    except Exception as exc:
        logger.exception("Interview prep agent failed for user %s", state.get("user_id"))
        return {
            **state,
            "status": "failed",
            "error": f"Interview prep generation failed: {str(exc)[:200]}",
            "messages": state["messages"]
            + [AIMessage(content=f"Interview prep generation failed for {target_role}.")],
        }
