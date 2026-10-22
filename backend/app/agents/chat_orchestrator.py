"""chat_orchestrator.py — Career Copilot conversational graph (AG-UI served).

A multi-turn chat agent that orchestrates the existing CareerCraft agents:
it starts runs (job_search, auto_apply, resume_optimize, ...), monitors them,
and summarizes results. It is orchestration-only by design:

- Every tool is scoped to the authenticated user via the request-scoped
  ContextVar set by the JWT middleware — never a client-supplied id.
- The LLM is a key-free gateway client (see llm_gateway.get_chat_gateway_llm);
  the real provider key never enters this graph's context.
- There is deliberately no tool that can approve a pending action, cancel a
  run, or send anything: HITL stays a browser action on
  POST /api/v1/agents/{run_id}/approve.

Served over the AG-UI protocol by ag-ui-langgraph (see api/v1/copilot_chat.py).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agents.prompts import SECURITY_RULES, with_security_rules
from app.core.request_context import current_user_id

logger = logging.getLogger(__name__)

AGENT_TYPE = "chat"

# The copilot may start these task types. Everything else (and anything that
# would approve/cancel/send) is out of its reach by construction.
ALLOWED_TASK_TYPES = frozenset(
    {
        "computer_task",
        "job_search",
        "auto_apply",
        "resume_optimize",
        "cover_letter",
        "interview_prep",
        "interview_coach",
        "company_research",
        "salary_intelligence",
        "email",
        "email_monitor",
        "linkedin_optimize",
        "linkedin_outreach",
    }
)

_MAX_CONTEXT_CHARS = 8000
_MAX_LIST_RESULTS = 25

UNAUTHENTICATED_REPLY = (
    "This chat session is not authenticated. Please sign in again and reopen the Copilot."
)

_SYSTEM_PROMPT = f"""You are Career Copilot, the orchestration assistant inside CareerCraft AI.

Your job is to help the user drive their job search end to end by calling tools:
start agent runs (job search, auto-apply, resume tailoring, cover letters,
interview prep, company research, salary intelligence), monitor their progress,
list their applications, and run quick keyless job searches.

Rules:
- Start a run only when the user asks for work that needs one, and tell them the
  run id and that they can watch it on the Agents page.
- When the user supplies a specific job URL and asks to apply, use
  start_job_application(job_url). Do not search for other jobs or collect a
  target role/location first. The tool wakes the browser and starts a reviewed
  application task for that exact link. Preparation is not submission.
- For auto_apply without a specific link, collect search_query (target role), location,
  and optionally
  max_applications (1–5). This pipeline searches for jobs and prepares child
  application workflows; it does not accept saved-job ids or schedule a week
  of work. Never claim those unsupported actions are done.
- For a generic company career page or portal browser task, start computer_task
  with context {{"task":"the user's requested work"}}. The tool wakes their computer
  on demand. Each browser change pauses for approval. Login
  is private: direct the user to saved logins or human takeover in the panel;
  never ask for a password in chat. Saved resume uploads are supported through
  reviewed browser steps. Users can also upload a saved resume while holding
  control in the browser panel. Tell them to save a resume in the app first.
- When asked about a run's progress, use get_run_status. If a run is
  awaiting_approval, tell the user exactly that an approval card / the approval
  page is where they accept or reject it. You can NEVER approve, reject,
  cancel, or submit anything yourself, and you must never claim you did.
- Never fabricate applications, emails, job listings, or statuses — report
  only what tools return. If a tool returns an error, explain it plainly.
- Keep answers short and concrete. Use markdown lists when enumerating.
- If the user asks you to do something the tools cannot do, say so and point
  them to the right page (Jobs, Applications, Agents, Settings).

{SECURITY_RULES}"""


def _user_id() -> str | None:
    return current_user_id.get()


async def _get_user(db, user_id: str):
    from sqlalchemy import select

    from app.models.db import User

    result = await db.execute(select(User).where(User.clerk_user_id == user_id))
    return result.scalars().first()


def _compact(value: Any, limit: int = 2000) -> Any:
    """Truncate long strings / nested payloads before they reach the model."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return value if not isinstance(value, str) else text
    return text[:limit] + "…[truncated]"


# ── Tools ────────────────────────────────────────────────────────


@tool
async def start_agent_run(task_type: str, context: dict | None = None) -> str:
    """Start a CareerCraft agent run for the signed-in user.

    Args:
        task_type: one of job_search, auto_apply, resume_optimize,
            cover_letter, interview_prep, interview_coach, company_research,
            salary_intelligence, email, email_monitor, linkedin_optimize,
            linkedin_outreach, computer_task.
        context: task context, e.g. {"query": "Senior Python Engineer",
            "location": "Remote"} for job_search. Keep it small.
    """
    user_id = _user_id()
    if not user_id:
        return json.dumps({"error": "Chat session is not authenticated."})
    if task_type not in ALLOWED_TASK_TYPES:
        return json.dumps(
            {
                "error": f"task_type {task_type!r} is not allowed from chat.",
                "allowed": sorted(ALLOWED_TASK_TYPES),
            }
        )
    context = context or {}
    if not isinstance(context, dict):
        return json.dumps({"error": "context must be an object."})
    if len(json.dumps(context, default=str)) > _MAX_CONTEXT_CHARS:
        return json.dumps({"error": "context is too large."})
    from app.api.v1.run_utils import queue_agent_run
    from app.core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            user = await _get_user(db, user_id)
            if user is None:
                return json.dumps({"error": "User account not found."})
            if task_type == "computer_task":
                task = context.get("task")
                if not isinstance(task, str) or not task.strip():
                    return json.dumps({"error": "Describe the browser task in context.task."})
                from app.services.computer_service import audit, computer_backend, dispatch

                state = await computer_backend(user.id)
                await dispatch(user.id, "start", state=state)
                await audit(user.id, "start", backend=state["backend"])
                try:
                    run_id = await queue_agent_run(db, user, task_type, context)
                except Exception:
                    # No run will drive it: don't leave the browser open.
                    with contextlib.suppress(Exception):
                        await dispatch(user.id, "stop", state=state)
                    raise
            else:
                run_id = await queue_agent_run(db, user, task_type, context)
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        if detail is not None:
            # HTTPException from queue_agent_run (validation, concurrency 429,
            # workflow unavailable) — surface its client-safe detail.
            return json.dumps({"error": _compact(detail, 500)})
        logger.warning("chat start_agent_run failed: %s", type(exc).__name__)
        return json.dumps({"error": "Could not start the run — try again shortly."})
    return json.dumps(
        {
            "run_id": run_id,
            "task_type": task_type,
            "status": "queued",
            "note": "Run queued. The user can watch it on the Agents page; "
            "ask for its status with get_run_status. If it ends up "
            "awaiting_approval, the user must approve it in the UI — you "
            "cannot approve it.",
        }
    )


@tool
async def start_job_application(job_url: str) -> str:
    """Prepare an application to the exact job link supplied by the user.

    Wakes their browser on demand. Browser mutations and submission still
    require the user's approval. Login needs private human help; saved resume
    uploads are supported after review.
    """
    from app.services.computer_service import AGENT_WRITES, ComputerAction

    try:
        ComputerAction(operation="navigate", parameters={"url": job_url}).validate_operation(
            AGENT_WRITES
        )
    except ValueError:
        return json.dumps(
            {"error": "Provide a valid HTTP(S) job URL without embedded credentials."}
        )
    return await start_agent_run.ainvoke(
        {
            "task_type": "computer_task",
            "context": {
                "task": "Prepare my application for this exact job: "
                + job_url
                + ". Open this link first. Use my saved profile and resume facts. "
                "Ask for missing information; never invent answers. "
                "Pause for my approval before browser changes and final submission.",
                "job_url": job_url,
            },
        }
    )


@tool
async def get_run_status(run_id: str) -> str:
    """Check the status of one of the user's agent runs.

    Args:
        run_id: the run id returned by start_agent_run.
    """
    user_id = _user_id()
    if not user_id:
        return json.dumps({"error": "Chat session is not authenticated."})
    try:
        row_id = uuid.UUID(str(run_id))
    except ValueError:
        return json.dumps({"error": "run_id must be a UUID."})
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun

    async with AsyncSessionLocal() as db:
        user = await _get_user(db, user_id)
        if user is None:
            return json.dumps({"error": "User account not found."})
        result = await db.execute(
            select(AgentRun).where(AgentRun.id == row_id, AgentRun.user_id == user.id)
        )
        run = result.scalars().first()
    if run is None:
        return json.dumps({"error": "Run not found."})
    pending = None
    if run.status == "awaiting_approval" and isinstance(run.output, dict):
        # Only the action type plus a short summary reach the model; the full
        # payload stays in the UI approval card.
        pending = {
            "action_type": run.output.get("type") or run.output.get("action_type"),
            "summary": _compact(run.output.get("summary") or run.output.get("details"), 400),
        }
    return json.dumps(
        {
            "run_id": str(run.id),
            "task_type": run.agent_type,
            "status": run.status,
            "pending_action": pending,
            "result": _compact(run.output, 2000) if run.status == "completed" else None,
            "error": "Agent failed" if run.status == "failed" else None,
        }
    )


@tool
async def list_recent_runs(limit: int = 5) -> str:
    """List the user's most recent agent runs.

    Args:
        limit: how many to return (max 25).
    """
    user_id = _user_id()
    if not user_id:
        return json.dumps({"error": "Chat session is not authenticated."})
    limit = max(1, min(int(limit), _MAX_LIST_RESULTS))
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun

    async with AsyncSessionLocal() as db:
        user = await _get_user(db, user_id)
        if user is None:
            return json.dumps({"error": "User account not found."})
        result = await db.execute(
            select(AgentRun)
            .where(AgentRun.user_id == user.id)
            .order_by(AgentRun.started_at.desc())
            .limit(limit)
        )
        runs = result.scalars().all()
    return json.dumps(
        {
            "runs": [
                {
                    "run_id": str(run.id),
                    "task_type": run.agent_type,
                    "status": run.status,
                    "started_at": (run.started_at.isoformat() if run.started_at else None),
                    "completed_at": (run.completed_at.isoformat() if run.completed_at else None),
                }
                for run in runs
            ]
        }
    )


@tool
async def list_applications(limit: int = 10) -> str:
    """List the user's tracked job applications, newest first.

    Args:
        limit: how many to return (max 25).
    """
    user_id = _user_id()
    if not user_id:
        return json.dumps({"error": "Chat session is not authenticated."})
    limit = max(1, min(int(limit), _MAX_LIST_RESULTS))
    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.db import JobApplication

    async with AsyncSessionLocal() as db:
        user = await _get_user(db, user_id)
        if user is None:
            return json.dumps({"error": "User account not found."})
        result = await db.execute(
            select(JobApplication)
            .where(JobApplication.user_id == user.id)
            .order_by(JobApplication.found_at.desc())
            .limit(limit)
        )
        apps = result.scalars().all()
    return json.dumps(
        {
            "applications": [
                {
                    "id": str(app.id),
                    "company": app.company,
                    "role": app.role,
                    "location": app.location,
                    "status": app.status,
                    "match_score": app.match_score,
                    "job_url": app.job_url,
                    "applied_at": (app.applied_at.isoformat() if app.applied_at else None),
                }
                for app in apps
            ]
        }
    )


@tool
async def search_jobs_now(keywords: str, location: str = "Remote") -> str:
    """Run a quick keyless job-board search and return live listings.

    For deeper scored search (resume match, more platforms), start a
    job_search run instead.

    Args:
        keywords: what to search for, e.g. "Senior Python Engineer".
        location: e.g. "Remote", "Bengaluru", "New York".
    """
    user_id = _user_id()
    if not user_id:
        return json.dumps({"error": "Chat session is not authenticated."})
    keywords = str(keywords).strip()[:100]
    location = str(location).strip()[:100] or "Remote"
    if len(keywords) < 2:
        return json.dumps({"error": "keywords is too short."})
    from app.services.job_search_service import search_all_platforms

    jobs, warnings = await search_all_platforms(
        {"search_query": keywords, "location": location, "max_results": 5},
        # Keyless public boards only — the chat tool must never spend the
        # operator's paid third-party quota (same contract as the demo API).
        platforms=["open_apis_keyless"],
        timeout_s=20,
    )
    return json.dumps(
        {
            "jobs": [
                {
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "url": job.get("url") or job.get("job_url"),
                    "platform": job.get("platform"),
                }
                for job in jobs[:5]
            ],
            "warnings": warnings[:3] or None,
        }
    )


TOOLS = [
    start_agent_run,
    start_job_application,
    get_run_status,
    list_recent_runs,
    list_applications,
    search_jobs_now,
]


# ── Graph ────────────────────────────────────────────────────────


chat_execution_id: ContextVar[uuid.UUID | None] = ContextVar("chat_execution_id", default=None)


class ChatPromptTooLarge(ValueError):
    pass


def bounded_prompt(messages: list) -> list:
    """Keep recent whole user turns, including every tool call/result pair.

    Transcript retention is independent of model context. Conservative JSON
    sizing leaves room for tool schemas and gateway envelope fields.
    """
    system = SystemMessage(content=_SYSTEM_PROMPT)
    groups = []
    for message in messages:
        if getattr(message, "type", "") == "human" or not groups:
            groups.append([])
        groups[-1].append(message)
    selected = []
    for group in reversed(groups):
        # Interrupted historical turns can contain a call without its result.
        # Omit the whole turn; replaying or inventing a result is unsafe.
        pending = set()
        complete = True
        for message in group:
            calls = getattr(message, "tool_calls", [])
            if calls:
                if pending:
                    complete = False
                pending.update(call["id"] for call in calls)
            if getattr(message, "type", "") == "tool":
                call_id = message.tool_call_id
                if call_id not in pending:
                    complete = False
                pending.discard(call_id)
        if pending or not complete:
            continue
        candidate = [system, *group, *selected]
        serialized = json.dumps(convert_to_openai_messages(candidate), ensure_ascii=True).encode()
        if len(candidate) > 90 or len(serialized) > 180000:
            break
        selected = [*group, *selected]
    if not selected:
        raise ChatPromptTooLarge(
            "The latest chat turn is too large. Start a new chat with a shorter message."
        )
    return with_security_rules([system, *selected])


async def _log_turn(
    user_id: str,
    response: AIMessage,
    *,
    status: str = "completed",
):
    """Accumulate model usage on the already-admitted whole-turn run."""
    from sqlalchemy import func, select, update

    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun as AgentRunRow
    from app.models.db import User

    execution_id = chat_execution_id.get()
    if execution_id is None:
        return
    usage = getattr(response, "usage_metadata", None) or {}
    tokens = int(usage.get("total_tokens", 0) or 0)
    output = {"preview": _compact(response.content, 500)}
    if status == "failed":
        output = {"error": "Chat model unavailable"}
    async with AsyncSessionLocal() as db:
        # Ownership remains enforced even if called outside the HTTP adapter.
        await db.execute(
            update(AgentRunRow)
            .where(
                AgentRunRow.id == execution_id,
                AgentRunRow.user_id.in_(select(User.id).where(User.clerk_user_id == user_id)),
            )
            .values(
                tokens_used=func.coalesce(AgentRunRow.tokens_used, 0) + tokens,
                output=output,
            )
        )
        await db.commit()


async def agent_node(state: MessagesState) -> dict:
    messages = state["messages"]
    user_id = _user_id()
    if not user_id:
        return {"messages": [AIMessage(content=UNAUTHENTICATED_REPLY)]}

    from fastapi import HTTPException

    from app.core.database import AsyncSessionLocal
    from app.core.llm_gateway import get_chat_gateway_llm

    status = "completed"
    async with AsyncSessionLocal() as db:
        user = await _get_user(db, user_id)
        if user is None:
            return {"messages": [AIMessage(content="User account not found.")]}
        try:
            # Settings and token budgets use the database UUID, while request
            # identity and the chat tools use Clerk's authenticated subject.
            llm = await get_chat_gateway_llm(str(user.id), db)
            response = await llm.bind_tools(TOOLS).ainvoke(bounded_prompt(messages))
        except ChatPromptTooLarge as exc:
            status = "failed"
            response = AIMessage(content=str(exc))
        except HTTPException as exc:
            status = "failed"
            message = "Could not reach your configured model. Try again shortly."
            if exc.status_code == 429:
                message = "Your daily token budget is used up. Try again tomorrow."
            elif exc.status_code == 400:
                message = "No active model is configured. Set one up in Settings → Models."
            response = AIMessage(content=message)
        except Exception as exc:
            status = "failed"
            logger.warning("chat model failed: %s", type(exc).__name__)
            response = AIMessage(
                content="Could not reach your configured model. Try again shortly."
            )

    await _log_turn(user_id, response, status=status)
    return {"messages": [response]}


def _should_continue(state: MessagesState) -> str:
    return tools_condition(state)


def build_chat_graph():
    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(TOOLS, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    # tools_condition routes to "tools" when the last AIMessage has tool_calls,
    # else to END. Tool results always loop back into the agent node.
    builder.add_conditional_edges("agent", _should_continue, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=InMemorySaver())
