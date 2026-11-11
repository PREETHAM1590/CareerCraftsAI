import logging

from langchain_core.messages import AIMessage

from app.agents._llm_json import call_llm_json
from app.agents.prompts.linkedin_prompt import (
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from app.agents.state import AgentState
from app.agents.thinking import think_and_select
from app.core.model_router import build_agent_llm
from app.core.sync_db import fetch_model_settings
from app.services.rag_service import retrieve

logger = logging.getLogger(__name__)


def linkedin_agent_node(state: AgentState) -> AgentState:
    try:
        user_id = state["user_id"]
        target_role = state["context"].get("target_role", "software engineer")

        model_settings = state.get("model_settings") or fetch_model_settings(user_id)
        if not model_settings:
            raise ValueError("No active model settings configured for user")

        chunks = retrieve(user_id, "resume", target_role, model_settings, k=5)
        context_text = "\n".join(c.page_content for c in chunks)
        context_warning = (
            None
            if context_text.strip()
            else "No resume text could be retrieved (no resume uploaded, or document search "
            "was unavailable), so these suggestions are not grounded in your resume."
        )

        llm = build_agent_llm(model_settings)

        # ── Think: Which experiences to highlight, what narrative ─────
        thinking = think_and_select(
            llm=llm,
            task_description=f"Optimize LinkedIn profile for {target_role}",
            user_context=context_text,
            target_context=f"Target role: {target_role}",
            selection_criteria="Which experiences/skills are most relevant? What narrative positions this person best?",
        )

        profile = call_llm_json(
            llm,
            SYSTEM_PROMPT,
            build_user_prompt(
                {"target_role": target_role, "current_profile": thinking},
                [context_text],
            ),
            OUTPUT_SCHEMA,
        )
        bullets = "\n".join(
            f"• {bullet}"
            for experience in profile.experiences[:3]
            for bullet in experience.bullets[:3]
        )

        return {
            **state,
            "status": "awaiting_approval",
            "pending_action": {
                "type": "linkedin_edits",
                "headline": profile.headline,
                "about": profile.about,
                "experience_bullets": bullets,
                "thinking": " ".join(profile.before_after_notes),
                # Edits are applied by the user; LinkedIn browser automation is retired.
                "live_browser": False,
                **({"context_warning": context_warning} if context_warning else {}),
            },
            "messages": state["messages"]
            + [AIMessage(content="LinkedIn sections ready for review.")],
        }
    except Exception as exc:
        logger.exception("LinkedIn agent failed for user %s", state.get("user_id"))
        return {
            **state,
            "status": "failed",
            "error": f"LinkedIn optimization failed: {str(exc)[:200]}",
            "messages": state["messages"]
            + [
                AIMessage(
                    content="LinkedIn optimization failed; no fabricated profile was created."
                )
            ],
        }
