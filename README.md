# CareerCraft AI

Self-hosted, full-stack job-search automation platform using specialized AI agents. Users bring their own AI API keys; agents prepare actions, but email sends and application submissions require explicit human approval unless a member turns on the capped auto-submit described below.

## Capabilities

- Search and score jobs from the shared catalog, live public sources, saved boards, and pasted company careers links; tailor resumes and generate ATS-optimized PDFs and cover letters
- Optimize LinkedIn profiles and draft recruiter outreach to contacts you supply
- Monitor email, draft replies, and schedule follow-ups
- Provide mock interviews, interview prep, company research, and salary negotiation support
- Apply to saved jobs through a Chrome extension in your own browser, with review and approval
- Retrieve context from uploaded documents using RAG and pgvector

## Architecture and stack

- **Frontend:** Next.js 16, TypeScript, Tailwind
- **Backend:** Python 3.12, FastAPI, SQLAlchemy, LangGraph
- **Workflows:** Temporal runs agent tasks, searches, applications, and follow-ups. The Temporal worker must be running for API-started work to progress.
- **Data:** Self-hosted PostgreSQL 16 + pgvector, Redis, local document storage
- **Other:** Clerk authentication, Nango Gmail/Drive integrations, Docker Compose
- **AI providers:** Anthropic, OpenAI, Google, Ollama, NVIDIA NIM, DeepSeek, OpenRouter

## Quick start

Prerequisites: Docker Compose, Clerk configuration, and an AI provider key.

1. Clone the repository and copy `.env.example` to `.env`.
2. Configure required secrets, database, Clerk, Redis, Temporal, and embedding settings.
3. Apply migrations with `python scripts/migrate.py --bootstrap` (first run).
4. Start with `make dev`.
5. Add an AI model in **Settings → Models** and upload a resume.

Local URLs: frontend `localhost:3000`, API docs `localhost:8000/docs`, Temporal UI `localhost:8233`.

## Job search and saved boards

Searches read the shared job catalog first and fill any shortfall from live public sources. The server does not drive job sites with a browser.

**Direct portal search** reads one company's board live. Paste a careers link into the jobs page (or call `POST /jobs/portal-search`). Supported links are on Workday, Greenhouse, Lever (including `jobs.eu.lever.co`), Ashby, SmartRecruiters, Workable, Recruitee, Personio, BambooHR, Teamtailor, Breezy, Pinpoint, HiBob, Trakstar, JobScore, Gem, Rippling, Oracle Recruiting, and iCIMS. Links to other systems, such as Taleo, Jobvite, SuccessFactors, or JazzHR, are refused.

**Saved boards** remember up to 20 careers links so that later searches include them. Add one with the save option on a portal search, or through `POST /jobs/boards`.

## Applying and auto-submit

Applying happens in the Chrome extension in your own signed-in browser; see [`extension/README.md`](extension/README.md). Before submitting, the extension shows a review panel that marks where each answer came from. By default you press Submit yourself.

**Saved answers** (Settings) hold your answers to common screening questions. The extension fills from these and your profile, and never drafts answers for them.

**Auto-submit** (Settings → Applying) is off by default. When on, the server may approve a fully answered form without a click, up to a daily cap of 1–50 (default 10) per rolling 24 hours. It never submits on LinkedIn or Naukri, and it refuses any form with an AI-drafted answer.

**Audit trail:** every approval records whether you or the auto-submit setting approved it, the time, the answers submitted, and a review screenshot when the browser captured one. This is shown on each application.

**Extension devices:** each paired browser has its own token. Tokens expire after 90 days and refresh automatically during the last 30 days. Up to five browsers can be connected; pairing a sixth disconnects the least recently used one. Browsers can be revoked under **Settings → Integrations → Browser extension**.

## Maintenance and evals

The company board catalog is maintained by an operator command, not by the app: `python -m app.board_harvest health` checks each catalogued board. Add `--prune` to remove dead (404/410) entries from `backend/app/services/job_sources.json`, and `--report <file>` to write per-source results. It makes many outbound requests, so run it rarely.

Retrieval is covered by offline evals that run in CI (`cd backend && pytest tests/evals -v`). They use a deterministic stub, so real-embedding quality is not yet measured. See [`docs/RAG_EVALS.md`](docs/RAG_EVALS.md).

## Development and testing

Common commands: `make dev`, `make test`, `make test-integration`, `make lint`, `make format`, `make build`.

Tests include mocked unit/security suites, Postgres/Redis integration tests, frontend checks, and optional live end-to-end tests. Live E2E tests reject approval checkpoints; actual sends require explicit environment flags.

## Safety and security

- Server-side approval gates email sends and application submissions. Auto-submit is the only path that skips a click, and it is capped and audited.
- API keys are encrypted at rest with AES-256-GCM; protected routes verify Clerk JWTs.
- API queries are scoped to the authenticated user.
- Agent model calls use an internal LLM gateway; rate limits and token budgets are supported.
- Job posts, emails, and retrieved documents are treated as untrusted data in prompts. Inbound email changes an application's status only when the sender's domain belongs to the company or a known job platform.
- LinkedIn sign-in happens in your own browser. CareerCraft does not collect LinkedIn passwords.
- Applications run in the user's own browser via the extension. The backend does not drive job sites.

## Known limitations

- Applying has not been tested against real Workday, Greenhouse, Lever, or Ashby forms. Test by hand on three Workday tenants before relying on auto-submit.
- Auto-submitted applications usually have no screenshot, because screenshots are captured only when you click the extension's toolbar icon.
- Indian job boards (such as Naukri) return no results: the server-side scraper for them is retired.
- Outreach uses contacts you supply. A Proxycurl contact lookup remains only as a fallback, and only when `PROXYCURL_API_KEY` is set and no contacts are given.
- Retrieval can return no results on some errors, and it applies no similarity cutoff. Real-embedding quality is unmeasured.
- Before deploying, apply the migrations `20261111120000_auto_submit_audit_and_token_expiry` and `20261111130000_saved_job_boards`.

## Deployment

Production Compose configuration is under `deploy/oracle-vm/`; the Temporal server is deployed separately or via Temporal Cloud. Production updates are manual. See `docs/DEPLOYMENT.md`.

## Documentation and license

Architecture, API, database, deployment, development, security, configuration, and integration guides are in `docs/`. **License: MIT.**
