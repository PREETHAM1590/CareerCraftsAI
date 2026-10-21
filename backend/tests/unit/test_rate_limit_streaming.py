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


def test_route_lookup_is_cached_and_limits_still_apply():
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
    client = TestClient(app)
    assert [client.get("/limited").status_code for _ in range(3)] == [200, 200, 429]
    assert [client.get("/open").status_code for _ in range(3)] == [200, 200, 200]
    # One lookup per distinct method+path, reused afterwards.
    assert {k[3] for k in rate_limit._route_handlers} == {"/limited", "/open"}
