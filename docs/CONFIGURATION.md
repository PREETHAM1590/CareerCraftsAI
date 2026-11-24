# Configuration Reference

All configuration is loaded from environment variables. The backend uses `pydantic-settings` (`app/core/config.py`) — values are validated at startup and the app exits with a clear error if required variables are missing.

Copy `.env.example` to `.env` and fill in values before starting.

---

## Required Variables

These must be set for the app to start.

| Variable | Example | Description |
|---|---|---|
| `APP_SECRET_KEY` | `openssl rand -hex 32` | AES-256 master key for API key encryption. 32+ bytes. |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:pass@db.xyz.supabase.co:5432/postgres` | Async PostgreSQL connection string. Must use `asyncpg` driver. |
| `SUPABASE_URL` | `https://xyz.supabase.co` | Supabase project URL. |
| `SUPABASE_SERVICE_KEY` | `eyJhbGci...` | Supabase `service_role` key. Never expose to frontend. |
| `SUPABASE_JWT_SECRET` | `your-jwt-secret` | From Supabase → Settings → API → JWT Secret. Used to verify user tokens locally. |
| `NEXT_PUBLIC_SUPABASE_URL` | `https://xyz.supabase.co` | Supabase URL exposed to Next.js client. |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | `eyJhbGci...` | Supabase `anon` key exposed to Next.js client. Safe to expose. |
| `REDIS_URL` | `redis://redis:6379` | Redis connection URL. Use `redis://redis:6379` when running via Docker Compose. |

---

## Application Settings

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `development` | `development` or `production`. Production disables `/docs` and debug logging. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed CORS origins. In production: `https://yourdomain.com` |
| `NEXT_PUBLIC_APP_URL` | `http://localhost:3000` | Full frontend URL. Used for OAuth redirect construction. |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend URL as seen by the browser. |
| `PUBLIC_API_URL` | unset | Public https address of the API as recipients' mail apps reach it. Open tracking adds its pixel only when this is a public https URL (never localhost). |

---

## External API Keys

All optional — features gracefully degrade if not set.

| Variable | Provider | Used By | Description |
|---|---|---|---|
| `HUNTER_API_KEY` | [hunter.io](https://hunter.io) | Recruiter outreach, EmailAgent, LeadsAgent | Finds recruiter email addresses by domain + name. |
| `JOOBLE_API_KEY` | [jooble.org](https://jooble.org/api/about) | Job discovery | Enables the Jooble source (India). Inactive without a key. |
| `REED_API_KEY` | [reed.co.uk/developers](https://www.reed.co.uk/developers) | Job discovery | Enables the Reed source (UK). Inactive without a key. |
| `USAJOBS_API_KEY`, `USAJOBS_USER_AGENT` | [developer.usajobs.gov](https://developer.usajobs.gov) | Job discovery | Enables the USAJobs source (US federal). `USAJOBS_USER_AGENT` is the email you registered with. Inactive without both. |
| `CAREERJET_API_KEY`, `CAREERJET_USER_IP` | [Careerjet partners](https://www.careerjet.com/partners/api) | Job discovery | Enables the Careerjet source (India). Inactive without a key. |
| `GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_ID` | [Programmable Search](https://programmablesearch.google.com/) | Job discovery | Enables the `google:*` sources, which search for postings on Greenhouse, Lever, Ashby and Workday. Inactive without both. Free quota is 100 queries a day; each source uses one per day. |
| `FUNDING_FEEDS` | Startup-news RSS (default: Inc42, YourStory) | Job ranking | JSON list of feeds. Headlines like "Acme raises $5M" mark Acme as recently funded for 45 days and add a small ranking bonus. `[]` turns it off. |
| `APOLLO_API_KEY` | [apollo.io](https://apollo.io) | Recruiter outreach | Second finder; matches a named recruiter at a company. |
| `PROSPEO_API_KEY` / `FINDYMAIL_API_KEY` | [prospeo.io](https://prospeo.io) / [findymail.com](https://findymail.com) | Recruiter outreach | More finders, tried after Hunter and Apollo. Inert without a key. Response shapes follow their documentation and were not run live. |
| `ZEROBOUNCE_API_KEY` / `NEVERBOUNCE_API_KEY` / `MILLIONVERIFIER_API_KEY` | the provider | Recruiter outreach | Verifies an address before it is emailed. Without one, nothing counts as verified and every email waits for the member's approval. |
| `APPLY_DAILY_CAP`, `APPLY_MIN_GAP_SECONDS`, `AUTO_APPLY_MIN_SCORE` | none | Applications | Applications per rolling 24 hours (25), seconds between two (120), and the match score an automatic application needs (70). |
| `PROXYCURL_API_KEY` | [proxycurl.com](https://proxycurl.com) | LinkedInOutreachAgent | Optional fallback contact lookup, used only when a request supplies no contacts. Without it, members supply contacts. |
| `EXA_API_KEY` | [exa.ai](https://exa.ai) | CompanyResearchAgent, SalaryAgent, InterviewPrepAgent | Neural web search for research tasks. |
| `RESEND_API_KEY` | [resend.com](https://resend.com) | ResendService | Sends transactional emails (account notifications, not job emails). |
| `RESEND_FROM_EMAIL` | — | ResendService | Sender address, e.g. `noreply@careercraftsai.me`. Its domain must be verified in Resend or every send is rejected. Default `noreply@jobagent.ai`. |
| `YOUTUBE_API_KEY` | [Google Cloud](https://console.cloud.google.com) | InterviewPrepAgent | Fetches interview prep videos (optional enrichment). |

---

## Browser Use (retired)

Server-side browser automation is retired: no code starts Chromium, and the backend does not drive job sites. The former browser session, debug-screenshot, delay, and anti-detection settings no longer exist. `BROWSER_USE_OLLAMA_MODEL` and `BROWSER_USE_OLLAMA_URL` are still defined in `backend/app/core/config.py`, but nothing reads them.

### Ollama allow-list

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_ALLOWED_HOSTS` | `localhost:11434,127.0.0.1:11434,[::1]:11434` | Comma-separated `host:port` allow-list for the **user-facing** `ollama` BYOK provider's base URL (Settings → AI Models). The server fetches this URL directly, so anything outside this list — including loopback/link-local/metadata addresses reached via a different host string — is rejected at input (CWE-918 hardening). Add your own Ollama host here if it doesn't run on localhost. |

### Docker Compose resource configuration

Applications run in the user's paired browser extension. Backend and worker
containers do not provision application browsers or store portal sessions.
See the deployment guide for workflow draining and the reversible archive migration.

---

## Agent Settings

| Variable | Default | Description |
|---|---|---|
| `AGENT_DEFAULT_TIMEOUT_S` | `60` | Default agent run timeout. Overridden per-agent (see AGENTS.md). |
| `AGENT_MAX_CONCURRENT_PER_USER` | `2` | Max concurrent agent runs per user. Enforced by `POST /agents/run` itself (`api/v1/agents.py`), which counts the user's `queued`/`running` `agent_runs` rows and returns `429` above this limit — not a Temporal or worker-side concurrency setting. Applications waiting in the user's own browser (extension mode) use no server capacity and are excluded from the count. |
| `AGENT_THINKING_BUDGET_TOKENS` | `8000` | Extended thinking token budget for Claude. |
| `RAG_CHUNK_SIZE` | `500` | Document chunk size in tokens. |
| `RAG_CHUNK_OVERLAP` | `50` | Chunk overlap in tokens. |
| `RAG_TOP_K` | `5` | Number of chunks to retrieve per RAG query. |

---

## Embeddings

Used by RAG (`app/services/rag_service.py`) and agent memory (`memory/embedder.py`).

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_PROVIDER` | *(empty)* | `openai`, `google` or `ollama`. Required when the user's chat provider has no embeddings API (Anthropic, DeepSeek, OpenRouter, NVIDIA NIM); otherwise uploads are saved but not indexed. |
| `EMBEDDING_API_KEY` | *(empty)* | API key for the `openai`/`google` fallback. The chat provider's key is never reused. |
| `EMBEDDING_OLLAMA_URL` | *(empty → `http://localhost:11434`)* | Server-side Ollama host for embeddings, e.g. `http://10.0.0.182:11434`. Used for `EMBEDDING_PROVIDER=ollama` and for memory embeddings when the user has no own Ollama URL; a user's own `ollama_url` wins only when their provider is `ollama`. Operator-set, so not checked against `OLLAMA_ALLOWED_HOSTS`. The host must have `qwen3-embedding:0.6b` pulled. |

Models: OpenAI `text-embedding-3-small` (1536-d), Google `gemini-embedding-001` (768-d), Ollama `qwen3-embedding:0.6b` (1024-d). Collections are named per provider and dimension, so after a model or dimension change users must re-upload documents to re-index them.

---

## Rate Limiting

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_DEFAULT` | `60/minute` | Default rate limit for all authenticated endpoints. |
| `RATE_LIMIT_AGENT_RUN` | `10/minute` | Rate limit for `POST /agents/run`. |
| `RATE_LIMIT_UPLOAD` | `5/minute` | Rate limit for document uploads. |

---

## Temporal

Every agent run, job search, application and follow-up executes as a durable
Temporal workflow. `python -m app.temporal_worker` (the `temporal-worker`
service) must have at least one instance running and polling
`TEMPORAL_TASK_QUEUE`, or `GET /health` reports `status: "degraded"` and
nothing the API starts makes progress.

| Variable | Default | Description |
|---|---|---|
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal server address for host-run processes. |
| `TEMPORAL_ADDRESS_DOCKER` | `temporal:7233` | Address used inside Docker Compose containers instead of `TEMPORAL_ADDRESS` (so they never resolve their own localhost). Point it at Temporal Cloud to use a managed deployment. |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace. |
| `TEMPORAL_TASK_QUEUE` | `careercraft` | Task queue the worker polls and the API starts workflows on. |
| `TEMPORAL_WORKER_CONCURRENCY` | `4` | Max concurrent activities per worker process (`max_concurrent_activities`). Scale further by running more `temporal-worker` replicas. |
| `WORKFLOW_TASK_TIMEOUT_S` | `300` | Max duration of one agent execute/continue activity. |
| `AGENT_APPROVAL_TIMEOUT_S` | `172800` (48h) | How long a run may sit at an approval checkpoint before it expires. |
| `TEMPORAL_ACTIVITY_START_TO_CLOSE_TIMEOUT_S` | `120` | Upper bound for one activity attempt. |
| `TEMPORAL_ACTIVITY_HEARTBEAT_TIMEOUT_S` | `30` | Heartbeat timeout for long-running activities. |
| `TEMPORAL_ACTIVITY_HEARTBEAT_INTERVAL_S` | `5` | How often a heartbeating activity checks in. |
| `TEMPORAL_SCHEDULES_ENABLED` | `true` | Whether the worker registers the recurring Schedules at start-up. |
| `DAILY_SEARCH_INTERVAL_HOURS` | `24` | Interval of the `daily-job-search` Temporal Schedule. |
| `MAINTENANCE_INTERVAL_SECONDS` | `60` | Interval of the `maintenance` Schedule (reconciles `agent_runs` and expires stale extension tasks). |
| `TEMPORAL_TLS_CERT_PATH` / `TEMPORAL_TLS_KEY_PATH` / `TEMPORAL_TLS_CA_PATH` | *(empty)* | mTLS certificate paths for Temporal Cloud. Empty connects in plaintext (local dev only). |

---

## Job Applications (extension / decision engine)

| Variable | Default | Description |
|---|---|---|
| `EXTENSION_TASK_CLAIM_TIMEOUT_S` | `86400` (24h) | An extension task nobody claims (extension offline) expires after this long. |
| `EXTENSION_TASK_COMPLETE_TIMEOUT_S` | `7200` (2h) | Once claimed, how long the user has to review and press Submit. |
| `DECISION_ENGINE_PROVIDER` | `auto` | `auto` (Jev if `TYPESAFE_API_KEY` is set, else Laya if `LAYA_URL` is set, else heuristics), or force `jev` / `laya` / `none`. |
| `DECISION_ENGINE_TIMEOUT_S` | `5.0` | Timeout for one decision-engine HTTP call. |
| `DECISION_ENGINE_MIN_CONFIDENCE` | `0.6` | Answers below this confidence are left to the user instead of acted on. |
| `TYPESAFE_API_KEY` | *(empty)* | TypeSafe Jev API key (hosted "System One" decision model). |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Jev API base URL. |
| `TYPESAFE_MODEL` | `jev-latest` | Jev model name. |
| `LAYA_URL` | *(empty)* | Self-hosted Laya server URL (see `deploy/laya/`), e.g. `http://laya:8088`. |
| `LAYA_API_KEY` | *(empty)* | Laya API key, if the self-hosted server requires one. |

See [`extension/README.md`](../extension/README.md) and
[`deploy/laya/README.md`](../deploy/laya/README.md) for how the extension
and the decision engine work end to end.

---

## Nginx

Nginx reads environment from the host. Set in `nginx/nginx.conf`:

| Variable | Description |
|---|---|
| `${DOMAIN}` | Your domain name. Replace with `sed -i 's/${DOMAIN}/yourdomain.com/g' nginx/nginx.conf` |

---

## Supabase Storage

| Variable | Default | Description |
|---|---|---|
| `SUPABASE_STORAGE_BUCKET` | `documents` | Bucket name for uploaded resumes and generated PDFs. Created by migration 0003. |

---

## Nango integrations (Gmail and Drive)

Gmail and Drive are authorized through Nango, not Supabase Auth. Configure
`NANGO_ENABLED`, `NANGO_SECRET_KEY`, `NANGO_WEBHOOK_SECRET`, and the
environment-specific `NANGO_PROVIDER_CONFIG_KEYS` mapping described in
[`NANGO_INTEGRATION.md`](NANGO_INTEGRATION.md). Nango holds and refreshes
provider credentials; CareerCraft does not store provider OAuth tokens.

---

## Development vs Production Differences

| Setting | Development | Production |
|---|---|---|
| `APP_ENV` | `development` | `production` |
| `/docs` endpoint | Enabled | Disabled |
| Debug logging | Enabled | Disabled |
| CORS origins | `localhost:3000` | `https://yourdomain.com` |
| HTTPS | Optional | Required (Nginx + Certbot) |
| Redis persistence | Not required | `appendonly yes` |
| Supabase | Any project | Dedicated project with backups enabled |

---

## .env.example

The repo includes `.env.example` with all variables and inline comments. Always copy it:

```bash
cp .env.example .env
```

Never commit `.env` — it is in `.gitignore`. Never commit real API keys in any file.
