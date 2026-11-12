import logging
from typing import Any

from fastapi import HTTPException
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import LLMResult

from app.core.config import settings
from app.core.security import decrypt_api_key
from app.models.db import UserModelSettings
from app.services.llm_proxy_service import get_redaction_callback

logger = logging.getLogger(__name__)
LLM_TIMEOUT_SECONDS = 60
LLM_MAX_RETRIES = 2
# Left unset, ChatOpenAI omits max_tokens and the provider's own default
# applies — too small once a reasoning model's hidden reasoning_content
# shares the same budget as the visible answer, truncating structured JSON
# output mid-string (observed: ResumeOutput validation failures on deepseek
# reasoning models). Large enough for a full tailored resume/cover letter
# plus reasoning overhead; per-user spend is still capped separately by
# _check_budget_sync/TokenTrackingCallback.
LLM_MAX_OUTPUT_TOKENS = 8192

import contextvars

# A mutable box held in a ContextVar, not a threading.local: sync agent nodes
# run via asyncio.to_thread, which copies the context into the worker thread.
# The worker mutates the same box the event-loop side reads back, and each
# concurrent run (its own asyncio task/context) gets an isolated box.
_agent_token_totals: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "agent_token_totals", default=None
)


def begin_token_tracking() -> None:
    """Start a fresh token counter for the agent run in the current context."""
    _agent_token_totals.set([0])


def _add_tokens(count: int) -> None:
    box = _agent_token_totals.get()
    if box is None:
        box = [0]
        _agent_token_totals.set(box)
    box[0] += count


def get_and_reset_tokens() -> int:
    box = _agent_token_totals.get()
    if box is None:
        return 0
    total, box[0] = box[0], 0
    return total


def _total_tokens(response: LLMResult) -> int:
    token_usage = (response.llm_output or {}).get("token_usage") or {}
    total = token_usage.get("total_tokens") or 0
    if total:
        return total
    # Providers/clients that report usage only per message (usage_metadata).
    try:
        return sum(
            (
                getattr(gen, "message", None)
                and (gen.message.usage_metadata or {}).get("total_tokens")
            )
            or 0
            for gens in response.generations
            for gen in gens
        )
    except Exception:
        return 0


class TokenTrackingCallback(BaseCallbackHandler):
    """Tracks token usage per LLM call for budget enforcement."""

    def __init__(self, user_id: str):
        self.user_id = user_id

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Record token usage after each LLM call."""
        total = _total_tokens(response)
        if total > 0:
            _add_tokens(total)
            from app.services.token_budget_service import record_tokens_sync

            try:
                record_tokens_sync(self.user_id, total)
            except Exception as exc:
                logger.debug("Token budget tracking failed for user %s: %s", self.user_id, exc)


def _make_llm(model_settings, api_key: str) -> BaseChatModel:
    match model_settings.provider:
        case "anthropic":
            from langchain_anthropic import ChatAnthropic

            # Extended thinking is only supported on claude-3-7-sonnet and above,
            # and claude-sonnet-4-x / claude-opus-4-x families.
            # Haiku models and older Sonnet/Opus do NOT support the thinking param —
            # passing it causes a 400 "unsupported parameter" error from the API.
            _THINKING_MODELS = (
                "claude-3-7-sonnet",
                "claude-sonnet-4",
                "claude-opus-4",
            )
            model_name: str = model_settings.model_name or ""
            supports_thinking = any(model_name.startswith(m) for m in _THINKING_MODELS)

            kwargs: dict = {
                "model": model_name,
                "api_key": api_key,
                "timeout": LLM_TIMEOUT_SECONDS,
                "max_retries": LLM_MAX_RETRIES,
            }
            if supports_thinking:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": settings.AGENT_THINKING_BUDGET_TOKENS,
                }
            return ChatAnthropic(**kwargs)
        case "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model_settings.model_name,
                api_key=api_key,
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )
        case "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=model_settings.model_name,
                google_api_key=api_key,
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
            )
        case "ollama":
            from langchain_ollama import ChatOllama

            return ChatOllama(
                model=model_settings.model_name,
                base_url=model_settings.ollama_url,
                timeout=LLM_TIMEOUT_SECONDS,
            )
        case "nvidia_nim":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model_settings.model_name,
                api_key=api_key,
                base_url="https://integrate.api.nvidia.com/v1",
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )
        case "deepseek":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model_settings.model_name,
                api_key=api_key,
                base_url="https://api.deepseek.com",
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )
        case "openrouter":
            # OpenRouter is OpenAI-compatible, so we use the ChatOpenAI
            # client pointed at their public base URL. Any model on
            # OpenRouter's catalog (e.g. moonshotai/kimi-k2) works.
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model_settings.model_name,
                api_key=api_key,
                base_url="https://openrouter.ai/api/v1",
                default_headers={
                    # Recommended by OpenRouter for attribution/rankings.
                    "HTTP-Referer": "https://careercraftai.local",
                    "X-Title": "CareerCraft AI",
                },
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )
        case "opencode":
            # OpenCode Zen (https://opencode.ai/zen) is Anomaly's hosted
            # polyglot gateway: 50+ models (GPT, Claude, Gemini, Qwen,
            # DeepSeek, MiniMax, Kimi, GLM, Grok, MiMo, Nemotron, Big
            # Pickle, etc.) reachable with a single API key. Everything
            # is exposed as OpenAI-compatible chat completions, so a
            # plain ChatOpenAI client with the Zen base URL works.
            #
            # IMPORTANT: ChatOpenAI appends `/chat/completions` to the
            # base_url automatically. If you include `/chat/completions`
            # in the base_url, the client will request
            #   .../v1/chat/completions/chat/completions
            # which 404s and returns an HTML error page (the symptom:
            # "Model test failed: <!DOCTYPE html>..." in the toast).
            # Base URL must end in `/v1` only — NOT `/v1/chat/completions`.
            #
            # The full model catalog lives at:
            #   https://opencode.ai/zen/v1/models
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=model_settings.model_name,
                api_key=api_key,
                base_url="https://opencode.ai/zen/v1",
                default_headers={
                    "HTTP-Referer": "https://careercraftai.local",
                    "X-Title": "CareerCraft AI",
                },
                timeout=LLM_TIMEOUT_SECONDS,
                max_retries=LLM_MAX_RETRIES,
                max_tokens=LLM_MAX_OUTPUT_TOKENS,
            )
        case _:
            raise HTTPException(
                status_code=400, detail=f"Unknown provider: {model_settings.provider}"
            )


async def get_llm(user_id: str, db, task_type: str = "") -> BaseChatModel:
    from sqlalchemy import select
    from app.services.token_budget_service import check_budget

    # Check budget before building LLM
    remaining = await check_budget(user_id)
    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail="Daily token budget exceeded. Resets at midnight UTC.",
        )

    result = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.user_id == user_id,
            UserModelSettings.is_active == True,  # noqa: E712
        )
    )
    model_settings: UserModelSettings | None = result.scalars().first()
    if not model_settings:
        raise HTTPException(
            status_code=400, detail="No active model configured. Add a model in Settings."
        )

    # Through the gateway like every agent: no provider key here, and the
    # member's personal details are replaced before the request leaves.
    from app.core.llm_gateway import gateway_chat_model

    llm = await gateway_chat_model(str(user_id), model_settings)
    llm.callbacks = [get_redaction_callback(), TokenTrackingCallback(user_id)]
    return CachingLLM(llm, user_id, task_type)


def _check_budget_sync(user_id: str) -> None:
    """Synchronous token budget check for agent nodes that call _build_llm.

    Uses a direct Redis connection (not asyncio) so it's safe to call from
    LangGraph nodes running in a thread executor.  Raises HTTPException(429)
    if the user has exhausted their daily budget.  Silently skips the check
    on any Redis error so a Redis outage never blocks an agent run.
    """
    from app.services.token_budget_service import DEFAULT_DAILY_LIMIT, _budget_key

    try:
        import redis as sync_redis

        url = settings.REDIS_URL
        r = sync_redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
        key = _budget_key(user_id)
        used = int(r.get(key) or 0)
        if used >= DEFAULT_DAILY_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Daily token budget exceeded ({used}/{DEFAULT_DAILY_LIMIT}). "
                    "Resets at midnight UTC."
                ),
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Redis unavailable — degrade gracefully, never block the agent
        logger.debug("Sync budget check skipped (Redis unavailable): %s", exc)


def _build_llm(model_settings) -> BaseChatModel:
    """Build LLM directly from model_settings object (no DB lookup needed).

    Calls the provider directly, bypassing the gateway's PII redaction: only
    for the Settings key test, whose prompt is fixed text (see
    tests/security/test_llm_bypass_guard.py).

    Enforces the daily token budget before building — raises HTTPException(429)
    when the budget is exhausted.  The check uses a sync Redis call so it is
    safe inside LangGraph nodes that run in a thread executor.
    """
    user_id = getattr(model_settings, "user_id", None)
    if user_id:
        _check_budget_sync(str(user_id))

    api_key = decrypt_api_key(model_settings.api_key_enc, settings.APP_SECRET_KEY)
    llm = _make_llm(model_settings, api_key)
    callbacks = [get_redaction_callback()]
    if user_id:
        callbacks.append(TokenTrackingCallback(str(user_id)))
    llm.callbacks = callbacks
    return llm


def build_agent_llm(model_settings) -> BaseChatModel:
    """The LLM every agent node uses: it reaches the provider through the
    internal gateway with a short-lived session token, so the member's real
    API key never enters agent memory, prompts or traces. Same budget check
    and token tracking as _build_llm, which stays for the key test in
    Settings (it must call the provider directly)."""
    from app.core.llm_gateway import build_gateway_llm

    user_id = getattr(model_settings, "user_id", None)
    if not user_id:
        raise ValueError("Agent LLMs are built for a member; model settings have no user_id")
    return build_gateway_llm(model_settings, str(user_id))


# ── LLM response caching (Redis, 1h TTL) ────────────────────────
# Cache is skipped for email/auto_apply tasks since they're always
# unique and shouldn't be cached. Uses sha256 of (user_id, task_type,
# prompt_text) as the cache key.

import hashlib

_SKIP_CACHE_TASKS = {"email", "auto_apply", "email_monitor", "linkedin_outreach"}

_llm_cache = None


def _get_llm_cache():
    global _llm_cache
    if _llm_cache is None:
        import redis as sync_redis

        url = settings.REDIS_URL
        if url.startswith("redis://"):
            url = url.replace("redis://", "", 1)
        _llm_cache = sync_redis.from_url(f"redis://{url}", decode_responses=True)
    return _llm_cache


def _cache_key(user_id: str, task_type: str, prompt: str) -> str:
    h = hashlib.sha256(f"{user_id}:{task_type}:{prompt}".encode()).hexdigest()[:32]
    return f"llm_cache:{h}"


class CachingLLM:
    """Wraps a BaseChatModel with Redis response caching.

    Only caches for non-email, non-apply tasks. Cache TTL: 1 hour.
    """

    def __init__(self, llm: BaseChatModel, user_id: str, task_type: str):
        self._llm = llm
        self._user_id = user_id
        self._task_type = task_type or ""
        self._cache = _get_llm_cache()

    @property
    def callbacks(self):
        return self._llm.callbacks

    @callbacks.setter
    def callbacks(self, value):
        self._llm.callbacks = value

    def invoke(self, messages, **kwargs):
        if self._task_type in _SKIP_CACHE_TASKS:
            return self._llm.invoke(messages, **kwargs)
        prompt = str(messages[-1].content) if hasattr(messages[-1], "content") else str(messages)
        key = _cache_key(self._user_id, self._task_type, prompt)
        try:
            cached = self._cache.get(key)
            if cached:
                import json
                from langchain_core.messages import AIMessage

                data = json.loads(cached)
                return AIMessage(content=data["content"])
        except Exception:
            pass
        result = self._llm.invoke(messages, **kwargs)
        try:
            content = result.content if hasattr(result, "content") else str(result)
            import json

            self._cache.setex(key, 3600, json.dumps({"content": content}))
        except Exception:
            pass
        return result

    def __getattr__(self, name):
        return getattr(self._llm, name)
