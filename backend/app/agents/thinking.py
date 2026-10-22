"""
thinking.py — Critical thinking utilities for agent harness.

Provides a pre-action reasoning step that agents can invoke to:
1. Analyze the task and user context
2. Select only relevant information (e.g., 3 of 5 projects)
3. Decide on the best approach before executing
4. Explain reasoning for transparency

This makes agents smarter by forcing deliberate analysis before action,
rather than dumping all context into a single prompt.
"""
import logging
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

THINKING_SYSTEM = """You are a critical thinking engine. Before any action is taken, \
you analyze the situation and make strategic decisions. You think step-by-step.

Rules:
- Be selective — less is more. Only include what's directly relevant.
- Explain your reasoning briefly.
- Output structured decisions the action agent can follow."""


def think_and_select(
    llm: BaseChatModel,
    task_description: str,
    user_context: str,
    target_context: str,
    selection_criteria: str = "",
) -> str:
    """Run a critical thinking step before agent action.

    Args:
        llm: The LLM to use for reasoning
        task_description: What the agent is about to do
        user_context: All available user data (projects, experience, etc.)
        target_context: The target (JD, company info, recipient, etc.)
        selection_criteria: Optional specific criteria for selection

    Returns:
        Structured thinking output with selections and reasoning
    """
    prompt = f"""TASK: {task_description}

USER'S FULL CONTEXT:
{user_context}

TARGET:
{target_context}

{f"SELECTION CRITERIA: {selection_criteria}" if selection_criteria else ""}

THINK STEP BY STEP:
1. What does the target specifically need/want?
2. Which parts of the user's context are DIRECTLY relevant? (score each HIGH/MEDIUM/LOW)
3. What should be INCLUDED? (only HIGH and MEDIUM)
4. What should be OMITTED? (LOW relevance — leaving it out makes the output stronger)
5. What's the best angle/narrative to take?

OUTPUT YOUR DECISIONS:
INCLUDE: <what to include and why>
OMIT: <what to leave out and why>
APPROACH: <the strategy to use>
REASONING: <1-2 sentences explaining your logic>"""

    try:
        response = llm.invoke([
            SystemMessage(content=THINKING_SYSTEM),
            HumanMessage(content=prompt),
        ])
        return response.content
    except Exception as exc:
        logger.warning("Thinking step failed, proceeding without: %s", exc)
        return ""


def think_about_job_match(
    llm: BaseChatModel,
    user_profile: str,
    job_description: str,
) -> dict:
    """Critical thinking specifically for job matching decisions.

    Returns structured analysis of whether/how to apply.
    """
    prompt = f"""Analyze this job match critically.

USER PROFILE:
{user_profile[:1500]}

JOB DESCRIPTION:
{job_description[:1500]}

THINK:
1. What are the MUST-HAVE requirements? Does the user meet them?
2. What are the NICE-TO-HAVE requirements? How many does the user meet?
3. Is there a skills gap? If yes, is it bridgeable?
4. What's the user's strongest selling point for THIS specific role?
5. Should the user apply? (YES/MAYBE/NO)
6. If YES, what angle should the application take?

FORMAT:
MATCH_LEVEL: <HIGH/MEDIUM/LOW>
DECISION: <YES/MAYBE/NO>
STRENGTHS: <top 3 strengths for this role>
GAPS: <any gaps>
ANGLE: <recommended approach>
REASONING: <why>"""

    try:
        response = llm.invoke([
            SystemMessage(content=THINKING_SYSTEM),
            HumanMessage(content=prompt),
        ])
        text = response.content

        # Parse structured output
        result = {"raw": text, "decision": "MAYBE", "match_level": "MEDIUM"}
        for line in text.split("\n"):
            if line.startswith("MATCH_LEVEL:"):
                result["match_level"] = line.split(":", 1)[1].strip()
            elif line.startswith("DECISION:"):
                result["decision"] = line.split(":", 1)[1].strip()
            elif line.startswith("ANGLE:"):
                result["angle"] = line.split(":", 1)[1].strip()
        return result
    except Exception as exc:
        logger.warning("Job match thinking failed: %s", exc)
        return {"raw": "", "decision": "MAYBE", "match_level": "MEDIUM"}
