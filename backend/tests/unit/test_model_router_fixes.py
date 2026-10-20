"""Regression tests for Fix 3: model router + token budget (2026-06-13 audit).

Bugs caught:
  1. _make_llm passed thinking={...} to ALL Anthropic models, including Haiku
     which doesn't support it → 400 "unsupported parameter" from the API.
     Fix: only pass thinking param for claude-3-7-sonnet, claude-sonnet-4-x,
     claude-opus-4-x.

  2. _build_llm() bypassed check_budget() — all agent LLM calls skipped the
     daily token budget.  get_llm() (async/API path) checked it; _build_llm()
     (sync/agent path, used by ALL 13 LangGraph nodes) did not.
     Fix: _check_budget_sync() called at top of _build_llm().

  3. AES-256-GCM round-trip (confirmed working — regression test included so
     any future change to the crypto layer is caught immediately).
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# 1. Thinking param: only applied to supported Anthropic models
# ---------------------------------------------------------------------------


def _make_anthropic_settings(model_name: str):
    s = MagicMock()
    s.provider = "anthropic"
    s.model_name = model_name
    s.api_key_enc = "enc_key"
    return s


@pytest.mark.parametrize(
    "model_name",
    [
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-3-7-sonnet-20250219",
    ],
)
def test_thinking_param_applied_to_supported_models(model_name):
    """OLD bug: thinking param always passed → 400 on any non-thinking model.
    NEW: only passed to claude-3-7-sonnet, claude-sonnet-4-x, claude-opus-4-x.
    """
    from app.core.model_router import _make_llm

    settings_obj = _make_anthropic_settings(model_name)
    with patch("langchain_anthropic.ChatAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        with patch("app.core.model_router.decrypt_api_key", return_value="key"):
            _make_llm(settings_obj, "key")

        call_kwargs = mock_cls.call_args.kwargs
        assert (
            "thinking" in call_kwargs
        ), f"{model_name} should have thinking param but it was absent"
        assert call_kwargs["thinking"]["type"] == "enabled"


@pytest.mark.parametrize(
    "model_name",
    [
        "claude-haiku-4-5",
        "claude-3-haiku-20240307",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",  # original Opus (not Opus 4)
        "claude-3-sonnet-20240229",
    ],
)
def test_thinking_param_NOT_applied_to_unsupported_models(model_name):
    """Haiku and pre-4.x models must NOT receive the thinking param."""
    from app.core.model_router import _make_llm

    settings_obj = _make_anthropic_settings(model_name)
    with patch("langchain_anthropic.ChatAnthropic") as mock_cls:
        mock_cls.return_value = MagicMock()
        _make_llm(settings_obj, "key")

        call_kwargs = mock_cls.call_args.kwargs
        assert "thinking" not in call_kwargs, (
            f"{model_name} received thinking param — would cause 400 from Anthropic API. "
            f"kwargs={call_kwargs}"
        )


# ---------------------------------------------------------------------------
# 2. _build_llm: budget check fires before LLM construction
# ---------------------------------------------------------------------------


def _make_settings(provider: str = "openai", model_name: str = "gpt-4o-mini"):
    s = MagicMock()
    s.provider = provider
    s.model_name = model_name
    s.api_key_enc = "enc_key"
    s.user_id = "00000000-0000-0000-0000-000000000001"
    return s


def test_build_llm_raises_429_when_budget_exhausted():
    """OLD bug: _build_llm skipped check_budget → agents ran regardless of budget.
    NEW: raises HTTPException(429) when budget is exhausted.
    """
    from app.core.model_router import _build_llm

    settings_obj = _make_settings()

    with (
        patch(
            "app.core.model_router._check_budget_sync",
            side_effect=HTTPException(status_code=429, detail="budget exceeded"),
        ),
        patch("app.core.model_router.decrypt_api_key", return_value="key"),
        patch("app.core.model_router._make_llm", return_value=MagicMock()),
        patch("app.core.model_router.get_redaction_callback", return_value=MagicMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            _build_llm(settings_obj)

    assert exc_info.value.status_code == 429


def test_build_llm_proceeds_when_budget_available():
    """When budget is available, _build_llm must return an LLM without raising."""
    from app.core.model_router import _build_llm

    mock_llm = MagicMock()

    with (
        patch("app.core.model_router._check_budget_sync"),  # no-op = budget ok
        patch("app.core.model_router.decrypt_api_key", return_value="key"),
        patch("app.core.model_router._make_llm", return_value=mock_llm),
        patch("app.core.model_router.get_redaction_callback", return_value=MagicMock()),
    ):
        result = _build_llm(_make_settings())

    assert result is not None


def test_build_llm_skips_budget_check_when_no_user_id():
    """If model_settings has no user_id (e.g. internal calls), skip budget check."""
    from app.core.model_router import _build_llm

    settings_obj = _make_settings()
    settings_obj.user_id = None  # no user_id

    check_called = []

    with (
        patch(
            "app.core.model_router._check_budget_sync",
            side_effect=lambda uid: check_called.append(uid),
        ),
        patch("app.core.model_router.decrypt_api_key", return_value="key"),
        patch("app.core.model_router._make_llm", return_value=MagicMock()),
        patch("app.core.model_router.get_redaction_callback", return_value=MagicMock()),
    ):
        _build_llm(settings_obj)

    assert len(check_called) == 0, "Budget check must not fire when user_id is absent"


def test_check_budget_sync_raises_429_when_over_limit():
    """_check_budget_sync must raise HTTPException(429) when Redis reports over-limit."""
    from app.core.model_router import _check_budget_sync

    with patch("redis.from_url") as mock_redis_cls:
        mock_r = MagicMock()
        # Simulate 600000 tokens used (over 500000 default limit)
        mock_r.get.return_value = "600000"
        mock_redis_cls.return_value = mock_r

        with pytest.raises(HTTPException) as exc_info:
            _check_budget_sync("usr_test")

    assert exc_info.value.status_code == 429
    assert "exceeded" in exc_info.value.detail.lower()


def test_check_budget_sync_degrades_gracefully_on_redis_error():
    """If Redis is unavailable, _check_budget_sync must NOT crash — degrade silently."""
    from app.core.model_router import _check_budget_sync

    with patch("redis.from_url", side_effect=ConnectionError("Redis down")):
        # Must not raise — Redis errors are swallowed
        _check_budget_sync("usr_test")  # no exception = pass


def test_check_budget_sync_allows_call_when_under_limit():
    """When tokens used < limit, must return without raising."""
    from app.core.model_router import _check_budget_sync

    with patch("redis.from_url") as mock_redis_cls:
        mock_r = MagicMock()
        mock_r.get.return_value = "1000"  # well under 500k
        mock_redis_cls.return_value = mock_r

        _check_budget_sync("usr_test")  # no exception = pass


# ---------------------------------------------------------------------------
# 3. AES-256-GCM round-trip (regression guard for crypto layer)
# ---------------------------------------------------------------------------


def test_aes_gcm_round_trip_standard_key():
    from app.core.security import decrypt_api_key, encrypt_api_key

    secret = "test-secret-key-32-chars-minimum!!"
    plaintext = "sk-ant-api03-real-looking-key-with-enough-length"
    encrypted = encrypt_api_key(plaintext, secret)

    assert plaintext not in encrypted  # ciphertext must not contain plaintext
    assert decrypt_api_key(encrypted, secret) == plaintext


def test_aes_gcm_unique_ciphertext_per_call():
    from app.core.security import encrypt_api_key

    secret = "test-secret-key-32-chars-minimum!!"
    enc1 = encrypt_api_key("same-key", secret)
    enc2 = encrypt_api_key("same-key", secret)
    assert enc1 != enc2  # unique salt per call


def test_aes_gcm_tampered_ciphertext_raises():
    import base64

    from app.core.security import decrypt_api_key, encrypt_api_key

    secret = "test-secret-key-32-chars-minimum!!"
    encrypted = encrypt_api_key("my-api-key", secret)
    raw = bytearray(base64.b64decode(encrypted))
    raw[-1] ^= 0xFF
    tampered = base64.b64encode(bytes(raw)).decode()

    with pytest.raises(Exception):
        decrypt_api_key(tampered, secret)


# ---------------------------------------------------------------------------
# 4. TokenTrackingCallback accumulates tokens correctly
# ---------------------------------------------------------------------------


def test_token_tracking_callback_accumulates():
    """TokenTrackingCallback must add tokens to the thread-local accumulator."""
    from langchain_core.outputs import LLMResult

    from app.core.model_router import TokenTrackingCallback, get_and_reset_tokens

    cb = TokenTrackingCallback("usr_test")
    result = MagicMock(spec=LLMResult)
    result.llm_output = {"token_usage": {"total_tokens": 150}}

    # Reset accumulator first
    get_and_reset_tokens()

    with patch("app.services.token_budget_service.record_tokens_sync") as record:
        cb.on_llm_end(result)
    record.assert_called_once_with("usr_test", 150)

    total = get_and_reset_tokens()
    assert total == 150


def test_token_tracking_callback_noop_on_zero_tokens():
    """Zero-token response must not increment the accumulator."""
    from langchain_core.outputs import LLMResult

    from app.core.model_router import TokenTrackingCallback, get_and_reset_tokens

    cb = TokenTrackingCallback("usr_test")
    result = MagicMock(spec=LLMResult)
    result.llm_output = {"token_usage": {"total_tokens": 0}}

    get_and_reset_tokens()
    cb.on_llm_end(result)
    assert get_and_reset_tokens() == 0


def test_tokens_recorded_in_worker_thread_are_visible_to_the_run():
    """Sync agent nodes run via asyncio.to_thread; their LLM tokens must reach the
    caller (previously a threading.local lost them, so tokens_used was always 0)."""
    import asyncio

    from app.core.model_router import _add_tokens, begin_token_tracking, get_and_reset_tokens

    async def run(count: int) -> int:
        begin_token_tracking()
        await asyncio.to_thread(_add_tokens, count)
        await asyncio.sleep(0)
        return get_and_reset_tokens()

    async def main():
        return await asyncio.gather(run(120), run(7))

    assert asyncio.run(main()) == [120, 7]


def test_token_tracking_reads_usage_metadata_when_llm_output_has_none():
    from app.core.model_router import _total_tokens

    message = MagicMock()
    message.usage_metadata = {"total_tokens": 42}
    generation = MagicMock()
    generation.message = message
    result = MagicMock()
    result.llm_output = None
    result.generations = [[generation]]

    assert _total_tokens(result) == 42
