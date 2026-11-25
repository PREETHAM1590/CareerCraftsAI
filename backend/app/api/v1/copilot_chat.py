"""AG-UI endpoint for the Nova chat graph.

Serves the chat orchestrator over the AG-UI protocol so the CopilotKit React
SDK can talk to it directly. Mounted at /api/v1/agents/chat — a non-public
path, so the app-level Clerk JWT middleware authenticates every request and
records the subject into request_context for the graph's tools.

The database stores owner-scoped messages and restores them into the graph
after restarts; thread ids live in the browser localStorage.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import anyio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.v1.deps import get_current_user
from app.core.request_context import current_user_id
from app.services import copilot_history as history

logger = logging.getLogger(__name__)

COPILOT_CHAT_PATH = "/api/v1/agents/chat"
COPILOT_AGENT_NAME = "career-copilot"


def scoped_thread_id(user_id: str, thread_id: str) -> str:
    """Checkpoint keys belong to an authenticated user, never a bare client id."""
    return hashlib.sha256(f"{len(user_id)}:{user_id}{thread_id}".encode()).hexdigest()


def mount_copilot_chat(app: FastAPI) -> None:
    """Wire the chat graph as an AG-UI FastAPI endpoint."""
    try:
        from ag_ui.core import EventType, RunAgentInput, RunErrorEvent
        from ag_ui.encoder import EventEncoder
        from ag_ui_langgraph import LangGraphAgent
    except ImportError:  # pragma: no cover - depends on optional dependency
        logger.warning("ag-ui-langgraph not installed — /agents/chat disabled")
        return

    from app.agents.chat_orchestrator import build_chat_graph, chat_execution_id

    # Define the route here so identity and checkpoint ownership are enforced
    # before the adapter reads any history. Keep the public thread id on wire.
    async def chat_endpoint(input_data: RunAgentInput, request: Request):
        user_id = current_user_id.get()
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not input_data.thread_id or len(input_data.thread_id) > 200:
            raise HTTPException(status_code=422, detail="Invalid thread id")
        if not input_data.run_id or len(input_data.run_id) > 200:
            raise HTTPException(status_code=422, detail="Invalid run id")
        db_user_id, messages, execution_id = await history.start_turn(
            user_id,
            input_data.thread_id,
            input_data.run_id,
            [
                m.model_dump(mode="json", by_alias=True, exclude_none=True)
                for m in input_data.messages
            ],
        )
        from ag_ui.core import Message
        from pydantic import TypeAdapter

        scoped_input = input_data.model_copy(
            update={
                "thread_id": scoped_thread_id(user_id, input_data.thread_id),
                # Chat has no client tools or graph-control surface. In particular,
                # commands must never jump straight to a graph's tools node.
                "messages": TypeAdapter(list[Message]).validate_python(messages),
                "state": {},
                "tools": [],
                "forwarded_props": {},
            }
        )
        encoder = EventEncoder(accept=request.headers.get("accept"))
        request_agent = LangGraphAgent(
            name=COPILOT_AGENT_NAME,
            graph=build_chat_graph(),
            emit_raw_events=False,
            config={"recursion_limit": 20},
        )

        async def events():
            snapshot = None
            status = "failed"
            token = chat_execution_id.set(execution_id)
            try:
                # Request-owned checkpoints live only for this bounded stream.
                # SQL restores the durable transcript on every request.
                async with asyncio.timeout(300):
                    async for event in request_agent.run(scoped_input):
                        if event.type == "MESSAGES_SNAPSHOT":
                            snapshot = [
                                m.model_dump(mode="json", by_alias=True, exclude_none=True)
                                for m in event.messages
                            ]
                        if event.type == "RUN_FINISHED":
                            status = "completed"
                        if event.type == "RUN_ERROR":
                            status = "failed"
                            event = event.model_copy(
                                update={"message": "Chat unavailable. Try again shortly."}
                            )
                        if hasattr(event, "thread_id"):
                            event = event.model_copy(update={"thread_id": input_data.thread_id})
                        yield encoder.encode(event)
            except TimeoutError:
                yield encoder.encode(
                    RunErrorEvent(
                        type=EventType.RUN_ERROR,
                        message="Chat timed out. Try again shortly.",
                    )
                )
            finally:
                chat_execution_id.reset(token)
                with anyio.CancelScope(shield=True):
                    await history.finish_turn(
                        db_user_id,
                        input_data.thread_id,
                        input_data.run_id,
                        snapshot,
                        execution_id=execution_id,
                        status=status,
                    )

        return StreamingResponse(events(), media_type=encoder.get_content_type())

    # RunAgentInput is an optional dependency imported above; resolve the local
    # annotation explicitly for FastAPI (this module uses future annotations).
    chat_endpoint.__annotations__["input_data"] = RunAgentInput
    app.post(COPILOT_CHAT_PATH, dependencies=[Depends(get_current_user)])(chat_endpoint)

    @app.get(f"{COPILOT_CHAT_PATH}/threads", dependencies=[Depends(get_current_user)])
    async def threads():
        user_id = current_user_id.get()
        if not user_id:
            raise HTTPException(401, "Authentication required")
        return await history.list_threads(user_id)

    @app.get(
        f"{COPILOT_CHAT_PATH}/threads/{{thread_id}}",
        dependencies=[Depends(get_current_user)],
    )
    async def thread(thread_id: str):
        user_id = current_user_id.get()
        if not user_id:
            raise HTTPException(401, "Authentication required")
        return await history.get_thread(user_id, thread_id)

    @app.get(f"{COPILOT_CHAT_PATH}/health", dependencies=[Depends(get_current_user)])
    def health():
        return {"status": "ok", "agent": {"name": COPILOT_AGENT_NAME}}

    logger.info("Copilot chat endpoint mounted at %s", COPILOT_CHAT_PATH)
