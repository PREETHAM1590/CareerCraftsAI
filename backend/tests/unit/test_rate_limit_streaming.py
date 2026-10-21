"""A rate-limited streaming response must keep streaming past its first chunk."""

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.rate_limit import StreamingSafeSlowAPIMiddleware


def test_limited_stream_sends_every_chunk():
    app = FastAPI()
    app.state.limiter = Limiter(key_func=get_remote_address, headers_enabled=True)
    app.add_middleware(StreamingSafeSlowAPIMiddleware)

    @app.get("/stream")
    @app.state.limiter.limit("100/minute")
    async def stream(request: Request):
        async def events():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    response = TestClient(app).get("/stream")
    assert response.status_code == 200
    assert response.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    assert "x-ratelimit-limit" in response.headers


def test_route_lookup_is_cached_and_limits_still_apply(monkeypatch):
    from app.core import rate_limit

    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_middleware(StreamingSafeSlowAPIMiddleware)

    @app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request):
        return {"ok": True}

    @app.get("/open")
    @limiter.exempt
    async def open_route(request: Request):
        return {"ok": True}

    rate_limit._route_handlers.clear()
    lookups = []
    real_lookup = rate_limit._slowapi_find_route_handler

    def counting_lookup(routes, scope):
        lookups.append(scope["path"])
        return real_lookup(routes, scope)

    monkeypatch.setattr(rate_limit, "_slowapi_find_route_handler", counting_lookup)
    client = TestClient(app)
    assert [client.get("/limited").status_code for _ in range(3)] == [200, 200, 429]
    assert [client.get("/open").status_code for _ in range(3)] == [200, 200, 200]
    # Six requests, one route scan per distinct path.
    assert sorted(lookups) == ["/limited", "/open"]


def test_route_cache_evicts_least_recently_used(monkeypatch):
    from app.core import rate_limit

    monkeypatch.setattr(rate_limit, "_ROUTE_CACHE_MAX", 2)
    monkeypatch.setattr(
        rate_limit, "_slowapi_find_route_handler", lambda routes, scope: scope["path"]
    )
    rate_limit._route_handlers.clear()
    routes = []
    scope = lambda path: {"type": "http", "method": "GET", "path": path}  # noqa: E731
    find = rate_limit._cached_find_route_handler
    find(routes, scope("/a"))
    find(routes, scope("/b"))
    find(routes, scope("/a"))  # /a is now the most recently used
    find(routes, scope("/c"))  # evicts /b, not /a
    assert [k[-1] for k in rate_limit._route_handlers] == ["/a", "/c"]
