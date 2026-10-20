"""ApiRequestMiddleware keeps the old three-middleware behaviour; the
verified-token cache never outlives a token or caches a failure."""

import time

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core import clerk_auth
from app.core.request_context import current_user_id
from app.core.request_middleware import ApiRequestMiddleware


def _app():
    app = FastAPI()
    app.add_middleware(ApiRequestMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/v1/me")
    async def me(request: Request):
        return {
            "sub": request.state.user["sub"],
            "rid": request.state.request_id,
            "ctx": current_user_id.get(),
        }

    @app.get("/api/v1/sets-own-header")
    async def own_header():
        return JSONResponse({"ok": True}, headers={"X-Frame-Options": "SAMEORIGIN"})

    return app


async def _get(path, headers=None):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        return await client.get(path, headers=headers or {})


def _assert_security_headers(response):
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    )
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_public_path_skips_auth_and_gets_headers():
    response = await _get("/health")
    assert response.status_code == 200
    _assert_security_headers(response)


@pytest.mark.asyncio
async def test_missing_token_is_401_with_headers_and_echoed_request_id():
    response = await _get("/api/v1/me", headers={"X-Request-ID": "req-1"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing or invalid Authorization header"
    _assert_security_headers(response)
    assert response.headers["x-request-id"] == "req-1"


@pytest.mark.asyncio
async def test_invalid_token_is_401(monkeypatch):
    def reject(token):
        raise HTTPException(status_code=401, detail="bad")

    monkeypatch.setattr(clerk_auth, "verify_token", reject)
    response = await _get("/api/v1/me", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token"
    _assert_security_headers(response)


@pytest.mark.asyncio
async def test_valid_token_sets_state_and_context(monkeypatch):
    monkeypatch.setattr(clerk_auth, "verify_token", lambda token: {"sub": "user_1"})
    response = await _get("/api/v1/me", headers={"Authorization": "Bearer t", "X-Request-ID": "r"})
    assert response.status_code == 200
    assert response.json() == {"sub": "user_1", "rid": "r", "ctx": "user_1"}
    assert current_user_id.get() is None  # reset after the request


@pytest.mark.asyncio
async def test_middleware_headers_replace_a_routes_own(monkeypatch):
    monkeypatch.setattr(clerk_auth, "verify_token", lambda token: {"sub": "u"})
    response = await _get("/api/v1/sets-own-header", headers={"Authorization": "Bearer t"})
    assert response.headers.get_list("x-frame-options") == ["DENY"]


# ── verified-token cache ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_skips_reverification_until_exp(monkeypatch):
    calls = []

    def verify(token):
        calls.append(token)
        return {"sub": "u", "exp": time.time() + 60}

    monkeypatch.setattr(clerk_auth, "verify_token", verify)
    for _ in range(3):
        assert (await clerk_auth.verify_token_cached("tok"))["sub"] == "u"
    assert calls == ["tok"]


@pytest.mark.asyncio
async def test_cache_never_serves_an_expired_token(monkeypatch):
    calls = []

    def verify(token):
        calls.append(token)
        if len(calls) > 1:
            raise HTTPException(status_code=401, detail="Token expired")
        return {"sub": "u", "exp": time.time() + 0.05}

    monkeypatch.setattr(clerk_auth, "verify_token", verify)
    await clerk_auth.verify_token_cached("tok")
    time.sleep(0.1)
    with pytest.raises(HTTPException):
        await clerk_auth.verify_token_cached("tok")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_never_stores_a_failure(monkeypatch):
    calls = []

    def verify(token):
        calls.append(token)
        raise HTTPException(status_code=401, detail="bad")

    monkeypatch.setattr(clerk_auth, "verify_token", verify)
    for _ in range(2):
        with pytest.raises(HTTPException):
            await clerk_auth.verify_token_cached("forged")
    assert len(calls) == 2
    assert not clerk_auth._token_cache


@pytest.mark.asyncio
async def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(clerk_auth, "_TOKEN_CACHE_MAX", 3)
    monkeypatch.setattr(
        clerk_auth, "verify_token", lambda token: {"sub": token, "exp": time.time() + 60}
    )
    for i in range(5):
        await clerk_auth.verify_token_cached(f"t{i}")
    assert len(clerk_auth._token_cache) == 3
