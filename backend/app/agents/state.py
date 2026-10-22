from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentEvent(TypedDict):
    event_type: Literal["thinking", "tool_call", "tool_result", "checkpoint", "complete", "error"]
    payload: dict
    timestamp: str


class CheckpointPayload(TypedDict):
    action_type: str
    details: dict


class _AgentStateRequired(TypedDict):
    """Fields every node may read — KeyError-safe only when these are always present."""
    user_id: str
    run_id: str


class AgentState(_AgentStateRequired, total=False):
    """Shared state passed between LangGraph nodes.

    Required fields (user_id, run_id) are declared in _AgentStateRequired so
    that TypedDict enforcement makes them non-optional.  All other fields are
    optional (total=False) to allow partial updates at each node.

    messages holds langchain BaseMessage objects (AIMessage, HumanMessage, etc.)
    not plain dicts — the list[dict] annotation in the original schema was wrong
    and caused silent type mismatches at serialisation time.
    """
    task: str
    task_type: str
    status: str
    context: dict
    # Holds langchain BaseMessage instances (AIMessage, HumanMessage, etc.)
    messages: list[Any]
    pending_action: dict | None
    result: dict | None
    error: str | None
    # Resolved ORM settings row; nodes fall back to the DB when absent.
    model_settings: Any
    # Total tokens consumed by this run — written by ResumeAgent and any agent
    # that tracks token usage; read by agent_runs_repository for billing records.
    tokens_used: int
