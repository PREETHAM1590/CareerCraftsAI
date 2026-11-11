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

## 5. Plan

### Phase 0 — land the prototypes (days)

Already in the working tree, tests passing, not committed:

- Extension: Workday driver for the multi-step wizard, whole-word option
  matching, custom dropdown / prompt / split-date support, resume attach on
  hidden inputs, step-change waiting with validation messages, missing host
  permissions, an opt-in auto-apply switch, and "Autofill this page".
- Backend: fixed Workday and BambooHR legacy searches, fan-out timeout, Workday
  pagination; new Workable, Oracle and iCIMS connectors; `resolve_careers_url`
  and `search_board`.

Before merging: review the diff, then test by hand on three real Workday
tenants and one each of Greenhouse, Lever and Ashby. None of this has touched a
live application form.

### Phase 1 — make auto-apply trustworthy (1–2 weeks)

1. Backend records whether a submit was approved by a person or by auto-apply,
   and enforces the setting server-side with a per-day cap.
2. Saved answers: a per-user store for sponsorship, notice period, salary,
   relocation, and voluntary disclosures. Auto-apply only uses answers the user
   wrote; anything AI-drafted forces a review.
3. Hard stops stay hard: CAPTCHA, sign-in, account creation, verification
   codes, consent checkboxes, unfilled required fields, validation errors.
4. Workday gaps: date-picker popups and clicking "Add" for extra work-history
   and education blocks.
5. Evidence: store a screenshot and the submitted answers per application.
6. Keep auto-submit off for LinkedIn (its terms explicitly forbid automation)
   and Naukri (terms not reviewed); allow it on employer-hosted portals.
7. Fix the `SUBMITTED` marker bug or delete the server-side path.

### Phase 2 — direct portal search (1–2 weeks)

1. API endpoint and UI field for "paste a company careers URL", persisting
   user-added boards.
2. Delete the legacy search paths; `job_connectors` becomes the only one.
3. Catalog health check: most configured SmartRecruiters IDs look invalid, and
   Lever `netflix` and Recruitee `mollie` return 404. Add a scheduled check
   with a dead-board cache, and pass the user's keywords to Workday's
   server-side search instead of refreshing with an empty query.
4. Add Taleo, SuccessFactors, Jobvite and JazzHR connectors.
5. Honour every location in a search, not just the first.
6. Host-aware concurrency and 429 backoff.

### Phase 3 — resume quality (1 week)

Single-column template in PDF and DOCX, format chosen by portal, parse-back
check, and a fact guard on generated claims.

### Phase 4 — hygiene (ongoing)

Remove LinkedIn credential storage and the dead adapters, expire extension
tokens and cap devices, rewrite the README to match reality, split the
oversized modules, and publish the extension to the Chrome Web Store.

## 6. Not verified

- No live application form on any portal was exercised.
- Workday findings in the review were not live-tested by the reviewer; the
  connector prototype did return jobs from live boards.
- Frontend logic, migrations, deployment configs and LLM prompts were not
  reviewed in depth.
- Naukri and Workday-hosted site terms were not read; anti-bot behaviour per
  portal is unsourced.
- Browser-backed backend tests could not run here (no Playwright Chromium).
