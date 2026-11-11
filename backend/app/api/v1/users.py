import asyncio
import logging
import uuid as _uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import encrypt_api_key
from app.models.db import AgentRun, JobApplication, User, UserModelSettings, UserPreferences
from app.models.schemas import (
    JobSearchProfileResponse,
    ModelSettingsCreate,
    ModelSettingsResponse,
    UserPreferencesResponse,
    UserPreferencesSchema,
    UserProfileUpdate,
    UserResponse,
)

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)


class DashboardStats(BaseModel):
    applications_count: int
    interviews_count: int
    avg_match_score: float
    followups_due: int
    recent_agent_runs: list[dict]


@router.get("/me/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    uid = current_user.id

    # One aggregate query keeps dashboard load to 2 DB round trips total
    # (stats + recent runs) instead of 5; AVG ignores NULL match scores.
    stats_res = await db.execute(
        select(
            func.count(JobApplication.id),
            func.coalesce(
                func.sum(case((JobApplication.status == "interview", 1), else_=0)),
                0,
            ),
            func.avg(JobApplication.match_score),
            func.coalesce(
                func.sum(case((JobApplication.status == "applied", 1), else_=0)),
                0,
            ),
        ).where(
            JobApplication.user_id == uid,
        )
    )
    applications_count, interviews_count, avg_raw, followups_due = stats_res.one()
    applications_count = int(applications_count or 0)
    interviews_count = int(interviews_count or 0)
    followups_due = int(followups_due or 0)
    avg_match_score: float = float(avg_raw) if avg_raw is not None else 0.0

    # recent_agent_runs — last 5
    runs_res = await db.execute(
        select(AgentRun)
        .where(AgentRun.user_id == uid)
        .order_by(AgentRun.started_at.desc())
        .limit(5)
    )
    runs = runs_res.scalars().all()
    recent_agent_runs = [
        {
            "id": str(r.id),
            "agent_type": r.agent_type,
            "status": r.status,
            "created_at": r.started_at.isoformat() if r.started_at else None,
        }
        for r in runs
    ]

    return DashboardStats(
        applications_count=applications_count,
        interviews_count=interviews_count,
        avg_match_score=avg_match_score,
        followups_due=followups_due,
        recent_agent_runs=recent_agent_runs,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserResponse)
async def update_profile(
    payload: UserProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(current_user, field, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="That email is already in use") from exc
    return current_user


# Bumped whenever the Terms of Service or Privacy Policy materially change.
# Keep in sync with the "Last updated" date on frontend/(marketing)/terms
# and /privacy — a version bump does not by itself require existing users
# to re-accept; that decision (and any re-prompt) is a separate, deliberate
# product call, not something this constant triggers automatically.
POLICY_VERSION = "2026-09-26"


@router.post("/me/consent", response_model=UserResponse)
async def record_policy_consent(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Record that the signed-in user just agreed to the ToS + Privacy Policy.

    Called once, right after sign-up completes (the checkbox is required to
    submit the sign-up form, so reaching this call already implies assent —
    this endpoint's job is only to make that durable with a timestamp and
    the policy revision in force at the time). Safe to call again: it always
    overwrites with the current moment and POLICY_VERSION, so a user who
    re-accepts (e.g. after a version bump) gets the latest record kept.
    """
    current_user.policy_accepted_at = datetime.now(UTC)
    current_user.policy_version = POLICY_VERSION
    await db.flush()
    logger.info(
        "Policy consent recorded: %s (version %s)", current_user.clerk_user_id, POLICY_VERSION
    )
    return current_user


@router.get("/me/export")
async def export_my_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download every record tied to your account as a ZIP of JSON files —
    the self-service DPDP Act 'right to access' request, answered immediately
    instead of needing a support email."""
    from app.services.data_export_service import build_user_data_export

    archive = await build_user_data_export(db, current_user)
    filename = f"careercraft-data-export-{datetime.now(UTC).date().isoformat()}.zip"
    logger.info("Data export downloaded: %s (%d bytes)", current_user.clerk_user_id, len(archive))
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/me/preferences", response_model=UserPreferencesResponse | None)
async def get_preferences(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == current_user.id)
    )
    return result.scalar_one_or_none()


def _model_uuid(value: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=404, detail="Model not found") from None


_CLEARABLE_PREFERENCES = {
    "experience_level",
    "years_experience",
    "job_type",
    "work_mode",
    "salary_min",
    "salary_max",
    "current_title",
    "bio",
}


@router.patch("/me/preferences", response_model=UserPreferencesResponse)
async def upsert_preferences(
    payload: UserPreferencesSchema,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == current_user.id)
    )
    prefs = result.scalar_one_or_none()
    # Only what the client sent: a PATCH of one switch must not reset target
    # roles, locations or the browser choice to their schema defaults. A
    # profile field can be cleared with an explicit null; the rest cannot.
    update_data = {
        field: value
        for field, value in payload.model_dump(exclude_unset=True).items()
        if value is not None or field in _CLEARABLE_PREFERENCES
    }
    if update_data.get("auto_apply_enabled") and not (prefs and prefs.auto_apply_enabled):
        # Switching the rule on: it covers jobs found from now, not the backlog.
        update_data["auto_rule_enabled_at"] = datetime.now(UTC)
    if prefs is None:
        prefs = UserPreferences(user_id=current_user.id, **update_data)
        db.add(prefs)
    else:
        for field, value in update_data.items():
            setattr(prefs, field, value)
    await db.flush()
    return prefs


@router.get("/me/job-search-profile", response_model=JobSearchProfileResponse)
async def get_job_search_profile(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Build job search profile from resume + preferences (server-side).

    Analyzes resume data, extracts skills, suggests roles, and validates
    preferences to prevent client-side manipulation.
    """
    from app.models.db import UserDocument

    # Fetch user preferences
    prefs_result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == current_user.id)
    )
    prefs = prefs_result.scalar_one_or_none()

    # Fetch resume documents
    docs_result = await db.execute(
        select(UserDocument)
        .where(UserDocument.user_id == current_user.id, UserDocument.doc_type == "resume")
        .order_by(UserDocument.is_primary.desc(), UserDocument.embedded_at.desc())
    )
    resume_doc = docs_result.scalars().first()

    # Build preferences object (with defaults)
    saved_prefs = UserPreferencesSchema(
        experience_level=prefs.experience_level if prefs else None,
        years_experience=prefs.years_experience if prefs else None,
        job_type=prefs.job_type if prefs else None,
        work_mode=prefs.work_mode if prefs else None,
        salary_min=prefs.salary_min if prefs else None,
        salary_max=prefs.salary_max if prefs else None,
        target_roles=prefs.target_roles if prefs else [],
        preferred_locations=prefs.preferred_locations if prefs else [],
        current_title=prefs.current_title if prefs else None,
        bio=prefs.bio if prefs else None,
    )

    # Extract skills from resume ATS data (server-side only)
    skills: list[str] = []
    if resume_doc and resume_doc.ats_data:
        matched_keywords = resume_doc.ats_data.get("matched_keywords", [])
        if isinstance(matched_keywords, list):
            skills = matched_keywords[:8]

    # Build role suggestions from preferences or current title
    role_suggestions: list[str] = []
    if saved_prefs.target_roles:
        role_suggestions = saved_prefs.target_roles[:5]
    elif saved_prefs.current_title:
        role_suggestions = [saved_prefs.current_title]

    # Infer experience level from years if not set
    inferred_exp_level = saved_prefs.experience_level
    if not inferred_exp_level and saved_prefs.years_experience is not None:
        years = saved_prefs.years_experience
        if years == 0:
            inferred_exp_level = "fresher"
        elif years <= 2:
            inferred_exp_level = "junior"
        elif years <= 5:
            inferred_exp_level = "mid"
        elif years <= 10:
            inferred_exp_level = "senior"
        else:
            inferred_exp_level = "lead"

    # Build search query preview
    search_query_preview = role_suggestions[0] if role_suggestions else "Set target role"

    # Determine work mode for location preview
    work_modes = saved_prefs.work_mode.split(",") if saved_prefs.work_mode else []
    primary_work_mode = work_modes[0].strip() if work_modes else "remote"

    # Build location preview
    location_preview = "Remote"
    if primary_work_mode != "remote" and saved_prefs.preferred_locations:
        location_preview = saved_prefs.preferred_locations[0]
    elif primary_work_mode != "remote":
        location_preview = "Any"

    # Identify missing fields
    missing_fields: list[str] = []
    if not resume_doc:
        missing_fields.append("resume")
    if not role_suggestions:
        missing_fields.append("target role")
    if saved_prefs.years_experience is None:
        missing_fields.append("years of experience")

    # Build analysis notes
    analysis_notes: list[str] = []
    if resume_doc:
        analysis_notes.append(
            "Search agent will analyze full resume text when you run search. "
            "This panel shows saved preferences and resume ATS keywords."
        )
    else:
        analysis_notes.append("Upload resume so agents can analyze skills and experience.")

    return JobSearchProfileResponse(
        resume_found=bool(resume_doc),
        resume_filename=resume_doc.filename if resume_doc else None,
        role_suggestions=role_suggestions,
        skills=skills,
        inferred_years_experience=saved_prefs.years_experience,
        inferred_experience_level=inferred_exp_level,
        saved_preferences=saved_prefs,
        search_query_preview=search_query_preview,
        location_preview=location_preview,
        work_mode_preview=saved_prefs.work_mode,
        missing_fields=missing_fields,
        analysis_notes=analysis_notes,
    )


@router.post("/me/models", response_model=ModelSettingsResponse, status_code=201)
@limiter.limit("5/minute")
async def add_model_settings(
    request: Request,
    payload: ModelSettingsCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    # Saving a model again replaces its old key instead of keeping both, so
    # stale encrypted keys do not pile up. Other models stay switchable.
    await db.execute(
        sa_delete(UserModelSettings).where(
            UserModelSettings.user_id == current_user.id,
            UserModelSettings.provider == payload.provider,
            UserModelSettings.model_name == payload.model_name,
        )
    )
    # Exactly one active model (also enforced by a partial unique index).
    await db.execute(
        sa_update(UserModelSettings)
        .where(UserModelSettings.user_id == current_user.id)
        .values(is_active=False)
    )

    encrypted_key = encrypt_api_key(payload.api_key, settings.APP_SECRET_KEY)
    model_setting = UserModelSettings(
        user_id=current_user.id,
        provider=payload.provider,
        api_key_enc=encrypted_key,
        model_name=payload.model_name,
        ollama_url=payload.ollama_url,
        is_active=True,
    )
    db.add(model_setting)
    await db.flush()
    return model_setting


@router.get("/me/models", response_model=list[ModelSettingsResponse])
async def list_model_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(UserModelSettings).where(UserModelSettings.user_id == current_user.id)
    )
    return result.scalars().all()


class ModelTestRequest(BaseModel):
    model_id: str


@router.patch("/me/models/{model_id}/activate", response_model=ModelSettingsResponse)
async def activate_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import update as sa_update

    # Deactivate the others in one statement before activating the target,
    # so the one-active index never sees two active rows mid-flush.
    await db.execute(
        sa_update(UserModelSettings)
        .where(UserModelSettings.user_id == current_user.id)
        .values(is_active=False)
    )
    target_res = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.id == _model_uuid(model_id),
            UserModelSettings.user_id == current_user.id,
        )
    )
    target = target_res.scalars().first()
    if not target:
        raise HTTPException(status_code=404, detail="Model not found")
    target.is_active = True
    await db.flush()
    return target


@router.delete("/me/models/{model_id}", status_code=204)
async def delete_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):

    result = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.id == _model_uuid(model_id),
            UserModelSettings.user_id == current_user.id,
        )
    )
    model = result.scalars().first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    await db.delete(model)
    await db.flush()


@router.post("/me/models/test")
async def test_model(
    payload: ModelTestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):

    from langchain_core.messages import HumanMessage

    from app.core.model_router import _build_llm

    result = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.id == _model_uuid(payload.model_id),
            UserModelSettings.user_id == current_user.id,
        )
    )
    model_setting = result.scalars().first()
    if not model_setting:
        raise HTTPException(status_code=404, detail="Model setting not found")

    try:
        llm = _build_llm(model_setting)
        resp = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="Reply with exactly: OK")]),
            timeout=60,
        )
        return {"success": True, "response": resp.content.strip()[:200]}
    except TimeoutError as exc:
        logger.warning(
            "Model test timed out for user %s model %s", current_user.id, payload.model_id
        )
        raise HTTPException(
            status_code=422,
            detail="Model test timed out after 60s. The model may be cold-starting — try again, or pick a smaller model. Also verify the API key and model name.",
        ) from exc
    except Exception as exc:
        logger.warning(
            "Model test failed for user %s model %s: %s",
            current_user.id,
            payload.model_id,
            exc,
        )
        raise HTTPException(status_code=422, detail=f"Model test failed: {exc}"[:300]) from exc


# ---------------------------------------------------------------------------
# Saved screening answers
# ---------------------------------------------------------------------------


class SavedAnswerRequest(BaseModel):
    value: str | bool | list[str]


def _saved_answer_key(question_key: str) -> str:
    from app.applications import saved_answers

    if not saved_answers.valid_key(question_key):
        raise HTTPException(status_code=404, detail="Unknown question")
    return question_key


@router.get("/me/saved-answers")
async def list_saved_answers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reusable answers to screening questions. The extension fills these
    as the member's own answers (never drafted), so only these and profile
    data let an application be submitted without review."""
    from app.applications import saved_answers

    return {"answers": await saved_answers.list_answers(db, current_user.id)}


@router.put("/me/saved-answers/{question_key}")
@limiter.limit("60/minute")
async def put_saved_answer(
    request: Request,
    question_key: str,
    payload: SavedAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.applications import saved_answers

    key = _saved_answer_key(question_key)
    value = payload.value
    texts = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    if len(texts) > 20 or any(len(t) > saved_answers.MAX_VALUE_CHARS for t in texts):
        raise HTTPException(status_code=422, detail="Answer is too long")
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise HTTPException(status_code=422, detail="Answer is empty; delete it instead")
    await saved_answers.save_answer(db, current_user.id, key, value)
    await db.flush()
    return {"question_key": key, "value": value}


@router.delete("/me/saved-answers/{question_key}", status_code=204)
async def delete_saved_answer(
    question_key: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.applications import saved_answers

    await saved_answers.delete_answer(db, current_user.id, _saved_answer_key(question_key))
    await db.flush()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# LinkedIn credentials (no longer collected) + Auto-mode toggle
# ---------------------------------------------------------------------------


class AutoModeRequest(BaseModel):
    mode: str  # 'auto' or 'drafts'


class ConnectedAccountsResponse(BaseModel):
    google: bool
    gmail_send: bool


# LinkedIn sign-in happens in the member's own browser; passwords are no
# longer accepted. DELETE stays so anyone who stored one can still erase it.
@router.delete("/me/linkedin-credentials")
async def delete_linkedin_credentials(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Erase LinkedIn credentials stored by older versions."""
    current_user.linkedin_email_enc = None
    current_user.linkedin_password_enc = None
    await db.flush()
    return {"status": "deleted"}


@router.get("/me/connected-accounts", response_model=ConnectedAccountsResponse)
async def get_connected_accounts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the Nango-managed Gmail connection state."""
    from app.models.db import IntegrationConnection

    result = await db.execute(
        select(IntegrationConnection.status).where(
            IntegrationConnection.user_id == current_user.id,
            IntegrationConnection.provider == "gmail",
        )
    )
    gmail_connected = result.scalar_one_or_none() == "connected"
    return ConnectedAccountsResponse(google=gmail_connected, gmail_send=gmail_connected)


@router.patch("/me/auto-mode")
async def set_auto_mode(
    payload: AutoModeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle auto-mode: 'auto' (send immediately) or 'drafts' (save for review)."""
    if payload.mode not in ("auto", "drafts"):
        raise HTTPException(status_code=400, detail="mode must be 'auto' or 'drafts'")
    current_user.auto_mode = payload.mode
    await db.flush()
    return {"auto_mode": payload.mode}


@router.get("/me/auto-mode")
async def get_auto_mode(
    current_user: User = Depends(get_current_user),
):
    """Get current auto-mode setting."""
    return {"auto_mode": current_user.auto_mode or "drafts"}


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@router.patch("/me/password", status_code=204)
async def change_password(
    payload: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
):
    """Password changes are managed by Clerk, not this API."""
    _ = (payload, current_user)
    if len(payload.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    raise HTTPException(
        status_code=400, detail="Password changes are managed by Clerk — use Account Settings"
    )


# Requesting deletion starts a grace period before anything is actually
# removed; cancelling (by the user or automatically on their next visit)
# starts a cooldown before they can request deletion again.
DELETION_GRACE_DAYS = 15
DELETION_COOLDOWN_DAYS = 30


@router.delete("/me", response_model=UserResponse)
async def request_account_deletion(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Request account deletion. Nothing is deleted yet: this starts a
    15-day grace period (deletion_scheduled_for), during which the account
    stays fully usable and the deletion can be cancelled — from Settings, or
    automatically the next time this person opens the app after leaving. A
    maintenance sweep hard-deletes the account once the grace period passes.
    """
    if (
        current_user.deletion_cooldown_until
        and current_user.deletion_cooldown_until > datetime.now(UTC)
    ):
        raise HTTPException(
            status_code=429,
            detail={
                "error": "deletion_cooldown_active",
                "message": "You cancelled a deletion recently — try again after the cooldown.",
                "cooldown_until": current_user.deletion_cooldown_until.isoformat(),
            },
        )

    current_user.deletion_requested_at = datetime.now(UTC)
    current_user.deletion_scheduled_for = current_user.deletion_requested_at + timedelta(
        days=DELETION_GRACE_DAYS
    )
    await db.flush()
    logger.info("Account deletion requested: %s", current_user.clerk_user_id)
    return current_user


@router.post("/me/cancel-deletion", response_model=UserResponse)
async def cancel_account_deletion(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending account deletion. Starts a 30-day cooldown before
    deletion can be requested again, to prevent immediate flip-flopping."""
    if current_user.deletion_requested_at is None:
        raise HTTPException(status_code=400, detail="No pending deletion to cancel")

    current_user.deletion_requested_at = None
    current_user.deletion_scheduled_for = None
    current_user.deletion_cooldown_until = datetime.now(UTC) + timedelta(
        days=DELETION_COOLDOWN_DAYS
    )
    await db.flush()
    logger.info("Account deletion cancelled: %s", current_user.clerk_user_id)
    return current_user
