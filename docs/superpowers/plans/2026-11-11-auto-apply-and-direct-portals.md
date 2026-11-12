# Auto-apply, direct portal search and ATS-aware resumes — review and plan

Date: 2026-11-11. Sources: a read-only review of this repository, a source-level
read of five reference projects, web research on ATS platforms, and two
prototype change sets that are in the working tree but not committed.

## 1. Harsh review: 4/10

| Area | Score | Why |
|---|---:|---|
| Architecture | 4 | Three overlapping job-search paths; registered application adapters nobody calls. |
| Correctness | 4 | Several core paths cannot succeed as written (see below). |
| Security | 6 | Good URL-fetch hardening and key encryption; weak extension-token lifecycle. |
| Tests | 6 | About 1,500 fast unit tests, but a chunk of them cover dead code. |
| Features / UX | 3 | The README promises far more than a user can actually do. |
| Maintainability | 3 | Oversized modules, blanket exception handlers, stale docs, committed tool output. |

### What is bad

1. **Server-side apply is switched off.** Browser tasks raise "automation
   retired", so form filling falls back to manual and the Indian-platform
   scrapers return nothing. The README still advertises them.
2. **Workday was effectively unsupported.** Two unvalidated tenants in the
   catalog, a legacy fetcher pointing at a dead endpoint, and no Workday
   application support at all.
3. **The Greenhouse / Lever / Ashby / Generic backend adapters are dead code.**
   They are registered and tested, never called.
4. **Submission can never report success.** Success requires a `SUBMITTED`
   marker that the agent prompt never asks for.
5. **Search drops every location but the first**, silently.
6. **The legacy ATS fan-out timeout did not time out** (pool shutdown waited
   for stragglers).
7. **LinkedIn passwords are still collected and stored** although nothing uses
   them. That is pure liability.
8. **Extension tokens never expire and devices are uncapped.** The extension is
   also only installable via "Load unpacked".
9. **Recruiter lookup depends on Proxycurl**, a third party whose availability
   was not verified.
10. **The extension filled forms wrongly**: substring option matching ("No"
    could select "Not applicable", "Male" could select "Female"), custom
    dropdowns ignored, resume uploads skipped on styled drop zones, failed
    fills reported as filled, and missing host permissions for several portals.

### What is good

- Outbound URL fetching is properly hardened (HTTPS only, public-address
  checks, DNS pinning, redirect revalidation).
- About 30 job-board connector families with tenant checks and safe XML parsing.
- AES-GCM for user API keys; extension tokens stored hashed.
- Clerk auth handles expiry and concurrent provisioning correctly.
- Temporal workflows with approval checkpoints and duplicate-search reuse.
- A large, fast unit suite and CI security checks.

### What is missing

Must have: working Workday apply; one search path; a health-checked source
catalog; "paste a careers URL" search; saved answers for screening questions;
an audit trail of what was submitted; job liveness checks before applying.

Nice to have: fact-checking of generated resume claims; per-source freshness
stats; self-hosted ATS detection (Phenom, TalentBrew, Jibe); Chrome Web Store
distribution; a privacy filter on outbound LLM calls.

## 2. What the reference projects do better

| Project | License | Worth taking |
|---|---|---|
| strelov1/freehire | MIT (code may be ported with attribution) | Question-level form model and label-addressed fills (`extension/lib/form.ts`), combobox driver, multi-step walker, CV attach; HTML-based ATS detection (`internal/ingest/atsdetect`); per-source stats; adapters for iCIMS, Taleo, Oracle, SuccessFactors, Jobvite, Dover, Gem, Comeet. |
| career-ops-hq/career-ops | MIT | Workday CXS discovery with the 2,000-offset cap, retries and rate-limit handling; host-aware concurrency, 429 backoff, dead-board cache; liveness and repost checks. |
| feder-cr/Jobs_Applier_AI_Agent | MIT | Per-ATS apply instructions, explicit "blocked" states (captcha, closed, missing credentials), screenshots and answers kept as evidence, email verification-code plugins. Its bundled datasets carry mixed licenses. |
| AienTech/dossier | AGPL-3.0 (ideas only, do not copy code) | Fact guard that flags names, numbers and credentials in generated documents that are not grounded in the profile or posting. |
| adeebmirza/lazee.dev.next | No license (ideas only) | Saved answers reused across applications; its extension is not in the repository, and it has no job discovery. |

## 3. Job portals companies use

Keyless listing endpoints that work without scraping:

| Portal | Recognise by | Listing endpoint |
|---|---|---|
| Workday | `*.wdN.myworkdayjobs.com`, `*.myworkdaysite.com` | `POST /wday/cxs/{tenant}/{site}/jobs` |
| Greenhouse | `boards.greenhouse.io`, `job-boards.greenhouse.io` | `boards-api.greenhouse.io/v1/boards/{token}/jobs` |
| Lever | `jobs.lever.co`, `jobs.eu.lever.co` | `api.lever.co/v0/postings/{site}` |
| Ashby | `jobs.ashbyhq.com` | `api.ashbyhq.com/posting-api/job-board/{slug}` |
| SmartRecruiters | `jobs.smartrecruiters.com` | `api.smartrecruiters.com/v1/companies/{id}/postings` |
| Workable | `apply.workable.com` | `apply.workable.com/api/v1/widget/accounts/{slug}` |
| Recruitee | `{slug}.recruitee.com` | `/api/offers/` |
| Personio | `{slug}.jobs.personio.de` | `/xml` |
| BambooHR | `{slug}.bamboohr.com` | `/careers/list` |
| Teamtailor, Breezy, Pinpoint, HiBob, JobScore | vendor subdomains | already have connectors |
| Oracle Recruiting Cloud, iCIMS | `*.oraclecloud.com/hcmUI/CandidateExperience`, `*.icims.com` | connectors prototyped |

Not yet covered: Taleo, SAP SuccessFactors, Jobvite, JazzHR, and the HR-suite
portals (ADP, UKG, Dayforce, Paylocity, Rippling), plus Phenom / Eightfold /
Avature front ends. Market-share figures for these are vendor samples that
contradict each other; none is reliable enough to quote.

## 4. Resume guidance per portal

One "ATS-safe" master resume, with small per-portal adjustments:

- Text-based PDF under 2 MB. Keep a DOCX copy for Personio and for older
  Taleo / iCIMS sites.
- One column. No tables, text boxes, images, icons or skill bars.
- Standard headings (Experience, Education, Skills), standard fonts, one
  consistent date format, contact details in the body rather than the header.
- Mirror the posting's wording for skills and job titles only where it is true.
- Workday: the upload is parsed into form fields, so the parsed work history
  and education must be reviewed and corrected on the My Experience step.
- Greenhouse / Lever / Ashby: a human reads the original file, so readability
  matters more than parser tricks.
- The "75% of resumes are auto-rejected by ATS" claim has no supporting
  evidence. Rejections come mostly from knockout questions and recruiter
  search filters, so truthful, complete screening answers matter more than
  keyword stuffing.

Product consequence: generate one single-column template in both PDF and DOCX,
pick the format from the detected portal, and run a parse-back check on the
generated file (extract text, confirm name, email, titles and dates survive).

## 5. Status after the fix round (2026-11-12)

Everything below is on branch `feat/auto-apply-direct-portals-hardening`.
Backend unit, security and eval suites: 1,674 passed. Extension: 20 node tests
and 112 browser e2e checks. Frontend: typecheck clean, 36 tests.

### Done

**Auto-apply (extension + backend)**
- Workday multi-step driver: My Information, My Experience (resume upload,
  work history and education with "Add" for extra blocks, calendar popups),
  application questions, voluntary disclosures only from saved answers, step
  navigation with validation-error detection.
- Fixed wrong option matching, ignored custom widgets, skipped resume uploads,
  fill failures reported as filled, and missing host permissions.
- Server-controlled auto-submit: off by default, per-user switch and daily cap
  (1-50) in Settings → Applying, enforced at approve-submit; never on LinkedIn
  or Naukri; refused when any answer is AI-drafted; audit row per approval
  with answers, sources and optional screenshot; shown in the applications
  drawer as "Auto-submitted" vs "Approved by you".
- Saved answers API and UI (work authorization, sponsorship, notice period,
  salary, relocation, start date, voluntary disclosures).
- Dead server-side apply path removed (auto_apply_service, form_filler,
  applications/adapters, validator) after confirming no live callers.
- Device tokens expire after 90 days with rotation; 5 devices per user;
  LinkedIn password endpoint removed (cleanup SQL below).
- Extension 0.6.0, least-privilege manifest, README matches behaviour.

**Direct portal search**
- `POST /jobs/portal-search` resolves a pasted careers URL (Workday, Greenhouse,
  Lever, Ashby, SmartRecruiters, Workable, Recruitee, Personio, BambooHR,
  Teamtailor, Oracle, iCIMS) and fetches through the hardened client;
  `GET/POST/DELETE /jobs/boards` saves boards (max 20) that join future
  searches. A panel on the jobs page drives it.
- Every location is honoured in the interactive, daily and ranking paths.
- Legacy ATS fetchers deleted; job_connectors is the single implementation.
- Catalog health command `python -m app.board_harvest health [--prune]`:
  22 dead boards pruned, 45 live boards added (19 Workday tenants);
  165 checked → 142 ok, 22 empty, 0 dead.
- Workday/Oracle/iCIMS/Workable receive the user's keywords server-side;
  per-host concurrency cap of 4 and 429/503 backoff.

**Agents, prompt injection, RAG**
- All 17 agents exercised with a stub model. Fixed: orchestrator stuck on an
  unknown task, email monitor reporting success on total failure, interview
  coach sending its raw template, company-research and computer-agent
  KeyErrors, LinkedIn agent flagging retired automation.
- Shared `untrusted()/fenced()/one_line()` helpers; fence break-out closed in
  every live prompt; profile leak via browser navigation blocked; contact
  details stripped from outreach notes; the model can no longer choose email
  recipients; memory-poisoning filter; wider API-key redaction.
- Inbound email changes an application's status only when the sender's
  domain is the company's or a known ATS; otherwise it becomes a suggestion.
- RAG: user scoping verified, blank/NUL chunk fixes, `distance` on results,
  `format_context()` for delimited context, offline retrieval eval
  (`backend/tests/evals`, docs/RAG_EVALS.md) wired into CI. Stub-embedding
  scores validate plumbing only; real-embedding scores are unmeasured.

### Still open
- Nothing here has touched a real Workday, Greenhouse, Lever or Ashby form;
  test by hand on three Workday tenants before trusting auto-submit.
- Apply both migrations (`20261111120000_auto_submit_audit_and_token_expiry`,
  `20261111130000_saved_job_boards`) before deploying; no Postgres was
  available here, so they have not been executed.
- Screenshots are captured only after the user clicks the toolbar icon, so
  auto-submitted applications usually have none.
- `rag_service.retrieve` still returns `[]` on error for non-resume types and
  applies no similarity cutoff.
- `/linkedin/outreach/identify` should accept user-supplied contacts
  (Proxycurl is retired); `auto_apply_pipeline.py` still references it.
- Taleo, SuccessFactors, Jobvite and JazzHR connectors; Chrome Web Store
  publication; README rewrite; splitting oversized modules.
- Browser check of the Applying settings page, the application drawer and the
  jobs portal panel.

### LinkedIn credential cleanup (run deliberately)
```sql
UPDATE public.users SET linkedin_password_enc = NULL, linkedin_email_enc = NULL
 WHERE linkedin_password_enc IS NOT NULL OR linkedin_email_enc IS NOT NULL;
-- after removing both columns from backend/app/models/db.py and deploying:
ALTER TABLE public.users DROP COLUMN linkedin_password_enc, DROP COLUMN linkedin_email_enc;
```

## 6. Continuation: privacy, India eligibility and release verification

This section supersedes the earlier "Still open" implementation list where
code has since landed. Do not interpret historical test counts as current
release evidence; use the pull request's Verification section for the final run.

### Implemented since the first checkpoint

- Request-local PII placeholders at the model gateway, local rehydration,
  privacy preferences, deterministic identity-field answers, contact-stripped
  embeddings and process-wide log scrubbing. This is pseudonymisation, not a
  guarantee of anonymity: unknown free-text PII, member names in embeddings,
  old vectors and explicit privacy opt-outs remain important limitations.
- India-first job classification, city aliases, eligible-remote filtering,
  more live-verified Indian employer boards and public connectors. Persisted
  preference and application-candidate eligibility checks are part of the
  final integration pass. India eligibility is not proof of Indian ownership
  or headquarters, and remote eligibility must still be checked on the posting.
- Additional verified ATS families: Jobvite, JazzHR, Dover, Paylocity,
  SuccessFactors (verified hosts only), Taleo, Keka, Kula, Freshteam and Zoho
  Recruit. No promise of "every provider": login-only or anti-bot-protected
  services are explicitly unavailable rather than bypassed.
- Resume fact checks, full-source handling, PDF/DOCX parse-back, A4 defaults,
  stronger repair/fallback handling, and a hermetic FastAPI route journey.
- Real Postgres migration/concurrency/deletion tests, explicit pairing-change
  confirmation, job-host permission gates and production packaging without
  localhost injection.
- Model-cache key/privacy fixes and cache corruption handling. Production
  dependency audits retain a documented upstream Zod 3 exception.

### Release limitations

- Managed frontend Preview was attempted and returned HTTP 500: no Clerk
  publishable key is configured. The server was stopped through lifecycle
  controls; signed-in settings, jobs and application-drawer visual checks
  remain pending. No authentication bypass was introduced.
- No live employer application was submitted. Local browser fixtures are
  not proof of compatibility with every Workday tenant or job portal.
- Real LLM placeholder fidelity and semantic retrieval quality are unmeasured
  without configured providers; deterministic evals validate plumbing only.
- Complex-script PDF rendering remains limited; parse-back warnings must be
  shown rather than silently claiming all characters survived.
- Production RLS/runtime-role isolation, sandbox Docker-socket privileges,
  backups/restore, and deployment rollback require a separate operational
  rehearsal. They are not resolved by passing application unit tests.

See `docs/reports/2026-11-12-architecture-security-review.md` for the architecture
assessment, `docs/PRIVACY_PII.md` for research and residual exposure, and
`docs/TESTING.md` for the scope of each test suite.
