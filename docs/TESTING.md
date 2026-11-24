# Testing

What each suite covers, how to run it, and what is not covered. Commands run from the
repository root unless a directory is given.

## Suites at a glance

| Suite | Needs secrets/network | Runs in CI | Command |
| --- | --- | --- | --- |
| Backend unit + security | No | Yes (`backend` job) | `cd backend && pytest tests/unit tests/security` |
| Offline RAG eval | No | Yes | `cd backend && pytest tests/evals` |
| Hermetic core journey | No (needs a local PostgreSQL server binary) | Collect-only today; see below | `cd backend && pytest tests/e2e/test_hermetic_core_journey.py` |
| E2E harness contract | No | Collect-only | `cd backend && pytest tests/e2e/test_harness_contract.py` |
| E2E collection guard | No | Yes (`e2e-collection`) | `cd backend && pytest tests/e2e --collect-only -q` |
| Durable workflow integration | Docker (Postgres + Redis) | Yes | see `docker-compose.test.yml` and the `backend-workflow-integration-test` CI job |
| LLM-backed integration (`tests/integration`) | Yes (`INTEGRATION=1`, a provider key) | No | `cd backend && INTEGRATION=1 ... pytest tests/integration` |
| Live agent matrix / user journeys / screen smoke | Yes (live stack, `TEST_JWT`, Clerk test account) | Manual `workflow_dispatch` only | `bash scripts/run_e2e_tests.sh` |
| Frontend unit | No | Yes | `cd frontend && npm test` |
| Extension unit | No | Yes | `cd extension && node --test test/*.test.mjs` |
| Extension browser apply flow | No (needs Playwright + Chromium) | Yes | `cd extension && npm install --no-save playwright && npx playwright install chromium && node test/e2e_apply.mjs` |

## Hermetic core journey (`backend/tests/e2e/test_hermetic_core_journey.py`)

One test walks the product's main path through the app's **real FastAPI routes**
(httpx ASGI client) against a **real, throwaway PostgreSQL** that the test starts with
`initdb`/`pg_ctl` and builds with `Base.metadata.create_all` (`hermetic_postgres.py`).
It skips cleanly when no PostgreSQL server binaries are installed (set `HERMETIC_PG_BIN`
to the server's `bin` directory to point at one). GitHub's ubuntu runners have one.

Steps and what is asserted:

1. `POST /rag/upload` a real PDF: parsed by the real isolated parser; name, email, employers survive.
2. `POST /jobs/portal-search` against a canned Greenhouse board (connector HTTP stubbed): the matching job is saved as an application with its description.
3. `POST /resume/optimize`: the real resume agent runs with a stub model. The tailored text states only supported facts (no warnings, `terraform` listed as missing, never claimed); the stored PDF and the PDF/DOCX downloads pass the parse-back check (`app/services/resume_parseback.py`); `GET /resume/tailored/{id}` returns no warnings.
4. Pair a device (`POST /extension/pair`), claim the task, report progress, `POST .../plan` (the email field is answered from the member's profile).
5. `POST .../review` then `.../approve-submit`: one approval row is recorded (member, review hash, application), task and attempt move to `submitting`, the submitted outcome is relayed to the workflow, and a replayed approval is refused (409).

A second test has the stub model invent an employer, a skill and a number and checks that
each reaches the member as a "Not found in your documents" warning.

Replaced boundaries (each is a seam to extend, not a gap to hide): the LLM, HTTP, file
storage, event bus and Temporal. Temporal is replaced by calling the same code its
activities call (`record_run_result`, and creating the attempt/task rows that the
reserve and extension activities create).

## Resume correctness tests

- `tests/unit/test_resume_fact_guard.py`: adversarial grounding. Invented employers, titles, degrees, certifications, skills (also lowercase), inflated numbers or changed units, shifted years, invented months, dates moved between jobs, reversed dates, dropped jobs.
- `tests/unit/test_resume_parseback.py`: real PDF/DOCX files from an Indian candidate profile (+91 phone, CGPA, percentage, 12 projects, diacritics, a 150-character URL) are read back for text, order, columns, fonts, images, size, page count, date formats; Devanagari/Tamil loss is reported.
- `tests/unit/test_resume_tailoring_integrity.py`: the agent end to end with a stub model, and honest ATS scoring (keyword stuffing lowers the score; skills-only keywords earn partial credit).
- `tests/unit/test_search_cache*.py`: shared job-search cache (fakeredis).

## Not covered

- Live model quality. Every automated test uses a stub model; none says anything about
  how well a real model tailors. The live suites need a provider key.
- Complex-script rendering: the PDF renderer cannot shape Devanagari/Tamil, so such text
  is reported as missing by parse-back instead of drawn (the Word export keeps it).
- Real job-board payloads, `/jobs/search` (Temporal-started catalog + live adapters),
  Gmail/LinkedIn/Calendar integrations, and real Chrome submission on a real portal.
- The live suites' route lists are checked against the OpenAPI schema by hand only
  (last check: every path resolves); their behaviour needs the live stack.
- The extension's `test/e2e_live.py`, `e2e_browser_mode.py` and `mv3_gate.py` are manual tools.
- Running the hermetic journey in CI: `ci.yml` only collects `tests/e2e`. Add
  `pytest tests/e2e/test_hermetic_core_journey.py tests/e2e/test_harness_contract.py` to a
  job on a runner with PostgreSQL to run it on every PR.
