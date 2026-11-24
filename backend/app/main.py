from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1 import (
    agent_memory,
    agents,
    candidate_profile,
    company,
    computer,
    cover_letter,
    demo,
    email,
    extension,
    integrations,
    interview,
    interview_prep,
    jobs,
    leads,
    linkedin,
    metrics,
    notifications,
    outreach,
    rag,
    resume,
    salary,
    users,
)
from app.api.v1.copilot_chat import mount_copilot_chat
from app.core.config import settings
from app.core.llm_gateway import router as llm_gw
from app.core.rate_limit import StreamingSafeSlowAPIMiddleware, limiter
from app.core.request_middleware import ApiRequestMiddleware

logger = logging.getLogger(__name__)

_REQUIRED_VARS = [
    "APP_SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
]


def _check_env_vars() -> None:
    settings.validate_temporal_configuration()
    settings.validate_nango_configuration()
    missing = []
    for k in _REQUIRED_VARS:
        v = os.getenv(k)
        if not v or not v.strip():
            missing.append(k)
    if missing:
        logger.critical("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)
    logger.info("All %d required env vars present", len(_REQUIRED_VARS))
    # Clerk verifies every request. Warn loudly (but don't exit) so a misconfigured
    # deploy is obvious from the logs instead of only via blanket 401s.
    if not settings.CLERK_JWKS_URL and not settings.CLERK_ISSUER:
        logger.critical(
            "Clerk auth is not configured — set CLERK_ISSUER (or CLERK_JWKS_URL); "
            "every authenticated request will return 401 until you do"
        )


def _build_cors_origins(raw: str, env: str) -> list[str]:
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise ValueError("CORS_ORIGINS must not contain '*' — specify exact origins")
    if env != "production":
        for o in ("http://localhost:3000", "http://127.0.0.1:3000"):
            if o not in origins:
                origins.append(o)
    return origins


_cors_raw = settings.CORS_ORIGINS or settings.ALLOWED_ORIGINS or settings.FRONTEND_URL
_allowed_origins = _build_cors_origins(_cors_raw, settings.APP_ENV)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _check_env_vars()
    from app.integrations.factory import build_integration_gateway

    _app.state.integration_gateway = build_integration_gateway()

    from app.core.database import check_db_connection
    from app.core.redis_client import check_redis_connection

    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()
    if not db_ok:
        logger.error("Database connection failed at startup")
    if not redis_ok:
        logger.error("Redis connection failed at startup")

    try:
        from app.core.database import engine

        async with engine.begin() as conn:
            from sqlalchemy import text

            result = await conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            row = result.fetchone()  # CursorResult.fetchone() is synchronous
            if row:
                logger.info("pgvector installed: version %s", row[0])
            else:
                logger.warning("pgvector extension not installed — run CREATE EXTENSION vector")
    except Exception as exc:
        logger.warning("pgvector check failed: %s", exc)

    logger.info("Startup complete — db=%s redis=%s", db_ok, redis_ok)

    # Warm up the SSE publisher thread at startup so the first emit() never drops
    try:
        from app.core.event_bus import _ensure_publisher

        _ensure_publisher()
        logger.info("SSE publisher thread ready")
    except Exception as exc:
        logger.warning("SSE publisher warmup failed: %s", exc)

    yield
    logger.info("Shutting down")
    close_integration_gateway = getattr(_app.state.integration_gateway, "aclose", None)
    if close_integration_gateway is not None:
        await close_integration_gateway()
    from app.core.redis_client import close_redis

    await close_redis()
    from app.core.database import engine

    await engine.dispose()


# ── App factory ──────────────────────────────────────────────────
app = FastAPI(
    title="CareerCraft AI API",
    version="1.0.0",
    lifespan=_lifespan,
    docs_url="/docs" if settings.APP_ENV != "production" else None,
    redoc_url="/redoc" if settings.APP_ENV != "production" else None,
    openapi_url="/openapi.json" if settings.APP_ENV != "production" else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def _generic_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Starlette builds the stack so the LAST-registered middleware is OUTERMOST.
# CORS must therefore be registered last: a 401 returned by ApiRequestMiddleware has
# to travel back out through CORSMiddleware to pick up Access-Control-Allow-Origin,
# otherwise the browser reports an opaque CORS failure and the frontend can't tell
# an expired token from a dead network.
# Execution order: CORS → ApiRequestMiddleware (request ID, security headers,
# JWT) → route. One pure ASGI layer; see app/core/request_middleware.py.
app.add_middleware(StreamingSafeSlowAPIMiddleware)
app.add_middleware(ApiRequestMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    # Paginated lists (GET /jobs/applications) report their total here.
    expose_headers=["X-Total-Count", "X-Stage-Counts"],
)


# Routers
app.include_router(users.router, prefix="/api/v1")
app.include_router(demo.router, prefix="/api/v1")
app.include_router(agent_memory.router, prefix="/api/v1")
app.include_router(outreach.router, prefix="/api/v1")
app.include_router(metrics.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(rag.router, prefix="/api/v1")
app.include_router(resume.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(leads.router, prefix="/api/v1")
app.include_router(email.router, prefix="/api/v1")
app.include_router(agents.router, prefix="/api/v1")
app.include_router(computer.router, prefix="/api/v1")
app.include_router(interview_prep.router, prefix="/api/v1")
app.include_router(cover_letter.router, prefix="/api/v1")
app.include_router(interview.router, prefix="/api/v1")
app.include_router(salary.router, prefix="/api/v1")
app.include_router(company.router, prefix="/api/v1")
app.include_router(linkedin.router, prefix="/api/v1")
app.include_router(candidate_profile.router, prefix="/api/v1")
app.include_router(integrations.router, prefix="/api/v1")
app.include_router(extension.router, prefix="/api/v1")
app.include_router(agent_memory.router)

app.include_router(llm_gw)

# AG-UI chat endpoint (Career Copilot) — guarded by the JWT middleware like
# every other non-public path.
mount_copilot_chat(app)


# ── Health endpoints (no auth required) ──────────────────────────
# Bare liveness — no dependency checks, so a slow/degraded DB or Redis can't
# flap the load balancer's health probe. Use /health for deep diagnostics.
@app.get("/health/live")
@limiter.exempt
async def health_live():
    return {"status": "ok"}


_health_cache: dict = {"at": 0.0, "result": None}
_HEALTH_CACHE_SECONDS = 5.0


@app.get("/health")
@limiter.exempt
async def health():
    """The deep check opens database, Redis and Temporal connections and is
    public, so a burst of calls shares one result for a few seconds."""
    import time

    from fastapi.responses import JSONResponse

    now = time.monotonic()
    cached = _health_cache["result"]
    if cached is None or now - _health_cache["at"] > _HEALTH_CACHE_SECONDS:
        cached = await _compute_health()
        _health_cache.update(at=now, result=cached)
    status_code, content = cached
    return JSONResponse(status_code=status_code, content=content)


async def _compute_health() -> tuple[int, dict]:
    from app.core.database import check_db_connection
    from app.core.redis_client import check_redis_connection

    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()
    pgvector_ok = False
    try:
        from sqlalchemy import text

        from app.core.database import engine

        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            pgvector_ok = result.fetchone() is not None
    except Exception:
        pgvector_ok = False
    all_ok = bool(db_ok and redis_ok and pgvector_ok)
    # Temporal is reported but kept out of the 200/503 decision: pages and
    # read APIs keep working while it is down, and the container healthcheck
    # must not flap the whole stack on a Temporal restart. "degraded" makes
    # the outage visible — no server, or no worker polling the task queue,
    # means agent runs cannot start.
    from app.core.temporal_client import check_temporal_health

    temporal = await check_temporal_health()
    temporal_ok = temporal.get("connected") and temporal.get("workers") != 0
    status = "error" if not all_ok else ("ok" if temporal_ok else "degraded")
    return (
        200 if all_ok else 503,
        {
            "status": status,
            "version": app.version,
            "db": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error",
            "pgvector": "ok" if pgvector_ok else "error",
            "temporal": temporal,
        },
    )
