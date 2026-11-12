import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str | None = None
    # Auth subject (`sub`). Clerk emits a text id like "user_2abc..." (~32 chars),
    # so this is no longer a fixed-width UUID — only non-empty and bounded.
    clerk_user_id: str = Field(min_length=1, max_length=255)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    avatar_url: str | None
    headline: str | None
    phone: str | None
    linkedin_url: str | None
    onboarding_completed: bool
    policy_accepted_at: datetime | None
    policy_version: str | None
    deletion_requested_at: datetime | None
    deletion_scheduled_for: datetime | None
    deletion_cooldown_until: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserProfileUpdate(BaseModel):
    # No email: it comes from the identity provider's verified address. A
    # member-set address could squat on someone else's sign-up.
    full_name: str | None = Field(None, max_length=200)
    headline: str | None = Field(None, max_length=300)
    phone: str | None = Field(None, max_length=30)
    linkedin_url: str | None = Field(None, max_length=500)
    onboarding_completed: bool | None = None

    @field_validator("onboarding_completed")
    @classmethod
    def onboarding_must_be_boolean(cls, value):
        if value is None:
            raise ValueError("onboarding_completed must be a boolean when supplied")
        return value


class UserPreferencesSchema(BaseModel):
    experience_level: str | None = None
    years_experience: int | None = Field(None, ge=0, le=60)
    job_type: str | None = None
    work_mode: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    preferred_locations: list[str] = Field(default_factory=list, max_length=20)
    current_title: str | None = Field(None, max_length=200)
    bio: str | None = Field(None, max_length=2000)
    # When True, autonomous job search and apply use a visible Chromium
    # streamed to the UI.  When False (default), the headless job-board
    # API waterfall runs.  Always overridable per-request.
    prefer_live_browser: bool = False
    # Optional (not plain bool with a default) so a client can PATCH other
    # preference fields without silently resetting one of these to its
    # schema default — omitted means "leave the stored value alone".
    notify_email: bool | None = None
    notify_agent_alerts: bool | None = None
    notify_followup_reminders: bool | None = None
    notify_weekly_digest: bool | None = None
    daily_search_enabled: bool | None = None
    inbox_tracking_enabled: bool | None = None
    outreach_daily_cap: int | None = Field(default=None, ge=1, le=100)
    outreach_auto_send: bool | None = None
    outreach_track_opens: bool | None = None
    notify_daily_summary: bool | None = None
    auto_apply_enabled: bool | None = None
    # Auto-apply rule: new saved job >= auto_rule_min_match -> auto_rule_action.
    # auto_apply_enabled above is its pause switch.
    auto_rule_min_match: int | None = Field(default=None, ge=0, le=100)
    auto_rule_action: Literal["apply", "outreach", "both", "notify"] | None = None
    # Submit fully answered forms without the popup approval (never on
    # LinkedIn or Naukri); capped per rolling 24 hours.
    auto_submit_enabled: bool | None = None
    auto_submit_daily_cap: int | None = Field(default=None, ge=1, le=50)
    # Hide contact details/IDs and, separately, the member's name from the model.
    llm_hide_personal_info: bool | None = None
    llm_hide_name: bool | None = None
    # Resume preferences (asked once on first auto-apply; resume_prefs_set_at
    # stays null until then). The client sets resume_prefs_set_at when saving.
    resume_template: str | None = Field(None, max_length=50)
    resume_page_target: Literal[1, 2] | None = None
    resume_tailor_per_job: bool | None = None
    resume_tone: str | None = Field(None, max_length=50)
    resume_prefs_set_at: datetime | None = None


class UserPreferencesResponse(UserPreferencesSchema):
    id: uuid.UUID
    user_id: uuid.UUID

    model_config = {"from_attributes": True}


class NotificationResponse(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None = None
    link: str | None = None
    read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    notifications: list[NotificationResponse]
    unread_count: int


def _ollama_allowlist() -> set[tuple[str, int]]:
    """Host:port destinations an operator has explicitly permitted for the
    ``ollama`` provider's base URL. Defaults to the local Ollama daemon only —
    set OLLAMA_ALLOWED_HOSTS (comma-separated host:port pairs) to permit a
    self-hosted Ollama on another box (see pentest finding vuln-0001: this
    field used to be unvalidated free text and could reach loopback,
    container-bridge and link-local addresses)."""
    import os

    raw = os.getenv("OLLAMA_ALLOWED_HOSTS", "localhost:11434,127.0.0.1:11434,[::1]:11434")
    allowed: set[tuple[str, int]] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith("["):
            host, _, port = item[1:].partition("]:")
        else:
            host, _, port = item.rpartition(":")
        if port.isdigit():
            allowed.add((host.strip().lower().rstrip("."), int(port)))
    return allowed


class ModelSettingsCreate(BaseModel):
    provider: Literal[
        "anthropic",
        "openai",
        "google",
        "ollama",
        "nvidia_nim",
        "deepseek",
        "openrouter",
        "opencode",
    ]
    api_key: str = Field(min_length=1, max_length=4096)
    model_name: str
    ollama_url: str | None = None

    @field_validator("ollama_url")
    @classmethod
    def _validate_ollama_url(cls, value: str | None) -> str | None:
        """Reject anything but an operator-allow-listed host:port (CWE-918:
        the server fetches this URL directly — see vuln-0001)."""
        if value is None:
            return value
        import ipaddress
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("ollama_url must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("ollama_url must not contain credentials")
        host = parsed.hostname.lower().rstrip(".")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError("ollama_url has an invalid port") from exc
        if (host, port) not in _ollama_allowlist():
            raise ValueError("ollama_url host is not permitted — add it to OLLAMA_ALLOWED_HOSTS")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                raise ValueError("ollama_url host is not permitted")
        return value


class ModelSettingsResponse(BaseModel):
    id: uuid.UUID
    provider: str
    model_name: str | None
    is_active: bool

    model_config = {"from_attributes": True}


# ─── Cover Letter Schemas ─────────────────────────────────────────────────────


class CoverLetterRequest(BaseModel):
    job_application_id: uuid.UUID
    tone: Literal["formal", "casual", "bold", "concise", "story"] = "formal"


class CoverLetterResponse(BaseModel):
    id: uuid.UUID
    content: str
    tone: str
    version_number: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Interview Coach Schemas ──────────────────────────────────────────────────


class InterviewStartRequest(BaseModel):
    role: str
    company: str | None = None
    job_application_id: uuid.UUID | None = None
    question_type: Literal["behavioral", "technical", "situational"] | None = None


class AnswerSubmitRequest(BaseModel):
    answer_text: str = Field(min_length=10)


class AnswerEvaluation(BaseModel):
    score: int = Field(ge=0, le=100)
    rating: Literal["poor", "fair", "good", "excellent"]
    tips: list[str] = Field(min_length=1)


# ─── Salary Intelligence Schemas ─────────────────────────────────────────────


class SalaryReportRequest(BaseModel):
    role: str
    company: str | None = None
    location: str
    offer_amount: int | None = None
    job_application_id: uuid.UUID | None = None


class SalaryReportResponse(BaseModel):
    id: uuid.UUID
    p25: int
    p50: int
    p75: int
    classification: str | None = None
    negotiation_script: dict | None = None
    data_unavailable: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Company Research Schemas ─────────────────────────────────────────────────


class CompanyResearchRequest(BaseModel):
    company_name: str
    force_refresh: bool = False


class CompanyIntelResponse(BaseModel):
    id: uuid.UUID
    company_name: str
    overview: str | None = None
    culture_summary: str | None = None
    news_items: list[dict] = []
    tech_stack: list[str] = []
    glassdoor_sentiment: str | None = None
    partial_data: dict | None = None
    researched_at: datetime

    model_config = {"from_attributes": True}


# ─── NL Job Search Schemas ────────────────────────────────────────────────────


class SearchInterpretation(BaseModel):
    role_title: str | None = None
    seniority: str | None = None
    location: str | None = None
    remote_preference: str | None = None
    salary_range: tuple[int, int] | None = None
    tech_stack: list[str] = []


# ─── Resume Persona Schemas ──────────────────────────────────────────────────


class PersonaCreate(BaseModel):
    name: str = Field(max_length=100)
    description: str | None = Field(None, max_length=2000)
    primary_resume_id: uuid.UUID | None = None
    target_keywords: list[str] = Field(default_factory=list, max_length=50)


class PersonaUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=2000)
    primary_resume_id: uuid.UUID | None = None
    target_keywords: list[str] | None = Field(None, max_length=50)


class PersonaResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    primary_resume_id: uuid.UUID | None = None
    target_keywords: list[str] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── ATS Score Schemas ────────────────────────────────────────────────────────


class ATSScoreRequest(BaseModel):
    resume_text: str = Field(min_length=100)
    jd_text: str = Field(min_length=100)
    resume_id: uuid.UUID | None = None
    job_application_id: uuid.UUID | None = None


class ATSScoreResponse(BaseModel):
    id: uuid.UUID
    composite_score: int
    keyword_score: int
    readability_score: int
    format_score: int
    missing_keywords: list[str] = []
    suggestions: list[str] = []
    flesch_kincaid: float | None = None
    avg_sentence_length: float | None = None
    format_checks: dict = {}
    scored_at: datetime

    model_config = {"from_attributes": True}


# ─── LinkedIn Outreach Schemas ────────────────────────────────────────────────


class OutreachIdentifyRequest(BaseModel):
    company: str
    role_context: str | None = None


class OutreachMessageResponse(BaseModel):
    id: uuid.UUID
    company: str
    contact_name: str
    contact_title: str | None = None
    contact_linkedin_url: str | None = None
    message: str
    status: str
    approved_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Job Search Profile Schemas ───────────────────────────────────────────────


class JobSearchProfileResponse(BaseModel):
    resume_found: bool
    resume_filename: str | None = None
    role_suggestions: list[str] = []
    skills: list[str] = []
    inferred_years_experience: int | None = None
    inferred_experience_level: str | None = None
    saved_preferences: UserPreferencesSchema
    search_query_preview: str
    location_preview: str
    work_mode_preview: str | None = None
    missing_fields: list[str] = []
    analysis_notes: list[str] = []
