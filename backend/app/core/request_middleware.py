"""Per-request API middleware: request ID, security headers and Clerk JWT auth.

One pure ASGI layer instead of three ``@app.middleware("http")`` wrappers.
Each ``BaseHTTPMiddleware`` re-wraps the request and streams the response
through an extra task; together they cost ~1.15 ms of CPU per request,
several times the route itself. Behaviour is unchanged:

- every response (including the 401s raised here) carries ``X-Request-ID``
  and the security headers, replacing any a route set;
- ``request.state.request_id`` and ``request.state.user`` are set for routes
  and the rate limiter;
- the current-user contextvar is set for the whole request, streamed body
  included.
"""

from __future__ import annotations

import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
# Swagger UI / ReDoc (non-production only) load scripts and styles from a CDN.
_DOCS_PATHS = ("/docs", "/redoc")
_HSTS = "max-age=63072000; includeSubDomains"

PUBLIC_PATHS = {
    "/health",
    "/health/live",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/integrations/webhooks/nango",
    # Public marketing-site demo: intentionally unauthenticated, gated
    # instead by a per-IP lifetime search cap (see app/api/v1/demo.py).
    "/api/v1/demo/job-search",
    "/api/v1/demo/job-search/quota",
}
# Authenticated by their own mechanism, never by a Clerk session JWT:
_SELF_AUTHED_PREFIXES = (
    # Browser extension device token, checked by the extension router.
    "/api/v1/extension/device/",
    # Open-tracking pixel fetched by the recipient's mail app; its token is
    # unguessable and it only ever records "opened".
    "/api/v1/outreach/open/",
    # Internal LLM calls use short-lived Redis sessions validated by the gateway.
    "/llm-gateway/v1/",
)


def _needs_jwt(scope: Scope) -> bool:
    path = scope["path"]
    if path in PUBLIC_PATHS or path.startswith(_SELF_AUTHED_PREFIXES):
        return False
    return scope["method"] != "OPTIONS"


class ApiRequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        rid = headers.get("x-request-id") or str(uuid.uuid4())
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        is_docs = scope["path"].startswith(_DOCS_PATHS)
        is_https = scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["X-Request-ID"] = rid
                for name, value in _SECURITY_HEADERS.items():
                    out[name] = value
                if not is_docs:
                    out["Content-Security-Policy"] = _API_CSP
                if is_https:
                    out["Strict-Transport-Security"] = _HSTS
            await send(message)

        if not _needs_jwt(scope):
            await self.app(scope, receive, send_with_headers)
            return

        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            await JSONResponse(
                status_code=401, content={"detail": "Missing or invalid Authorization header"}
            )(scope, receive, send_with_headers)
            return

        from app.core.clerk_auth import verify_token_cached
        from app.core.request_context import reset_current_user_id, set_current_user_id

        try:
            payload = await verify_token_cached(auth_header.split(" ", 1)[1])
        except Exception:
            await JSONResponse(status_code=401, content={"detail": "Invalid or expired token"})(
                scope, receive, send_with_headers
            )
            return

        state["user"] = payload
        # Graph nodes (e.g. the AG-UI chat copilot) have no route dependency to
        # receive the user through — they read this instead.
        identity_token = set_current_user_id(str(payload.get("sub") or "") or None)
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            reset_current_user_id(identity_token)
