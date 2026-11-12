# Security

This document covers CareerCraft AI's security architecture, threat model, known CVEs, and audit process.

---

## Threat Model

| Asset | Threat | Control |
|---|---|---|
| User API keys (Anthropic, OpenAI, etc.) | Read from DB | AES-256-GCM encryption at rest |
| User resume data | Unauthorized access | RLS: `user_id = auth.uid()` enforced in DB |
| Job application actions | CSRF / replay | JWT on every request; HITL gate for irreversible actions |
| Email sending (Gmail) | Sending without consent | Server-side HITL gate; `/approve` endpoint required |
| Job applications in a browser | Cross-user contamination, job-site credentials on the server | Applications run only in the member's own browser via the extension; the server runs no browser |
| Admin endpoints | Public access | Nginx blocks `/internal/*` at edge |
| Rate limiting | Credential stuffing / scraping | slowapi (60 req/min/user), Nginx (10 req/min for auth) |

---

## Authentication

### Supabase JWT Verification

All protected backend routes verify the Supabase access token locally in `supabase_auth.py`:

```python
# Verification: no round-trip to Supabase on each request
payload = jwt.decode(
    token,
    SUPABASE_JWT_SECRET,
    algorithms=["HS256"],
    audience="authenticated"
)
user_id = payload["sub"]
```

The `SUPABASE_JWT_SECRET` comes from Supabase dashboard → Settings → API. It must be kept secret — exposure allows forging tokens.

### Frontend Session Management

- `@supabase/ssr` manages cookie-based sessions
- `middleware.ts` calls `supabase.auth.getUser()` on every request and refreshes expired tokens
- Unauthenticated requests to `(app)/*` routes are redirected to `/login`
- Access tokens are short-lived (1 hour); refresh tokens are rotated on use

### Nango provider authorization

Gmail and Drive authorization uses Nango Connect. Nango stores and refreshes
provider credentials; CareerCraft verifies Nango webhooks and never stores or
logs provider OAuth tokens.

---

## API Key Encryption

User AI provider API keys (Anthropic, OpenAI, etc.) are encrypted with AES-256-GCM before database storage.

**Key derivation** (`security.py`):
```python
# Each key gets a unique salt
salt = os.urandom(16)
# PBKDF2 derives an encryption key from APP_SECRET_KEY + salt
encryption_key = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=32,
    salt=salt,
    iterations=100_000,
).derive(APP_SECRET_KEY.encode())
# AES-256-GCM encryption (includes integrity check)
ciphertext, nonce, tag = aes_256_gcm_encrypt(key, plaintext)
```

Stored columns: `api_key_enc` (ciphertext + nonce + tag), `api_key_salt` (per-key salt).

**Decryption happens only once per request**, in `llm_gateway.py`, immediately before the LLM call. The plaintext key is never stored in Redis, logs, or DB.

---

## Row-Level Security

Every user data table has RLS enabled. Policies enforce that users can only SELECT/INSERT/UPDATE/DELETE their own rows:

```sql
-- Example: applications table
ALTER TABLE public.applications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users see own applications"
  ON public.applications
  FOR ALL
  USING (
    user_id = (SELECT id FROM public.users WHERE clerk_user_id = auth.uid())
  );
```

This means even a bug in application code that constructs a wrong query cannot return another user's data — the database enforces isolation.

**Tables with RLS:** `users`, `user_model_settings`, `user_preferences`, `documents`, `resume_personas`, `applications`, `agent_runs`, `cover_letter_versions`, `interview_sessions`, `salary_reports`, `leads`, `linkedin_outreach_queue`, `ats_scores`

**Tables without RLS:** `company_intel` (shared public data, no PII)

---

## Rate Limiting

Two layers:

**Nginx (per IP):**
```nginx
limit_req_zone $binary_remote_addr zone=auth:10m rate=10r/m;
# Applied to: /auth/*, /api/v1/users/model-settings
```

**slowapi (per authenticated user):**
```python
@limiter.limit("60/minute")  # default
@limiter.limit("10/minute")  # agent runs
@limiter.limit("5/minute")   # document uploads
```

Rate limit headers on every response: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.

---

## Human-in-the-Loop Gate

No agent can send an email or submit a job application without user approval. The one exception is auto-submit, which a member enables in settings: the server then approves a fully answered form itself, within a daily cap, and records it as auto-approved. It never applies on LinkedIn or Naukri and refuses forms with AI-drafted answers. Email approval is enforced server-side:

1. Agent emits `pending_action` in state and pauses
2. Backend stores the pending action in Redis with the `run_id` key
3. SSE `checkpoint` event reaches the frontend
4. Frontend renders `ApprovalModal` with full action preview
5. **Only** `POST /api/v1/agents/{run_id}/approve {approved: true}` resumes the action
6. Any other code path (background jobs, direct service calls) cannot bypass this

The `/approve` endpoint verifies:
- JWT belongs to the user who owns the run
- Run is in `pending_approval` state
- Action type matches what was stored server-side (prevents parameter tampering)

---

## Background jobs have no HTTP trigger

Job search, follow-up drafts, the daily search and the application status
check (`app/services/scheduled_jobs.py`) run only inside Temporal activities
on the `temporal-worker` (see `docs/ARCHITECTURE.md` §8). No route executes
them out of band, and `tests/security/test_api_security.py::
test_no_out_of_band_job_trigger_routes` keeps it that way. Operators trigger
them through the Temporal CLI/UI, which must itself stay off the public
internet (see `docs/DEPLOYMENT.md`). Nginx still answers `/internal/*` with
404 as defense in depth:

```nginx
location /internal { return 404; }
```

### Browser extension device tokens

The Chrome extension (`extension/`) authenticates to `/api/v1/extension/device/*` with a device token instead of the user's Clerk session: a `ccx_`-prefixed random token, generated once when a browser is paired (`POST /extension/pair`) and shown to the user exactly once. Only its SHA-256 hash (`extension_service.hash_token`) is ever stored, as `extension_devices.token_hash`; the plaintext token cannot be recovered from the database. Each paired browser is listed under **Settings → Integrations → Browser extension** and can be revoked independently (`DELETE /extension/devices/{id}`), which is checked on every subsequent device-token request. Tokens expire after 90 days and are rotated through `POST /extension/device/token/rotate` during their last 30 days; each member may have at most five active devices. Requests authenticated with a device token are rate-limited per device, not per user (`core/rate_limit.py` keys on a hash of the `Bearer ccx_…` value), so one compromised or misbehaving browser cannot exhaust another device's or another user's limit.

---

## No Server-Side Browser Automation

The backend does not drive job sites. Server-side browser execution is retired (`run_browser_task` raises `BrowserAutomationRetired`), so no job-site session or password is held on the server. Applications run in the member's own signed-in browser through the extension, which does not solve CAPTCHAs and stops for sign-in, CAPTCHAs, and unanswered questions. LinkedIn passwords are no longer accepted; `DELETE /users/me/linkedin-credentials` erases any stored by older versions.

---

## Untrusted Content

Job postings, emails, and retrieved documents are data, not instructions:
- Agent prompts wrap this text with the helpers in `backend/app/agents/prompts/__init__.py` (`untrusted()` and `fenced()`), and the shared security rules tell the model to ignore instructions inside fenced data.
- Retrieved document chunks are wrapped in `<retrieved_context>` by `rag_service.format_context()`.
- Inbound email changes an application's status only when the sender's domain is the company's own or a known job platform (`application_status_service.sender_is_trusted`). Other mail is recorded and the member is asked to check it; nothing moves.

---

## Input Validation

All API inputs are validated by Pydantic models before reaching business logic. SQLAlchemy uses parameterized queries — no raw SQL with user input.

Security tests verify:
- SQL injection attempts return 422 (Pydantic rejects them before DB)
- XSS payloads in text fields are stored as plain text (no HTML rendering server-side)
- Oversized uploads are rejected by file size limits in `rag.py`

---

## Known CVEs and Patches

| CVE | Component | Status |
|---|---|---|
| CVE-2025-68664 | LangChain serialization | Fixed — `langchain-core>=1.4.0` in `constraints.txt` |
| CVE-2025-67644 | LangGraph SQLite injection | Blocked — `langchain-core<1.3.0` excluded in `constraints.txt` |
| langchain-community removal | Vector store dependency | Replaced with `langchain-postgres==0.0.17` |

`constraints.txt` pins upper bounds for all packages with active CVEs. Always run `pip install -r requirements.txt -c constraints.txt` to respect these bounds.

---

## Security Testing

### Automated (CI)

```bash
# SAST — static analysis, blocks on HIGH severity
cd backend && bandit -r app/ -f txt

# Dependency CVE scan
pip-audit
npm audit
```

### Test suite

```bash
# 6 security-specific tests
cd backend && pytest tests/security -v
```

Security tests cover:
- `test_sql_injection.py` — SQL injection attempts rejected
- `test_xss.py` — XSS payloads stored safely
- `test_auth_required.py` — all protected endpoints return 401 without JWT
- `test_rls.py` — users cannot access other users' data
- `test_api_key_encryption.py` — stored keys are ciphertext, not plaintext
- `test_rate_limiting.py` — 429 returned after limit exceeded

---

## Security Headers (Nginx)

```nginx
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
add_header X-Frame-Options DENY always;
add_header X-Content-Type-Options nosniff always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Content-Security-Policy "
  default-src 'self';
  script-src 'self' 'unsafe-inline';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data: https:;
  connect-src 'self' https://*.supabase.co wss://*.supabase.co;
" always;
```

TLS configuration:
```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:...;
ssl_prefer_server_ciphers off;
```

---

## Reporting a Security Issue

Do not open a public GitHub issue for security vulnerabilities.

Email: security@[yourdomain.com]

Include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Suggested fix (if any)

We aim to respond within 48 hours and release a patch within 7 days for critical issues.

---

## Security Checklist (Production)

- [ ] `APP_SECRET_KEY` is 32+ random bytes (`openssl rand -hex 32`)
- [ ] `SUPABASE_JWT_SECRET` matches Supabase dashboard value
- [ ] `APP_ENV=production` (disables `/docs`, debug logging)
- [ ] Supabase Auth redirect URLs locked to production domain only
- [ ] RLS enabled on all user-data tables (verify in Supabase dashboard)
- [ ] All CVE-pinned packages installed via `constraints.txt`
- [ ] Bandit SAST shows no HIGH findings (`bandit -r app/ -l HIGH`)
- [ ] `npm audit --audit-level=high` shows no high/critical issues
- [ ] Internal routes blocked at Nginx (test: `curl https://yourdomain.com/internal/` → 404)
- [ ] SSL certificate valid and auto-renewing (`certbot renew --dry-run`)
- [ ] Redis not exposed publicly (port 6379 not in VPS firewall rules)
