# CareerCraft AI — Complete Architecture

> Verified against codebase: `deploy/oracle-vm/` (the actual production stack — see `docs/DEPLOYMENT.md`), `backend/app/main.py`, `backend/app/agents/orchestrator.py`, `backend/app/core/`, `backend/app/services/`, `backend/app/api/v1/`, `backend/app/workflows/`, `extension/`, `deploy/laya/`, `frontend/src/`, `supabase/migrations/`.
> Background jobs (agent runs, job search, applications, follow-ups, recurring jobs) run only as Temporal workflows — see §8.
> The root `docker-compose.yml` and `nginx/` this doc used to reference were a generic/alternate deployment path that was never the one actually used in production — both were retired; `deploy/oracle-vm/` is the only real deployment.

---

## 0. Repository Layout

```
CareerCraft AI/
├── frontend/               # Next.js App Router (port 3000)
│   └── src/
│       ├── app/(app)/      # dashboard, resume, jobs, applications, cover-letter,
│       │                   # linkedin, email, interview, interview-prep, company,
│       │                   # salary, leads, agents, onboarding, settings
│       ├── app/(auth)/ app/(marketing)/
│       ├── components/ lib/ store/  # api.ts, sse.ts, agentStore.ts, userSlice.ts
├── backend/                # FastAPI Python 3.12 (port 8000)
│   └── app/
│       ├── main.py         # app factory, JWT+CORS+request-id middleware, /health
│       ├── api/v1/         # routers (incl. extension.py)
│       ├── agents/         # orchestrator + sub-agents + memory/
│       ├── core/           # config, database, redis_client, event_bus,
│       │                   # supabase_auth, security, llm_gateway, model_router,
│       │                   # rate_limit, temporal_client
│       ├── services/       # rag, gmail, exa, browser, extension_service,
│       │                   # decision_engine, scheduled_jobs, workflow_service...
│       ├── workflows/      # every Temporal workflow + activity (see §8)
│       │   ├── registry.py     # WORKFLOWS / ACTIVITIES the worker hosts
│       │   ├── starters.py     # API-side start/signal helpers (503 if down)
│       │   ├── agent_run.py, job_search.py, auto_apply.py, followup.py
│       │   └── scheduled.py    # Temporal Schedules (daily search, maintenance)
│       ├── tools/ models/ schemas/
│       └── temporal_worker.py  # `python -m app.temporal_worker` — runs every workflow/activity
├── extension/               # Chrome extension — applies in the user's own browser
│   └── src/background.js (pairing, polling), src/content/ (fill + review panel)
├── deploy/laya/             # Self-hosted "System One" decision server (Jev alternative)
├── supabase/migrations/    # plain-PostgreSQL SQL migrations
├── nginx/                  # nginx.conf.template, nginx-dev.conf.template
├── docker-compose.yml      # frontend, backend, temporal-worker, temporal(+postgres+ui), redis, nginx
├── docker-compose.dev.yml / .test.yml
└── deploy/oracle-vm/ · scripts/
```

---

## 1. System-Level Deployment Diagram

```mermaid
graph TB
subgraph Client["Client Layer"]
  Browser["Browser<br/>Next.js SPA"]
end
subgraph Edge["Edge — nginx:80/443"]
  Nginx["nginx<br/>nginx.conf.template<br/>DOMAIN envsubst<br/>/ → frontend:3000<br/>/api/ → backend:8000"]
end
subgraph Frontend["Frontend — Next.js — frontend:3000"]
  NextApp["App Router<br/>src/app/(app)/<br/>dashboard, resume, jobs,<br/>applications, cover-letter,<br/>linkedin, email, interview,<br/>interview-prep, company,<br/>salary, leads, agents,<br/>onboarding, settings"]
  ApiLib["src/lib/api.ts<br/>axios + Supabase JWT<br/>deduplicatedGet"]
  SSEHook["src/lib/sse.ts<br/>useAgentStream()<br/>SSE + 3s poll reconcile"]
  Store["src/store/<br/>agentStore.ts, userSlice.ts"]
  NextApp --- ApiLib
  ApiLib --- SSEHook
  SSEHook --- Store
end
subgraph Backend["Backend — FastAPI — backend:8000"]
  JWT["JWT middleware<br/>main.py:96<br/>Clerk / Supabase verify"]
  Routers["API Routers — /api/v1<br/>users, rag, resume, jobs,<br/>leads, email, agents,<br/>browser, interview,<br/>interview_prep, cover_letter,<br/>salary, company, linkedin,<br/>extension, memory,<br/>llm-gateway"]
  Harness["AgentHarness<br/>agents/harness.py<br/>RAG inject + token track"]
  Orch["LangGraph Supervisor<br/>agents/orchestrator.py<br/>TASK_ROUTES + SSE emit"]
  Gateway["LLM Gateway<br/>core/llm_gateway.py<br/>session-token proxy<br/>Redis TTL 3600s"]
  EventBus["EventBus<br/>core/event_bus.py<br/>agent:{run_id}:events<br/>Pub/Sub → SSE"]
  Starters["workflows/starters.py<br/>start_workflow / signal<br/>raises 503 if Temporal is down"]
  Decision["decision_engine.py<br/>Jev / Laya / heuristics"]
end
subgraph Workers["Temporal"]
  TemporalSrv[("Temporal Server<br/>self-hosted (temporal service)<br/>or Temporal Cloud")]
  TWorker["temporal-worker<br/>python -m app.temporal_worker<br/>task_queue=careercraft<br/>AgentRunWorkflow, JobSearchWorkflow,<br/>AutoApplyWorkflow, FollowupWorkflow<br/>+ Schedules: daily search, maintenance"]
  TUI["Temporal UI<br/>inspect workflow history"]
end
subgraph ExtLayer["Browser Extension"]
  Ext["User's Chrome browser<br/>claims extension_tasks,<br/>fills + shows review panel,<br/>submits only on user click"]
end
subgraph Data["Data Layer"]
  PG[("Self-hosted Postgres 16<br/>+ pgvector<br/>plain-SQL migrations<br/>langchain_pg_embedding<br/>HNSW m=16 ef=64")]
  Redis[("Redis 8-alpine<br/>SSE pub/sub<br/>rate-limit counters<br/>LLM gateway sessions<br/>maxmemory 512mb — no queues")]
  Docs[("Document Store<br/>/data/documents<br/>local volume, shared by<br/>backend + temporal-worker")]
end
subgraph External["External Services"]
  Clerk["Clerk<br/>RS256 JWKS auth"]
  LLMProv["BYOK Providers<br/>OpenAI, Anthropic,<br/>Google, Ollama,<br/>Nvidia NIM, DeepSeek,<br/>OpenRouter"]
  Gmail["Gmail + Drive API<br/>via Nango"]
  JobBoards["Job Boards<br/>ATS career boards via job_connectors,<br/>JobSpy (LinkedIn, Indeed),<br/>Remotive, RemoteOK, Arbeitnow"]
  Enrich["Enrichment<br/>Hunter, ProxyCurl,<br/>Exa, Tavily/Brave/Serp,<br/>YouTube, Resend"]
  DecisionSvc["TypeSafe Jev (hosted)<br/>or self-hosted Laya"]
end
Browser --> Nginx
Nginx --> Frontend
Nginx --> JWT
JWT --> Routers
Routers --> Harness
Harness --> Orch
Orch --> Gateway
Gateway --> LLMProv
Harness --> PG
Orch --> EventBus
EventBus --> Redis
EventBus --> SSEHook
Routers --> Starters
Starters --> TemporalSrv
TemporalSrv --> TWorker
TWorker --> Ext
Ext --> Routers
TWorker --> PG
TWorker --> Docs
Routers --> Decision
Decision --> DecisionSvc
Backend --> PG
Backend --> Redis
Backend --> Docs
Backend --> Gmail
Backend --> JobBoards
Backend --> Enrich
Frontend --> Clerk
Backend --> Clerk
```

### Docker Compose services (`docker-compose.yml`)

| Service | Build / Image | Ports | Depends | Notes |
|---|---|---|---|---|
| `frontend` | `./frontend` | `3000:3000` | `backend:healthy` | `NEXT_PUBLIC_*` baked as build args; `HOSTNAME=0.0.0.0`; healthcheck on `/` |
| `backend` | `./backend` | `8000:8000` | `redis:healthy`, `temporal:started` | `mem_limit 2.5g`, `shm 256m` for Chromium; `/health` healthcheck; `browser_debug:/tmp/browser_debug`, `docstore:/data/documents` |
| `redis` | `redis:8-alpine` | `6379` (exposed) | — | `--appendonly yes --maxmemory 512mb --maxmemory-policy noeviction --requirepass`; SSE pub/sub, rate limiting, LLM sessions — **not** a job queue |
| `temporal-postgres` | `postgres:16-alpine` | — | — | Temporal's own event-history store, separate from the app's Postgres |
| `temporal` | `temporalio/auto-setup:1.24` | `127.0.0.1:7233:7233` | `temporal-postgres:healthy` | Self-hosted Temporal server; point `TEMPORAL_ADDRESS_DOCKER` at Temporal Cloud instead for production |
| `temporal-ui` | `temporalio/ui:2.31.2` | `127.0.0.1:8233:8080` | `temporal` | Web UI for inspecting workflow executions |
| `temporal-worker` | `./backend` `python -m app.temporal_worker` | — | `redis:healthy`, `temporal:healthy` | Executes every workflow/activity and registers the Schedules; `mem 2g`, `shm 256m`, `stop_grace 360s`, `docstore:/data/documents`. Not optional — without it nothing the API starts makes progress and `/health` reports `"degraded"` |
| `nginx` | `nginx:alpine` | `80,443` | `frontend:started`, `backend:healthy` | `nginx.conf.template` with `${DOMAIN}` envsubst; `/etc/letsencrypt` mount |

`docker-compose.dev.yml` runs the same shape but replaces the three Temporal services with a single `temporalio/temporal:latest` dev-mode container (in-memory history, UI on `:8233`).

Production (`deploy/oracle-vm/`, see `docs/DEPLOYMENT.md`) differs: Temporal server, its Postgres and UI run on the same VM as the app, loopback-only (`127.0.0.1:7233`), and `notification-worker` (`python -m app.notification_worker`) is a `compose.yml` service alongside `temporal-worker`.

---

## 2. Frontend Architecture

**Stack:** Next.js App Router, TypeScript, Tailwind + shadcn/ui, Zustand 4 (global UI `agentStore.ts`, `userSlice.ts`) + Axios (`lib/api.ts`), SSE hook (`lib/sse.ts`), Motion + Three.js, Sonner toasts, `@supabase/ssr` + Clerk sessions.

**Route groups (`frontend/src/app/`):**

- `(marketing)/` — public landing, pricing, docs, about
- `(auth)/` + `sso-callback/` — login / register
- `(app)/` — authenticated `AppShell` (sidebar + topbar):
  `dashboard, resume, jobs, applications, company, cover-letter, email, interview, interview-prep, leads, linkedin, onboarding, salary, settings, agents`

**HTTP client (`src/lib/api.ts`):**

```ts
API_BASE_URL = NEXT_PUBLIC_API_URL + "/api/v1"
apiClient // axios, 30s timeout, deduplicatedGet() for concurrent GETs
// request interceptor: getSupabaseAuthToken() -> Authorization: Bearer <jwt>
// response interceptor: logs METHOD URL -> status + detail, no auto-redirect loop
apiErrorMessage(err, fallback) // prefers FastAPI `detail`, handles 422 list
```

**Streaming (`src/lib/sse.ts` — `useAgentStream(runId)`):**

1. `POST /agents/run` → `run_id`; `initRun(id)` in store
2. `fetch GET /agents/{id}/stream` with Bearer token → parse `event:` / `data:` frames
3. `addEvent`, `setCheckpoint` on `checkpoint`, `setComplete` on `complete`, `clearCheckpoint` on `approved`
4. DB is authoritative: `GET /agents/runs/{id}` reconcile every 3s (survives refresh / lost SSE)
5. Retry with backoff ×3 on transport failure; `error` event triggers reconcile (broken bus ≠ failed agent)

---

## 3. Backend — FastAPI (`backend/app/main.py`)

**Stack:** FastAPI, Python 3.12, SQLAlchemy 2.0 async, `slowapi` rate limiting, pydantic-settings (`core/config.py`).

**Startup requirements (`main.py:38`):** `APP_SECRET_KEY, DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET, REDIS_URL`. Clerk warns loudly if `CLERK_JWKS_URL`/`CLERK_ISSUER` missing (all authed requests 401).

**Middleware order (execution: CORS → request-id → JWT → route):**

1. `CORSMiddleware` (registered last = outermost; never `*`, exact origins only)
2. `_request_id_middleware` — `X-Request-ID` uuid4 passthrough
3. `_jwt_middleware` — skips `GET /health`, `/health/live`, `/docs`, `/openapi.json`, the public demo search, the Nango webhook, `OPTIONS`; requires `Authorization: Bearer`, `verify_token()` → `request.state.user`
4. `slowapi` limiter + generic 500 handler (logs method+path, returns `Internal server error`)

**Lifespan:** checks DB + Redis connectivity, verifies `vector` pg_extension, warms SSE publisher thread (`event_bus._ensure_publisher`), disposes engine on shutdown.

**Health:** `GET /health` (no auth) returns `{status, version, db, redis, pgvector, temporal: {connected, workers, task_queue, notification_workers, notification_email_workers}}`. HTTP status is 200 if db/redis/pgvector are all ok, else 503 (`status: "error"`). Temporal is reported but kept out of that 200/503 decision — a Temporal outage or no worker polling the task queue instead sets `status: "degraded"` (still HTTP 200), since read APIs keep working while it is down. `notification_workers` / `notification_email_workers` are the pollers on `careercraft-notifications` / `careercraft-notifications-email` (`null` if unknown); they are reported only and never change `status`.

**Routers (`main.py:206`):**

| Prefix `/api/v1` | Module | Endpoints (representative) |
|---|---|---|
| `/users` | `api/v1/users.py` | `GET /users/me`, `PUT /users/profile`, model-settings CRUD |
| `/rag` | `api/v1/rag.py` | `POST /rag/upload`, `POST /rag/search` |
| `/resume` | `api/v1/resume.py` | `POST /resume/optimize`, `GET /resume/download/{id}` |
| `/jobs` | `api/v1/jobs.py` | `POST /jobs/search`, `POST /jobs/portal-search`, saved boards CRUD, applications CRUD, `POST /jobs/applications/{id}/prepare-apply` |
| `/leads` | `api/v1/leads.py` | `GET /leads`, `POST /leads/{id}/action` |
| `/email` | `api/v1/email.py` | `GET /email/threads`, `POST /email/compose`, `POST /email/approve/{id}` (only send path) |
| `/agents` | `api/v1/agents.py` | `POST /agents/run`, `GET /agents/{id}/stream` (SSE), `POST /agents/{id}/approve`, `GET /agents/runs/{id}` |
| `/interview` | `api/v1/interview.py` | `POST /interview/session`, `POST /interview/answer` |
| `/interview-prep` | `api/v1/interview_prep.py` | `POST /interview-prep/generate` |
| `/cover-letter` | `api/v1/cover_letter.py` | `POST /cover-letter/generate` |
| `/salary` | `api/v1/salary.py` | `POST /salary/benchmark` |
| `/company` | `api/v1/company.py` | `GET /company/{name}/research` |
| `/linkedin` | `api/v1/linkedin.py` | `POST /linkedin/optimize`, `POST /linkedin/outreach` |
| `/memory` | `memory/routes.py` | semantic memory CRUD |
| `/extension` | `api/v1/extension.py` | web app: pair/list/revoke devices, list/cancel tasks; `/extension/device/*`: the extension itself (device token auth) claims tasks, fetches fill plans, reports progress, asks the decision engine |
| `/llm-gateway` | `core/llm_gateway.py` | `POST|GET /llm-gateway/v1/{path:path}` proxy |

---

## 4. LangGraph Orchestrator (`backend/app/agents/orchestrator.py`)

Supervisor graph over shared `AgentState`:

```python
class AgentState(TypedDict, total=False):
    user_id: str
    run_id: str
    task: str            # deprecated — use task_type
    task_type: str       # canonical
    status: str          # running, awaiting_approval, completed, failed
    context: dict
    messages: list[dict]
    pending_action: dict | None
    result: dict | None
    error: str | None
```

**Routing table (`TASK_ROUTES`):**

| `task_type` | Graph node | Agent file |
|---|---|---|
| `resume_optimize` | `resume` | `agents/resume_agent.py` |
| `job_search` | `job_search` | `agents/job_search.py` |
| `cover_letter` | `cover_letter` | `agents/cover_letter_agent.py` |
| `linkedin_optimize` | `linkedin` | `agents/linkedin_agent.py` |
| `email` | `email` | `agents/email_agent.py` |
| `interview_coach`, `evaluate_answer` | `interview_coach` | `agents/interview_coach_agent.py` (`_interview_coach_wrapper`: session? evaluate : start) |
| `interview_prep` | `interview_prep` | `agents/interview_prep_agent.py` |
| `company_research` | `company_research` | `agents/company_research_agent.py` |
| `salary_intelligence` | `salary` | `agents/salary_agent.py` |
| `nl_job_search` | `nl_search` | `agents/nl_search_agent.py` |
| `linkedin_outreach` | `linkedin_outreach` | `agents/linkedin_outreach_agent.py` |
| `email_monitor` | `email_monitor` | `agents/email_monitor_agent.py` |
| `auto_apply` | `auto_apply` | `agents/auto_apply_pipeline.py` (`_auto_apply_wrapper`) |

> `follow_up` is **NOT** a graph node — `schedule_followups()` (`agents/followup_agent.py`) starts a Temporal `FollowupWorkflow` (`app/workflows/starters.py::start_followups`), and the day-5/day-12 drafts run as timers inside that workflow (see §8).

```mermaid
graph TD
  Entry["POST /agents/run<br/>task_type + context"] --> Route{"_route_task()<br/>terminal? END : TASK_ROUTES[task_type]"}
  Route --> R["resume"] & J["job_search"] & N["nl_search"] & C["cover_letter"] & L["linkedin"] & LO["linkedin_outreach"] & E["email"] & EM["email_monitor"] & AA["auto_apply"] & IC["interview_coach"] & IP["interview_prep"] & CR["company_research"] & S["salary"]
  R & J & N & C & L & LO & E & EM & AA & IC & IP & CR & S --> Out{"status"}
  Out -->|awaiting_approval| CP["emit checkpoint + persist pending_action"]
  Out -->|completed| OK["emit complete + upsert agent_runs"]
  Out -->|failed| ER["emit error='Agent failed'"]
  R & J & N & C & L & LO & E & EM & AA & IC & IP & CR & S --> END_T(["END"])
```

**Node runner (`_make_node_runner` + `_run_agent_safely`):** emits `log` on start, runs sync fn in thread / async directly, captures tokens via `model_router.get_and_reset_tokens()`, upserts `agent_runs` (status/output/tokens/duration/error) unless durable mode (`context._durable`, worker commits itself), maps terminal status → `checkpoint` / `error` / `complete` SSE.

---

## 5. All Agents — Reference

| Agent | File | Tools / Services | Input → Output | Timeout / Budget |
|---|---|---|---|---|
| **JobSearchAgent** | `agents/job_search.py` | `job_search_service` (catalog-first via `job_connectors`, live adapters), saved boards, `RAGService.retrieve()` | `{query, location, salary_min, experience_years, platforms[]} → {jobs[{id,title,company,location,salary,match_score 0-100,url,platform}]}` — keyword+location+experience scoring | 120s / 3000 |
| **ResumeAgent** | `agents/resume_agent.py` | `RAGService.retrieve()`, `app.services.ats_service`, `app.services.pdf_service` | `{job_description, persona_id?, tone?} → {resume_text, ats_score, ats_suggestions[], document_id, download_url}`; RAG `{user}_resume` + `{user}_achievements` | 60s / 4000 |
| **LinkedInAgent** | `agents/linkedin_agent.py` | `RAGService.retrieve()` | target role → `{headline, about, experience_bullets[3]}` suggestions only | 60s / 3000 |
| **CoverLetterAgent** | `agents/cover_letter_agent.py` | `RAGService.retrieve()`, `ThinkingWrapper` (extended thinking) | `{job_description, tone} → {cover_letter, document_id}` | 90s / 6000 |
| **EmailAgent** | `agents/email_agent.py` | `GmailService.get_threads()`, `HunterService.find_email()`, `RAGService.retrieve()` | thread context → recruiter draft; send **only** via `/email/approve/{id}` | 60s / 3000 |
| **FollowUpAgent** | `agents/followup_agent.py` | `start_followups()` (Temporal `FollowupWorkflow`), `GmailService.get_threads()`, `RAGService.retrieve()` | application → day-5 + day-12 draft timers; auto-cancel on reply | 60s / 3000 |
| **EmailMonitorAgent** | `agents/email_monitor_agent.py` | Gmail poll | On-demand `email_monitor` agent run → threads needing action + draft replies | 60s / 3000 |
| **InterviewCoachAgent** | `agents/interview_coach_agent.py` | live session graph | `POST /interview/session` start → `POST /interview/answer` loop; scores clarity/relevance/depth 0-10 | 30s/turn / 2000 |
| **InterviewPrepAgent** | `agents/interview_prep_agent.py` | `RAGService.retrieve()`, `EXAService.search()`, `YouTubeService.search()` | role → question bank + study guide (no live session) | 60s / 3000 |
| **CompanyResearchAgent** | `agents/company_research_agent.py` | `EXAService.search()`, `EXAService.get_page()` | company → `{culture, interview_process, financials, recent_news, key_people}`; cached 7d in `company_intel` | 120s / 5000 |
| **SalaryAgent** | `agents/salary_agent.py` | `EXAService.search()`, `ThinkingWrapper` | `{role, location, experience_years, current_salary?, offer_amount?} → {percentiles{p25,p50,p75,p90}, total_comp, negotiation_script, report_id}` | 90s / 6000 |
| **NLSearchAgent** | `agents/nl_search_agent.py` | delegates to JobSearchAgent | `"senior backend at climate startup, remote, $150k+"` → structured query → job search | 60s / 3000 |
| **LinkedInOutreachAgent** | `agents/linkedin_outreach_agent.py` | `LinkedInOutreachService` | company/contact → queued message (`linkedin_outreach_queue`, `pending_approval`) | 60s / 3000 |
| **AutoApplyPipeline** | `agents/auto_apply_pipeline.py` | drafts resume, cover letter, and recruiter outreach; submission is not server-side (see §16) | Approval checkpoints before any email is sent; server-side browser steps raise `BrowserAutomationRetired` | 300s |
| **RAG Memory** | `agents/memory/`, `services/rag_service.py`, `agents/semantic_memory.py` | `PGVector` (langchain-postgres) | ingest: PDF/DOCX → 500/50 chunks → embeddings → pgvector `{user}_{doc}` | — |

All agents: model resolved from `user_model_settings` via `llm_gateway.py` (never hardcoded); RAG context injected by harness; every LLM call logged to `agent_runs.tokens_used`; max 2 concurrent runs/user.

---

## 6. RAG Pipeline (`backend/app/services/rag_service.py`)

```mermaid
flowchart LR
  UP["Upload PDF/DOCX<br/>POST /rag/upload"] --> EX["extract_text()<br/>PyMuPDF / python-docx"]
  EX --> CH["chunk_text()<br/>RecursiveCharacter 500/50"]
  CH --> EM["get_embedding_model()<br/>resolve_embedding_settings()"]
  EM --> ST["get_vector_store()<br/>PGVector {user}_{doc}_{provider}_{dim}d"]
  ST --> HNSW["ensure HNSW<br/>vector_cosine_ops<br/>m=16 ef=64"]
  Q["Agent query"] --> QE["embed query"] --> SS["similarity_search k=5"] --> AG["Agent context"]
```

- **Dimensions:** `openai 1536` (`text-embedding-3-small`), `google 768` (`gemini-embedding-001`, `output_dimensionality=768`), `ollama 1024` (`qwen3-embedding:0.6b`); `EMBEDDING_DIMENSIONS` map, keyed by the *effective* embedding provider; provider-namespaced collections prevent dim mismatch on provider switch. Changing a model's vector size therefore starts a new, empty collection — users re-upload documents to re-index.
- **Query instruction (Ollama):** Qwen3-Embedding is instruction-aware, so `QwenOllamaEmbeddings` prefixes **queries** with `Instruct: {task}\nQuery:{query}`; documents are embedded bare.
- **Embedding-capable:** `openai, google, ollama`. Chat-only providers (anthropic, nvidia_nim, deepseek, openrouter) use `EMBEDDING_PROVIDER`; the fallback never reuses the chat key — openai/google need `EMBEDDING_API_KEY`, ollama uses `EMBEDDING_OLLAMA_URL` (operator-set; a user's own `ollama_url` wins only when their provider is `ollama`; default `http://localhost:11434`).
- **Doc types:** `resume, achievements, certifications, portfolio, notes`.
- **Fallback:** resume retrieval failure → `fetch_user_profile_text()` raw-resume `Document`.

---

## 7. LLM Gateway — BYOK Proxy (`backend/app/core/llm_gateway.py`)

Agents never see real keys:

```
Agent → ChatOpenAI(base_url=LLM_GATEWAY_URL, api_key=session_token)
     → Gateway proxy_llm_request (/llm-gateway/v1/{path})
     → Redis lookup session → decrypt api_key_enc (AES-256, APP_SECRET_KEY)
     → forward to provider (OpenAI Bearer / Anthropic x-api-key)
     → redact_keys() response → return
```

- `create_gateway_session(user_id, model_settings)` — HMAC-SHA256(`APP_SECRET_KEY`) token (48ch + nonce), Redis `llm_gw:session:{token}` JSON `{user_id, provider, model_name, api_key_enc}`, TTL 3600s, cross-worker visible.
- `get_gateway_llm(user_id, db)` — loads active `UserModelSettings`, returns key-free `ChatOpenAI`.
- Every agent node builds its model with `model_router.build_agent_llm(model_settings)`, a gateway `ChatOpenAI` with budget check and token tracking; a unit test fails if an agent module builds a keyed model. `_build_llm` (direct, keyed) remains only for the Settings key test.
- The gateway proxies text chat completions only (no tools, structured output or streaming). Embeddings for RAG and semantic memory still use the decrypted key inside the worker.
- The worker must reach the backend: `LLM_GATEWAY_URL` is `http://backend:8000/llm-gateway/v1` in dev Compose and `http://127.0.0.1:18100/llm-gateway/v1` on the Oracle stack.
- Provider URLs: `openai, anthropic, google (generativelanguage), nvidia_nim (integrate.api.nvidia)`.
- Defense in depth: `llm_proxy_service.redact_keys()` strips key echoes.

**Model router (`core/model_router.py`):** provider-agnostic `BaseChatModel` + per-user `token_budget` + `get_and_reset_tokens()` accounting.

---

## 8. Temporal — Workflows & Workers

Every background job — agent runs, job searches, applications, follow-ups,
and the recurring daily search/maintenance/status-check jobs — executes as a
durable Temporal workflow. There is no separate Node worker and no
Python dispatch-polling loop; Redis is not used as a job queue (see §10).

### `temporal-worker` (`backend/app/temporal_worker.py`)

`python -m app.temporal_worker` connects to Temporal (`TEMPORAL_ADDRESS`),
registers the recurring Schedules (`ensure_schedules`, unless
`TEMPORAL_SCHEDULES_ENABLED=false`), then starts a `Worker` polling
`TEMPORAL_TASK_QUEUE` (default `careercraft`) with every workflow and
activity listed in `app/workflows/registry.py`. `max_concurrent_activities`
is `TEMPORAL_WORKER_CONCURRENCY` (default 4). Scale by running more replicas
of this service — they share the same task queue.

### Workflows (`backend/app/workflows/`)

| Workflow | File | Workflow ID | Purpose |
|---|---|---|---|
| `AgentRunWorkflow` | `agent_run.py` | `agent-run/{run_id}` | Durable execution of one `POST /agents/run`; loops through execute → `awaiting_approval` → `decide` signal → continue, expiring after `AGENT_APPROVAL_TIMEOUT_S` |
| `JobSearchWorkflow` | `job_search.py` | `job-search/{run_id}` | One `POST /jobs/search` run; retries once, else marks the run failed |
| `AutoApplyWorkflow` | `auto_apply.py` | `auto-apply/{user_id}/{job_application_id}` | One job application, from reservation to submission — see §16 |
| `FollowupWorkflow` | `followup.py` | `followup/{application_id}` | Durable timers for the day-5 and day-12 follow-up drafts (`workflow.sleep`), stopped early if the application is cancelled or not found |
| `DailySearchWorkflow`, `StatusCheckWorkflow`, `MaintenanceWorkflow` | `scheduled.py` | `scheduled/{schedule_id}` | Started by the Temporal Schedules below |

### Schedules (`backend/app/workflows/scheduled.py::ensure_schedules`)

Registered/updated idempotently by every worker at start-up
(`ScheduleOverlapPolicy.SKIP` — a slow run is never stacked on by the next tick):

| Schedule id | Interval (setting) | Runs |
|---|---|---|
| `daily-job-search` | `DAILY_SEARCH_INTERVAL_HOURS` (24h) | `daily_search_activity` for all users |
| `maintenance` | `MAINTENANCE_INTERVAL_SECONDS` (60s) | Reconciles `agent_runs` with Temporal, expires orphaned extension tasks, reconciles application attempts |

### API-side starters (`backend/app/workflows/starters.py`)

The API never touches Temporal workflow objects directly — it calls
`start_agent_run`, `signal_agent_decision`, `start_job_search`,
`start_auto_apply`, `signal_extension_update`, `start_followups`, all of
which derive the workflow's id from what it works on so a retried request
finds the workflow already running instead of starting a duplicate. If
Temporal cannot be reached, `_client()` raises `WorkflowUnavailable` and the
calling endpoint returns **503**.

### Concurrency, rate limits and retries

- **Per user:** `POST /agents/run` admits at most `AGENT_MAX_CONCURRENT_PER_USER`
  (2) `queued`/`running` runs per user, serialized with a row lock on the user
  (`api/v1/agents.py`); `apply_prepare` runs waiting in the extension don't count.
  Request rates are capped per user by slowapi (`/jobs/search` 10/min,
  `/agents/run` `RATE_LIMIT_AGENT_RUN`, prepare-apply 5/hour).
- **Per worker:** `max_concurrent_activities=TEMPORAL_WORKER_CONCURRENCY`.
  There is no task-queue-wide rate limit on `careercraft` (the notification
  email queue has one, `NOTIFICATION_EMAIL_RATE_LIMIT_PER_SECOND`).
- **Retries:** job search 2 attempts, then `fail_job_search_activity` marks
  the run failed; follow-up drafts 3 attempts (30s initial); daily search 3 attempts (1 min initial); maintenance 1 attempt (it runs
  again on the next tick); agent execution 2 attempts; a continuation (which
  may send an email or click Submit) never retries.
- **Cancellation:** a rejected checkpoint is a `decide(approved=False)`
  signal — the workflow ends `cancelled`; follow-ups stop once the
  application moves on or the recruiter replies; an unanswered checkpoint
  expires after `AGENT_APPROVAL_TIMEOUT_S`.

### Running a job by hand

There is no HTTP trigger. Use the Temporal CLI (or the UI) against the same
namespace, e.g. `temporal schedule trigger --schedule-id daily-job-search`
(also `maintenance`, `application-status-check`), and `temporal workflow
describe --workflow-id agent-run/<run_id>` to inspect a run.

### Idempotency

`ApplicationAttempt` + a compare-and-swap on submit is the idempotency
ledger shared by both apply modes. Preparation activities (navigate,
extract, fill) retry with bounded exponential backoff; the one activity that
can reach the real Submit click never retries (`maximum_attempts=1`) — an
ambiguous outcome becomes `needs_verification`, never an automatic retry.

---

## 9. Real-Time Event Bus (`backend/app/core/event_bus.py` + `services/sse_service.py`)

- Channels: `agent:{run_id}:events` (SSE), `agent:{run_id}:queue` (trigger). Publisher: dedicated daemon thread + loop (`_ensure_publisher`, warmed at startup); `publish()` is thread-safe via `run_coroutine_threadsafe`; 2s publish timeout, drops with warning if not ready.
- `emit(run_id, type, data)` — suppressed for terminal events under durable mode (`suppress_terminal_events`, worker publishes post-commit).
- `stream_events(run_id, 300s)` — subscribes, yields `event: {type}\ndata: {json}\n\n`, 5s `ping` keepalive, ends on `complete`/`error`; Redis error → single `event: error {"message":"event bus unavailable"}` (never SSE 500).
- **Events:** `thinking {step,message}`, `tool_call {tool,input}`, `tool_result {tool,output}`, `checkpoint {action_type,details}`, `complete {result}`, `error {message}`.

---

## 10. Data Layer

### Postgres (`backend/app/models/db.py`, `supabase/migrations/0001-0033`)

| Table | Key columns |
|---|---|
| `users` | `id, email, clerk_user_id, google_id, linkedin_url, *_enc tokens, auto_mode (drafts|auto), onboarding_completed` |
| `user_model_settings` | `user_id, provider, api_key_enc (AES-256), model_name, ollama_url, is_active, token_budget` |
| `user_documents` | `user_id, doc_type, filename, storage_path (/data/documents), raw_text, embedded_at, is_primary, ats_score, ats_data` |
| `job_applications` | `user_id, company, role, location, job_url, jd_text, match_score, resume_id, cover_letter(_id), status, applied_at, followup_day5/12` |
| `leads` | `user_id, name, email, company, linkedin_url, status (cold...), last_contact` |
| `agent_runs` | `id, user_id, agent_type, status (running|queued|awaiting_approval|completed|failed|error), input/output JSONB, tokens_used, duration_ms` |
| `cover_letter_versions` | `user_id, job_application_id, document_id, tone, version_number` |
| `extension_devices` | `user_id, name, token_hash (SHA-256 of a "ccx_…" device token), created_at, last_seen_at, revoked_at` |
| `extension_tasks` | `user_id, device_id?, job_application_id, run_id, attempt_id, workflow_id, status, created_at` — claimed by the extension, progress relayed to `AutoApplyWorkflow` as signals |
| `application_attempts` | idempotency ledger for one apply attempt (used by the extension); carries `workflow_id`/`temporal_run_id` |
| `interview_sessions` | `user_id, job_application_id?, role, company, questions/answers/scores JSONB, overall_score, status` |
| `salary_reports` | `user_id, role, company, location, p25/p50/p75, offer_amount, classification, negotiation_script, data_sources` |
| `company_intel` | `user_id, company_name, overview, culture_summary, news_items, tech_stack, glassdoor_sentiment` (7-day cache) |
| `resume_personas` | `user_id, name, description, primary_resume_id, target_keywords` |
| `linkedin_outreach_queue` | `user_id, company, contact_name/title/url, message, status (pending_approval), approved_at, sent_at` |
| `ats_scores` | `user_id, resume_id?, job_application_id?, composite/keyword/readability/format, missing_keywords, suggestions` |
| `user_preferences` | `user_id (unique), experience_level, years_experience, job_type, work_mode, salary_min/max, target_roles, preferred_locations, prefer_live_browser` |
| `langchain_pg_embedding` | pgvector store + `idx_langchain_embedding_hnsw` |

RLS on all user tables (`user_id = auth.uid()` / `clerk_user_id()` fixes in `0018,0023,0024,0028,0030,0032`); pgvector HNSW fixes in `0007,0025,0032`.

### Redis (`redis:8-alpine`)

`agent:{run}:events` pub/sub for SSE, `llm_gw:session:*` (TTL 3600), rate-limit counters (including per-device counters for extension tokens, §13), `cache:{hash}` (1h). **Not** a job queue — Temporal owns all scheduling and durable execution (§8); Temporal's own event history lives in its dedicated `temporal-postgres` database, not this Redis.

### Document storage

Local disk `DOCUMENT_STORAGE_DIR=/data/documents` (Docker named volume shared by `backend` and `temporal-worker` — back it up, nothing else holds a copy).

---

## 11. Request Lifecycle

```mermaid
sequenceDiagram
participant FE as Frontend<br/>(api.ts + sse.ts)
participant NX as nginx
participant BE as FastAPI<br/>(main.py)
participant T as Temporal Server
participant TW as temporal-worker<br/>(app/temporal_worker.py)
participant OR as Harness + Orchestrator
participant GW as LLM Gateway
participant DB as Postgres+pgvector
participant R as Redis (SSE pub/sub)
participant LLM as BYOK Provider
FE->>NX: HTTPS + Bearer Clerk JWT
NX->>BE: proxy /api/*, JWT middleware verify
BE->>DB: INSERT agent_runs (queued)
BE->>T: start_agent_run() — AgentRunWorkflow(id=agent-run/{run_id})
BE->>FE: return run_id
FE->>BE: GET /agents/{id}/stream (SSE)
T->>TW: dispatch execute_agent_run_activity
TW->>OR: Harness.run(user_id, task_type, context)
OR->>DB: RAG retrieve() HNSW top-k
OR->>GW: create_gateway_session() Redis TTL, ChatOpenAI(gateway_url, session_token)
GW->>DB: decrypt api_key_enc AES-256
GW->>LLM: forward with real key, redactKeys() reply
LLM-->>OR: completion (key never in agent memory)
OR->>DB: upsert agent_runs (output/tokens/duration)
OR->>R: emit checkpoint|complete|error (event_bus.publish)
R-->>FE: SSE: thinking, tool_call, tool_result,<br/>checkpoint, complete, error + ping
FE->>FE: reconcile GET /agents/runs/{id} /3s
FE->>BE: POST approve (HITL)
BE->>T: signal_agent_decision() — `decide` signal
```

---

## 12. End-to-End Flows

**Auto-apply:** the `auto_apply` agent prepares resume, cover letter, and outreach drafts for approval. Submitting an application happens only through the extension flow described below.

**Job search:** `POST /jobs/search` → `start_job_search()` starts `JobSearchWorkflow(id=job-search/{run_id})` → `run_job_search_activity` → `search_all_platforms` reads the shared catalog first (`job_connectors`), then live adapters (JobSpy, RemoteOK, Remotive, Arbeitnow, ATS boards) fill any shortfall → score → `complete {matches[]}`. No browser is used.

**Job application (extension mode):** click Apply → `start_auto_apply()` starts `AutoApplyWorkflow(id=auto-apply/{user}/{application})` → `reserve_application_attempt` → `create_extension_task_activity` (an `extension_tasks` row) → the user's browser polls, claims it (`POST /extension/device/tasks/claim`), fills the form, and shows a review panel → user presses Submit → the extension reports `submitted` (`POST /extension/device/tasks/{id}/events`), relayed as an `extension_update` signal to the workflow → `finish_extension_task_activity` → `schedule_followup_activity` starts the `FollowupWorkflow`. See §16.

**Email:** `GET /email/threads` (Gmail) → `EmailAgent` draft (Hunter + RAG) → `checkpoint` → `ApprovalModal` → `POST /email/approve/{id}` (sole send path) → Resend/Gmail send → `FollowUpAgent` scheduled (`FollowupWorkflow`); `EmailMonitorAgent` (on-demand `email_monitor` agent run) drafts replies to recruiter responses (HITL before any reply).

---

## 13. Security & Guardrails

- **HITL gates (mandatory, non-bypassable):** every email send, LinkedIn outreach, and AutoApply checkpoint emits `checkpoint` + `awaiting_approval`. Application submits need the member's click in the extension, except when auto-submit is enabled: the server then approves within the daily cap and never on LinkedIn or Naukri.
- **Secrets:** `api_key_enc`, `linkedin_*_enc`, `google_*_token_enc`, `state_enc` all AES-256 (`security.py`, `APP_SECRET_KEY`); plaintext never in `user_model_settings`; gateway session-token pattern (above).
- **Isolation:** RLS everywhere; JWT verified per-request (Clerk RS256 JWKS or Supabase HS256); `agent_runs` audit of every run (input/output/tokens/duration); client only ever sees `"Agent failed"`.
- **Transport/limits:** TLS via nginx + HSTS; exact-origin CORS; SlowAPI (`60/min` default, `10/min` agent-run, `5/min` upload, `100/min` strict); 2 concurrent runs/user; token budgets per agent (table §5, overridable `user_model_settings.token_budget`); timeouts per agent (table §5).
- **Infra:** there is no HTTP route that runs a background job outside Temporal — activities call `scheduled_jobs.py` in-process; nginx still returns 404 for `/internal/*` as defense in depth. Bandit SAST on CI.
- **Extension device tokens:** `ccx_`-prefixed, only their SHA-256 hash is stored (`extension_devices.token_hash`); revocable per-device in Settings; rate-limited per device (`rate_limit.py` keys on the token hash), separate from per-user limits.
- **Untrusted content:** prompts fence job, email, and document text with `untrusted()`/`fenced()` (`agents/prompts/__init__.py`); inbound email changes application status only from a trusted sender domain. See [SECURITY.md](SECURITY.md).

---

## 14. External Integrations

| Service | Used by | Auth |
|---|---|---|
| Hunter.io | EmailAgent, leads — recruiter email discovery | `HUNTER_API_KEY` |
| ProxyCurl | LinkedInAgent — profile data | `PROXYCURL_API_KEY` |
| Exa (+Tavily/Brave/Serp/Bing/Google CSE/DuckDuckGo/Mojeek/SearXNG) | CompanyResearch, Salary, InterviewPrep, job search waterfall | `EXA_API_KEY`, etc. |
| Adzuna + RapidAPI | JobSearch waterfall | `ADZUNA_APP_ID/KEY`, `RAPIDAPI_KEY` |
| Nango (Gmail / Drive) | EmailAgent, EmailMonitor, Drive | Nango environment key + provider config keys |
| Resend | transactional email | `RESEND_API_KEY` |
| YouTube | InterviewPrep videos | `YOUTUBE_API_KEY` (optional) |
| Playwright / BrowserUse | none: server-side browser execution is retired (`BrowserAutomationRetired`) | `BROWSER_USE_OLLAMA_URL/MODEL` are defined in config but no code uses them |
| Clerk | identity, JWT verification | `CLERK_ISSUER` / `CLERK_JWKS_URL`, `CLERK_SECRET_KEY` |
| CareerCraft browser extension | Application flow — fills and submits in the user's own signed-in browser | device token (`ccx_…`), see §13 |
| TypeSafe Jev / self-hosted Laya | decision engine for in-page choices the extension can't resolve by exact text match | `TYPESAFE_API_KEY` or `LAYA_URL` (`deploy/laya/`); falls back to built-in heuristics |

---

## 15. Configuration (`backend/app/core/config.py`)

Required: `APP_SECRET_KEY, DATABASE_URL, REDIS_URL`. Auth: `CLERK_JWKS_URL/ISSUER/SECRET/AUDIENCE`. App: `APP_ENV, LOG_LEVEL, FRONTEND_URL, CORS_ORIGINS, NEXT_PUBLIC_*`. Temporal: `TEMPORAL_ADDRESS(_DOCKER), TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE=careercraft, TEMPORAL_WORKER_CONCURRENCY=4, TEMPORAL_SCHEDULES_ENABLED, DAILY_SEARCH_INTERVAL_HOURS, MAINTENANCE_INTERVAL_SECONDS, TEMPORAL_TLS_*`. Applying: `EXTENSION_TASK_CLAIM_TIMEOUT_S, EXTENSION_TASK_COMPLETE_TIMEOUT_S`. Decision engine: `DECISION_ENGINE_PROVIDER=auto|jev|laya|none, DECISION_ENGINE_MIN_CONFIDENCE, TYPESAFE_API_KEY, LAYA_URL`. Browser: `BROWSER_USE_*` (development search tools only). RAG: `RAG_CHUNK_SIZE=500, RAG_CHUNK_OVERLAP=50, RAG_TOP_K=5`. Limits: `RATE_LIMIT_*`, `AGENT_*`. Storage: `DOCUMENT_STORAGE_DIR=/data/documents`. See [docs/CONFIGURATION.md](CONFIGURATION.md) for the full reference; all external API keys are optional.

---

## 16. Auto-Apply: Temporal Workflow + Browser Extension

Auto-Apply (submitting one job application) runs as `AutoApplyWorkflow`
(`backend/app/workflows/auto_apply.py`) executes only in the paired browser
extension:

- **`extension`** (default) — the application is handed to the user's own
  browser through the CareerCraft Chrome extension (`extension/`). No server
  ever holds the user's job-site session.

**Extension flow:**
```
POST /jobs/applications/{application_id}/prepare-apply
  └─▶ start_auto_apply() → AutoApplyWorkflow(id="auto-apply/{user_id}/{job_application_id}")
        └─▶ activity: reserve_application_attempt   (ApplicationAttempt + AgentRun, idempotent on run_id)
        └─▶ activity: create_extension_task_activity  (an extension_tasks row, status "pending")
        └─▶ wait: extension claims the task (POST /extension/device/tasks/claim,
             EXTENSION_TASK_CLAIM_TIMEOUT_S, default 24h) — the extension also
             polls every ~30s
        └─▶ wait: extension fills the form in the user's browser, shows a review
             panel, user presses Submit (EXTENSION_TASK_COMPLETE_TIMEOUT_S, default 2h)
             — progress arrives as `extension_update` signals relayed from
             POST /extension/device/tasks/{id}/events
        └─▶ activity: finish_extension_task_activity
        └─▶ on outcome "submitted": activity schedule_followup_activity
             → starts FollowupWorkflow(id="followup/{application_id}")
  Signals: extension_update(dict)   Query: status() -> state/pending_action/result/error
```

The workflow retains the `ApplicationAttempt` ledger and starts follow-ups only on confirmed submission.

**Decision engine** (`backend/app/services/decision_engine.py`): while
filling a form, the extension asks `POST /extension/device/decide` for
typed, fast decisions plain-text matching can't resolve — which button
advances the form, which dropdown option matches a saved answer, did this
page confirm the submission. The backend answers via `DECISION_ENGINE_PROVIDER`
(`auto` picks TypeSafe **Jev** if `TYPESAFE_API_KEY` is set, else self-hosted
**Laya** if `LAYA_URL` is set, else `none`); a provider error, or an answer
below `DECISION_ENGINE_MIN_CONFIDENCE`, falls back to built-in heuristics
(token-overlap similarity) rather than blocking the extension. See
[`extension/README.md`](../extension/README.md) and
[`deploy/laya/README.md`](../deploy/laya/README.md).

**Retry and idempotency policy:** reservation and task completion activities use
bounded retries. The reservation reuses its deterministic run ID under
at-least-once execution. A submitted or uncertain attempt cannot be reset,
including when a workflow ID is reused for a new execution. External submit
clicks belong to the user's extension and must follow explicit form review.

**Deployment topology:** the Temporal server is self-hosted (`temporal` +
`temporal-postgres` + `temporal-ui` services in `docker-compose.yml`) or
Temporal Cloud. Host-run development uses `TEMPORAL_ADDRESS=localhost:7233`;
containers use `TEMPORAL_ADDRESS_DOCKER=temporal:7233` by default. Set the
latter to the managed Temporal address when Compose runs against Temporal
Cloud, with the `TEMPORAL_TLS_*` settings for mTLS.

**Local development:**
```bash
cd backend && pip install -r requirements.txt   # installs temporalio
docker compose -f docker-compose.dev.yml up -d   # includes a dev-mode Temporal server
# Or, running the worker on the host:
python -m app.temporal_worker
uvicorn app.main:app --reload --port 8000
```
`GET /health` reports `"temporal": {"connected": bool, "workers": int, "task_queue": str}`. It never fails the 200/503 decision — a Temporal outage makes overall `status` "degraded" while pages and read APIs keep working, since agent runs simply cannot start until a worker is polling again.

---

## 17. File Map (where to look)

- Entry: `backend/app/main.py`, `frontend/src/app/`, `backend/app/temporal_worker.py` (run with `python -m app.temporal_worker`, see §8/§16)
- Temporal: `backend/app/workflows/{registry,starters,agent_run,job_search,auto_apply,followup,scheduled,activities,agent_activities,job_activities,extension_activities}.py`, `backend/app/core/temporal_client.py`
- Orchestration: `backend/app/agents/orchestrator.py`, `harness.py`, `state.py`, `strategies.py`, `base_agent.py`
- Agents: `backend/app/agents/*.py` (see §5 table)
- API: `backend/app/api/v1/*.py` (incl. `extension.py`)
- Core: `backend/app/core/{config,database,redis_client,event_bus,supabase_auth,security,llm_gateway,model_router,rate_limit,temporal_client,agent_runs_repository}.py`
- Services: `backend/app/services/{rag_service,workflow_service,scheduled_jobs,extension_service,decision_engine,sse_service,llm_gateway,llm_proxy_service,job_search_service,job_platforms_service,indian_platforms_service,naukri_service,gmail_service,resend_service,hunter_service,email_finder_service,proxycurl_service,exa_service,youtube_service,ats_service,pdf_service,persona_service,browser_control_service,form_filler_service,storage_service,drive_service,integration_proxy_service,auto_apply_service,linkedin_outreach_service,token_budget_service,model_catalog_service,search_presets}.py`
- Extension: `extension/src/background.js`, `extension/src/content/*.js`, `extension/README.md`
- Decision engine: `backend/app/services/decision_engine.py`, `deploy/laya/`
- Frontend: `frontend/src/lib/{api,sse,supabase-token}.ts`, `frontend/src/store/agentStore.ts`
- Data: `backend/app/models/db.py`, `supabase/migrations/`
- Infra: `docker-compose*.yml`, `Dockerfile*`, `nginx/*.template`, `deploy/`
