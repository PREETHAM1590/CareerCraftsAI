# Keeping personal details away from the model

CareerCraft sends job descriptions, resumes and drafts to the member's chosen
LLM provider. Contact details and government IDs are not needed for that
work, so they are hidden from the provider and put back locally.

## How it works

Every chat/completion call from an agent, the copilot and the computer agent
goes through the internal LLM gateway (`backend/app/core/llm_gateway.py`).
Before a request leaves, the gateway:

1. Loads the member's privacy setting and the personal values they stored
   themselves (`backend/app/services/pii_profile.py`): login email, account
   phone/LinkedIn/full name, application-profile email, phone, PIN code,
   profile links and first/last name, the resume "Fix gaps" contact facts, and
   approved saved answers whose question is about identity (email, phone,
   address, date of birth, Aadhaar, PAN, passport, UPI, links).
2. Builds a per-request `PiiVault` (`backend/app/core/pii_vault.py`) and
   replaces those values, plus pattern-detected identifiers, with typed
   placeholders: `[[NAME_1]]`, `[[EMAIL_1]]`, `[[PHONE_1]]`, `[[URL_1]]`,
   `[[AADHAAR_1]]`, `[[PAN_1]]`, `[[PASSPORT_1]]`, `[[UPI_1]]`, `[[DOB_1]]`,
   `[[CARD_1]]`, `[[ADDRESS_1]]`, `[[POSTAL_CODE_1]]`. The same value always
   gets the same token within a request.
3. Adds a "PRIVATE VALUES" rule to the system message telling the model the
   tokens are opaque and must be copied verbatim.
4. Rehydrates the reply (text and tool-call arguments) before returning it,
   so agents, drafts and form plans receive the real values.

The mapping exists only in memory for one request. It is never logged,
cached or stored. Gateway logs carry placeholder counts only, and a log
filter scrubs emails, phones and IDs from gateway log records.

### Pattern detection

Applied to every message, including third-party text (job descriptions,
recruiter emails, scraped pages):

| Kind | Rule |
|------|------|
| Email | `local@domain.tld`; shared mailboxes (`careers@`, `noreply@`, `hr@` …) are left readable |
| Phone | Indian mobiles with `+91`/`0091`/`91`/`0` prefixes and any grouping, Indian landlines with STD code, `+`-prefixed international numbers, `(415) 555-0100` style |
| Aadhaar | 12 digits starting 2–9 **and** a valid Verhoeff check digit |
| PAN | `AAAPA9999A` with a valid holder-type letter |
| Passport | Indian passport format, only right after the word "passport" |
| UPI | `handle@okaxis`, `@ybl`, `@paytm` and other UPI handles |
| Card | 13–19 digits passing Luhn |
| Date of birth | a date right after "DOB" / "date of birth" / "born" |
| Profile URL | LinkedIn `/in/`, GitHub, GitLab, X/Twitter, LeetCode, Kaggle, Medium, Behance, Dribbble, HackerRank, Codeforces |

Not redacted on purpose: city, state and country (needed for job matching),
employers, titles, dates of employment, salaries, company websites.

### Names

The member's name is redacted by default (`[[NAME_1]]`, `[[FIRST_NAME_1]]`,
`[[LAST_NAME_1]]`) and rehydrated, so a cover letter is still signed with the
real name. Name parts shorter than three letters are not matched, and a
first name that is also a common English word (Will, May, Grace …) is hidden
only as part of the full name. A member's surname that also belongs to a
recruiter in the job post becomes the same token and is restored correctly,
but the model may not realise they are different people. Members can turn
name redaction off while keeping contact details hidden.

## Settings

Server (`backend/app/core/config.py`, all default `True`):

- `PII_REDACTION_ENABLED` – master switch.
- `PII_REDACTION_STRICT` – if the member's values cannot be loaded or
  redaction raises, the gateway returns 503 and **nothing is sent**. When
  false it logs and sends the request unredacted.
- `PII_REDACT_NAMES` – default for name redaction.
- `PII_PATTERN_DETECTION` – detect identifiers that are not in the profile.

Per member: `UserPreferences.llm_hide_personal_info` and
`UserPreferences.llm_hide_name` (both default on). They are read with
`getattr`, so redaction is on for everyone until those columns exist.

## Form filling

Identity fields on application forms are filled from stored values without a
model: the extension's one-click autofill uses rules in
`extension/src/content/autofill.js`, and the backend planner resolves
`personal.*`, `links.*` and `location.city` from saved answers and the
application profile before any model is considered
(`backend/app/applications/answer_resolver.py`). Only unrecognised free-text
questions reach the model, and the gateway hides contact details in those
prompts too.

## What this does not cover

- **Embeddings.** Resume chunks and job-match text are embedded by the
  member's provider (OpenAI or Gemini) unless an Ollama embedding model is
  configured. Those calls do not pass through the gateway; contact details
  in resume chunks reach the embedding provider today.
- **Direct provider calls outside the gateway.** `model_router.get_llm`
  calls the provider directly; free-text answers drafted for the extension
  planner currently use it.
- **Third-party names** in job descriptions and inbound email are not
  detected, and neither are spelled-out or paraphrased values ("nine eight
  seven …"), values in images or attachments, or non-ASCII text that was
  JSON-escaped before reaching the gateway.
- **Model behaviour.** If a model drops or rewrites a token beyond simple
  case/spacing/bracket changes, the reply contains the token instead of the
  value; unknown tokens are left as they are, never guessed.
- **Provider retention.** Redaction lowers what a provider sees; it is not a
  guarantee. Prefer paid API tiers (Gemini's unpaid tier may use prompts to
  improve products and allows human review), zero-data-retention where
  offered, or a local Ollama model for the most sensitive steps.
- **Tracing.** If LangSmith tracing is ever enabled, agent-side traces
  contain the unredacted prompts; set `LANGSMITH_HIDE_INPUTS=true` and
  `LANGSMITH_HIDE_OUTPUTS=true` or trace only the gateway.
