# Keeping personal details away from the model

CareerCraft sends job descriptions, resumes and drafts to the member's chosen
LLM provider. Contact details and government IDs are not needed for that
work, so they are hidden from the provider and put back locally.

## How it works

Every chat/completion call from an agent, the copilot, the computer agent and
the extension planner's answer drafts (`model_router.get_llm`) goes through
the internal LLM gateway (`backend/app/core/llm_gateway.py`).
`backend/tests/security/test_llm_bypass_guard.py` fails if app code builds a
provider chat client, imports a provider SDK or names a provider endpoint
anywhere else; the only direct call left is the Settings "test this key"
button, which sends a fixed prompt.
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
cached or stored. Gateway logs carry placeholder counts only. The API, the
Temporal worker and the notification worker scrub emails, phones and IDs
from every log record they create, tracebacks included
(`pii_vault.install_pii_log_scrubbing`).

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
- `PII_REDACTION_FORCED` (default `False`) – ignore a member's opt-out of
  hiding personal details; their name switch still applies.

Per member, in Settings → Models → Privacy: `UserPreferences.llm_hide_personal_info`
and `UserPreferences.llm_hide_name` (both default on; migration
`20261112090000_llm_privacy_preferences.sql`). Turning the first off also
stops name hiding.

## Form filling

Identity fields on application forms are filled from stored values without a
model: the extension's one-click autofill uses rules in
`extension/src/content/autofill.js`, and the backend planner
(`backend/app/applications/`) recognises name parts, email, phone and
country code, street address, city, state, PIN/ZIP, country, current
location, date of birth, Aadhaar/PAN/passport and other IDs, nationality,
gender and other voluntary disclosures, LinkedIn/GitHub/portfolio links,
current/expected CTC and notice period — as human labels or as ATS field
names (`legalNameSection_firstName`, `job_application[email]`,
`_systemfield_phone`, `addressSection_postalCode`). These are answered from
saved answers or the application profile, or left for the member; they are
never drafted. A label that asks for a personal detail without matching a
rule ("Do you have a valid passport?") is refused by the drafting path too.
Settings → Applying → Saved answers offers middle name, phone country code,
date of birth, street address and nationality; government ID numbers are
deliberately not stored and are left for the member to type.

When a genuine free-text question is drafted, contact lines and IDs are
removed from the resume text before the prompt is built, and the gateway
hides anything left (the name, values in unusual formats).

## Embeddings and stored chunks

`rag_service.get_embedding_model` wraps every embedding model so emails,
phone numbers, profile links and IDs are removed before text is sent to the
embedding provider (resume chunks, retrieval queries, job-match text,
company research). Resume and document chunks are also stored without them;
the resume header is built from the profile's verified contact
(`resume_facts.verified_contact`), never from chunks.

## What this does not cover

- **Names in embeddings.** Embedding text is stripped by pattern only, so
  the member's name still reaches the embedding provider. Chunks and vectors
  stored before this change keep their contact lines until the document is
  uploaded again.
- **Decision engine.** When `TYPESAFE_API_KEY` or `LAYA_URL` is set, matching
  a stored answer to a dropdown's options sends the question and the stored
  value (for example a country or gender) to that service, outside the
  gateway.
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
