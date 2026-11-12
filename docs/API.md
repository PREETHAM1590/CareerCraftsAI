# API Reference

Base URL: `http://localhost:8000/api/v1` (dev) or `https://yourdomain.com/api/v1` (prod)

All endpoints require `Authorization: Bearer <supabase_access_token>` unless marked **Public**.

Interactive docs at `/docs` (dev mode only — disabled in production).

---

## Authentication

### Headers

```
Authorization: Bearer <token>
Content-Type: application/json
```

Token is the Supabase access token from `supabase.auth.getSession()` on the frontend, or the JWT from `supabase.auth.signIn()` responses.

### Errors

All endpoints use standard HTTP status codes with this error body:

```json
{
  "detail": "Human-readable error message"
}
```

| Code | Meaning |
|---|---|
| 400 | Bad request / validation error |
| 401 | Missing or invalid JWT |
| 403 | Authenticated but not authorized (wrong user) |
| 404 | Resource not found |
| 422 | Validation error (Pydantic) |
| 429 | Rate limit exceeded (60 req/min) |
| 500 | Internal server error |

---

## Health

### `GET /health` — **Public**

```json
{"status": "ok", "version": "1.0.0"}
```

---

## Agents

### `POST /agents/run`

Start an agent run. Returns immediately with `run_id`. Stream progress via SSE.

**Request:**
```json
{
  "task": "optimize resume",
  "params": {
    "job_description": "We are looking for a Senior Python Engineer...",
    "persona_id": "uuid-optional"
  }
}
```

**Response `202`:**
```json
{
  "run_id": "uuid",
  "status": "queued",
  "stream_url": "/api/v1/agents/uuid/stream"
}
```

**Task values:** `search_jobs`, `optimize_resume`, `cover_letter`, `optimize_linkedin`, `email_recruiter`, `follow_up`, `mock_interview`, `interview_prep`, `company_research`, `salary_benchmark`, `auto_apply`

---

### `GET /agents/{run_id}/stream`

Server-Sent Events stream for a running agent. Keep connection open until `complete` or `error` event.

**Response:** `text/event-stream`

```
event: thinking
data: {"step": 1, "message": "Retrieving resume context from RAG..."}

event: tool_call
data: {"tool": "rag_retrieve", "input": {"query": "python engineer experience"}}

event: tool_result
data: {"tool": "rag_retrieve", "output": {"chunks": 4, "relevance": 0.87}}

event: checkpoint
data: {"action_type": "send_email", "to": "recruiter@company.com", "subject": "...", "body": "..."}

event: complete
data: {"result": {...}, "tokens_used": 1847, "duration_ms": 8420}

event: error
data: {"message": "OpenAI API rate limit exceeded"}
```

---

### `POST /agents/{run_id}/approve`

Resume agent after HITL checkpoint. Must be called within 10 minutes of `checkpoint` event.

**Request:**
```json
{
  "approved": true,
  "edits": {
    "body": "Optional: user-edited version of the pending action"
  }
}
```

**Response `200`:**
```json
{"status": "resumed"}
```

**Response when cancelled:**
```json
{"status": "cancelled"}
```

---

### `GET /agents/runs`

List user's agent run history.

**Query params:** `limit` (default 20), `offset`, `status` (queued|running|complete|error|cancelled)

**Response `200`:**
```json
{
  "runs": [
    {
      "run_id": "uuid",
      "task": "optimize_resume",
      "status": "complete",
      "tokens_used": 1847,
      "duration_ms": 8420,
      "created_at": "2026-06-05T10:00:00Z"
    }
  ],
  "total": 42
}
```

---

### `GET /agents/runs/{run_id}`

Get details of a single agent run including full input/output.

**Response `200`:**
```json
{
  "run_id": "uuid",
  "task": "optimize_resume",
  "status": "complete",
  "input": {...},
  "output": {...},
  "tokens_used": 1847,
  "duration_ms": 8420,
  "created_at": "2026-06-05T10:00:00Z"
}
```

---

## Resume

### `POST /resume/optimize`

Tailor resume to a job description using RAG + LLM. Generates PDF.

**Request:**
```json
{
  "job_description": "We are looking for a Senior Python Engineer...",
  "persona_id": "uuid-optional",
  "tone": "professional"
}
```

**`tone` values:** `professional`, `concise`, `detailed`

**Response `200`:**
```json
{
  "document_id": "uuid",
  "resume_text": "...",
  "ats_score": 84,
  "ats_suggestions": [
    "Add 'Kubernetes' to skills section",
    "Quantify impact in 3rd bullet under Acme Corp"
  ],
  "download_url": "/api/v1/resume/download/uuid"
}
```

---

### `GET /resume/download/{document_id}`

Download the generated resume PDF.

**Response `200`:** `application/pdf` binary

---

### `GET /resume/personas`

List all resume personas for the user.

**Response `200`:**
```json
{
  "personas": [
    {
      "id": "uuid",
      "name": "Backend Engineer",
      "description": "Focus on Python, distributed systems",
      "created_at": "2026-06-01T00:00:00Z"
    }
  ]
}
```

---

### `POST /resume/personas`

Create a new resume persona.

**Request:**
```json
{
  "name": "Tech Lead",
  "description": "Leadership focus, system design emphasis"
}
```

**Response `201`:**
```json
{"id": "uuid", "name": "Tech Lead"}
```

---

## RAG / Documents

### `POST /rag/upload`

Upload a document (PDF, DOCX, or TXT) to seed the RAG pipeline.

**Request:** `multipart/form-data`

| Field | Type | Description |
|---|---|---|
| `file` | file | PDF, DOCX, or TXT |
| `doc_type` | string | `resume`, `achievements`, `certifications`, `portfolio`, `notes` |
| `persona_id` | string | Optional — associate with a persona |

**Response `201`:**
```json
{
  "document_id": "uuid",
  "chunks_ingested": 14,
  "doc_type": "resume",
  "filename": "my_resume.pdf"
}
```

---

### `POST /rag/search`

Semantic search across user's documents.

**Request:**
```json
{
  "query": "leadership experience managing teams",
  "doc_types": ["resume", "achievements"],
  "top_k": 5
}
```

**Response `200`:**
```json
{
  "results": [
    {
      "chunk": "Led a team of 8 engineers across 3 time zones...",
      "score": 0.91,
      "doc_type": "resume",
      "document_id": "uuid"
    }
  ]
}
```

---

### `GET /rag/documents`

List all uploaded documents.

**Response `200`:**
```json
{
  "documents": [
    {
      "id": "uuid",
      "filename": "my_resume.pdf",
      "doc_type": "resume",
      "chunks": 14,
      "uploaded_at": "2026-06-01T00:00:00Z"
    }
  ]
}
```

---

### `DELETE /rag/documents/{document_id}`

Delete a document and its vector embeddings.

**Response `204`:** No content

---

## Jobs

### `GET /jobs/search`

Search for jobs across platforms.

**Query params:**

| Param | Type | Description |
|---|---|---|
| `query` | string | Job title or keywords |
| `location` | string | City, country, or "remote" |
| `salary_min` | int | Minimum salary (USD) |
| `experience_years` | int | Years of experience |
| `platforms` | string[] | linkedin, indeed, naukri, glassdoor |
| `limit` | int | Max results (default 20) |

**Response `200`:**
```json
{
  "jobs": [
    {
      "id": "uuid",
      "title": "Senior Python Engineer",
      "company": "Acme Corp",
      "location": "Remote",
      "salary": "$130k–$160k",
      "match_score": 87,
      "url": "https://...",
      "platform": "linkedin",
      "posted_at": "2026-06-03T00:00:00Z"
    }
  ],
  "total": 142
}
```

---

### `GET /jobs/applications`

List the user's job application pipeline.

**Query params:** `status` (saved|applied|interviewing|offer|rejected|withdrawn)

**Response `200`:**
```json
{
  "applications": [
    {
      "id": "uuid",
      "job_title": "Senior Python Engineer",
      "company": "Acme Corp",
      "status": "interviewing",
      "applied_at": "2026-06-01T00:00:00Z",
      "next_followup_at": "2026-06-06T09:00:00Z",
      "notes": "..."
    }
  ]
}
```

---

### `POST /jobs/applications`

Manually add a job application.

**Request:**
```json
{
  "job_title": "Senior Python Engineer",
  "company": "Acme Corp",
  "job_url": "https://...",
  "status": "applied",
  "notes": "Applied via LinkedIn Easy Apply"
}
```

**Response `201`:**
```json
{"id": "uuid", "status": "applied"}
```

---

### `PATCH /jobs/applications/{id}`

Update application status or notes.

**Request:**
```json
{
  "status": "interviewing",
  "notes": "Phone screen scheduled for June 10"
}
```

**Response `200`:**
```json
{"id": "uuid", "status": "interviewing"}
```

---

### `POST /jobs/portal-search`

Read one company's careers board live and save the matches to the member's job list. Rate limit: 10/minute.

**Request:**
```json
{
  "url": "https://acme.wd5.myworkdayjobs.com/en-US/External",
  "query": "data engineer",
  "locations": ["Remote", "Berlin"],
  "max_results": 25,
  "save_board": false
}
```

`url` is required (3–2048 characters). `query` defaults to `""`, `locations` takes up to 5 entries, `max_results` is 1–50 (default 25), and `save_board` defaults to `false`.

**Response `200`:**
```json
{
  "board": {"source_id": "workday:acme-external", "family": "workday", "label": "Acme", "saved_board_id": null},
  "total_found": 42,
  "saved_count": 7,
  "jobs": []
}
```

`jobs` holds application records as returned by `GET /jobs/applications`. `saved_board_id` is set when `save_board` is `true`.

**Errors:** `422` when the link is not a supported careers page (the body lists the supported portals); `404` when the portal has no board at that address; `429` when the portal is rate limiting; `504` on timeout; `502` when the portal cannot be read.

---

### `GET /jobs/boards`

List the member's saved boards.

**Response `200`:**
```json
[
  {
    "id": "uuid",
    "source_id": "greenhouse:acme",
    "family": "greenhouse",
    "label": "Acme",
    "careers_url": "https://boards.greenhouse.io/acme",
    "created_at": "2026-11-01T00:00:00Z"
  }
]
```

---

### `POST /jobs/boards`

Save a careers link so later searches include it. Nothing is fetched. Rate limit: 30/minute.

**Request:**
```json
{"url": "https://boards.greenhouse.io/acme", "label": "Acme"}
```

`label` is optional (up to 150 characters) and defaults to a label derived from the board.

**Response `201`:** a board object as in `GET /jobs/boards`.

**Errors:** `422` for an unsupported link; `409` when 20 boards are already saved.

---

### `DELETE /jobs/boards/{board_id}`

Remove a saved board.

**Response `204`:** no body. `404` if the board does not belong to the member.

---

## Email

### `GET /email/threads`

Fetch Gmail threads relevant to job search (recruiter emails, application confirmations).

**Response `200`:**
```json
{
  "threads": [
    {
      "thread_id": "...",
      "subject": "Re: Application - Senior Engineer at Acme",
      "from": "recruiter@acme.com",
      "snippet": "Thank you for your application...",
      "needs_action": true,
      "received_at": "2026-06-04T14:00:00Z"
    }
  ]
}
```

---

### `POST /email/compose`

Draft an outreach or follow-up email. Does NOT send.

**Request:**
```json
{
  "recruiter_name": "Jane Smith",
  "recruiter_company": "Acme Corp",
  "recruiter_email": "jane@acme.com",
  "job_title": "Senior Engineer",
  "email_type": "outreach",
  "tone": "professional"
}
```

**`email_type` values:** `outreach`, `followup_5day`, `followup_12day`, `reply`

**Response `200`:**
```json
{
  "email_id": "uuid",
  "to": "jane@acme.com",
  "subject": "Interested in Senior Engineer role at Acme Corp",
  "body": "...",
  "status": "pending_approval"
}
```

---

### `POST /email/approve/{email_id}`

Send the email via Gmail after user approval.

**Request:**
```json
{
  "approved": true,
  "edits": {
    "subject": "Optional edited subject",
    "body": "Optional edited body"
  }
}
```

**Response `200`:**
```json
{"status": "sent", "gmail_message_id": "..."}
```

---

## Cover Letter

### `POST /cover-letter/generate`

Generate a cover letter for a job application.

**Request:**
```json
{
  "job_description": "...",
  "company_name": "Acme Corp",
  "hiring_manager": "Jane Smith",
  "tone": "professional",
  "word_limit": 350,
  "use_thinking": true
}
```

**Response `200`:**
```json
{
  "version_id": "uuid",
  "cover_letter": "...",
  "variants": [
    {"tone": "professional", "text": "..."},
    {"tone": "concise", "text": "..."}
  ],
  "word_count": 312
}
```

---

## Interview

### `POST /interview/session`

Start a new mock interview session.

**Request:**
```json
{
  "job_title": "Senior Software Engineer",
  "company": "Google",
  "interview_type": "behavioral",
  "num_questions": 5
}
```

**`interview_type` values:** `technical`, `behavioral`, `system_design`, `mixed`

**Response `201`:**
```json
{
  "session_id": "uuid",
  "first_question": "Tell me about a time you had to make a difficult technical decision under time pressure."
}
```

---

### `POST /interview/answer`

Submit an answer and get AI feedback.

**Request:**
```json
{
  "session_id": "uuid",
  "answer": "In my previous role at Acme Corp, I..."
}
```

**Response `200`:**
```json
{
  "scores": {
    "clarity": 8,
    "relevance": 9,
    "depth": 7
  },
  "feedback": "Strong use of STAR format. Consider quantifying the impact — e.g., how much did the new architecture reduce latency?",
  "next_question": "Describe a time you had to influence a team without direct authority.",
  "session_complete": false
}
```

---

### `GET /interview/sessions`

List past interview sessions with aggregate scores.

**Response `200`:**
```json
{
  "sessions": [
    {
      "session_id": "uuid",
      "job_title": "Senior Software Engineer",
      "company": "Google",
      "avg_score": 7.8,
      "completed_at": "2026-06-04T15:00:00Z"
    }
  ]
}
```

---

## Company Research

### `GET /company/{company_name}/research`

Get a company intelligence briefing. Returns cached result if fresher than 7 days.

**Query params:** `sections` (comma-separated: culture, interview_process, financials, recent_news, key_people)

**Response `200`:**
```json
{
  "company": "Stripe",
  "culture": "...",
  "glassdoor_rating": 4.2,
  "glassdoor_reviews_count": 3200,
  "interview_process": "...",
  "recent_news": [
    {"title": "Stripe raises $1B...", "url": "...", "date": "2026-05-01"}
  ],
  "funding": "$9.4B total raised",
  "headcount": "~8,000",
  "key_people": [
    {"name": "Patrick Collison", "role": "CEO", "linkedin": "..."}
  ],
  "intel_id": "uuid",
  "cached_at": "2026-06-01T00:00:00Z"
}
```

---

## Salary

### `POST /salary/benchmark`

Benchmark compensation for a role and location.

**Request:**
```json
{
  "role": "Senior Software Engineer",
  "location": "San Francisco, CA",
  "experience_years": 7,
  "skills": ["Python", "Kubernetes", "PostgreSQL"],
  "current_salary": 150000,
  "offer_amount": 180000
}
```

**Response `200`:**
```json
{
  "report_id": "uuid",
  "percentiles": {
    "p25": 165000,
    "p50": 190000,
    "p75": 220000,
    "p90": 260000
  },
  "total_comp": {
    "base": 190000,
    "equity_annual": 50000,
    "bonus": 20000,
    "total": 260000
  },
  "your_offer_percentile": 55,
  "negotiation_script": "Based on market data showing P75 at $220k, I'd like to discuss...",
  "key_talking_points": ["Your P75 is $30k above the offer", "Remote-adjusted market..."]
}
```

---

## LinkedIn

### `POST /linkedin/optimize`

Generate optimized LinkedIn profile sections.

**Request:**
```json
{
  "target_role": "Staff Software Engineer",
  "industry": "FinTech",
  "linkedin_url": "https://linkedin.com/in/..."
}
```

**Response `200`:**
```json
{
  "headline": "Staff Software Engineer | Python · Distributed Systems · FinTech",
  "about": "...",
  "experience_bullets": {
    "Acme Corp - Senior Engineer": ["...", "...", "..."]
  },
  "optimization_score": 78,
  "keyword_gaps": ["Kafka", "microservices", "SRE"],
  "suggestions": ["Add 'Open to Work' for 3x more recruiter views"]
}
```

---

### `POST /linkedin/outreach`

Queue a LinkedIn outreach message for approval.

**Request:**
```json
{
  "recruiter_linkedin_url": "https://linkedin.com/in/recruiter",
  "message_type": "connection_request",
  "job_title": "Senior Engineer",
  "company": "Acme Corp"
}
```

**Response `201`:**
```json
{
  "outreach_id": "uuid",
  "message": "Hi Jane, I came across the Senior Engineer role at Acme...",
  "status": "pending_approval"
}
```

---

## Leads

### `GET /leads`

List recruiter leads.

**Query params:** `status` (new|contacted|responded|converted|closed), `company`, `limit`, `offset`

**Response `200`:**
```json
{
  "leads": [
    {
      "id": "uuid",
      "name": "Jane Smith",
      "title": "Technical Recruiter",
      "company": "Acme Corp",
      "email": "jane@acme.com",
      "linkedin_url": "https://...",
      "status": "contacted",
      "source": "hunter_io",
      "created_at": "2026-06-01T00:00:00Z"
    }
  ],
  "total": 23
}
```

---

### `POST /leads`

Manually add a recruiter lead.

**Request:**
```json
{
  "name": "Jane Smith",
  "title": "Technical Recruiter",
  "company": "Acme Corp",
  "email": "jane@acme.com",
  "linkedin_url": "https://..."
}
```

**Response `201`:**
```json
{"id": "uuid"}
```

---

## Users

### `GET /users/me`

Get current user profile.

**Response `200`:**
```json
{
  "id": "uuid",
  "clerk_user_id": "...",
  "email": "user@example.com",
  "full_name": "Alice Smith",
  "avatar_url": "https://...",
  "created_at": "2026-01-01T00:00:00Z"
}
```

---

### `PUT /users/profile`

Update user profile.

**Request:**
```json
{
  "full_name": "Alice Smith",
  "headline": "Senior Python Engineer",
  "location": "San Francisco, CA",
  "target_role": "Staff Engineer",
  "target_salary": 220000
}
```

**Response `200`:**
```json
{"updated": true}
```

---

### `GET /users/model-settings`

Get current AI model configuration (keys are masked).

**Response `200`:**
```json
{
  "active_provider": "anthropic",
  "active_model": "claude-sonnet-4-6",
  "providers": [
    {
      "provider": "anthropic",
      "model": "claude-sonnet-4-6",
      "api_key_masked": "sk-ant-...****",
      "token_budget": 6000
    }
  ]
}
```

---

### `PUT /users/model-settings`

Save AI model API key (encrypted before storage).

**Request:**
```json
{
  "provider": "anthropic",
  "api_key": "sk-ant-...",
  "model": "claude-sonnet-4-6",
  "token_budget": 6000,
  "set_as_active": true
}
```

**Response `200`:**
```json
{"updated": true}
```

---

### `GET /users/me/saved-answers`

List the member's screening answers. Each catalogue question appears, with `value` `null` until answered, followed by any other saved answers (`group` `"other"`).

**Response `200`:**
```json
{
  "answers": [
    {
      "question_key": "disclosure.veteran_status",
      "question": "Veteran status",
      "group": "voluntary",
      "optional": true,
      "value": null,
      "updated_at": null
    }
  ]
}
```

---

### `PUT /users/me/saved-answers/{question_key}`

Save or replace one answer. `question_key` has the form `group.name` (lowercase letters, digits, `_` and spaces after the dot). Rate limit: 60/minute.

**Request:**
```json
{"value": "Yes"}
```

`value` is a string, a boolean, or a list of up to 20 strings. Each text value is limited to 2000 characters. A blank string is rejected; send `DELETE` instead.

**Response `200`:**
```json
{"question_key": "disclosure.veteran_status", "value": "Yes"}
```

**Errors:** `404` for a malformed or internal key; `422` for an empty or over-long answer.

---

### `DELETE /users/me/saved-answers/{question_key}`

Remove a saved answer.

**Response `204`:** no body.

---

## Rate Limits

| Scope | Limit |
|---|---|
| All authenticated endpoints | 60 requests / minute |
| Auth endpoints (`/auth/*`) | 10 requests / minute |
| Agent runs (`/agents/run`) | 10 requests / minute |
| Document upload (`/rag/upload`) | 5 requests / minute |

Rate limit headers returned on every response:
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 58
X-RateLimit-Reset: 1717584060
```

When exceeded: `429 Too Many Requests` with `Retry-After` header.
