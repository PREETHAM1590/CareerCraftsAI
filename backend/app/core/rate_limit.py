import logging

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

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
    from app.core.config import settings

    if settings.APP_ENV == "production" and settings.REDIS_URL:
        return settings.REDIS_URL
    return "memory://"


# swallow_errors: if Redis is unreachable the limiter logs and lets the
# request through (fails open) rather than turning a Redis blip into 500s.
limiter = Limiter(key_func=_get_user_or_ip, storage_uri=_storage_uri(), swallow_errors=True)

# Stricter limits for sensitive endpoints (use as decorator):
# @limiter.limit("5/minute")  — for API key submission
# @limiter.limit("10/minute") — for auth endpoints
