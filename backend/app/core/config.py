from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration — single source of truth for all env vars.

    Required vars cause a clear Pydantic ValidationError at startup if missing.
    Optional vars have safe defaults so the app starts without them.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Required — app exits with clear error if any are missing ────────
    APP_SECRET_KEY: str
    DATABASE_URL: str
    REDIS_URL: str

    # ── Clerk Auth (primary identity provider) ─────────────────────────
    # Clerk signs session tokens with RS256. Either point CLERK_JWKS_URL at the
    # instance JWKS endpoint directly, or set CLERK_ISSUER and let the JWKS URL
    # be derived as "{issuer}/.well-known/jwks.json".
    #   CLERK_ISSUER   e.g. https://clean-mudfish-42.clerk.accounts.dev
    #   CLERK_JWKS_URL e.g. https://clean-mudfish-42.clerk.accounts.dev/.well-known/jwks.json
    # CLERK_SECRET_KEY is only needed for Backend API calls (user lookups), not
    # for token verification.
    CLERK_JWKS_URL: str = ""
    CLERK_ISSUER: str = ""
    CLERK_SECRET_KEY: str = ""
    # Clerk session tokens carry no fixed `aud` by default. Set this only if the
    # instance is configured to emit one — when empty, audience is not checked.
    CLERK_AUDIENCE: str = ""

    # ── App environment ────────────────────────────────────────────────
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    FRONTEND_URL: str = "http://localhost:3000"

    # ── CORS ───────────────────────────────────────────────────────────
    CORS_ORIGINS: str = "http://localhost:3000"
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    # ── Next.js public URLs ────────────────────────────────────────────
    NEXT_PUBLIC_APP_URL: str = "http://localhost:3000"
    NEXT_PUBLIC_API_URL: str = "http://localhost:8000"
    # https address of this API as recipients' mail apps reach it. Open
    # tracking adds its pixel only when this is set to a public https URL.
    PUBLIC_API_URL: str | None = None

    # ── Redis ──────────────────────────────────────────────────────────
    REDIS_PASSWORD: str | None = None

    # ── LLM gateway ────────────────────────────────────────────────────
    LLM_GATEWAY_URL: str = "http://localhost:8000/llm-gateway/v1"

    # Private OpenBot relay, reached over a deployment network or SSH tunnel.
    SANDBOX_RELAY_URL: str = ""
    SANDBOX_RELAY_TOKEN: str = ""

    # ── Browser Use ────────────────────────────────────────────────────
    # Controller LLM — use a local Ollama model for cost-efficient navigation
    # steps. Set BROWSER_USE_OLLAMA_URL to your Ollama instance; leave empty
    # to fall back to the user's configured BYOK model.
    BROWSER_USE_OLLAMA_MODEL: str = "llama3.2"
    BROWSER_USE_OLLAMA_URL: str = ""  # e.g. "http://localhost:11434"
    # Session limits — cap concurrent Chromium sessions to avoid OOM.
    # Rule of thumb: floor(VPS_RAM_GB * 1.5); e.g. 4GB VPS → 6, 2GB VPS → 3.
    BROWSER_USE_MAX_CONCURRENT_SESSIONS: int = 4
    BROWSER_USE_SESSION_MEM_LIMIT_MB: int = 500
    # Debug screenshots — save a PNG on every browser task failure.
    # Mount BROWSER_DEBUG_DIR as a Docker volume for persistence.
    BROWSER_DEBUG_SCREENSHOTS: bool = False
    BROWSER_DEBUG_DIR: str = Field(
        default_factory=lambda: str(Path(tempfile.gettempdir()) / "browser_debug")
    )
    # Human-like delay ranges per action type (milliseconds).
    # Lowering these increases detection risk; raising them slows runs.
    BROWSER_DELAY_NAVIGATE_MIN_MS: int = 1500  # page navigation
    BROWSER_DELAY_NAVIGATE_MAX_MS: int = 3500
    BROWSER_DELAY_FILL_MIN_MS: int = 300  # form field fill
    BROWSER_DELAY_FILL_MAX_MS: int = 800
    BROWSER_DELAY_CLICK_MIN_MS: int = 200  # button / link click
    BROWSER_DELAY_CLICK_MAX_MS: int = 600
    BROWSER_DELAY_EXTRACT_MIN_MS: int = 500  # wait after page load for extraction
    BROWSER_DELAY_EXTRACT_MAX_MS: int = 1500

    # ── External API keys (all optional) ───────────────────────────────
    # Recruiter email finders and verifiers: bring your own keys. With no
    # verifier key set, no address counts as verified and every one is
    # held for the member to confirm.
    HUNTER_API_KEY: str = ""
    PROSPEO_API_KEY: str = ""
    FINDYMAIL_API_KEY: str = ""
    APOLLO_API_KEY: str = ""
    ZEROBOUNCE_API_KEY: str = ""
    NEVERBOUNCE_API_KEY: str = ""
    MILLIONVERIFIER_API_KEY: str = ""
    PROXYCURL_API_KEY: str = ""
    EXA_API_KEY: str = ""
    RESEND_API_KEY: str = ""
    # Must be on a domain verified in the Resend account, or every send is rejected.
    RESEND_FROM_EMAIL: str = "noreply@jobagent.ai"
    YOUTUBE_API_KEY: str = ""
    AGENTQL_API_KEY: str | None = None
    FIRECRAWL_API_KEY: str | None = None
    SEARXNG_URL: str | None = None
    RAPIDAPI_KEY: str | None = None
    JOOBLE_API_KEY: str | None = None
    CAREERJET_API_KEY: str | None = None
    # Careerjet asks for the IP of the person searching; this is the server's.
    CAREERJET_USER_IP: str = "127.0.0.1"
    GOOGLE_CSE_API_KEY: str | None = None
    GOOGLE_CSE_ID: str | None = None
    # Startup-news RSS feeds read for "X raises ..." headlines; empty disables.
    FUNDING_FEEDS: list[str] = [
        "https://inc42.com/feed/",
        "https://yourstory.com/feed",
        "https://entrackr.com/feed",
    ]
    ADZUNA_APP_ID: str | None = None
    ADZUNA_APP_KEY: str | None = None
    # Reed (UK) API key, sent as the basic-auth username. Free at reed.co.uk/developers.
    REED_API_KEY: str | None = None
    # USAJobs (US federal) key and the registered contact email sent as User-Agent.
    USAJOBS_API_KEY: str | None = None
    USAJOBS_USER_AGENT: str | None = None
    WORKABLE_API_TOKENS: dict[str, str] = {}
    JOB_SOURCE_OVERRIDES: list[dict] = []

    # ── Search providers ───────────────────────────────────────────────
    TAVILY_API_KEY: str | None = None
    BRAVE_API_KEY: str | None = None
    SERPAPI_API_KEY: str | None = None
    BING_SEARCH_API_KEY: str | None = None
    DUCKDUCKGO_ENABLED: bool = True
    MOJEEK_ENABLED: bool = True

    # ── Agent configuration ────────────────────────────────────────────
    AGENT_DEFAULT_TIMEOUT_S: int = 60
    AGENT_MAX_CONCURRENT_PER_USER: int = 2
    # ── Application limits (protect the member's accounts on job boards) ──
    # Applications started per rolling 24 hours, the least gap between two,
    # and the match score an automatic application needs (0-100).
    APPLY_DAILY_CAP: int = 25
    APPLY_MIN_GAP_SECONDS: int = 120
    AUTO_APPLY_MIN_SCORE: int = 70
    AGENT_THINKING_BUDGET_TOKENS: int = 8000
    # Upper bound for one agent execution activity (LLM calls + tools).
    WORKFLOW_TASK_TIMEOUT_S: int = Field(default=300, ge=30, le=1800)
    # How long a run may sit in awaiting_approval before it expires.
    AGENT_APPROVAL_TIMEOUT_S: int = Field(default=48 * 3600, ge=60, le=14 * 24 * 3600)

    # ── Temporal (the execution engine for every agent run and schedule) ─
    # The API starts and signals workflows; `python -m app.temporal_worker`
    # executes them. Nothing runs unless at least one worker polls
    # TEMPORAL_TASK_QUEUE.
    TEMPORAL_ADDRESS: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_TASK_QUEUE: str = "careercraft"
    # TLS/mTLS — required for Temporal Cloud, optional for a self-hosted dev
    # server. Leave all three empty to connect in plaintext (local dev only).
    TEMPORAL_TLS_CERT_PATH: str = ""
    TEMPORAL_TLS_KEY_PATH: str = ""
    TEMPORAL_TLS_CA_PATH: str = ""
    TEMPORAL_WORKER_CONCURRENCY: int = Field(default=4, ge=1, le=64)
    # Notifications run on their own task queues, polled by a separate
    # notification_worker.py process (can run on a different machine) —
    # so a notification failure never shares retry scope with, or blocks
    # the worker capacity of, the job-search/follow-up activity that
    # triggered it. The email queue additionally gets its own rate limit
    # below, independent of TEMPORAL_WORKER_CONCURRENCY.
    TEMPORAL_NOTIFICATION_TASK_QUEUE: str = "careercraft-notifications"
    TEMPORAL_NOTIFICATION_EMAIL_TASK_QUEUE: str = "careercraft-notifications-email"
    # Resend's default cap is 10 req/s/team; stay under it to leave headroom
    # for other API traffic sharing the same team/key.
    NOTIFICATION_EMAIL_RATE_LIMIT_PER_SECOND: float = Field(default=8.0, gt=0, le=100)
    TEMPORAL_ACTIVITY_START_TO_CLOSE_TIMEOUT_S: int = Field(default=120, ge=10, le=1800)
    TEMPORAL_ACTIVITY_HEARTBEAT_TIMEOUT_S: int = Field(default=30, ge=5, le=300)
    TEMPORAL_ACTIVITY_HEARTBEAT_INTERVAL_S: int = Field(default=5, ge=1, le=60)
    # Recurring jobs registered as Temporal Schedules by the worker at start.
    TEMPORAL_SCHEDULES_ENABLED: bool = True
    DAILY_SEARCH_INTERVAL_HOURS: int = Field(default=24, ge=1, le=168)
    INBOX_STATUS_INTERVAL_HOURS: int = Field(default=24, ge=1, le=168)
    # How often approved recruiter emails are sent and replies are checked.
    DAILY_SUMMARY_INTERVAL_HOURS: int = Field(default=24, ge=1, le=168)
    AUTO_APPLY_QUEUE_INTERVAL_HOURS: int = Field(default=6, ge=1, le=168)
    # Applications the agent queues per member per run, on top of the daily cap.
    AUTO_APPLY_QUEUE_BATCH: int = Field(default=3, ge=1, le=10)
    OUTREACH_INTERVAL_MINUTES: int = Field(default=30, ge=5, le=1440)
    MAINTENANCE_INTERVAL_SECONDS: int = Field(default=60, ge=30, le=3600)

    # ── Job applications ───────────────────────────────────────────────
    # Fill and submit in the user's own browser through the
    # CareerCraft extension (they stay logged in to LinkedIn/Naukri there).
    # A task nobody claims in this window expires (extension offline).
    EXTENSION_TASK_CLAIM_TIMEOUT_S: int = Field(default=24 * 3600, ge=60, le=7 * 24 * 3600)
    # Once claimed, the user has this long to review and submit.
    EXTENSION_TASK_COMPLETE_TIMEOUT_S: int = Field(default=2 * 3600, ge=60, le=24 * 3600)

    # ── Decision engine for in-browser choices ─────────────────────────
    # System One models answer typed questions (choice / score / noul) with
    # calibrated probabilities in milliseconds — which field is this,
    # which option matches, is this a confirmation page. "auto" uses Jev
    # when TYPESAFE_API_KEY is set, else a self-hosted Laya at LAYA_URL,
    # else the built-in heuristics.
    DECISION_ENGINE_PROVIDER: Literal["auto", "jev", "laya", "none"] = "auto"
    DECISION_ENGINE_TIMEOUT_S: float = Field(default=5.0, gt=0, le=60)
    DECISION_ENGINE_MIN_CONFIDENCE: float = Field(default=0.6, ge=0, le=1)
    TYPESAFE_API_KEY: str = ""
    TYPESAFE_BASE_URL: str = "https://api.typesafe.ai"
    TYPESAFE_MODEL: str = "jev-latest"
    LAYA_URL: str = ""  # e.g. http://laya:8088 — see deploy/laya
    LAYA_API_KEY: str = ""

    # Nango keeps provider OAuth tokens outside the application database. The
    # secret and webhook signing key are server-only; the public key is only
    # available to the frontend when Nango's SDK requires it.
    # Gmail/Drive have no direct-OAuth fallback — this must stay enabled, and
    # validate_nango_configuration() fails startup loudly if its secrets are
    # missing rather than letting the app boot with those integrations dead.
    NANGO_ENABLED: bool = True
    NANGO_BASE_URL: str = "https://api.nango.dev"
    NANGO_SECRET_KEY: str = ""
    NANGO_PUBLIC_KEY: str = ""
    NANGO_GITHUB_PUBLIC_ONLY: bool = False
    NANGO_WEBHOOK_SECRET: str = ""
    # JSON mapping from product provider to this environment's Nango
    # integration unique key, e.g. {"gmail": "google-mail-production"}.
    # These identifiers are deployment configuration, not Nango catalog names.
    NANGO_PROVIDER_CONFIG_KEYS: dict[str, str] = {}
    NANGO_REQUEST_TIMEOUT_S: int = Field(default=15, ge=1, le=60)

    # ── RAG configuration ──────────────────────────────────────────────
    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 50
    RAG_TOP_K: int = 5
    EMBEDDING_PROVIDER: str = ""
    # Used only when EMBEDDING_PROVIDER stands in for a chat provider that has
    # no embeddings API (the chat provider's own key is never reused).
    EMBEDDING_API_KEY: str = ""
    # Server-side Ollama for embeddings (e.g. http://10.0.0.182:11434) — used
    # for EMBEDDING_PROVIDER=ollama and for memory embeddings when a user has no
    # own Ollama URL. Empty = http://localhost:11434. Operator-set, so it is not
    # checked against the per-user OLLAMA_ALLOWED_HOSTS list.
    EMBEDDING_OLLAMA_URL: str = ""

    # ── Rate limiting ──────────────────────────────────────────────────
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_AGENT_RUN: str = "10/minute"
    RATE_LIMIT_UPLOAD: str = "5/minute"
    RATE_LIMIT_STR: str = "100/minute"

    # ── Document storage ───────────────────────────────────────────────
    # Uploaded resumes/documents live on local disk (self-hosted Postgres too —
    # both moved off Supabase because the deployment VM has no outbound IPv6,
    # which Supabase's managed hosts require). In Docker this is a named volume
    # shared by backend and temporal-worker; back it up, nothing else holds a copy.
    DOCUMENT_STORAGE_DIR: str = "/data/documents"

    @model_validator(mode="after")
    def _inject_redis_password(self) -> Settings:
        if (
            self.REDIS_PASSWORD
            and self.REDIS_URL.startswith("redis://")
            and "@" not in self.REDIS_URL.split("redis://", 1)[1].split("/", 1)[0]
        ):
            self.REDIS_URL = self.REDIS_URL.replace(
                "redis://", f"redis://:{self.REDIS_PASSWORD}@", 1
            )
        return self

    def validate_temporal_configuration(self) -> None:
        """Raise a clear startup error for invalid Temporal settings."""
        problems: list[str] = []
        for name, value in (
            ("TEMPORAL_ADDRESS", self.TEMPORAL_ADDRESS),
            ("TEMPORAL_NAMESPACE", self.TEMPORAL_NAMESPACE),
            ("TEMPORAL_TASK_QUEUE", self.TEMPORAL_TASK_QUEUE),
        ):
            if not value.strip():
                problems.append(f"{name} must be set")

        has_cert = bool(self.TEMPORAL_TLS_CERT_PATH)
        has_key = bool(self.TEMPORAL_TLS_KEY_PATH)
        if has_cert != has_key:
            problems.append(
                "both TEMPORAL_TLS_CERT_PATH and TEMPORAL_TLS_KEY_PATH must be set together"
            )
        elif has_cert:
            for name, raw_path in (
                ("TEMPORAL_TLS_CERT_PATH", self.TEMPORAL_TLS_CERT_PATH),
                ("TEMPORAL_TLS_KEY_PATH", self.TEMPORAL_TLS_KEY_PATH),
                ("TEMPORAL_TLS_CA_PATH", self.TEMPORAL_TLS_CA_PATH),
            ):
                if raw_path and not Path(raw_path).is_file():
                    problems.append(f"{name} does not point to a readable file")

        if (
            self.TEMPORAL_ACTIVITY_HEARTBEAT_TIMEOUT_S
            >= self.TEMPORAL_ACTIVITY_START_TO_CLOSE_TIMEOUT_S
        ):
            problems.append(
                "TEMPORAL_ACTIVITY_HEARTBEAT_TIMEOUT_S must be lower than "
                "TEMPORAL_ACTIVITY_START_TO_CLOSE_TIMEOUT_S"
            )
        if self.TEMPORAL_ACTIVITY_HEARTBEAT_INTERVAL_S * 2 >= (
            self.TEMPORAL_ACTIVITY_HEARTBEAT_TIMEOUT_S
        ):
            problems.append(
                "TEMPORAL_ACTIVITY_HEARTBEAT_INTERVAL_S must be less than half of "
                "TEMPORAL_ACTIVITY_HEARTBEAT_TIMEOUT_S"
            )

        if problems:
            raise ValueError("Invalid Temporal configuration: " + "; ".join(problems))

    def validate_nango_configuration(self) -> None:
        """Reject an enabled gateway without its backend-only credentials."""
        if not self.NANGO_ENABLED:
            return

        problems: list[str] = []
        if not self.NANGO_SECRET_KEY.strip():
            problems.append("NANGO_SECRET_KEY must be set when NANGO_ENABLED=true")
        if not self.NANGO_WEBHOOK_SECRET.strip():
            problems.append("NANGO_WEBHOOK_SECRET must be set when NANGO_ENABLED=true")
        if not self.NANGO_BASE_URL.startswith(("https://", "http://")):
            problems.append("NANGO_BASE_URL must be an http(s) URL")
        if problems:
            raise ValueError("Invalid Nango configuration: " + "; ".join(problems))

    @property
    def REDIS_URL_SAFE(self) -> str:
        import re

        url = self.REDIS_URL
        return re.sub(r"://.*@", "://***@", url)


settings = Settings()
