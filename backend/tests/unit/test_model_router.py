import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.core.model_router import _build_llm
from app.models.db import UserModelSettings


def make_settings(provider: str, model_name: str = "test-model", ollama_url: str | None = None):
    s = MagicMock(spec=UserModelSettings)
    s.provider = provider
    s.model_name = model_name
    s.api_key_enc = "encrypted-key"
    s.ollama_url = ollama_url
    return s


@pytest.mark.parametrize(
    "provider,expected_class_path",
    [
        ("anthropic", "langchain_anthropic.ChatAnthropic"),
        ("openai", "langchain_openai.ChatOpenAI"),
        ("google", "langchain_google_genai.ChatGoogleGenerativeAI"),
        ("ollama", "langchain_ollama.ChatOllama"),
        ("nvidia_nim", "langchain_openai.ChatOpenAI"),
        ("openrouter", "langchain_openai.ChatOpenAI"),
        ("opencode", "langchain_openai.ChatOpenAI"),
    ],
)
def test_model_router_dispatches_correct_class(provider, expected_class_path):
    settings = make_settings(
        provider, ollama_url="http://localhost:11434" if provider == "ollama" else None
    )
    module, cls = expected_class_path.rsplit(".", 1)
    with (
        patch(f"{module}.{cls}") as mock_cls,
        patch("app.core.model_router.decrypt_api_key", return_value="plaintext-key"),
        patch("app.core.model_router.settings") as mock_app_settings,
    ):
        mock_app_settings.APP_SECRET_KEY = "secret"
        mock_cls.return_value = MagicMock()
        _build_llm(settings)
        mock_cls.assert_called_once()


@pytest.mark.parametrize(
    "provider",
    ["nvidia_nim", "openrouter", "opencode"],
)
def test_openai_compatible_base_url_does_not_double_chat_completions(provider):
    """Regression test: ChatOpenAI appends /chat/completions to base_url.
    If the base_url already ends in /chat/completions, the client
    requests /v1/chat/completions/chat/completions → 404 + HTML.
    Only runs for non-default-base providers — the plain `openai`
    provider intentionally uses the OpenAI default (no base_url).
    """
    settings = make_settings(provider, model_name="some-model")
    with (
        patch("langchain_openai.ChatOpenAI") as mock_cls,
        patch("app.core.model_router.decrypt_api_key", return_value="key"),
        patch("app.core.model_router.settings") as mock_app_settings,
    ):
        mock_app_settings.APP_SECRET_KEY = "secret"
        mock_cls.return_value = MagicMock()
        _build_llm(settings)
        call_kwargs = mock_cls.call_args.kwargs
        base_url = call_kwargs.get("base_url")
        assert base_url is not None, f"{provider}: base_url not passed to ChatOpenAI"
        assert not base_url.rstrip("/").endswith("/chat/completions"), (
            f"{provider}: base_url={base_url!r} ends in /chat/completions. "
            f"ChatOpenAI appends /chat/completions automatically, so this "
            f"would cause /v1/chat/completions/chat/completions 404s."
        )


def test_model_router_raises_when_no_settings():
    with pytest.raises((ValueError, TypeError, AttributeError)):
        _build_llm(None)


def test_model_router_raises_on_unknown_provider():
    s = make_settings("unknown_provider")
    with (
        patch("app.core.model_router.decrypt_api_key", return_value="key"),
        patch("app.core.model_router.settings") as mock_app_settings,
    ):
        mock_app_settings.APP_SECRET_KEY = "secret"
        from fastapi.exceptions import HTTPException

        with pytest.raises((ValueError, KeyError, AttributeError, HTTPException)):
            _build_llm(s)


# ── Regression test: get_llm returns CachingLLM ─────────────────────────────


@pytest.mark.asyncio
async def test_get_llm_returns_caching_llm():
    """get_llm() must wrap the raw BaseChatModel in CachingLLM so the Redis
    response cache is active for every agent call."""
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.core.model_router import get_llm, CachingLLM

    user_id = str(_uuid.uuid4())

    # Fake active model settings row
    mock_settings = MagicMock()
    mock_settings.provider = "openai"
    mock_settings.model_name = "gpt-4o-mini"
    mock_settings.api_key_enc = "enc-key"
    mock_settings.user_id = user_id

    # Fake DB that returns the mock settings
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_settings
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    session = {"token": "gw-session", "gateway_url": "http://gw/v1", "model": "gpt-4o-mini"}
    # check_budget is imported inside get_llm, so patch at the source module
    with (
        patch("app.services.token_budget_service.check_budget", new=AsyncMock(return_value=10_000)),
        patch("app.core.model_router.decrypt_api_key", return_value="plaintext-key") as decrypt,
        patch("app.core.llm_gateway.create_gateway_session", new=AsyncMock(return_value=session)),
        patch("langchain_openai.ChatOpenAI") as mock_openai_cls,
        patch("app.core.model_router._get_llm_cache") as mock_cache_factory,
        patch("app.core.model_router.get_redaction_callback", return_value=MagicMock()),
    ):
        mock_openai_cls.return_value = MagicMock()
        mock_cache_factory.return_value = MagicMock()

        result = await get_llm(user_id, mock_db, task_type="resume")

    assert isinstance(result, CachingLLM), (
        f"get_llm() returned {type(result).__name__!r}, expected CachingLLM. "
        "The Redis LLM cache is inactive."
    )
    assert result._task_type == "resume"
    assert result._user_id == user_id
    # Through the PII-redacting gateway with a session token, never the key.
    decrypt.assert_not_called()
    kwargs = mock_openai_cls.call_args.kwargs
    assert kwargs["base_url"] == "http://gw/v1" and kwargs["api_key"] == "gw-session"


@pytest.mark.asyncio
async def test_get_llm_default_task_type_is_empty_string():
    """Calling get_llm without task_type should default to '' (cache enabled for non-skip tasks)."""
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.core.model_router import get_llm, CachingLLM

    user_id = str(_uuid.uuid4())

    mock_settings = MagicMock()
    mock_settings.provider = "openai"
    mock_settings.model_name = "gpt-4o-mini"
    mock_settings.api_key_enc = "enc-key"
    mock_settings.user_id = user_id

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_settings
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    session = {"token": "gw-session", "gateway_url": "http://gw/v1", "model": "gpt-4o-mini"}
    # check_budget is imported inside get_llm, so patch at the source module
    with (
        patch("app.services.token_budget_service.check_budget", new=AsyncMock(return_value=10_000)),
        patch("app.core.llm_gateway.create_gateway_session", new=AsyncMock(return_value=session)),
        patch("langchain_openai.ChatOpenAI") as mock_openai_cls,
        patch("app.core.model_router._get_llm_cache") as mock_cache_factory,
        patch("app.core.model_router.get_redaction_callback", return_value=MagicMock()),
    ):
        mock_openai_cls.return_value = MagicMock()
        mock_cache_factory.return_value = MagicMock()

        result = await get_llm(user_id, mock_db)

    assert isinstance(result, CachingLLM)
    assert result._task_type == ""
