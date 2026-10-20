"""
llm_gateway.py — Internal LLM Gateway (proxy pattern).

Agents call this gateway with a session token instead of a real API key.
The gateway injects the real API key at the HTTP transport layer.

Even if an agent is fully compromised via prompt injection, it cannot
extract the API key because the key never exists in the agent's context.

Architecture:
    Agent → ChatOpenAI(base_url="http://localhost:8001/v1", api_key=session_token)
         → LLM Gateway (this service)
         → Injects real API key
         → Forwards to actual provider (OpenAI, Anthropic, etc.)

Usage:
    from app.core.llm_gateway import get_gateway_llm
    llm = await get_gateway_llm(user_id, db)
    # llm has NO access to the real API key — only a session token
"""

import hashlib
import hmac
import json
import logging
import re
import time

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response

from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import decrypt_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-gateway", tags=["llm-gateway"])

# Tool-calling sessions are opt-in per gateway session (chat copilot only).
# Plain agent sessions keep the original strict text-only contract.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_TOOLS = 32
_TOOLS_JSON_MAX_CHARS = 65536
_TOOL_DESCRIPTION_MAX_CHARS = 2048
_TOOL_CALL_ID_MAX_CHARS = 128
_TOOL_ARGS_MAX_CHARS = 20000

# Gateway sessions are stored in Redis (not in-process) so they are visible
# across all workers and expire automatically via TTL — no unbounded growth.
_SESSION_TTL_SECONDS = 3600
_SESSION_KEY_PREFIX = "llm_gw:session:"


def _get_redis() -> aioredis.Redis:
    from app.core.redis_client import get_redis

    return get_redis()


# Internal base URL the agent uses to reach this gateway. Configurable so it
# works in containerized/multi-host deploys (not hardcoded to localhost).
_GATEWAY_BASE_URL = settings.LLM_GATEWAY_URL


def _generate_session_token(user_id: str, provider: str) -> str:
    """Generate a unique short-lived session token for gateway auth.

    Includes a random nonce so two sessions for the same user/provider in the
    same second do not collide to the same token.
    """
    import secrets

    nonce = secrets.token_hex(8)
    payload = f"{user_id}:{provider}:{time.time()}:{nonce}"
    return hmac.HMAC(
        settings.APP_SECRET_KEY.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:48]


async def create_gateway_session(user_id: str, model_settings) -> dict:
    """Create a gateway session for a user. Returns session token + gateway URL.

    The session token is NOT the API key — it's a temporary credential
    that the gateway uses to look up the real key at request time. Stored in
    Redis with a TTL so it is visible across workers and auto-expires.
    """
    token = _generate_session_token(user_id, model_settings.provider)

    session_data = {
        "user_id": user_id,
        "provider": model_settings.provider,
        "model_name": model_settings.model_name,
        "api_key_enc": model_settings.api_key_enc,
        "ollama_url": model_settings.ollama_url,
        "created_at": time.time(),
    }

    r = _get_redis()
    await r.setex(
        f"{_SESSION_KEY_PREFIX}{token}",
        _SESSION_TTL_SECONDS,
        json.dumps(session_data),
    )

    return {
        "token": token,
        "gateway_url": _GATEWAY_BASE_URL,
        "model": model_settings.model_name,
    }


async def _get_session(token: str) -> dict | None:
    """Validate and return session data from Redis. None if expired/invalid."""
    if not token:
        return None
    r = _get_redis()
    raw = await r.get(f"{_SESSION_KEY_PREFIX}{token}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _validate_tools(raw_tools) -> list | None:
    """Validate an OpenAI ``tools`` array before it reaches the provider.

    Function-type tools only, bounded count and size, safe names, object JSON
    schemas. Unknown keys are dropped so only the validated shape is forwarded.
    """
    from jsonschema import Draft202012Validator, SchemaError

    if raw_tools is None:
        return None
    if not isinstance(raw_tools, list) or len(raw_tools) > _MAX_TOOLS:
        raise ValueError("Invalid tools")
    validated = []
    for tool in raw_tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("Invalid tools")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise ValueError("Invalid tools")
        name = function.get("name")
        description = function.get("description", "")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise ValueError("Invalid tools")
        if not isinstance(description, str) or len(description) > _TOOL_DESCRIPTION_MAX_CHARS:
            raise ValueError("Invalid tools")
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError("Invalid tools")
        if parameters:
            if parameters.get("type") != "object":
                raise ValueError("Tool parameters must be an object schema")
            try:
                Draft202012Validator.check_schema(parameters)
            except SchemaError:
                raise ValueError("Invalid tool parameter schema") from None
        cleaned: dict = {
            "type": "function",
            "function": {
                "name": name,
                "parameters": parameters or {"type": "object", "properties": {}},
            },
        }
        if description:
            cleaned["function"]["description"] = description
        validated.append(cleaned)
    if len(json.dumps(validated)) > _TOOLS_JSON_MAX_CHARS:
        raise ValueError("Invalid tools")
    return validated


def _validate_assistant_tool_calls(raw_calls) -> None:
    """Shape-check assistant ``tool_calls`` echoed back in chat history."""
    if raw_calls is None:
        return
    if not isinstance(raw_calls, list) or len(raw_calls) > _MAX_TOOLS:
        raise ValueError("Invalid tool call")
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError("Invalid tool call")
        function = call.get("function")
        if not isinstance(function, dict):
            raise ValueError("Invalid tool call")
        name = function.get("name")
        arguments = function.get("arguments")
        call_id = call.get("id")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise ValueError("Invalid tool call")
        if not isinstance(arguments, str) or len(arguments) > _TOOL_ARGS_MAX_CHARS:
            raise ValueError("Invalid tool call")
        if not isinstance(call_id, str) or not call_id or len(call_id) > _TOOL_CALL_ID_MAX_CHARS:
            raise ValueError("Invalid tool call")


def _validated_messages(raw_messages: list[dict], *, allow_tools: bool) -> list:
    """Shape-check message dicts, then convert to LangChain messages.

    Plain sessions keep the strict text-only contract. Tool sessions also
    accept assistant tool_calls and tool result turns — still text only.
    """
    for message in raw_messages:
        role = message.get("role")
        content = message.get("content")
        if role in ("system", "user"):
            if not isinstance(content, str):
                raise ValueError("Only text messages are supported")
        elif role == "assistant":
            if content is None:
                if not (allow_tools and message.get("tool_calls")):
                    raise ValueError("Only text messages are supported")
            elif not isinstance(content, str):
                raise ValueError("Only text messages are supported")
            if message.get("tool_calls"):
                if not allow_tools:
                    raise ValueError("Only text messages are supported")
                _validate_assistant_tool_calls(message.get("tool_calls"))
        elif role == "tool":
            if not allow_tools:
                raise ValueError("Only text messages are supported")
            if not isinstance(content, str):
                raise ValueError("Only text messages are supported")
            if not isinstance(message.get("tool_call_id"), str):
                raise ValueError("Only text messages are supported")
        else:
            raise ValueError("Only text messages are supported")
    from langchain_core.messages import convert_to_messages

    return convert_to_messages(raw_messages)


@router.post("/v1/{path:path}", operation_id="proxy_llm_request_post")
@router.get("/v1/{path:path}", operation_id="proxy_llm_request_get")
@limiter.exempt
async def proxy_llm_request(path: str, request: Request) -> Response:
    """Proxy LLM requests — inject real API key at transport layer.

    The agent sends requests here with a session token as the "api_key".
    We strip it, look up the real key, and forward to the actual provider.
    """
    # Extract session token from Authorization header
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""

    session = await _get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired gateway session")

    # Native provider adapters belong at the credential boundary. Reuse the
    # configured adapters rather than pretending every provider speaks OpenAI.
    if request.method != "POST" or path != "chat/completions":
        raise HTTPException(status_code=404, detail="Unsupported gateway operation")
    from types import SimpleNamespace

    from pydantic import BaseModel, Field, ValidationError

    from app.agents.prompts import with_security_rules
    from app.core.model_router import _make_llm
    from app.services.llm_proxy_service import get_redaction_callback, redact_keys

    class ChatRequest(BaseModel):
        model: str = Field(min_length=1, max_length=200)
        messages: list[dict] = Field(min_length=1, max_length=100)
        stream: bool = False
        tools: list[dict] | None = None

    try:
        body = await request.body()
        if len(body) > 250000:
            raise HTTPException(status_code=413, detail="Chat input is too large")
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise ValueError("Expected object")
        allow_tools = bool(session.get("allow_tools"))
        if allow_tools:
            raw["tools"] = _validate_tools(raw.get("tools"))
        elif raw.get("tools"):
            raise HTTPException(status_code=422, detail="Unsupported chat operation")
        if any(raw.get(field) for field in ("functions", "response_format")):
            raise HTTPException(status_code=422, detail="Unsupported chat operation")
        payload = ChatRequest.model_validate(raw)
        messages = _validated_messages(payload.messages, allow_tools=allow_tools)
        # Every agent's calls pass here, so this is where the shared
        # untrusted-content rules are guaranteed, inline prompts included.
        messages = with_security_rules(messages)
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid chat request") from None
    if payload.model != session["model_name"]:
        raise HTTPException(status_code=403, detail="Model does not match gateway session")
    if payload.stream:
        raise HTTPException(status_code=422, detail="Streaming is unavailable on this gateway")
    if sum(len(str(message.content)) for message in messages) > 200000:
        raise HTTPException(status_code=413, detail="Chat input is too large")
    key = (
        decrypt_api_key(session["api_key_enc"], settings.APP_SECRET_KEY)
        if session.get("api_key_enc")
        else ""
    )
    model_settings = SimpleNamespace(
        provider=session["provider"],
        model_name=session["model_name"],
        ollama_url=session.get("ollama_url"),
    )
    try:
        llm = _make_llm(model_settings, key)
        llm.callbacks = [get_redaction_callback()]
        if payload.tools:
            llm = llm.bind_tools(payload.tools)
        result = await llm.ainvoke(messages)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "gateway_provider_failed provider=%s error_type=%s",
            session["provider"],
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Model provider request failed") from None
    content = result.content
    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    message: dict = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if allow_tools:
        tool_calls = []
        # LangChain normalizes calls across providers. Anthropic/Google do not
        # populate the OpenAI-specific additional_kwargs representation.
        for call in result.tool_calls or []:
            tool_calls.append(
                {
                    "id": call.get("id")
                    or "call_" + _generate_session_token(session["user_id"], session["provider"]),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["args"]),
                    },
                }
            )
        if tool_calls:
            message["tool_calls"] = tool_calls
            finish_reason = "tool_calls"
    usage = result.usage_metadata or {}
    response = {
        "id": "chatcmpl-" + _generate_session_token(session["user_id"], session["provider"]),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": session["model_name"],
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }
    return Response(content=redact_keys(json.dumps(response)), media_type="application/json")


async def get_gateway_llm(user_id: str, db):
    """Build an LLM instance that routes through the gateway (key-free).

    The returned LLM uses the gateway URL as base_url and a session token
    as the api_key. The real API key never enters the agent's memory.
    """
    from langchain_openai import ChatOpenAI
    from sqlalchemy import select

    from app.models.db import UserModelSettings

    result = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.user_id == user_id,
            UserModelSettings.is_active == True,  # noqa: E712
        )
    )
    model_settings = result.scalars().first()
    if not model_settings:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="No active model configured.")

    from app.services.token_budget_service import check_budget

    if await check_budget(user_id) <= 0:
        raise HTTPException(status_code=429, detail="Daily token budget exceeded.")
    session = await create_gateway_session(user_id, model_settings)

    # All providers get an OpenAI-compatible interface via the gateway
    return ChatOpenAI(
        model=session["model"],
        api_key=session["token"],  # NOT the real key — just a session token
        base_url=session["gateway_url"],
        timeout=120,
        max_retries=0,
    )


async def get_chat_gateway_llm(user_id: str, db):
    """Chat-copilot entry point: gateway LLM permitted to request tool calls.

    Same key-free contract as get_gateway_llm, but the Redis session carries
    allow_tools=True so the proxy accepts a validated tools array and returns
    tool_calls. Only the temporary session token reaches the chat graph.
    """
    from langchain_openai import ChatOpenAI
    from sqlalchemy import select

    from app.core.model_router import TokenTrackingCallback
    from app.models.db import UserModelSettings
    from app.services.llm_proxy_service import get_redaction_callback
    from app.services.token_budget_service import check_budget

    result = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.user_id == user_id,
            UserModelSettings.is_active == True,  # noqa: E712
        )
    )
    model_settings = result.scalars().first()
    if not model_settings:
        raise HTTPException(status_code=400, detail="No active model configured.")
    if await check_budget(user_id) <= 0:
        raise HTTPException(status_code=429, detail="Daily token budget exceeded.")

    token = _generate_session_token(user_id, model_settings.provider)
    data = {
        "user_id": user_id,
        "provider": model_settings.provider,
        "model_name": model_settings.model_name,
        "api_key_enc": model_settings.api_key_enc,
        "ollama_url": model_settings.ollama_url,
        "allow_tools": True,
        "created_at": time.time(),
    }
    r = _get_redis()
    await r.setex(f"{_SESSION_KEY_PREFIX}{token}", _SESSION_TTL_SECONDS, json.dumps(data))
    return ChatOpenAI(
        model=model_settings.model_name,
        api_key=token,
        base_url=_GATEWAY_BASE_URL,
        # LangGraph's event callbacks can implicitly request token streaming
        # even for ainvoke(). The gateway intentionally supports complete
        # responses only; AG-UI still streams graph/tool lifecycle events.
        disable_streaming=True,
        timeout=120,
        max_retries=0,
        callbacks=[get_redaction_callback(), TokenTrackingCallback(user_id)],
    )


def build_gateway_llm(model_settings, user_id: str):
    """Sync node entry point: only a temporary gateway credential reaches agents."""
    import redis
    from langchain_openai import ChatOpenAI

    from app.core.model_router import TokenTrackingCallback, _check_budget_sync
    from app.services.llm_proxy_service import get_redaction_callback

    _check_budget_sync(user_id)
    token = _generate_session_token(user_id, model_settings.provider)
    data = {
        "user_id": user_id,
        "provider": model_settings.provider,
        "model_name": model_settings.model_name,
        "api_key_enc": model_settings.api_key_enc,
        "ollama_url": model_settings.ollama_url,
        "created_at": time.time(),
    }
    with redis.from_url(settings.REDIS_URL, decode_responses=True) as client:
        client.setex(f"{_SESSION_KEY_PREFIX}{token}", _SESSION_TTL_SECONDS, json.dumps(data))
    return ChatOpenAI(
        model=model_settings.model_name,
        api_key=token,
        base_url=_GATEWAY_BASE_URL,
        timeout=120,
        max_retries=0,
        callbacks=[get_redaction_callback(), TokenTrackingCallback(user_id)],
    )
