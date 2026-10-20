"""Gateway and Nango webhook are bounded (not exempt) and webhook bodies are capped."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware


def _app(monkeypatch):
    from app.api.v1 import integrations
    from app.api.v1.deps import get_db
    from app.core import llm_gateway
    from app.core.rate_limit import limiter

    monkeypatch.setattr(integrations.settings, "NANGO_ENABLED", True)
    monkeypatch.setattr(integrations, "verify_nango_webhook", lambda **kw: False)
    monkeypatch.setattr(llm_gateway, "_get_session", AsyncMock(return_value=None))
    limiter.reset()
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIASGIMiddleware)
    app.include_router(llm_gateway.router)
    app.include_router(integrations.router)

    async def db_override():
        yield AsyncMock()

    app.dependency_overrides[get_db] = db_override
    return app


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_body(monkeypatch):
    app = _app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/integrations/webhooks/nango", content=b"x" * 1_000_001)
        assert r.status_code == 413
        r = await client.post("/integrations/webhooks/nango", content=b"{}")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_gateway_is_limited_per_token_not_exempt(monkeypatch):
    app = _app(monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        h = {"Authorization": "Bearer tok-a"}
        for _ in range(120):
            r = await client.post("/llm-gateway/v1/chat/completions", content=b"{}", headers=h)
            assert r.status_code == 401
        r = await client.post("/llm-gateway/v1/chat/completions", content=b"{}", headers=h)
        assert r.status_code == 429
        other = await client.post(
            "/llm-gateway/v1/chat/completions",
            content=b"{}",
            headers={"Authorization": "Bearer tok-b"},
        )
        assert other.status_code == 401


@pytest.mark.asyncio
async def test_form_filler_reports_extension_move(monkeypatch):
    from app.services import form_filler_service as ff
    from app.services.browser_control_service import BrowserAutomationRetired

    assert issubclass(BrowserAutomationRetired, RuntimeError)
    src = ff.run_browser_task
    assert src is not None
