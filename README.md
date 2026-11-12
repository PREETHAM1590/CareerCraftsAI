# CareerCraft AI

> **A full-stack, multi-agent job search automation platform. Bring your own AI API key — agents handle finding roles, tailoring resumes, preparing applications (a Chrome extension fills and submits them in your own browser), optimizing your LinkedIn profile, researching companies, negotiating salary, and following up with recruiters.**

Your data stays yours. You pay only for your own AI usage. Personal details such as email and phone are replaced with placeholders before text reaches your model provider (see [Privacy](#privacy-personal-details-and-the-model)).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Next.js 16](https://img.shields.io/badge/Next.js-16-black.svg)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-green.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-run%20in%20CI-informational.svg)](#testing)

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Feature Highlights](#feature-highlights)
- [Job Search](#job-search)
- [Applying and Auto-Submit](#applying-and-auto-submit)
- [Privacy: Personal Details and the Model](#privacy-personal-details-and-the-model)
- [Tailored Resumes](#tailored-resumes)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Development](#development)
- [Testing](#testing)
- [Project Structure](#project-structure)
- [Supported AI Providers](#supported-ai-providers)
- [API Overview](#api-overview)
- [Human-in-the-Loop](#human-in-the-loop)
- [Security](#security)
- [Database Schema](#database-schema)
- [Maintenance and Evals](#maintenance-and-evals)
- [Known Limitations](#known-limitations)
- [Deployment](#deployment-production)
- [Documentation](#documentation)
- [License](#license)

---

## What It Does

CareerCraft AI deploys a harness of specialized AI agents that collaborate to automate every stage of your job search:

| Agent | What it does |
|---|---|
| **Orchestrator** | LangGraph supervisor — routes tasks, manages shared state, streams live progress |
| **Job Search** | Searches the shared job catalog and live public sources through the connector registry (no browser automation); India-first by default; scores matches 0–100 |
| **Resume** | RAG-powered resume rewriting tailored to a specific JD; generates ATS-optimized PDF |
| **Cover Letter** | Generates personalized, role-specific cover letters with multiple tone variants |
| **LinkedIn** | Rewrites headline, About section, and experience bullets for a target role |
| **Email** | Reads Gmail threads for context, drafts personalized outreach to recruiters |
| **Follow-Up** | Schedules day-5 and day-12 follow-up emails automatically after you apply |
| **Email Monitor** | Monitors your inbox for recruiter replies and surfaces action items |
| **Interview Coach** | Conducts mock interviews, scores answers, gives structured feedback |
| **Interview Prep** | Generates role-specific question banks and study guides |
| **Company Research** | Deep-dives company culture, financials, news, Glassdoor signals, and interview patterns |
| **Salary Agent** | Benchmarks compensation using market data; generates negotiation scripts |
| **NL Search** | Natural-language job search — describe what you want in plain English |
| **Auto-Apply Pipeline** | Chains search → score → recruiter lookup → resume tailoring, then queues the application for your Chrome extension, which fills and submits it in your browser |
| **RAG** | Retrieves context from your uploaded documents via pgvector |

> Every action that sends an email or submits an application requires **explicit human approval** — agents prepare, you decide. The one exception is the opt-in, capped [auto-submit](#applying-and-auto-submit) setting, which is off by default.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Next.js 16 App Router  (port 3000)                         │
│  Dashboard · Resume · Jobs · Applications · Email           │
│  LinkedIn · Interview · Company · Salary · Leads · Settings │
└────────────────────────┬────────────────────────────────────┘
                         │ REST + SSE
┌────────────────────────▼────────────────────────────────────┐
│  FastAPI  (port 8000)                                        │
│  /agents · /resume · /jobs · /rag · /email · /interview     │
│  /company · /salary · /cover-letter · /linkedin · /leads    │
│  /extension (browser extension pairing + tasks)             │
└────────┬───────────────────────────┬────────────────────────┘
         │ LangGraph                 │ start/signal workflow
┌────────▼──────────────┐  ┌─────────▼──────────────────────┐
│  Agent Harness        │  │  Temporal Worker                 │
│  Orchestrator         │  │  python -m app.temporal_worker   │
│  Resume · LinkedIn    │  │  AgentRunWorkflow · JobSearch    │
│  Email · FollowUp     │  │  AutoApply · Followup            │
│  Cover Letter         │  │  Schedules: daily search,        │
│  Interview Coach      │  │  maintenance                     │
│  Company Research     │  │                                 │
│  Salary · NL Search   │  └─────────┬────────────────────────┘
│  Auto-Apply Pipeline  │            │
│  Email Monitor        │  ┌─────────▼────────────────────────┐
└────────┬──────────────┘  │  Temporal Server + UI            │
         │                 │  (self-hosted, or Temporal Cloud)│
         │                 └──────────────────────────────────┘
┌────────▼──────────────────────────────────────────────────┐
│  Self-hosted data layer                                    │
│  PostgreSQL 16 · pgvector · Redis 8 (SSE bus, rate limit,  │
│  LLM sessions) · local doc store                          │
└───────────────────────────────────────────────────────────┘
         │
┌────────▼──────────────────────────────────────────────────┐
│  Model Router  (BYOK)                                      │
│  Claude · GPT · Gemini · DeepSeek · OpenRouter · Ollama   │
└───────────────────────────────────────────────────────────┘
         │
┌────────▼──────────────────────────────────────────────────┐
│  External Integrations                                     │
│  Nango (Gmail/Drive) · Hunter · ProxyCurl · Exa · Resend  │
│  Browser extension (user's own browser) · Decision engine │
│  (TypeSafe Jev / self-hosted Laya)                        │
└───────────────────────────────────────────────────────────┘
```

**Infrastructure:** Everything runs under Docker Compose on your own host — self-hosted PostgreSQL 16 + pgvector, Redis, and local-disk document storage. Authentication is [Clerk](https://clerk.com); Gmail/Drive access goes through the [Nango](https://nango.dev) credential proxy; every agent run, job search, application and follow-up executes as a durable [Temporal](https://temporal.io) workflow — the `temporal-worker` service must be running or nothing the API starts makes progress.

> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design.

### Applying via the browser extension

Job applications are filled and
submitted in **your own browser**, not on the server. Install the extension
from `extension/`, connect it under **Settings → Integrations → Browser
extension**, and when you press Apply the `AutoApplyWorkflow` queues an
extension task that your browser claims, fills (LinkedIn Easy Apply, Naukri,
Greenhouse/Lever/Ashby/Workday-style forms), and shows you in a review panel
— nothing is submitted until you explicitly approve **Submit**, unless you
have turned on the opt-in [auto-submit](#applying-and-auto-submit) setting.
Server-side browser automation is retired: the backend never drives a job
site.
Ambiguous in-page decisions (which option matches, did this confirm
submission) are answered by a small "System One" decision engine — TypeSafe
**Jev** or a self-hosted **Laya** (`deploy/laya/`) — falling back to
built-in heuristics when neither is configured. See
[`extension/README.md`](extension/README.md) and
[`deploy/laya/README.md`](deploy/laya/README.md).

---

## Feature Highlights

### Auto-Apply Pipeline
The pipeline finds matching jobs, scores them, tailors your resume and queues each application for the Chrome extension, which fills the form in your own browser and shows a review panel. By default you press Submit yourself; the opt-in [auto-submit](#applying-and-auto-submit) setting is capped and never used on LinkedIn or Naukri. The server does not run a browser.

### ATS Scoring
Every generated resume is scored against the target job description using keyword analysis, section completeness, and formatting rules. Score and improvement suggestions are shown before you download.

### Company Intelligence
Deep research on any company: culture signals, recent news, funding history, Glassdoor ratings, interview patterns, and key people — all synthesized into a structured briefing.

### Salary Benchmarking
Market compensation data for any role + location, with percentile breakdowns and a ready-to-use negotiation script tailored to your experience level.

### Interview Coach
Live mock interview sessions with AI feedback. Scores your answers on clarity, relevance, and depth. Tracks improvement across sessions.

### Natural Language Job Search
Describe what you want in plain English ("senior backend role at a climate startup, remote, $150k+") and the agent translates it into structured search queries that run through the same connector-based [job search](#job-search) as everything else.

### LinkedIn Outreach Automation
Drafts personalized connection requests and InMails for the contacts you supply and queues them for your approval. If you supply none, a ProxyCurl lookup is used as a fallback, and only when `PROXYCURL_API_KEY` is configured. LinkedIn passwords are not collected.

### Resume Personas
Maintain multiple resume personas (e.g., "Backend Engineer", "Tech Lead", "Startup Generalist") — each with its own RAG context and tailoring strategy.

### Email Monitoring
Watches your inbox for recruiter replies, surfaces threads that need action, and drafts responses — so nothing falls through the cracks.

### Extended Thinking
Agents can use Claude's extended thinking mode for complex tasks like cover letter generation and salary negotiation, producing higher-quality, more nuanced outputs.

### Token Budget Management
Tracks LLM token usage per user per agent run. Enforces configurable budgets to prevent runaway costs on BYOK keys.

---

## Job Search

Job search has one implementation: `search_all_platforms` in `backend/app/services/job_search_service.py`, used by the Job Search agent (which the jobs API starts as a Temporal workflow), the chat orchestrator and scheduled searches. It reads the shared job catalog first and fills any shortfall from live public sources through the connector registry (`job_connectors.py`, 46 connector families: ATS platforms, aggregators, RSS and JSON-LD). Results are cached (`SEARCH_CACHE_ENABLED`, default on). The server does not drive job sites with a browser.

- **India-first (on by default).** Roles in India and remote roles open to India rank first, and roles open only outside India are hidden. It switches off when every searched place is outside India, and an explicit `india_focus` value on the search overrides it. `backend/app/services/india_focus.py` decides using structured connector fields first, then location text, then residency phrases in remote postings.
- **Multiple locations.** A search takes a list of locations (up to 20 preferred locations on the search request; Delhi/NCR covers the whole capital region).
- **Direct portal search.** Paste a company careers link on the jobs page (or call `POST /api/v1/jobs/portal-search`) to read that one board live. The link must resolve through `resolve_careers_url` to one of: Workday, Greenhouse, Lever (including `jobs.eu.lever.co`), Ashby, SmartRecruiters, Workable, Recruitee, Personio, BambooHR, Teamtailor, Breezy, Pinpoint, HiBob, Trakstar, JobScore, Gem, Rippling, Oracle Recruiting, iCIMS, Jobvite, JazzHR, Dover, Paylocity, Taleo, SAP SuccessFactors, Keka, Freshteam, Kula or Zoho Recruit. Other links are refused with a 422. Matches are saved to your applications list.
- **Saved boards.** Up to 20 careers links can be saved (`GET/POST /api/v1/jobs/boards`, `DELETE /api/v1/jobs/boards/{id}`) so later searches include them. The link is re-checked against the supported list on every use.
- **Indian job boards.** Naukri, Foundit, Instahyre, Cutshort, Shine, Internshala and similar boards have no public listing API and used to be read by the retired server-side browser automation. They now report "unavailable" (`indian_platforms_service.py`) instead of returning jobs. Indian employers are covered by the catalog and connectors above (Greenhouse, Lever, Ashby, SmartRecruiters, Workday India boards, Keka, Freshteam, Kula, Zoho Recruit), the Himalayas feed, and LinkedIn/Indeed through JobSpy.

---

## Applying and Auto-Submit

Applications are filled and submitted by the Chrome extension in your own browser; see [extension/README.md](extension/README.md). The extension shows a review panel marking where each answer came from, and by default you press Submit yourself.

- **Saved answers.** Answers to common screening questions (work authorization, notice period, expected CTC and similar) are stored under **Settings → Applying** (`/api/v1/users/me/saved-answers`, `/api/v1/candidate-profile/answers`). The extension fills from these and your profile, and does not draft answers for questions it must not guess.
- **Auto-submit.** Off by default, set under **Settings → Applying** (`user_preferences.auto_submit_enabled`, daily cap 1–50, default 10, counted over a rolling 24 hours). When on, the server may approve a fully answered form without a click. It never does so on LinkedIn or Naukri, and it refuses when any answer came from anything other than your saved answers or profile, such as an AI draft.
- **Audit trail.** Each approval is stored in `application_submit_approvals`: who approved it (`member` or `auto_submit_setting`), when, the answers submitted, and a screenshot of the reviewed form when the browser allowed one. It is shown on the application.
- **Device tokens.** Each paired browser has its own token, shown once and stored hashed. Tokens expire after 90 days and the extension swaps in a fresh one during the last 30. At most five browsers stay connected; pairing a sixth revokes the least recently used. You can revoke browsers under **Settings → Integrations → Browser extension**.
- **Pairing confirmation.** Switching the paired account from a web page needs a confirmation in the extension popup, and sites outside the built-in job/ATS list need a one-time approval there.

---

## Privacy: Personal Details and the Model

Chat and completion calls go through the internal LLM gateway. Before a request leaves, your email, phone, name, profile links and similar identifiers (from your stored profile, plus pattern detection for Aadhaar, PAN, passport, UPI, card numbers and dates of birth) are replaced with placeholders such as `[[EMAIL_1]]`; the reply is restored locally before agents see it. The mapping lives in memory for one request and is never logged or stored. Identity fields on application forms (name, email, phone, address, links, IDs) are filled from your stored profile without calling a model. Contact details and IDs are stripped from text before it is embedded or stored as RAG chunks. Redaction is on by default and can be turned off per member under **Settings → Models → Privacy**; with `PII_REDACTION_STRICT` (default on) the gateway sends nothing if redaction fails.

Documented limits: the member's name still reaches the embedding provider; third-party names, spelled-out values and values inside images or attachments are not detected; a model that rewrites a placeholder returns the placeholder instead of the value; the decision engine (Jev/Laya), when configured, receives stored answers outside the gateway; and redaction lowers what a provider sees without guaranteeing it. Details: [docs/PRIVACY_PII.md](docs/PRIVACY_PII.md).

---

## Tailored Resumes

Tailoring may reorder and reword but not add. A fact guard (`resume_grounding.py`) compares each tailored resume to your own documents and typed facts, and anything not supported — an invented employer, skill, number, date or title — reaches you as a "Not found in your documents" warning. The resume header uses contact details you verified, never model output. A parse-back check (`resume_parseback.py`) reads each generated PDF/DOCX back the way an applicant tracking system would and reports lost text, order or dates. Note that the PDF renderer cannot draw Devanagari or Tamil text (see [Known Limitations](#known-limitations)). Test coverage is described in [docs/TESTING.md](docs/TESTING.md).

---

## Tech Stack

**Backend**
- Python 3.12 · FastAPI 0.111+ · SQLAlchemy 2.0 async
- LangGraph 0.2+ · LangChain 0.3+ · langchain-postgres 0.0.17
- AES-256-GCM API key encryption (PBKDF2 key derivation)
- Redis 8 (SSE pub/sub, rate limiting, LLM sessions) · Playwright (end-to-end tests only; the server runs no browser automation)
- Temporal (every agent run, job search, application and follow-up runs as a durable workflow) · Nango (OAuth credential proxy)
- ReportLab 4 (PDF) · PyMuPDF + python-docx (parsing)
- Hunter.io · ProxyCurl (optional contact-lookup fallback) · Exa · Resend integrations
- Chrome extension (`extension/`) for in-browser applying · decision engine (TypeSafe Jev / self-hosted Laya)

**Frontend**
- Next.js 16.2.6 App Router · TypeScript 6 · Tailwind CSS 3.4
- shadcn/ui · Zustand 4 · TanStack Query 5
- Motion (Framer Motion successor) · Sonner notifications · Three.js

**Infrastructure**
- Self-hosted PostgreSQL 16 + pgvector 0.7+ · local-disk document storage
- Clerk authentication (Google, LinkedIn, GitHub, email/password, magic link)
- Docker Compose · Nginx (TLS 1.2/1.3 + security headers)
- GitHub Actions CI/CD

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- A [Clerk](https://clerk.com) application (free tier works)
- PostgreSQL 16 with the `pgvector` extension (the Compose stacks provide one)
- At least one AI provider API key (Anthropic, OpenAI, Google, or local Ollama)

### 1. Clone and configure

```bash
git clone https://github.com/blu59204/CareerCraftsAI.git
cd CareerCraftsAI
cp .env.example .env
```

Open `.env` and fill in:

```bash
# Required
APP_SECRET_KEY=<generate with: openssl rand -hex 32>
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
CLERK_ISSUER=https://[your-instance].clerk.accounts.dev
CLERK_SECRET_KEY=<from Clerk dashboard → API keys>
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=<from Clerk dashboard → API keys>
REDIS_URL=redis://redis:6379
TEMPORAL_ADDRESS=temporal:7233   # or your Temporal Cloud endpoint (+ TEMPORAL_TLS_* paths)

# Required if your active model has no embeddings API (Anthropic, DeepSeek,
# OpenRouter, NVIDIA NIM): which provider embeds documents for RAG
EMBEDDING_PROVIDER=ollama   # openai | google | ollama
EMBEDDING_OLLAMA_URL=http://localhost:11434   # server-side Ollama with qwen3-embedding:0.6b

# Optional — enables additional features
HUNTER_API_KEY=<hunter.io key for email finding>
PROXYCURL_API_KEY=<proxycurl key for LinkedIn data>
EXA_API_KEY=<exa.ai key for web search>
RESEND_API_KEY=<resend.com key for transactional email>
NANGO_SECRET_KEY=<Nango secret — enables Gmail/Drive integrations>
```

`.env.example` documents every variable.

### 2. Run database migrations

The SQL files in `supabase/migrations/` are plain PostgreSQL (the directory name is historical). `scripts/migrate.py` applies `deploy/oracle-vm/postgres-bootstrap.sql` (with `--bootstrap`, once, for the roles and `auth.*` helpers the migrations reference) and then every migration not yet recorded in its `schema_migrations` ledger:

```bash
export DATABASE_URL=postgresql://user:password@host:5432/dbname
python scripts/migrate.py --bootstrap   # first time
python scripts/migrate.py               # afterwards: applies only new files
```

A database migrated by hand before the runner existed needs `--baseline` once; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#2-apply-pending-database-migrations).

### 3. Start the stack

```bash
make dev
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API docs | http://localhost:8000/docs |
| Temporal UI | http://localhost:8233 |
| Redis | localhost:6379 |

### 4. Add your AI model

Go to **Settings → Models** and add your API key for at least one provider.

### 5. Upload your resume

Go to **Resume → Upload** and upload your current resume (PDF or DOCX). This seeds the RAG pipeline that all agents use as context.

---

## Development

### Commands

```bash
make dev              # start full stack (hot reload)
make test             # run backend unit tests
make test-integration # run integration tests (needs Postgres/Redis)
make lint             # ruff + eslint check
make format           # ruff --fix + black + eslint --fix
make build            # build all Docker images
make clean            # stop containers, remove volumes
```

### Backend only

```bash
cd backend
source .venv/bin/activate          # Linux/macOS
# .venv_win\Scripts\activate       # Windows

pip install -r requirements.txt -c constraints.txt

uvicorn app.main:app --reload --port 8000

pytest tests/unit tests/security -v  # 1,300+ test functions, mocked, no network
pytest -k "test_name" -v           # single test

bandit -r app/ -f txt              # SAST scan
ruff check . && black --check .    # lint
```

### Frontend only

```bash
cd frontend
npm install
npm run dev           # dev server :3000
npm run build
npm run lint
npm run type-check    # tsc --noEmit
npm run test          # jest
```

### Temporal worker only

```bash
cd backend
source .venv/bin/activate
python -m app.temporal_worker    # polls TEMPORAL_TASK_QUEUE (default "careercraft")
```

Nothing the API starts (agent runs, job searches, applications, follow-ups)
makes progress without this process running — check `GET /health` for
`"temporal": {"workers": 1}`.

> See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the full development guide.

---

## Testing

| Suite | Command | Needs |
|---|---|---|
| Unit + security | `cd backend && pytest tests/unit tests/security` | nothing — fully mocked |
| Integration | `cd backend && pytest tests/integration` | Postgres + Redis (`docker compose -f docker-compose.test.yml up -d`); LLM-backed cases also need `INTEGRATION=1` and a real provider key |
| Offline RAG eval | `cd backend && pytest tests/evals -v` | nothing — deterministic stub, runs in CI |
| Hermetic core journey | `cd backend && pytest tests/e2e/test_hermetic_core_journey.py` | a local PostgreSQL server binary (skips without one); collect-only in CI today |
| Extension | `cd extension && node --test test/*.test.mjs` | Node; the browser apply flow (`test/e2e_apply.mjs`) also needs Playwright + Chromium |
| End-to-end collection | `pytest backend/tests/e2e --collect-only` | nothing — runs in CI on every PR |
| Live end-to-end | `bash scripts/run_e2e_tests.sh` | a running stack plus `TEST_JWT`, `TEST_EMAIL`, `TEST_PASSWORD`, `WEB_URL`, `API_URL` |
| Frontend | `cd frontend && npm run type-check && npm run lint && npm run build` | Node 24 |

The live suite signs in through the real Clerk UI, loads all 20 authenticated screens at desktop and mobile widths, walks each screen's main user journey, and drives all 15 agent responsibilities through the API (`backend/tests/e2e/`). It **never approves** an email send, LinkedIn message, or job application: approval checkpoints are asserted and then rejected. Real sends require `ALLOW_LIVE_SENDS=1`; Gmail draft creation requires `ALLOW_GMAIL_DRAFTS=1`. Screenshots, videos and traces go to gitignored `backend/tests/e2e/.artifacts/`.

Every automated test uses a stub model; none measures real-model quality. What each suite covers and what is not covered is in [docs/TESTING.md](docs/TESTING.md).

In CI, `.github/workflows/ci.yml` runs unit, security, integration, frontend and e2e-collection jobs on every PR; the live suite runs only on a manual `workflow_dispatch` and uploads its artifacts even on failure.

---

## Project Structure

```
CareerCraftsAI/
├── backend/
│   ├── app/
│   │   ├── agents/
│   │   │   ├── orchestrator.py           # LangGraph supervisor
│   │   │   ├── harness.py                # Agent harness entry point
│   │   │   ├── job_search.py             # Job search + scoring (via job_search_service)
│   │   │   ├── resume_agent.py           # RAG resume tailoring + PDF
│   │   │   ├── cover_letter_agent.py     # Cover letter generation
│   │   │   ├── linkedin_agent.py         # LinkedIn profile optimization
│   │   │   ├── email_agent.py            # Recruiter outreach drafting
│   │   │   ├── email_monitor_agent.py    # Inbox monitoring + reply drafting
│   │   │   ├── followup_agent.py         # Scheduled follow-up emails
│   │   │   ├── interview_coach_agent.py  # Mock interviews + scoring
│   │   │   ├── interview_prep_agent.py   # Question banks + study guides
│   │   │   ├── company_research_agent.py # Company intelligence briefings
│   │   │   ├── salary_agent.py           # Salary benchmarking + negotiation
│   │   │   ├── chat_orchestrator.py      # Career Copilot chat graph (starts and monitors runs)
│   │   │   ├── linkedin_outreach_agent.py # LinkedIn outreach drafting
│   │   │   ├── auto_apply_pipeline.py    # Search → score → tailor → queue for the extension
│   │   │   ├── thinking.py               # Extended thinking utilities
│   │   │   ├── state.py                  # Shared LangGraph state schema
│   │   │   ├── strategies.py             # Agent strategy helpers
│   │   │   └── memory/                   # Agent memory manager
│   │   ├── api/v1/
│   │   │   ├── agents.py                 # Agent run + stream + approve
│   │   │   ├── resume.py                 # Resume optimize + download
│   │   │   ├── jobs.py                   # Job search, portal search, saved boards, applications
│   │   │   ├── extension.py              # Extension pairing, devices, tasks, approve-submit, audit
│   │   │   ├── candidate_profile.py      # Application profile + saved answers
│   │   │   ├── email.py                  # Email compose + send
│   │   │   ├── rag.py                    # Document upload + retrieval
│   │   │   ├── interview.py              # Interview coach sessions
│   │   │   ├── interview_prep.py         # Question banks
│   │   │   ├── cover_letter.py           # Cover letter generation
│   │   │   ├── company.py                # Company research
│   │   │   ├── salary.py                 # Salary benchmarking
│   │   │   ├── linkedin.py               # LinkedIn automation
│   │   │   ├── leads.py                  # Lead management
│   │   │   ├── users.py                  # User profile, preferences, saved answers
│   │   │   └── deps.py                   # Auth + DB dependencies
│   │   ├── core/
│   │   │   ├── config.py                 # pydantic-settings config
│   │   │   ├── database.py               # Async SQLAlchemy engine
│   │   │   ├── security.py               # AES-256-GCM encryption
│   │   │   ├── model_router.py           # BYOK model routing
│   │   │   ├── llm_gateway.py            # LLM gateway + token tracking + PII redaction
│   │   │   ├── pii_vault.py              # Placeholder substitution/restore, log scrubbing
│   │   │   ├── clerk_auth.py             # Clerk JWT (RS256/JWKS) verification
│   │   │   ├── event_bus.py              # Redis pub/sub for SSE
│   │   │   └── rate_limit.py             # slowapi (60 req/min/user)
│   │   └── services/
│   │       ├── rag_service.py            # pgvector ingestion + retrieval
│   │       ├── pdf_service.py            # ReportLab PDF generation
│   │       ├── ats_service.py            # ATS keyword scoring
│   │       ├── job_search_service.py     # The single job search: catalog first, live gap fill
│   │       ├── job_connectors.py         # Connector registry (46 families) + resolve_careers_url
│   │       ├── job_catalog.py            # Shared job catalog (job_sources.json boards)
│   │       ├── india_focus.py            # India-first classification and ranking
│   │       ├── saved_boards.py           # Direct portal search + saved careers boards
│   │       ├── indian_platforms_service.py # Naukri etc. report "unavailable"
│   │       ├── job_platforms_service.py  # LinkedIn/Indeed via JobSpy
│   │       ├── browser_control_service.py # Retired stub: server-side browser tasks fail closed
│   │       ├── pii_profile.py            # Member values the gateway hides
│   │       ├── resume_grounding.py       # Tailored-resume fact guard
│   │       ├── resume_parseback.py       # Generated PDF/DOCX read-back check
│   │       ├── resume_facts.py           # User-verified resume contact/facts
│   │       ├── apply_limits.py           # Auto-submit host block and daily cap
│   │       ├── gmail_service.py          # Gmail MCP integration
│   │       ├── proxycurl_service.py      # ProxyCurl LinkedIn data (optional fallback)
│   │       ├── exa_service.py            # Exa web search
│   │       ├── token_budget_service.py   # LLM token budget tracking
│   │       ├── resend_service.py         # Transactional email
│   │       ├── integration_proxy_service.py # Nango credential proxy
│   │       ├── storage_service.py        # Local-disk document storage
│   │       ├── extension_service.py      # Extension device pairing + task claiming
│   │       ├── decision_engine.py        # "System One" typed decisions (Jev/Laya/heuristics)
│   │       └── scheduled_jobs.py         # Job search/follow-up/status-check logic (called by Temporal activities)
│   │   ├── workflows/                    # Temporal workflows + activities
│   │   │   ├── registry.py               # Every workflow/activity the worker hosts
│   │   │   ├── starters.py               # API-side start/signal helpers (503 if Temporal is down)
│   │   │   ├── agent_run.py              # AgentRunWorkflow (agent-run/{run_id})
│   │   │   ├── job_search.py             # JobSearchWorkflow (job-search/{run_id})
│   │   │   ├── auto_apply.py             # AutoApplyWorkflow (user browser extension)
│   │   │   ├── followup.py               # FollowupWorkflow (followup/{application_id}, day 5/12)
│   │   │   └── scheduled.py              # Temporal Schedules: daily search, maintenance
│   │   ├── applications/                 # Form-answer planning (no ATS adapters; the extension submits)
│   │   ├── board_harvest.py              # Operator command: board discovery + catalog health
│   │   └── temporal_worker.py            # Run with `python -m app.temporal_worker`
│   └── tests/
│       ├── unit/                         # mocked, fast CI
│       ├── security/                     # auth, HITL bypass, input validation
│       ├── evals/                        # offline RAG retrieval eval
│       ├── integration/                  # Postgres/Redis-backed; LLM cases opt-in
│       └── e2e/                          # hermetic journey + live Playwright/API suite
├── frontend/src/
│   ├── app/
│   │   ├── (app)/                        # Authenticated routes
│   │   ├── (auth)/                       # Login · Register
│   │   └── (marketing)/                  # Landing · Pricing · Docs · About
│   ├── components/                       # UI components + agent stream + approval modal
│   ├── lib/                              # axios client · agent-run polling · SSE · Clerk token · Nango connect
│   └── store/                            # Zustand slices (agents, user)
├── extension/                             # Chrome extension — applies in the user's own browser
│   ├── src/background.js                 # pairing, polling, network calls
│   └── src/content/                      # DOM helpers, review panel, platform drivers
├── deploy/laya/                           # Self-hosted "System One" decision server (Jev alternative)
├── supabase/migrations/                  # 71 SQL migrations (plain PostgreSQL)
├── deploy/oracle-vm/                        # The actual production stack — self-hosted Postgres/Nginx, Compose
├── scripts/run_e2e_tests.sh              # Live end-to-end runner with preflight checks
├── docker-compose.dev.yml                # Local dev stack (hot reload)
├── docker-compose.test.yml               # Disposable CI integration-test infra (no prod creds/volumes)
├── Makefile                              # Top-level dev commands
└── locustfile.py                         # Load test baseline
```

---

## Supported AI Providers

| Provider | Chat Models | Embeddings |
|---|---|---|
| **Anthropic** | Claude Sonnet 4.6, Haiku 4.5 (+ extended thinking) | — (uses `EMBEDDING_PROVIDER`) |
| **OpenAI** | GPT-4o, GPT-4o-mini | text-embedding-3-small |
| **Google** | Gemini 2.0 Flash, Pro | gemini-embedding-001 (768-d) |
| **Ollama** | Any local model | qwen3-embedding:0.6b (1024-d) |
| **NVIDIA NIM** | Llama 3.1 70B, others | — (uses `EMBEDDING_PROVIDER`) |
| **DeepSeek** | DeepSeek chat models | — (uses `EMBEDDING_PROVIDER`) |
| **OpenRouter** | Any model in the OpenRouter catalog | — (uses `EMBEDDING_PROVIDER`) |

Multiple providers can be configured simultaneously — select the active model in **Settings → Models**. Providers without an embeddings API use the provider named in `EMBEDDING_PROVIDER` (`openai`, `google`, or `ollama`) for RAG; with `ollama`, `EMBEDDING_OLLAMA_URL` points at the server's Ollama (`ollama pull qwen3-embedding:0.6b`).

---

## API Overview

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/agents/run` | Start an agent run |
| `GET` | `/api/v1/agents/runs/{id}` | Poll a run's persisted status and output |
| `GET` | `/api/v1/agents/{id}/stream` | SSE live event stream |
| `POST` | `/api/v1/agents/{id}/approve` | Approve or cancel pending action |
| `POST` | `/api/v1/resume/optimize` | Generate tailored resume |
| `GET` | `/api/v1/resume/download/{id}` | Download PDF |
| `POST` | `/api/v1/rag/upload` | Upload document (PDF/DOCX/TXT) |
| `GET` | `/api/v1/jobs/applications` | List applications pipeline |
| `POST` | `/api/v1/email/compose` | Draft outreach email |
| `POST` | `/api/v1/email/approve/{id}` | Send approved email via Gmail |
| `POST` | `/api/v1/cover-letter/generate` | Generate cover letter |
| `POST` | `/api/v1/interview/session/start` | Start mock interview session |
| `POST` | `/api/v1/interview/session/{id}/answer` | Score an answer, get the next question |
| `GET` | `/api/v1/company/{name}/intel` | Get saved company intelligence |
| `POST` | `/api/v1/salary/report` | Benchmark compensation |
| `POST` | `/api/v1/linkedin/outreach/identify` | Find contacts and draft LinkedIn outreach |
| `GET` | `/api/v1/email/inbox-cleanup` | List promotional/update mail with unsubscribe links |
| `GET` | `/api/v1/integrations` | Gmail/Drive/Calendar connection status |
| `GET` | `/api/v1/leads` | List recruiter leads |
| `GET` | `/health` | Health check (app root, not under `/api/v1`) |

Long-running agents (company research, salary, interview prep, LinkedIn profile optimization) start with `POST /agents/run` and a `task_type`, then are polled via `GET /agents/runs/{id}`.

Full interactive docs at `http://localhost:8000/docs` (dev mode).

> See [docs/API.md](docs/API.md) for full API reference with request/response schemas.

---

## Human-in-the-Loop

No agent sends emails or submits applications without your explicit approval. The flow:

```
Agent completes task
        ↓
SSE pushes "checkpoint" event to browser
        ↓
ApprovalModal shows you exactly what will happen
        ↓
You click Approve → action executes
      OR
You click Cancel → action discarded
```

This is enforced server-side — the `/approve` endpoint is the only code path that triggers irreversible actions.

---

## Security

- **API keys** encrypted at rest with AES-256-GCM (PBKDF2, unique salt per key, decrypted only at request time)
- **Authentication** via Clerk session JWTs, verified locally on every protected route against Clerk's JWKS (RS256)
- **Data isolation** enforced in the API: every query is scoped to the authenticated user. The RLS policies in the migrations do not apply to the API, which connects as the table owner (see `deploy/oracle-vm/postgres-bootstrap.sql`)
- **Rate limiting** per-route limits via slowapi, keyed by user
- **Model calls** every agent reaches its model through the internal LLM gateway with a short-lived session token, so the member's API key stays out of agent memory; the gateway adds the shared untrusted-content rules to every request. Embeddings still use the key inside the worker
- **Account deletion** after the 15-day grace period, the sweep stops the member's workflows, revokes integrations, and deletes files, RAG collections, agent memory, Redis keys and the Clerk identity before the database row
- **No out-of-band job triggers** — background jobs run only as Temporal workflows; nginx still returns 404 for `/internal/*` as defense in depth
- **Browser isolation** Playwright creates a separate browser context per user
- **Dependency audit** backend installs from `requirements.lock`, and CI fails on a stale lock or a `pip-audit` finding; `npm audit` runs as a non-blocking report; `bandit` SAST on every PR
- **CVE-2025-68664** (LangChain serialization) — patched (langchain-core 1.4.0 or later; see the lock file)
- **CVE-2025-67644** (LangGraph SQLite injection) — blocked via `constraints.txt`
- **langchain-community** sunset — replaced with `langchain-postgres` for vector store

> See [docs/SECURITY.md](docs/SECURITY.md) for the full security posture.

---

## Database Schema

The migrations in `supabase/migrations/`, applied in order by `scripts/migrate.py`, include:

| Migration | Table / Change |
|---|---|
| 0001 | `users` |
| 0002 | `model_settings` |
| 0003 | `documents` |
| 0004 | `applications` |
| 0005 | `leads` |
| 0006 | `agent_runs` |
| 0007 | pgvector HNSW indexes |
| 0008 | Row-Level Security policies |
| 0009 | Auth provider migration (superseded by 0028) |
| 0010 | `user_preferences` |
| 0011 | `cover_letter_versions` |
| 0012 | `interview_sessions` |
| 0013 | `salary_reports` |
| 0014 | `company_intel` |
| 0015 | `resume_personas` |
| 0016 | `linkedin_outreach_queue` |
| 0017 | `ats_scores` |
| 0018 | RLS fix for `clerk_user_id` |
| 0019 | LinkedIn credentials + auto mode |
| 0021 | Google OAuth tokens |
| 0025, 0032 | HNSW embedding indexes |
| 0028 | Clerk third-party auth RLS (`users.clerk_user_id` holds the Clerk user id) |
| 0034 | Application attempts + outbound messages (send idempotency) |
| 0035 | Candidate profiles + saved form answers |
| 0036 | Temporal columns on application attempts |
| 0037 | Integration connections (Nango) |
| 20260909… | Durable agent workflow tables |

> See [docs/DATABASE.md](docs/DATABASE.md) for full schema reference with column types and RLS policies.

---

## Deployment (Production)

### 1. Configure the deployment

Follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) to provision the VM, protected environment files in `/opt/careercraft-secrets`, Clerk/Nango and the Temporal server. Configure `deploy/oracle-vm/nginx.conf` and the host TLS reverse proxy for your domain. There is no default root production Compose file.

### 2. Start production stack

From the repository root, with the deployment prerequisites configured:

```bash
docker compose -p careercraft-isolated -f deploy/oracle-vm/compose.yml up -d --build
docker compose -p careercraft-isolated -f deploy/oracle-vm/compose.yml ps
curl https://yourdomain.com/health
```

For a single small VM, `deploy/oracle-vm/compose.yml` runs the whole stack with host networking: backend, `temporal-worker` (runs every workflow and registers the Schedules — not optional), frontend, self-hosted Postgres, Redis, the Nginx gateway. The Temporal server itself is a separate stack, `deploy/oracle-vm/temporal-compose.yml`, meant for its own box; `TEMPORAL_ADDRESS` in `backend.env` points at it (or at Temporal Cloud). Backend, frontend, Postgres and Redis have healthchecks, and services that depend on them wait until they are healthy.

### 3. Run migrations

Run `scripts/migrate.py`, as in [Quick Start step 2](#2-run-database-migrations). The Oracle Compose stack runs the bootstrap automatically on first start.

### 4. Deploy updates

Deploys are manual: pull `master` on the VM and rebuild the Oracle Compose stack, as described in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). There is no auto-deploy workflow.

> See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the complete production deployment guide.

---

## Post-Launch Checklist

- [ ] Confirm HNSW indexes exist after first RAG ingestion (migrations `0007`, `0025`, `0032`)
- [ ] Set `APP_ENV=production` in `.env` (disables `/docs` endpoint)
- [ ] Configure Clerk sign-in providers (Google, LinkedIn, GitHub) and allowed redirect origins
- [ ] Configure Nango Gmail/Drive integration keys and the verified webhook URL
- [ ] Verify Redis `appendonly yes` is persisting to Docker volume
- [ ] Set up PostgreSQL backups and connection-pool alerts
- [ ] Confirm `GET /health` reports `"temporal": {"connected": true, "workers": >=1}`
- [ ] Set Hunter.io, ProxyCurl, and Exa API keys for full feature coverage

---

## Documentation

| Document | Path | Description |
|---|---|---|
| Product Requirements | `CareerCraft AI.md` | Authoritative PRD |
| Architecture | `docs/ARCHITECTURE.md` | System design, data flows, decisions |
| API Reference | `docs/API.md` | Full endpoint docs with schemas |
| Database Schema | `docs/DATABASE.md` | All tables, columns, RLS policies |
| Deployment Guide | `docs/DEPLOYMENT.md` | VPS, Docker, CI/CD, SSL |
| Development Guide | `docs/DEVELOPMENT.md` | Local setup, testing, conventions |
| Security | `docs/SECURITY.md` | Auth, encryption, CVEs, auditing |
| Nango integrations | `docs/NANGO_INTEGRATION.md` | Connect sessions, webhook setup, migration and rollback |
| Contributing | `docs/CONTRIBUTING.md` | PR process, code standards |
| Configuration | `docs/CONFIGURATION.md` | All environment variables |

---

## License

MIT
