from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

# ---- Agent node function imports ----
from app.agents.auto_apply_pipeline import run_auto_apply_pipeline
from app.agents.company_research_agent import company_research_node
from app.agents.cover_letter_agent import cover_letter_node
from app.agents.email_agent import email_agent_node
from app.agents.email_monitor_agent import email_monitor_node
from app.agents.interview_coach_agent import evaluate_answer_node, start_session_node
from app.agents.interview_prep_agent import interview_prep_agent_node
from app.agents.job_search import job_search_agent_node
from app.agents.linkedin_agent import linkedin_agent_node
from app.agents.linkedin_outreach_agent import linkedin_outreach_agent_node
from app.agents.resume_agent import resume_agent_node
from app.agents.salary_agent import salary_report_node
from app.agents.state import AgentState
from app.core.event_bus import emit

logger = logging.getLogger(__name__)
CLIENT_SAFE_AGENT_ERROR = "Agent failed"

# ---- Task keywords → agent node mapping ----
# Maps task_type values sent by the API/harness to graph node names.
TASK_ROUTES: dict[str, str] = {
    "resume_optimize": "resume",
    "job_search": "job_search",
    "linkedin_optimize": "linkedin",
    "linkedin_outreach": "linkedin_outreach",
    "email": "email",
    "cover_letter": "cover_letter",
    "interview_coach": "interview_coach",
    "evaluate_answer": "interview_coach",
    "interview_prep": "interview_prep",
    "company_research": "company_research",
    "salary_intelligence": "salary",
    "email_monitor": "email_monitor",
    "auto_apply": "auto_apply",
}

_TERMINAL_STATUSES = {"completed", "failed", "awaiting_approval"}


def _route_task(state: AgentState) -> str:
    status = state.get("status", "running")
    if status in _TERMINAL_STATUSES:
        return END
    task = (state.get("task_type") or state.get("task") or "").strip()
    return TASK_ROUTES.get(task, END)


# ---- Wrapper nodes for agents with non-standard signatures ----


async def _auto_apply_wrapper(state: AgentState) -> AgentState:
    ctx = state.get("context", {})
    result = await run_auto_apply_pipeline(
        user_id=state["user_id"],
        search_query=ctx.get("search_query", ctx.get("query", "")),
        location=ctx.get("location", "Remote"),
        max_applications=ctx.get("max_applications", 5),
        platforms=ctx.get("platforms"),
        linkedin_credentials=ctx.get("linkedin_credentials"),
        model_settings=state.get("model_settings"),
        live_browser=ctx.get("live_browser", False),
        run_id=state["run_id"],
    )
    pending = result if result.get("requires_approval") else None
    return {
        **state,
        "status": "awaiting_approval" if pending else "completed",
        "pending_action": pending,
        "result": result,
    }


async def _interview_coach_wrapper(state: AgentState) -> AgentState:
    ctx = state.get("context", {})
    # Both nodes make blocking LLM and DB calls; keep them off the event loop.
    if ctx.get("session_id"):
        return await asyncio.to_thread(evaluate_answer_node, state)
    return await asyncio.to_thread(start_session_node, state)


# ---- Safe agent runner (handles both sync and async node functions) ----


async def _run_agent_safely(agent_fn: Callable, state: AgentState) -> AgentState:
    start = time.time()
    from app.core.model_router import begin_token_tracking, get_and_reset_tokens

    begin_token_tracking()
    try:
        if asyncio.iscoroutinefunction(agent_fn):
            result = await agent_fn(state)
        else:
            result = await asyncio.to_thread(agent_fn, state)
        duration_ms = int((time.time() - start) * 1000)
        tokens = get_and_reset_tokens() or (
            result.get("tokens_used") if isinstance(result, dict) else None
        )

        if state.get("context", {}).get("_durable"):
            return {**result, "tokens_used": tokens}

        try:
            from app.core.agent_runs_repository import upsert_agent_run  # type: ignore

            await upsert_agent_run(
                run_id=state["run_id"],
                status=result.get("status") if isinstance(result, dict) else "completed",
                output=(result.get("result") if isinstance(result, dict) else None),
                tokens_used=tokens,
                duration_ms=duration_ms,
                error=result.get("error") if isinstance(result, dict) else None,
            )
        except Exception:
            logger.debug("agent_runs upsert skipped.", exc_info=True)

        return {**result, "tokens_used": tokens} if isinstance(result, dict) else result
    except Exception as exc:
        if state.get("context", {}).get("_durable"):
            raise
        duration_ms = int((time.time() - start) * 1000)
        err_msg = str(exc) or "unknown error"
        tokens = get_and_reset_tokens()

        try:
            from app.core.agent_runs_repository import upsert_agent_run  # type: ignore

            await upsert_agent_run(
                run_id=state["run_id"],
                status="failed",
                output=None,
                tokens_used=tokens or None,
                duration_ms=duration_ms,
                error=err_msg,
            )
        except Exception:
            logger.debug("agent_runs failure upsert skipped.", exc_info=True)

        try:
            emit(state["run_id"], "error", CLIENT_SAFE_AGENT_ERROR)
        except Exception:
            logger.debug("SSE error emit skipped.", exc_info=True)

        raise


# ---- Node runner with SSE emission ----


def _make_node_runner(node_name: str, agent_fn: Callable) -> Callable:
    async def runner(state: AgentState) -> AgentState:
        emit(state["run_id"], "log", f"[{node_name}] starting...")
        result_state = await _run_agent_safely(agent_fn, state)
        status = result_state.get("status") if isinstance(result_state, dict) else None

        if status == "awaiting_approval":
            emit(state["run_id"], "checkpoint", result_state.get("pending_action") or {})
        elif status == "failed":
            if result_state.get("error"):
                logger.warning(
                    "Agent failed for run %s: %s", state["run_id"], result_state.get("error")
                )
            emit(state["run_id"], "error", CLIENT_SAFE_AGENT_ERROR)
        elif status == "completed":
            emit(state["run_id"], "complete", result_state.get("result") or {})
        return result_state

    return runner


# ---- Node registry ----
_NODE_REGISTRY: dict[str, Callable] = {
    "job_search": job_search_agent_node,
    "resume": resume_agent_node,
    "cover_letter": cover_letter_node,
    "linkedin": linkedin_agent_node,
    "email": email_agent_node,
    "interview_prep": interview_prep_agent_node,
    "interview_coach": _interview_coach_wrapper,
    "company_research": company_research_node,
    "salary": salary_report_node,
    "linkedin_outreach": linkedin_outreach_agent_node,
    "email_monitor": email_monitor_node,
    "auto_apply": _auto_apply_wrapper,
    # follow_up is NOT a graph node — it's called directly from internal.py
    # as schedule_followups() with a completely different signature
}

_ALL_NODES = list(_NODE_REGISTRY.keys())


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    for node_name, agent_fn in _NODE_REGISTRY.items():
        graph.add_node(node_name, _make_node_runner(node_name, agent_fn))

    route_map = {name: name for name in _ALL_NODES}
    route_map[END] = END

    # set_conditional_entry_point is deprecated in LangGraph 1.x.
    graph.add_conditional_edges(START, _route_task, route_map)

    for node_name in _ALL_NODES:
        graph.add_edge(node_name, END)

    return graph.compile()


orchestrator = build_graph()
route_task = _route_task  # public alias for tests
