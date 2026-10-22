import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import (
    Mapped,
    Session,
    mapped_column,
    relationship,
    with_loader_criteria,
)

from app.core.database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "computer_mode IN ('auto','browser','cloud')", name="users_computer_mode_check"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String)
    avatar_url: Mapped[str | None] = mapped_column(String)
    google_id: Mapped[str | None] = mapped_column(String, unique=True)
    clerk_user_id: Mapped[str | None] = mapped_column(String, unique=True)
    phone: Mapped[str | None] = mapped_column(String)
    linkedin_url: Mapped[str | None] = mapped_column(String)
    headline: Mapped[str | None] = mapped_column(String)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    linkedin_email_enc: Mapped[str | None] = mapped_column(Text)
    linkedin_password_enc: Mapped[str | None] = mapped_column(Text)
    auto_mode: Mapped[str] = mapped_column(String, default="drafts")  # 'auto' or 'drafts'
    # Where the agent's browser runs: 'auto' (extension when online, else
    # cloud), 'browser' or 'cloud'. Migration 20261022120000.
    computer_mode: Mapped[str] = mapped_column(String, nullable=False, server_default="auto")
    # When the user clicked "I agree to the Terms of Service and Privacy
    # Policy" at sign-up, and which policy revision was current then. NULL on
    # accounts created before this was required — never backfilled.
    policy_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    policy_version: Mapped[str | None] = mapped_column(String(20))
    # Deletion is a 15-day grace period: requesting it stamps these two;
    # a maintenance sweep hard-deletes once deletion_scheduled_for passes.
    # Cancelling clears both and sets a 30-day deletion_cooldown_until.
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    model_settings: Mapped[list["UserModelSettings"]] = relationship(back_populates="user")
    documents: Mapped[list["UserDocument"]] = relationship(back_populates="user")
    applications: Mapped[list["JobApplication"]] = relationship(back_populates="user")
    leads: Mapped[list["Lead"]] = relationship(back_populates="user")
    agent_runs: Mapped[list["AgentRun"]] = relationship(back_populates="user")
    preferences: Mapped["UserPreferences | None"] = relationship(
        back_populates="user", uselist=False
    )
    notifications: Mapped[list["Notification"]] = relationship(back_populates="user")


class UserModelSettings(Base):
    __tablename__ = "user_model_settings"
    # At most one active model per member (migration 20261001102000).
    __table_args__ = (
        Index(
            "user_model_settings_one_active",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String, nullable=False)
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String)
    ollama_url: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["User"] = relationship(back_populates="model_settings")


class UserDocument(Base):
    __tablename__ = "user_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    doc_type: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    ats_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ats_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    user: Mapped["User"] = relationship(back_populates="documents")


class JobApplication(Base):
    __tablename__ = "job_applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    company: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str | None] = mapped_column(String)
    job_url: Mapped[str | None] = mapped_column(String)
    jd_text: Mapped[str | None] = mapped_column(Text)
    match_score: Mapped[int | None] = mapped_column(Integer)
    resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_documents.id", ondelete="SET NULL")
    )
    cover_letter: Mapped[str | None] = mapped_column(Text)
    cover_letter_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_documents.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="saved")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_day5: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_day12: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    found_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Soft delete: hidden from every ORM read by _hide_deleted_applications.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Assisted apply in the member's own browser: opened -> applied | failed.
    apply_state: Mapped[str | None] = mapped_column(String)

    user: Mapped["User"] = relationship(back_populates="applications")


@event.listens_for(Session, "do_orm_execute")
def _hide_deleted_applications(state) -> None:
    """Soft-deleted applications are invisible to every ORM SELECT unless the
    statement opts in with ``.execution_options(include_deleted=True)`` (dedupe
    checks do, so a deleted job isn't re-saved by the next search)."""
    if state.is_select and not state.execution_options.get("include_deleted", False):
        state.statement = state.statement.options(
            with_loader_criteria(
                JobApplication,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
            )
        )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    company: Mapped[str | None] = mapped_column(String)
    linkedin_url: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="cold")
    last_contact: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="leads")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    agent_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="running")
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="agent_runs")


class PortalCredential(Base):
    """Member-owned exact-origin website login. Plaintext is never returned."""

    __tablename__ = "portal_credentials"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    username_enc: Mapped[str] = mapped_column(Text, nullable=False)
    password_enc: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("user_id", "origin", name="portal_credentials_user_origin"),)


class CoverLetterVersion(Base):
    __tablename__ = "cover_letter_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_applications.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_documents.id", ondelete="CASCADE")
    )
    tone: Mapped[str] = mapped_column(String(10), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExtensionDevice(Base):
    """A browser paired with the CareerCraft extension. Only the token's
    SHA-256 is stored; the raw token is shown once at pairing time."""

    __tablename__ = "extension_devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), default="Browser")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only by the agent command long-poll: "online" for browser mode.
    agent_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExtensionTask(Base):
    """Work handed to the extension. The Temporal workflow named by
    workflow_id owns the lifecycle; the extension claims the task and
    reports progress, which the API relays to that workflow as signals."""

    __tablename__ = "extension_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extension_devices.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id", ondelete="CASCADE")
    )
    workflow_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30), default="apply")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    review_hash: Mapped[str | None] = mapped_column(String(64))
    review_url: Mapped[str | None] = mapped_column(Text)
    review_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_token_hash: Mapped[str | None] = mapped_column(String(64))
    submission_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationStatusEvent(Base):
    """An email that moved (or failed to match) an application's status.

    Unique per (user, Gmail message): a message is acted on at most once.
    """

    __tablename__ = "application_status_events"
    __table_args__ = (UniqueConstraint("user_id", "gmail_message_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id", ondelete="SET NULL"), index=True
    )
    gmail_message_id: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    company: Mapped[str | None] = mapped_column(String)
    subject: Mapped[str | None] = mapped_column(String)
    previous_status: Mapped[str | None] = mapped_column(String)
    new_status: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApplicationAttempt(Base):
    """Idempotency ledger for the durable submit click.

    One row per (user, job_application) — enforced by a unique constraint,
    not a new row per retry. State advances forward through the same row so
    a concurrent or repeated submit attempt can be detected and suppressed.
    """

    __tablename__ = "application_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    job_application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_applications.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )

    # workflow_id is the stable "auto-apply/{user_id}/{job_application_id}"
    # Temporal workflow id that drives this attempt; temporal_run_id is
    # Temporal's own per-execution run id (changes on Continue-As-New/
    # retry-as-new-workflow, unlike workflow_id).
    workflow_id: Mapped[str | None] = mapped_column(String, unique=True)
    temporal_run_id: Mapped[str | None] = mapped_column(String)

    state: Mapped[str] = mapped_column(String(30), default="preparing")

    submission_token: Mapped[str | None] = mapped_column(String, unique=True)
    external_application_id: Mapped[str | None] = mapped_column(String)
    confirmation_url: Mapped[str | None] = mapped_column(String)
    confirmation_text: Mapped[str | None] = mapped_column(Text)
    approved_snapshot_hash: Mapped[str | None] = mapped_column(String)

    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When this attempt is allowed to start (after the pacing gap). The next
    # reservation spaces itself from this, so a batch is spread out.
    scheduled_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "job_application_id", name="application_attempts_one_per_job"),
    )


class RecruiterOutreach(Base):
    """One email to a recruiter, from first draft through reply or bounce.

    state: held (address not verified, the member decides), draft (verified,
    waiting for approval), approved (will be sent within the daily cap),
    sending, sent, failed, cancelled.
    """

    __tablename__ = "recruiter_outreach"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id", ondelete="SET NULL")
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("recruiter_outreach.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(20), default="initial")  # initial | followup
    company: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str | None] = mapped_column(String)
    to_email: Mapped[str] = mapped_column(String, nullable=False)
    email_source: Mapped[str | None] = mapped_column(String(30))
    verdict: Mapped[str] = mapped_column(String(10), default="unknown")
    verified_by: Mapped[str | None] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    resume_version: Mapped[str | None] = mapped_column(String(64))
    # The tailored resume PDF (a user document) attached to the first email.
    resume_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_payload_hash: Mapped[str | None] = mapped_column(String(64))
    sending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_message_id: Mapped[str | None] = mapped_column(String)
    gmail_thread_id: Mapped[str | None] = mapped_column(String)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    followup_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only when the member turned open tracking on: a one-pixel image in
    # the email reports the first time it is loaded.
    open_token: Mapped[str | None] = mapped_column(String(40), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # one first email and one follow-up per application
        Index(
            "recruiter_outreach_one_per_kind",
            "user_id",
            "job_application_id",
            "kind",
            unique=True,
            postgresql_where=text("job_application_id IS NOT NULL"),
        ),
    )


class OutboundMessage(Base):
    """Idempotency ledger for approved outbound sends (email today)."""

    __tablename__ = "outbound_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(String(20), default="email")
    recipient: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str | None] = mapped_column(String)
    body_hash: Mapped[str] = mapped_column(String, nullable=False)

    state: Mapped[str] = mapped_column(String(30), default="draft")

    provider_message_id: Mapped[str | None] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationConnection(Base):
    """Provider-neutral connection reference; OAuth credentials remain in Nango."""

    __tablename__ = "integration_connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_config_key: Mapped[str] = mapped_column(String(100), nullable=False)
    external_connection_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    provider_metadata_enc: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_integration_connections_user_provider"),
        CheckConstraint(
            "status IN ('pending', 'connected', 'disconnected', 'error', 'revoked')",
            name="integration_connections_status_check",
        ),
    )


class IntegrationWebhookEvent(Base):
    """Hash-only Nango webhook replay ledger; payloads are deliberately not retained."""

    __tablename__ = "integration_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(40))
    external_connection_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CandidateProfile(Base):
    """Structured, explicitly-approved application data.

    Never populated by LLM inference — sponsorship, authorization, salary,
    and notice period come only from the user. See answer_resolver.py.
    """

    __tablename__ = "candidate_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    first_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)

    city: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
    country: Mapped[str | None] = mapped_column(String)
    postal_code: Mapped[str | None] = mapped_column(String)

    current_company: Mapped[str | None] = mapped_column(String)
    current_title: Mapped[str | None] = mapped_column(String)
    years_experience: Mapped[float | None] = mapped_column(Numeric)
    notice_period_days: Mapped[int | None] = mapped_column(Integer)

    linkedin_url: Mapped[str | None] = mapped_column(String)
    github_url: Mapped[str | None] = mapped_column(String)
    portfolio_url: Mapped[str | None] = mapped_column(String)

    current_salary: Mapped[float | None] = mapped_column(Numeric)
    expected_salary: Mapped[float | None] = mapped_column(Numeric)
    currency: Mapped[str | None] = mapped_column(String(10))

    work_authorization: Mapped[str | None] = mapped_column(String)
    requires_sponsorship: Mapped[bool | None] = mapped_column(Boolean)
    willing_to_relocate: Mapped[bool | None] = mapped_column(Boolean)
    remote_preference: Mapped[str | None] = mapped_column(String)

    default_resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_documents.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CandidateAnswer(Base):
    """A reusable, user-approved answer to a normalized application question.

    One row per (user, question_key) — later applications reuse it instead
    of asking again or generating a fresh answer.
    """

    __tablename__ = "candidate_answers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    question_key: Mapped[str] = mapped_column(String, nullable=False)
    normalized_question: Mapped[str | None] = mapped_column(Text)
    answer_type: Mapped[str] = mapped_column(String(20), default="text")
    answer: Mapped[dict] = mapped_column(JSONB, default=dict)

    source: Mapped[str] = mapped_column(String(20), default="user")
    confidence: Mapped[float] = mapped_column(Numeric, default=1.0)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    approved_by_user: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "question_key", name="candidate_answers_one_per_question"),
    )


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    questions: Mapped[list] = mapped_column(JSONB, default=list)
    answers: Mapped[list] = mapped_column(JSONB, default=list)
    scores: Mapped[list] = mapped_column(JSONB, default=list)
    overall_score: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SalaryReport(Base):
    __tablename__ = "salary_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    p25: Mapped[int] = mapped_column(Integer, nullable=False)
    p50: Mapped[int] = mapped_column(Integer, nullable=False)
    p75: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_amount: Mapped[int | None] = mapped_column(Integer)
    classification: Mapped[str | None] = mapped_column(String(20))
    negotiation_script: Mapped[dict | None] = mapped_column(JSONB)
    data_sources: Mapped[list] = mapped_column(JSONB, default=list)
    data_unavailable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompanyIntelModel(Base):
    __tablename__ = "company_intel"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    overview: Mapped[str | None] = mapped_column(Text)
    culture_summary: Mapped[str | None] = mapped_column(Text)
    news_items: Mapped[list] = mapped_column(JSONB, default=list)
    tech_stack: Mapped[list] = mapped_column(JSONB, default=list)
    glassdoor_sentiment: Mapped[str | None] = mapped_column(String(10))
    partial_data: Mapped[dict | None] = mapped_column(JSONB)
    researched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ResumePersona(Base):
    __tablename__ = "resume_personas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    primary_resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_documents.id", ondelete="SET NULL"), nullable=True
    )
    target_keywords: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LinkedInOutreachQueue(Base):
    __tablename__ = "linkedin_outreach_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_title: Mapped[str | None] = mapped_column(String(255))
    contact_linkedin_url: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending_approval")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AtsScore(Base):
    __tablename__ = "ats_scores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    resume_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_documents.id", ondelete="SET NULL"), nullable=True
    )
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id"), nullable=True
    )
    composite_score: Mapped[int] = mapped_column(Integer, nullable=False)
    keyword_score: Mapped[int] = mapped_column(Integer, nullable=False)
    readability_score: Mapped[int] = mapped_column(Integer, nullable=False)
    format_score: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_keywords: Mapped[list] = mapped_column(JSONB, default=list)
    suggestions: Mapped[list] = mapped_column(JSONB, default=list)
    flesch_kincaid: Mapped[float | None] = mapped_column(nullable=True)
    avg_sentence_length: Mapped[float | None] = mapped_column(nullable=True)
    format_checks: Mapped[dict] = mapped_column(JSONB, default=dict)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    experience_level: Mapped[str | None] = mapped_column(String)
    years_experience: Mapped[int | None] = mapped_column(Integer)
    job_type: Mapped[str | None] = mapped_column(String)
    work_mode: Mapped[str | None] = mapped_column(String)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    target_roles: Mapped[list | None] = mapped_column(JSONB, default=list)
    preferred_locations: Mapped[list | None] = mapped_column(JSONB, default=list)
    current_title: Mapped[str | None] = mapped_column(String)
    bio: Mapped[str | None] = mapped_column(Text)
    # When True, autonomous job search + apply will open a visible Chromium
    # window streamed to the UI over SSE.  When False (default), the headless
    # Remotive/Arbeitnow/Jobicy/JobSpy waterfall is used.  Set per-user.
    prefer_live_browser: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Notification channel toggles — surfaced in Settings → Notifications.
    notify_email: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    notify_agent_alerts: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    notify_followup_reminders: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    notify_weekly_digest: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Opt-in: the scheduled daily search runs browser automation and LLM
    # calls on the member's own API key, so it never starts unasked.
    daily_search_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Opt-in: scan the member's connected Gmail for replies to applications.
    inbox_tracking_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Opt-in: one email a day summarising applications and recruiter emails.
    notify_daily_summary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Opt-in: tailor a resume and queue the application in the member's browser
    # for saved jobs at or above the match-score threshold.
    auto_apply_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Recruiter outreach: emails sent per rolling 24 hours, and whether sends
    # may go out without per-email approval once a few have been approved.
    outreach_daily_cap: Mapped[int] = mapped_column(
        Integer, default=25, nullable=False, server_default="25"
    )
    outreach_auto_send: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # Off by default: adds a tracking pixel to recruiter emails (see
    # RecruiterOutreach.open_token).
    outreach_track_opens: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    # One auto-apply rule: new saved job >= auto_rule_min_match -> action.
    # auto_apply_enabled is its pause switch.
    auto_rule_min_match: Mapped[int] = mapped_column(
        Integer, default=70, nullable=False, server_default="70"
    )
    auto_rule_action: Mapped[str] = mapped_column(
        String, default="apply", nullable=False, server_default="apply"
    )
    # When auto_apply_enabled last went on; the rule only acts on jobs found after it.
    auto_rule_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Resume preferences, asked once on first auto-apply (resume_prefs_set_at
    # NULL = not asked yet) and editable in Settings.
    resume_template: Mapped[str | None] = mapped_column(String)
    resume_page_target: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False, server_default="2"
    )
    resume_tailor_per_job: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    resume_tone: Mapped[str] = mapped_column(
        String, default="professional", nullable=False, server_default="professional"
    )
    resume_prefs_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="preferences")


class Notification(Base):
    """In-app notification for a user — the bell dropdown in AppTopbar reads
    these. Created by notification_service.create_notification(); never
    written to directly so the notify_* preference gate and optional email
    dispatch stay in one place."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Free-form, not a DB enum — new types don't need a migration. Known
    # values today: "job_matches", "followup_ready".
    type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="notifications")


class NotificationDelivery(Base):
    """Per-channel delivery ledger for a Notification — the durable,
    queryable dead-letter record Temporal itself doesn't keep (a workflow
    that exhausts its retries just fails; history is only visible until
    namespace retention expires). NotificationWorkflow creates one row per
    additional channel (currently only "email") and drives it through
    pending -> sent, or -> dead once the channel's own retry budget is
    exhausted."""

    __tablename__ = "notification_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|sent|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Sent as Resend's Idempotency-Key header — stable across retries of the
    # same delivery so a retried send after a network blip never double-sends.
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'dead')",
            name="notification_deliveries_status_check",
        ),
    )


class ActionLog(Base):
    """Every auto-apply / outreach / delete action a member or rule takes."""

    __tablename__ = "action_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_applications.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, default="user", nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CopilotConversation(Base):
    """Durable AG-UI messages; composite ownership and a per-thread turn lease."""

    __tablename__ = "copilot_conversations"
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    thread_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    messages: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    active_run_id: Mapped[str | None] = mapped_column(String(200))
    active_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("copilot_conversations_owner_updated", "user_id", "updated_at"),)
