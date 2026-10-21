import logging

from fastapi import Request
from slowapi import Limiter
from slowapi import middleware as _slowapi_middleware
from slowapi.middleware import SlowAPIASGIMiddleware, _ASGIMiddlewareResponder
from slowapi.util import get_remote_address

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_user_or_ip(request: Request) -> str:
    """Extract user ID from JWT for per-user rate limiting, fallback to IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ccx_"):
        # Browser-extension device token: limit per paired device.
        import hashlib

        return "ext:" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    # Only a subject the middleware already verified counts. Reading it from
    # the unverified token would let a forged JWT with a random `sub` get a
    # fresh bucket on every request, including on public routes.
    user = getattr(request.state, "user", None)
    if isinstance(user, dict) and user.get("sub"):
        return str(user["sub"])
    return get_remote_address(request)


def _storage_uri() -> str:
    """Production keeps counters in Redis so every uvicorn worker (and every
    future app VM) shares one budget per user; in-memory counters would give
    each process its own and multiply every limit by the worker count."""

    if settings.APP_ENV == "production" and settings.REDIS_URL:
        return settings.REDIS_URL
    return "memory://"


# swallow_errors: if Redis is unreachable the limiter logs and lets the
# request through (fails open) rather than turning a Redis blip into 500s.

limiter = Limiter(
    key_func=_get_user_or_ip,
    storage_uri=_storage_uri(),
    swallow_errors=True,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
)

# Stricter limits for sensitive endpoints (use as decorator):
# @limiter.limit("5/minute")  — for API key submission
# @limiter.limit("10/minute") — for auth endpoints


class _StreamingSafeResponder(_ASGIMiddlewareResponder):
    """slowapi 0.1.10 re-sends ``http.response.start`` before every body chunk,
    which aborts streamed responses (SSE) on their second chunk. Send it once."""

    async def send_wrapper(self, message) -> None:
        if message["type"] == "http.response.body" and self.initial_message is None:
            await self.send(message)
            return
        await super().send_wrapper(message)
        if message["type"] == "http.response.body":
            self.initial_message = None


class StreamingSafeSlowAPIMiddleware(SlowAPIASGIMiddleware):
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        await _StreamingSafeResponder(self.app)(scope, receive, send)


# slowapi's ASGI middleware finds each request's handler by matching every route
# (153 here, no early exit) before FastAPI routes the request again: ~16% of the
# request path's Python time under load. Routes are fixed after startup, so the
# answer for a method + path never changes; remember it.
_route_handlers: dict[tuple, object] = {}
_ROUTE_CACHE_MAX = 4096  # ponytail: clear-when-full, LRU if id-heavy paths churn it
_slowapi_find_route_handler = _slowapi_middleware._find_route_handler


def _cached_find_route_handler(routes, scope):
    key = (scope["type"], scope.get("method"), scope.get("root_path", ""), scope["path"])
    try:
        return _route_handlers[key]
    except KeyError:
        handler = _slowapi_find_route_handler(routes, scope)
        if len(_route_handlers) >= _ROUTE_CACHE_MAX:
            _route_handlers.clear()
        _route_handlers[key] = handler
        return handler


_slowapi_middleware._find_route_handler = _cached_find_route_handler
