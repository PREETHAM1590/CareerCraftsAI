"""Shared worker/provider IPs never consume member-facing default buckets."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware


@pytest.mark.asyncio
async def test_gateway_methods_and_webhook_keep_authentication_without_default_bucket(monkeypatch):
    from app.api.v1 import integrations
    from app.api.v1.deps import get_db
    from app.core import llm_gateway
    from app.core.rate_limit import limiter

    monkeypatch.setattr(integrations.settings, "NANGO_ENABLED", True)
    monkeypatch.setattr(integrations, "verify_nango_webhook", lambda **kw: False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIASGIMiddleware)
    app.include_router(llm_gateway.router)
    app.include_router(integrations.router)

    async def db_override():
        yield AsyncMock()

    app.dependency_overrides[get_db] = db_override
    paths = [
        ("GET", llm_gateway.router.prefix + "/v1/chat/completions"),
        ("POST", llm_gateway.router.prefix + "/v1/chat/completions"),
        ("POST", integrations.router.prefix + "/webhooks/nango"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, path in paths:
            for _ in range(65):
                response = await client.request(method, path, content=b"{}")
                # Authentication still executes beyond the old 60/min IP cap.
                assert response.status_code == 401, (path, response.status_code)


@pytest.mark.asyncio
async def test_active_run_polling_has_a_separate_bounded_budget():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import uuid4

    from fastapi import Request

    from app.api.v1 import agents
    from app.api.v1.deps import get_current_user, get_db
    from app.core.rate_limit import limiter

    user_id, run_id = uuid4(), uuid4()
    run = SimpleNamespace(
        id=run_id,
        status="running",
        agent_type="cover_letter",
        input={},
        output={},
        tokens_used=0,
        duration_ms=0,
        started_at=None,
        completed_at=None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [run]
    result.scalar_one_or_none.return_value = run
    db = AsyncMock()
    db.execute.return_value = result

    async def db_override():
        yield db

    async def user_override(request: Request):
        request.state.user = {"sub": str(user_id)}
        return SimpleNamespace(id=user_id)

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIASGIMiddleware)
    app.include_router(agents.router)
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = user_override
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Maximum companion fan-out is 24 * 20 = 480 detail requests/minute.
        for _ in range(600):
            assert (await client.get(f"/agents/runs/{run_id}")).status_code == 200
        assert (await client.get(f"/agents/runs/{run_id}")).status_code == 429
        # List status has its own budget, independent of detail polling.
        for _ in range(65):
            assert (await client.get("/agents/runs")).status_code == 200
