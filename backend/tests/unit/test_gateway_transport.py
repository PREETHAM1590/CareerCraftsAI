"""Provider translation stays behind the session credential boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage

from app.core import llm_gateway as gateway
from app.core import model_router


@pytest.fixture
def transport(monkeypatch):
    session = {
        "user_id": "owner",
        "provider": "deepseek",
        "model_name": "configured-model",
        "api_key_enc": "ciphertext",
    }
    monkeypatch.setattr(gateway, "_get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(gateway, "decrypt_api_key", lambda *args: "private-key")
    app = FastAPI()
    app.include_router(gateway.router)
    from app.core.request_middleware import ApiRequestMiddleware

    app.add_middleware(ApiRequestMiddleware)
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "google", "deepseek", "ollama"])
async def test_native_adapter_returns_text_and_usage(transport, monkeypatch, provider):
    session = await gateway._get_session("session")
    session["provider"] = provider
    invoke = AsyncMock(
        return_value=AIMessage(
            content=[{"type": "text", "text": "Grounded edit"}],
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        )
    )

    def adapter(config, key):
        assert config.provider == provider
        assert config.model_name == "configured-model"
        assert key == "private-key"
        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(model_router, "_make_llm", adapter)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/llm-gateway/v1/chat/completions",
            headers={"Authorization": "Bearer session"},
            json={"model": "configured-model", "messages": [{"role": "user", "content": "Source"}]},
        )
    assert response.status_code == 200
    assert response.json()["usage"]["total_tokens"] == 10
    assert response.json()["choices"][0]["message"]["content"] == "Grounded edit"
    assert "private-key" not in response.text
    forwarded = invoke.call_args.args[0]
    assert forwarded[-1].content == "Source"
    assert forwarded[0].content.startswith("UNTRUSTED CONTENT")  # shared rules added


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,status",
    [
        ({"model": "other-model"}, 403),
        ({"stream": True}, 422),
        ({"messages": []}, 422),
        ({"tools": [{"type": "function"}]}, 422),
        ({"messages": [{"role": "user", "content": "x" * 260000}]}, 413),
    ],
)
async def test_invalid_request_never_reaches_provider(transport, monkeypatch, change, status):
    adapter = AsyncMock()
    monkeypatch.setattr(model_router, "_make_llm", adapter)
    payload = {"model": "configured-model", "messages": [{"role": "user", "content": "Source"}]}
    payload.update(change)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/llm-gateway/v1/chat/completions", json=payload)
    assert response.status_code == status
    adapter.assert_not_called()


@pytest.mark.asyncio
async def test_provider_failure_is_safe(transport, monkeypatch):
    monkeypatch.setattr(
        model_router,
        "_make_llm",
        lambda *args: SimpleNamespace(
            ainvoke=AsyncMock(side_effect=RuntimeError("private-key provider body"))
        ),
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/llm-gateway/v1/chat/completions",
            json={"model": "configured-model", "messages": [{"role": "user", "content": "Source"}]},
        )
    assert response.status_code == 502
    assert "private-key" not in response.text


@pytest.mark.asyncio
async def test_expired_session_denied(transport, monkeypatch):
    monkeypatch.setattr(gateway, "_get_session", AsyncMock(return_value=None))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/llm-gateway/v1/chat/completions", json={})
    assert response.status_code == 401
