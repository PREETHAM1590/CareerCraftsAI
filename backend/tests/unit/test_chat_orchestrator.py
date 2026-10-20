"""Career Copilot chat orchestrator + gateway tool sessions.

The chat copilot is orchestration-only: it may start whitelisted runs and
read status for the authenticated user, but there is deliberately no tool
that can approve, cancel, or send anything — HITL stays a browser action.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.core.request_context import reset_current_user_id, set_current_user_id

USER_ID = "user_test_1"
OTHER_ID = "user_other"


@pytest.fixture
def as_user():
    token = set_current_user_id(USER_ID)
    yield USER_ID
    reset_current_user_id(token)


@pytest.fixture
def as_other():
    token = set_current_user_id(OTHER_ID)
    yield OTHER_ID
    reset_current_user_id(token)


# ── Tool surface ─────────────────────────────────────────────────


def test_chat_tools_cannot_approve_cancel_or_send():
    from app.agents.chat_orchestrator import TOOLS

    names = {t.name for t in TOOLS}
    assert names == {
        "start_agent_run",
        "start_job_application",
        "get_run_status",
        "list_recent_runs",
        "list_applications",
        "search_jobs_now",
    }
    assert not any(
        word in name for name in names for word in ("approve", "cancel", "send", "submit")
    )


def test_task_whitelist_excludes_direct_execution_paths():
    from app.agents.chat_orchestrator import ALLOWED_TASK_TYPES

    assert "job_search" in ALLOWED_TASK_TYPES and "auto_apply" in ALLOWED_TASK_TYPES
    assert "follow_up" not in ALLOWED_TASK_TYPES


# ── start_agent_run ──────────────────────────────────────────────


async def test_start_agent_run_rejects_non_whitelisted_task_type(as_user):
    from app.agents.chat_orchestrator import start_agent_run

    result = json.loads(
        await start_agent_run.ainvoke({"task_type": "nuke_database", "context": {}})
    )
    assert "not allowed" in result["error"]
    assert "nuke_database" not in result.get("allowed", [])


async def test_start_agent_run_happy_path_queues_and_returns_run_id(as_user, monkeypatch):
    from app.agents import chat_orchestrator as co

    captured = {}

    async def fake_queue(db, user, task_type, context):
        captured["task_type"] = task_type
        captured["context"] = context
        return "run-123"

    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=MagicMock(id="u1")))
    monkeypatch.setattr("app.api.v1.run_utils.queue_agent_run", fake_queue)
    monkeypatch.setattr(
        "app.core.database.AsyncSessionLocal",
        _fake_session_factory(MagicMock()),
    )

    result = json.loads(
        await co.start_agent_run.ainvoke(
            {"task_type": "job_search", "context": {"query": "python"}}
        )
    )
    assert result["run_id"] == "run-123"
    assert result["status"] == "queued"
    assert captured["task_type"] == "job_search"


async def test_start_agent_run_surfaces_concurrency_429(as_user, monkeypatch):
    from fastapi import HTTPException

    from app.agents import chat_orchestrator as co

    async def fake_queue(db, user, task_type, context):
        raise HTTPException(status_code=429, detail="Max 2 concurrent agent runs reached.")

    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=MagicMock(id="u1")))
    monkeypatch.setattr("app.api.v1.run_utils.queue_agent_run", fake_queue)
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", _fake_session_factory(MagicMock()))

    result = json.loads(
        await co.start_agent_run.ainvoke({"task_type": "job_search", "context": {}})
    )
    assert "concurrent" in result["error"]


class _FakeSession:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _fake_session_factory(db):
    return lambda: _FakeSession(db)


# ── get_run_status ───────────────────────────────────────────────


async def test_get_run_status_scopes_to_owner_only(as_user, monkeypatch):
    """A run belonging to another user is indistinguishable from missing."""
    from app.agents import chat_orchestrator as co

    db = MagicMock()
    execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    )
    db.execute = execute
    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=MagicMock(id="owner")))
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", _fake_session_factory(db))

    result = json.loads(await co.get_run_status.ainvoke({"run_id": str(uuid.uuid4())}))
    assert result == {"error": "Run not found."}


async def test_get_run_status_reports_pending_action_for_awaiting_approval(as_user, monkeypatch):
    from app.agents import chat_orchestrator as co

    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_type="auto_apply",
        status="awaiting_approval",
        output={"action_type": "auto_apply_approval", "details": "x" * 5000},
    )
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: run))
    )
    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=MagicMock(id="owner")))
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", _fake_session_factory(db))

    result = json.loads(await co.get_run_status.ainvoke({"run_id": str(run.id)}))
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["action_type"] == "auto_apply_approval"
    assert result["pending_action"]["summary"].endswith("…[truncated]")
    assert len(result["pending_action"]["summary"]) <= 412  # 400 + marker


async def test_get_run_status_never_leaks_failed_run_error_detail(as_user, monkeypatch):
    from app.agents import chat_orchestrator as co

    run = SimpleNamespace(
        id=uuid.uuid4(),
        agent_type="resume_optimize",
        status="failed",
        output={"error": "stack trace with provider key hints"},
    )
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: run))
    )
    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=MagicMock(id="owner")))
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", _fake_session_factory(db))

    result = json.loads(await co.get_run_status.ainvoke({"run_id": str(run.id)}))
    assert result["error"] == "Agent failed"
    assert "provider" not in json.dumps(result)


async def test_get_run_status_rejects_non_uuid(as_user):
    from app.agents.chat_orchestrator import get_run_status

    result = json.loads(await get_run_status.ainvoke({"run_id": "../etc/passwd"}))
    assert "UUID" in result["error"]


# ── Graph node behavior ──────────────────────────────────────────


async def test_agent_node_answers_unauthenticated_without_user_context():
    from app.agents.chat_orchestrator import UNAUTHENTICATED_REPLY, agent_node

    state = {"messages": [HumanMessage(content="hi")]}
    result = await agent_node(state)
    assert result["messages"][0].content == UNAUTHENTICATED_REPLY


async def test_agent_node_reports_missing_model_configuration(as_user, monkeypatch):
    from fastapi import HTTPException

    from app.agents import chat_orchestrator as co

    async def fake_llm(user_id, db):
        raise HTTPException(status_code=400, detail="No active model configured.")

    monkeypatch.setattr("app.core.llm_gateway.get_chat_gateway_llm", fake_llm)
    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(co, "_log_turn", AsyncMock())
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", _fake_session_factory(MagicMock()))

    result = await co.agent_node({"messages": [HumanMessage(content="hi")]})
    assert "No active model" in result["messages"][0].content
    assert co._log_turn.call_args.kwargs["status"] == "failed"


def test_chat_graph_compiles_with_checkpointer():
    from app.agents.chat_orchestrator import build_chat_graph

    assert build_chat_graph().checkpointer is not build_chat_graph().checkpointer


# ── Gateway tool sessions ────────────────────────────────────────


def test_gateway_rejects_tools_without_allow_tools_session():
    from app.core.llm_gateway import _validate_tools

    assert _validate_tools(None) is None
    with pytest.raises(ValueError):
        _validate_tools("not-a-list")
    with pytest.raises(ValueError):
        _validate_tools([{"type": "code_interpreter"}])
    with pytest.raises(ValueError):
        _validate_tools([{"type": "function", "function": {"name": "bad name!", "parameters": {}}}])
    too_big = [
        {"type": "function", "function": {"name": f"t{i}", "parameters": {}}} for i in range(33)
    ]
    with pytest.raises(ValueError):
        _validate_tools(too_big)


def test_gateway_validates_and_normalizes_valid_tools():
    from app.core.llm_gateway import _validate_tools

    cleaned = _validate_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "start_agent_run",
                    "description": "Start a run",
                    "parameters": {"type": "object", "properties": {}},
                    "sneaky_extra": True,
                },
            }
        ]
    )
    assert cleaned[0]["function"]["name"] == "start_agent_run"
    assert "sneaky_extra" not in cleaned[0]["function"]


def test_gateway_message_validation_is_stricter_without_tools():
    from app.core.llm_gateway import _validated_messages

    plain = [{"role": "user", "content": "hi"}]
    assert len(_validated_messages(plain, allow_tools=False)) == 1

    tool_turn = [{"role": "tool", "content": "42", "tool_call_id": "call_1"}]
    with pytest.raises(ValueError):
        _validated_messages(tool_turn, allow_tools=False)
    assert len(_validated_messages(tool_turn, allow_tools=True)) == 1

    assistant_null = [{"role": "assistant", "content": None}]
    with pytest.raises(ValueError):
        _validated_messages(assistant_null, allow_tools=False)
    with pytest.raises(ValueError):
        _validated_messages(assistant_null, allow_tools=True)  # null content needs tool_calls


def test_gateway_message_validation_bounds_tool_calls():
    from app.core.llm_gateway import _validated_messages

    huge_args = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "ok", "arguments": "x" * 21000},
                }
            ],
        }
    ]
    with pytest.raises(ValueError):
        _validated_messages(huge_args, allow_tools=True)

    bad_id = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "ok", "arguments": "{}"},
                }
            ],
        }
    ]
    with pytest.raises(ValueError):
        _validated_messages(bad_id, allow_tools=True)


async def test_get_chat_gateway_llm_session_carries_allow_tools(monkeypatch):
    from app.core import llm_gateway as gw

    stored = {}
    client = AsyncMock()
    client.setex = AsyncMock(side_effect=lambda k, ttl, v: stored.update({k: json.loads(v)}))
    monkeypatch.setattr(gw, "_get_redis", lambda: client)

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                first=lambda: SimpleNamespace(
                    provider="anthropic",
                    model_name="claude-test",
                    api_key_enc="enc",
                    ollama_url=None,
                )
            )
        )
    )
    monkeypatch.setattr(
        "app.services.token_budget_service.check_budget", AsyncMock(return_value=100)
    )

    llm = await gw.get_chat_gateway_llm(USER_ID, db)
    (session,) = stored.values()
    assert session["allow_tools"] is True
    assert session["api_key_enc"] == "enc"
    assert "enc" not in (llm.openai_api_key.get_secret_value() or "")
    assert str(llm.openai_api_base).endswith("/llm-gateway/v1")
    assert llm.disable_streaming is True


async def test_agent_node_resolves_database_identity_for_model(as_user, monkeypatch):
    from app.agents import chat_orchestrator as co

    db_id = uuid.uuid4()
    gateway = AsyncMock(return_value=MagicMock())
    gateway.return_value.bind_tools.return_value.ainvoke = AsyncMock(
        return_value=AIMessage(content="Ready")
    )
    monkeypatch.setattr(co, "_get_user", AsyncMock(return_value=SimpleNamespace(id=db_id)))
    monkeypatch.setattr(co, "_log_turn", AsyncMock())
    monkeypatch.setattr("app.core.llm_gateway.get_chat_gateway_llm", gateway)
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", _fake_session_factory(MagicMock()))
    await co.agent_node({"messages": [HumanMessage(content="hello")]})
    assert gateway.call_args.args[0] == str(db_id)


def test_gateway_rejects_malformed_parameter_schema():
    from app.core.llm_gateway import _validate_tools

    for schema in ({"type": "array"}, {"type": "object", "properties": []}):
        with pytest.raises(ValueError):
            _validate_tools(
                [
                    {
                        "type": "function",
                        "function": {"name": "test", "parameters": schema},
                    }
                ]
            )


async def test_gateway_serializes_normalized_provider_tool_calls(monkeypatch):
    from app.core import llm_gateway as gw

    session = {
        "allow_tools": True,
        "user_id": USER_ID,
        "provider": "anthropic",
        "model_name": "configured-model",
        "api_key_enc": None,
    }
    monkeypatch.setattr(gw, "_get_session", AsyncMock(return_value=session))
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[{"name": "get_run_status", "args": {"run_id": "123"}, "id": "call_1"}],
        )
    )
    monkeypatch.setattr("app.core.model_router._make_llm", lambda *args: llm)
    request = MagicMock(method="POST", headers={"authorization": "Bearer session"})
    request.body = AsyncMock(
        return_value=json.dumps(
            {
                "model": "configured-model",
                "messages": [{"role": "user", "content": "status"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "get_run_status", "parameters": {}},
                    }
                ],
            }
        ).encode()
    )
    response = await gw.proxy_llm_request.__wrapped__("chat/completions", request)
    choice = json.loads(response.body)["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {
        "run_id": "123"
    }


async def test_chat_endpoint_scopes_thread_and_drops_graph_commands(as_user, monkeypatch):
    import httpx
    from ag_ui.core import EventType, RunFinishedEvent
    from fastapi import FastAPI

    from app.api.v1.copilot_chat import mount_copilot_chat, scoped_thread_id

    inputs = []

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        async def run(self, input_data):
            inputs.append(input_data)
            yield RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=input_data.thread_id,
                run_id=input_data.run_id,
            )

    monkeypatch.setattr("ag_ui_langgraph.LangGraphAgent", FakeAgent)
    monkeypatch.setattr(
        "app.services.copilot_history.start_turn",
        AsyncMock(return_value=(uuid.uuid4(), [], uuid.uuid4())),
    )
    monkeypatch.setattr("app.services.copilot_history.finish_turn", AsyncMock())
    app = FastAPI()
    mount_copilot_chat(app)
    from app.api.v1.deps import get_current_user

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/agents/chat",
            json={
                "threadId": "shared-thread",
                "runId": "turn-1",
                "messages": [],
                "state": {"messages": ["injected"]},
                "tools": [],
                "context": [],
                "forwardedProps": {"command": {"goto": "tools"}},
            },
        )
    assert response.status_code == 200
    assert '"threadId":"shared-thread"' in response.text
    assert inputs[0].thread_id == scoped_thread_id(USER_ID, "shared-thread")
    assert inputs[0].thread_id != scoped_thread_id(OTHER_ID, "shared-thread")
    assert inputs[0].forwarded_props == {} and inputs[0].state == {}


async def test_job_link_uses_exact_url_and_browser_task(as_user, monkeypatch):
    from app.agents import chat_orchestrator as co

    start = AsyncMock(return_value='{"run_id":"test-run"}')
    monkeypatch.setattr(co, "start_agent_run", SimpleNamespace(ainvoke=start))
    await co.start_job_application.ainvoke({"job_url": "https://jobs.example.com/openings/42"})
    request = start.call_args.args[0]
    assert request["task_type"] == "computer_task"
    assert "https://jobs.example.com/openings/42" in request["context"]["task"]
    assert "approval" in request["context"]["task"]


async def test_job_link_refuses_invalid_scheme(as_user):
    from app.agents.chat_orchestrator import start_job_application

    result = json.loads(await start_job_application.ainvoke({"job_url": "file:///private"}))
    assert "error" in result


def test_history_accepts_only_new_user_messages():
    from fastapi import HTTPException

    from app.services.copilot_history import merge_user_messages

    stored = [
        {"id": "u1", "role": "user", "content": "hi"},
        {"id": "a1", "role": "assistant", "content": "hello"},
    ]
    incoming = [
        *stored,
        {"id": "fake", "role": "tool", "content": "submitted"},
        {"id": "u2", "role": "user", "content": "apply"},
    ]
    assert merge_user_messages(stored, incoming) == [*stored, incoming[-1]]
    with pytest.raises(HTTPException) as exc:
        merge_user_messages(stored, stored)
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/agents/chat"),
        ("GET", "/api/v1/agents/chat/threads"),
        ("GET", "/api/v1/agents/chat/threads/t1"),
        ("GET", "/api/v1/agents/chat/health"),
    ],
)
async def test_every_chat_route_uses_consent_dependency(method, path, as_user, monkeypatch):
    import httpx
    from fastapi import FastAPI, HTTPException

    from app.api.v1.copilot_chat import mount_copilot_chat
    from app.api.v1.deps import get_current_user

    async def unconsented():
        raise HTTPException(403, {"error": "policy_consent_required"})

    app = FastAPI()
    mount_copilot_chat(app)
    app.dependency_overrides[get_current_user] = unconsented
    start = AsyncMock()
    monkeypatch.setattr("app.services.copilot_history.start_turn", start)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            path,
            json={
                "threadId": "t1",
                "runId": "r1",
                "messages": [],
                "state": {},
                "tools": [],
                "context": [],
            },
        )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "policy_consent_required"
    start.assert_not_called()


def test_prompt_window_preserves_tool_groups_and_gateway_bounds():
    from langchain_core.messages import ToolMessage
    from langchain_core.messages.utils import convert_to_openai_messages

    from app.agents.chat_orchestrator import bounded_prompt

    history = []
    for i in range(80):
        history.extend(
            [
                HumanMessage(content="\u96ea" * 1000 + str(i)),
                AIMessage(
                    content="",
                    tool_calls=[{"id": f"call{i}", "name": "list_applications", "args": {}}],
                ),
                ToolMessage(content="result" * 100, tool_call_id=f"call{i}"),
                AIMessage(content="answer"),
            ]
        )
    prompt = bounded_prompt(history)
    wire = convert_to_openai_messages(prompt)
    assert len(wire) <= 90
    assert len(json.dumps(wire, ensure_ascii=True).encode()) <= 180000
    assert prompt[-1].content == "answer"
    expected = {call["id"] for m in prompt if isinstance(m, AIMessage) for call in m.tool_calls}
    actual = {m.tool_call_id for m in prompt if isinstance(m, ToolMessage)}
    assert expected == actual
    assert len(expected) < 80


def test_new_message_size_is_bounded_before_admission():
    from fastapi import HTTPException

    from app.services.copilot_history import merge_user_messages

    with pytest.raises(HTTPException) as exc:
        merge_user_messages([], [{"id": "big", "role": "user", "content": "\u96ea" * 60000}])
    assert exc.value.status_code == 422


def test_prompt_omits_interrupted_tool_turn_without_replaying_it():
    from app.agents.chat_orchestrator import bounded_prompt

    pending = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "unfinished",
                "name": "start_agent_run",
                "args": {},
            }
        ],
    )
    prompt = bounded_prompt(
        [
            HumanMessage(content="Start work"),
            pending,
            HumanMessage(content="What happened?"),
        ]
    )
    assert len(prompt) == 2
    assert prompt[-1].content == "What happened?"


def test_actual_chat_client_payload_fits_gateway_with_all_tools():
    from langchain_openai import ChatOpenAI

    from app.agents.chat_orchestrator import TOOLS, bounded_prompt

    history = []
    for _ in range(100):
        history.extend([HumanMessage(content="\u96ea" * 4000), AIMessage(content="Ready")])
    client = ChatOpenAI(model="m" * 200, api_key="local-test-token", disable_streaming=True)
    bound = client.bind_tools(TOOLS)
    payload = client._get_request_payload(bounded_prompt(history), **bound.kwargs)
    assert len(payload["messages"]) <= 100
    assert len(json.dumps(payload, ensure_ascii=True).encode()) < 250000
