# JobMate Otto parity for Nova: Steel cloud browsers, portal accounts, codes over chat

Date: 2026-11-24. Status: decided 2026-11-24.
- Self-host on the existing OpenBot computers.
- CAPTCHAs: the user solves them.
- Portal accounts: opt-in per portal.
- LinkedIn: later, behind a flag.

## What we are copying (from JobMate's public site, privacy policy and app bundle)

| JobMate feature | How it works there | Do we have it? |
|---|---|---|
| Application browsers | Steel cloud Chrome sessions, driven by JobMate's worker over CDP. Logged-in sessions kept by Steel. | No. Self-hosted OpenBot containers on 24gb1, 8 slots. |
| Portal passwords | Steel Credentials vault, one per exact origin. JobMate keeps only email, origin, consent. | No. |
| Portal accounts created for you | Consent toggle "Let OTTO create job-site accounts". Signs up with a per-user `<alias>@apply.joinjobmate.com`. Workday: one account per employer, generated password in Steel's vault, EEO questions answered "I don't wish to answer". 3 tries a day per portal, a per-portal pause switch. | No. |
| Naukri / Unstop | Needs an Indian mobile. Asks "Do you already have a Naukri account?" Yes → sign in by SMS code. No → Otto fills the signup form, the portal texts the user, the user types the code into JobMate once. Codes used once, never stored. | Search only (`indian_platforms_service.py`). No apply. |
| Codes over chat | A blocked application becomes `needs_user_action` with kind `login`, `signup`, `otp` or `captcha`. The user replies with the code on Telegram/WhatsApp or in the app. The code is encrypted for one-time entry by the worker, never logged, never sent to an LLM. Email codes arrive at the application alias automatically. | No. |
| CAPTCHA | Stops and opens a "security check" for the user in a live browser. | Stops and fails (`browser_control_service.py`). |
| Applies on | LinkedIn Easy Apply, Naukri, Internshala, Greenhouse. 5 a day. Counts only confirmed submissions. | Greenhouse, Lever, Ashby adapters, run in the extension with review. |
| LinkedIn outreach | Linkup (third-party LinkedIn API). Login by email/password, or a Chrome extension that reads the `li_at` cookie and posts it to the server once. | No. |
| Gmail | Send-only OAuth scope, encrypted refresh token. | Gmail via Nango, but with read scopes (status tracking reads the inbox). |

## 0. Decisions needed before building

1. **Steel account.** Start on Launch ($0, $0.10 per browser-hour, 10 concurrent, 15-minute sessions). Scale is $250 a month for 100 concurrent and 1-hour sessions. Sessions run in the US only, which goes in our privacy policy.
2. **CAPTCHA policy.** The repo's current rule is "stop on a challenge, never evade it". Steel can solve reCAPTCHA, hCaptcha and Turnstile (`solveCaptcha`). Plan default: keep the rule. The user solves it in the embedded live browser, as JobMate does.
3. **Creating accounts for users.** Nova accepts each portal's terms for the user. Plan default: off by default, an explicit per-portal consent toggle, consent text stored with a timestamp.
4. **LinkedIn automation.** Linkup and the `li_at` approach break LinkedIn's User Agreement, and accounts can get restricted. Plan default: build last, behind a flag, with clear warnings.
5. **Application email domain.** Needs an MX record on a subdomain such as `apply.careercraftsai.me`, pointing at Resend inbound (we already have `RESEND_API_KEY`).
6. **Chat channel for codes.** Telegram is free and quick to set up. WhatsApp needs Meta business verification and a BSP such as Gupshup. Plan default: in-app first, then Telegram.

## Decision 2026-11-24: self-host, don't pay for Steel cloud

- **Self-hosted Steel Browser** (Apache 2.0, `steel-dev/steel-browser`) is one Chrome per container with no auth.
  - It has none of the Credentials vault, Profiles, CAPTCHA, live-view or multi-session features. Those are cloud-only.
  - So it adds nothing over our OpenBot computers on 24gb1, which already have per-user profiles, gVisor, the Squid egress allow-list, a slot-limited relay, human control and the snapshot/ref model.
- **Plan:** keep OpenBot as the browser layer. Build phases 2 to 5 on it, with our own encrypted credential vault in place of Steel's.
- Phase 1 below is kept only in case we move to Steel cloud later.
- **Capacity stays at 8 concurrent browsers per 24 GB host.** Add hosts with `deploy/computers` when the queue grows.
- **CAPTCHA:** the user solves it in the live browser (human control already exists).
  - Paid solver services (2Captcha, CapSolver and similar) are not planned: they break portal terms, risk getting user accounts banned, and go against this repo's "never evade a challenge" rule.

## Phase 1 (deferred): Steel as a third computer backend

Goal: the same `computer_service.dispatch()` contract, with `backend == "steel"` beside `cloud` (OpenBot relay) and `browser` (extension).

- `backend/app/services/steel_service.py`:
  - one Steel session per user, created on demand
  - `persistProfile` with one profile id per user, so logins survive
  - `inactivityTimeout`; release on idle, stop and purge
- Action layer over Playwright `connect_over_cdp(session.websocket_url)`:
  - `navigate`, `snapshot`, `click`, `type`, `scroll`, `upload` and `screenshot`
  - Snapshots keep today's ref + `snapshotId` model: injected JS tags interactive elements with `data-cc-ref`, and the snapshot id is bumped on every navigation.
  - Uploads go through `sessions.files.upload`, then `setInputFiles` on the `/files` path.
- Keep every check `act()` does today: reviewed snapshot for writes, audit rows, URL validation. Add a block on private and metadata IPs on navigate, since Squid no longer sits in front.
- Human control: embed Steel's `debugUrl` (interactive) in the Computer pane instead of 4-second screenshot polling.
- Concurrency: a Redis-backed fleet counter set to the plan limit, with a visible queue on `429 concurrency_limit_reached`.
- Config: `COMPUTER_PROVIDER=openbot|steel`, `STEEL_API_KEY`. OpenBot stays as a fallback until Steel has run in production for 2 weeks.
- Tests: a fake Steel client and a local Chromium behind the same CDP interface. Reuse `deploy/computers/fixtures` for upload, keyboard and privacy checks.

## Phase 2: portal credentials vault (self-hosted)

- Table `portal_credentials(user_id, origin, account_email, ciphertext, key_version, consent_at, consent_text_version, state, created_by_otto)`.
- Encryption: AES-256-GCM with a per-user key derived from `PORTAL_VAULT_KEY`, which lives only in the secrets env.
  - The user id and origin are bound in as associated data, so a row can't be decrypted for another user or another site.
- Only the relay's `credentials/fill` path decrypts, and only when the computer's current origin matches the stored origin exactly (reusing `credential_origin()`).
  - The password is typed into the page by the computer. It never reaches the model, the logs or the API responses.
- API: connect, status, revoke. Revoke wipes the ciphertext and keeps the row as a tombstone.
- Account deletion: `purge_user` also wipes the credentials.

## Phase 3: blocked steps and codes

- `application_actions.state = needs_user_action`, `kind in {login, signup, otp, captcha, assessment, missing_facts, legal}`.
- `POST /applications/{id}/code`:
  - The code is encrypted (Fernet with a key from env) into Redis with a 10-minute TTL and a single read.
  - The worker types it into the waiting field, and it is then deleted.
  - It never appears in logs, the DB or LLM prompts, enforced by a logging filter and a unit test.
- The UI shows an "Enter code" or "Open security check" card, the latter with the live browser.
- Then a Telegram bot: `/start <one-time link token>` links the chat. When Nova asks for a code it messages the user, and the next reply that looks like a code within the TTL goes down the same path.

## Phase 4: application email alias

- One `<handle>@apply.careercraftsai.me` alias per user, in table `apply_email_aliases`.
- A Resend inbound webhook stores messages per user. A parser pulls verification codes and links, and fills a waiting email-OTP step on its own.
- Employer confirmations mark the application as confirmed.

## Phase 5: portal accounts created by Nova

- Per-portal consent toggles with stored consent text. Default off.
- Generated 20-character passwords go straight to Steel. We never hold them.
- Signup flows, each with a confirmation check:
  - **Workday**: per employer tenant; email alias plus automatic email verification.
  - **Naukri**: mobile and SMS code; asks "existing account?" first.
  - **Internshala**: email alias plus verification.
- Limits: 3 tries a day per portal per user, plus a global per-portal kill switch for when a portal starts blocking.
- EEO and voluntary questions: "Decline to answer". Never invent facts. Missing facts become a `missing_facts` step.

## Phase 6: apply adapters on Steel

- Move the existing Greenhouse, Lever and Ashby adapters to run against a Steel page, as well as the extension.
- New adapters: LinkedIn Easy Apply, Naukri, Internshala.
- 5 a day by default.
- Count an application only on a confirmation page or confirmation email. Never resubmit when the result is uncertain.

## Phase 7: LinkedIn outreach (behind a flag)

- Extension v0.5:
  - add the `cookies` permission for `https://www.linkedin.com/*`
  - read `li_at` only when the user clicks Connect in CareerCraft
  - POST it once to the backend over the paired-device channel
- The backend hands the cookie to Linkup or Unipile and keeps only the provider's account id.
- Daily limits on connection requests and messages. Review mode by default.

## Phase 8: Gmail send-only for outreach

- Add a second Nango Gmail integration with only `gmail.send` for outreach, so users who don't want inbox tracking can connect that alone.
- Keep the read-scope connection optional, for status tracking.

## Order and size

Phases 1 → 2 → 3 unlock everything else. About 2 to 3 weeks of focused work for 1 to 3, then roughly a week per phase after that. Phase 1 can't be tested end to end without a Steel API key. Unit tests with the fake client work without one.

## Progress

- 2026-11-24: our agent is named **Nova** (renamed from Copilot).
- 2026-11-24: phases 2 and 3 built on the OpenBot computers:
  - Nova `sign_in` step: the saved login is resolved by the exact page origin and the AI never sees it.
  - Opt-in automatic sign-in per login, 3 a day per site, consent time and version stored.
  - Nova `enter_code` step: the code goes through a private route, is sealed in Redis for 10 minutes and read once.
  - Approval-dialog code box and auto sign-in toggle.
  - Still to do: Telegram code replies (phase 3b).
- 2026-11-24: phase 4 built. Per-member application address, signed Resend inbound webhook, inbox with codes and links, emailed codes handed to the waiting Nova step for the same site, and Nova context gets the address and fresh links.
  - Setup steps are in docs/CONFIGURATION.md ("Application email").
- 2026-11-24: phase 3b built: Nova asks for codes on Telegram; replies go through the sealed, read-once code path.
