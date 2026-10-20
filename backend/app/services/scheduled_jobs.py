"""Job search, follow-up drafts, the daily search and the status check.

Called only by Temporal activities (app/workflows/job_activities.py).
Nothing here schedules itself; Temporal workflows and Schedules decide when
these run. To run one by hand, trigger its Schedule or start its workflow
with the Temporal CLI (see docs/DEPLOYMENT.md).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.api.v1.run_utils import CLIENT_SAFE_AGENT_ERROR
from app.core.config import settings
from app.core.event_bus import emit

logger = logging.getLogger(__name__)


class JobSearchTrigger(BaseModel):
    user_id: str
    run_id: str
    search_query: str
    location: str
    max_results: int
    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    live_browser: bool = False
    work_mode: str = ""
    platforms: list[str] = Field(default_factory=list)
    remote: str = "any"
    resume_id: str | None = None
    persona_id: str | None = None
    posted_within_days: int = Field(default=30, ge=1, le=90)


async def run_job_search(
    payload: JobSearchTrigger,
):

    from sqlalchemy import select

    from app.agents.job_search import job_search_agent_node
    from app.agents.state import AgentState
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun

    state = AgentState(
        user_id=payload.user_id,
        run_id=payload.run_id,
        task_type="job_search",
        messages=[HumanMessage(content=payload.search_query)],
        context={
            "resume_id": payload.resume_id,
            "persona_id": payload.persona_id,
            "posted_within_days": payload.posted_within_days,
            "search_query": payload.search_query,
            "location": payload.location,
            "titles": payload.titles,
            "locations": payload.locations,
            "max_results": payload.max_results,
            "live_browser": payload.live_browser,
            "work_mode": payload.work_mode,
            "platforms": payload.platforms,
            "remote": payload.remote,
        },
        status="running",
        pending_action=None,
        result=None,
        error=None,
    )

    try:
        result_state = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, job_search_agent_node, state),
            timeout=120,
        )
    except TimeoutError:
        logger.error("Job search run %s timed out", payload.run_id)
        result_state = {**state, "status": "failed", "error": "Job search timed out"}

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(AgentRun).where(AgentRun.id == uuid.UUID(payload.run_id)))
        run = res.scalar_one_or_none()
        if run:
            if result_state["status"] == "failed" and result_state.get("error"):
                logger.warning(
                    "Job search run %s failed: %s",
                    payload.run_id,
                    result_state.get("error"),
                )
            run.status = result_state["status"]
            run.output = (
                result_state.get("result")
                if result_state["status"] == "completed"
                else {"error": CLIENT_SAFE_AGENT_ERROR}
            )
            run.completed_at = datetime.now(UTC)

        # Persistence lives in the node (_persist_saved_jobs: score >= 50,
        # idempotent on user_id+url), so the worker never double-writes —
        # not even when the activity is retried. The result carries saved_count.
        await db.commit()

    if result_state["status"] == "completed":
        emit(payload.run_id, "complete", result_state.get("result") or {})
        saved_count = (result_state.get("result") or {}).get("saved_count", 0)
        if saved_count:
            from app.workflows.starters import start_notification

            # Fire-and-forget on its own workflow: a Temporal-connectivity or
            # notification-layer failure here must never fail (and retry)
            # this activity — that would risk creating a duplicate follow-up
            # draft elsewhere. See NotificationWorkflow for why.
            try:
                await start_notification(
                    uuid.UUID(payload.user_id),
                    type="job_matches",
                    title=(
                        f"Job Agent found {saved_count} new match{'es' if saved_count != 1 else ''}"
                    ),
                    body=f'Search: "{payload.search_query}" in {payload.location}',
                    link="/jobs",
                )
            except Exception:
                logger.warning(
                    "Failed to start notification workflow for run %s",
                    payload.run_id,
                    exc_info=True,
                )
    elif result_state["status"] == "awaiting_approval":
        emit(payload.run_id, "checkpoint", result_state.get("pending_action") or {})
    else:
        emit(payload.run_id, "error", CLIENT_SAFE_AGENT_ERROR)

    logger.info(
        "Job search run %s finished with status %s",
        payload.run_id,
        result_state["status"],
    )
    return {"status": result_state["status"], "run_id": payload.run_id}


# Only applications still waiting on the employer get a follow-up.
FOLLOWUP_STATUSES = {"applied", "viewed"}


class FollowupTrigger(BaseModel):
    user_id: str
    application_id: str
    day: int


async def run_followup(
    payload: FollowupTrigger,
):
    """Draft one due follow-up (FollowupWorkflow decides when). This never
    sends email directly:

    1. If the recruiter already replied, cancel the remaining schedule.
    2. Otherwise draft a follow-up message and land it as an AgentRun
       awaiting_approval — the same checkpoint ledger every other agent
       uses. A human must approve via /agents/{run_id}/approve or
       /email/approve/{run_id}, both of which route the actual send
       through workflow_service.send_approved_email — never a second,
       ad-hoc Gmail call from here.
    """
    from sqlalchemy import select

    from app.agents.followup_agent import build_followup_draft
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun, JobApplication

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(JobApplication)
            .where(JobApplication.id == uuid.UUID(payload.application_id))
            .with_for_update()
        )
        application = res.scalar_one_or_none()
        if not application:
            logger.warning("Follow-up: application %s not found", payload.application_id)
            return {"status": "not_found", "application_id": payload.application_id}

        # Once the process has moved on (interview, offer, rejection) a
        # "just checking in" email is wrong — stop the remaining schedule.
        if application.status not in FOLLOWUP_STATUSES:
            logger.info(
                "Follow-up day-%d skipped for application %s — status is %s",
                payload.day,
                payload.application_id,
                application.status,
            )
            return {
                "status": "cancelled",
                "reason": f"application_{application.status}",
                "application_id": payload.application_id,
            }

        # Auto-cancel: check if recruiter already replied before drafting.
        if await _has_recruiter_replied(
            db,
            payload.user_id,
            application.company,
            application.role,
            application.applied_at,
        ):
            logger.info(
                "Follow-up day-%d CANCELLED for application %s — recruiter already replied",
                payload.day,
                payload.application_id,
            )
            application.followup_day5 = None
            application.followup_day12 = None
            await db.commit()
            return {
                "status": "cancelled",
                "reason": "recruiter_replied",
                "application_id": payload.application_id,
            }

        from app.services.email_finder_service import find_recruiter_email

        # A retried activity (timeout, lost worker) must not draft a second
        # email for the same application and day.
        existing = (
            (
                await db.execute(
                    select(AgentRun).where(
                        AgentRun.user_id == uuid.UUID(payload.user_id),
                        AgentRun.agent_type == "followup",
                        AgentRun.input["application_id"].astext == payload.application_id,
                        AgentRun.input["day"].astext == str(payload.day),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return {
                "status": existing.status,
                "run_id": str(existing.id),
                "day": payload.day,
                "application_id": payload.application_id,
            }

        try:
            recruiter = await find_recruiter_email(application.company)
        except Exception as exc:
            logger.warning("Recruiter email lookup failed for %s: %s", application.company, exc)
            recruiter = None
        recipient = (recruiter or {}).get("email") or ""

        draft = await asyncio.get_running_loop().run_in_executor(
            None,
            build_followup_draft,
            payload.user_id,
            application.company,
            application.role,
            application.applied_at,
            payload.day,
        )

        run = AgentRun(
            id=uuid.uuid4(),
            user_id=uuid.UUID(payload.user_id),
            agent_type="followup",
            status="awaiting_approval",
            input={"application_id": payload.application_id, "day": payload.day},
            output={
                "type": "send_email",
                "recipient": recipient,
                "subject": draft["subject"],
                "body": draft["body"],
                "application_id": payload.application_id,
                "day": payload.day,
            },
        )
        db.add(run)
        await db.commit()

    emit(str(run.id), "checkpoint", run.output)

    from app.workflows.starters import start_notification

    # Same reasoning as run_job_search above: this must never retry the
    # follow-up draft above just because the notification layer failed.
    try:
        await start_notification(
            uuid.UUID(payload.user_id),
            type="followup_ready",
            title="Follow-up email draft ready for review",
            body=f"{application.company} — {application.role}",
            link=f"/agents/{run.id}",
        )
    except Exception:
        logger.warning(
            "Failed to start notification workflow for application %s",
            payload.application_id,
            exc_info=True,
        )

    logger.info(
        "Follow-up day-%d drafted for application %s user %s — awaiting approval (run %s)",
        payload.day,
        payload.application_id,
        payload.user_id,
        run.id,
    )
    return {
        "status": "awaiting_approval",
        "run_id": str(run.id),
        "day": payload.day,
        "application_id": payload.application_id,
    }


async def _has_recruiter_replied(
    db,
    user_id: str,
    company: str,
    role: str,
    applied_at,
    window_days: int = 30,
) -> bool:
    """Check Gmail for recruiter replies since application was submitted.

    Searches Gmail for threads mentioning the company or role since applied_at.
    If any thread has a reply from someone who is NOT the user → recruiter replied.
    """
    from sqlalchemy import select

    from app.models.db import User
    from app.services.gmail_service import GmailMCPClient

    user_res = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = user_res.scalar_one_or_none()
    if not user or not user.email:
        return False

    user_email = user.email
    gmail = GmailMCPClient(user_id)
    since_clause = (
        f"after:{applied_at.strftime('%Y/%m/%d')}" if applied_at else f"newer_than:{window_days}d"
    )

    queries = []
    if company:
        queries.append(f"{company} {since_clause}")
    if role:
        queries.append(f'"{role}" {since_clause}')

    for query in queries:
        try:
            threads = await asyncio.to_thread(gmail.search_threads, query, 5)
        except Exception as exc:
            logger.debug("Gmail recruiter-reply search failed for query '%s': %s", query, exc)
            continue
        if not isinstance(threads, list):
            continue
        for thread in threads:
            thread_id = thread.get("threadId") or thread.get("id")
            if not thread_id:
                continue
            try:
                details = await asyncio.to_thread(gmail.get_thread, thread_id)
            except Exception as exc:
                logger.debug("Gmail thread fetch failed for %s: %s", thread_id, exc)
                continue
            messages = details.get("messages", details.get("Messages", []))
            for msg in reversed(messages):
                headers = msg.get("payload", {}).get("headers", [])
                from_addr = ""
                for h in headers:
                    if h.get("name", "").lower() == "from":
                        from_addr = h.get("value", "").lower()
                        break
                if (
                    from_addr
                    and user_email.lower() not in from_addr
                    and not _automated_sender(from_addr)
                ):
                    logger.info(
                        "Found recruiter reply in thread %s from %s for user %s",
                        thread_id,
                        from_addr,
                        user_id,
                    )
                    return True

    return False


_AUTOMATED_SENDERS = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "notifications@",
    "notification@",
    "greenhouse-mail.io",
    "hire.lever.co",
    "myworkday.com",
    "smartrecruiters.com",
    "workablemail.com",
    "icims.com",
    "ashbyhq.com",
    "linkedin.com",
    "naukri.com",
)


def _automated_sender(from_addr: str) -> bool:
    """Auto-confirmations from job boards and ATSs are not a recruiter's reply."""
    return any(token in from_addr for token in _AUTOMATED_SENDERS)


class StatusCheckTrigger(BaseModel):
    user_id: str


def _daily_search_eligible_ids():
    """Members who opted into the daily search, have an active model and saved
    target roles, and have not asked for their account to be deleted."""
    from sqlalchemy import select

    from app.models.db import User as UserModel
    from app.models.db import UserModelSettings, UserPreferences

    return (
        select(UserModel.id)
        .join(UserModelSettings, UserModelSettings.user_id == UserModel.id)
        .join(UserPreferences, UserPreferences.user_id == UserModel.id)
        .where(
            UserModelSettings.is_active == True,  # noqa: E712
            UserPreferences.daily_search_enabled == True,  # noqa: E712
            UserPreferences.target_roles.is_not(None),
            UserModel.deletion_scheduled_for.is_(None),
        )
        .distinct()
    )


async def list_daily_search_users() -> list[str]:
    """User ids the scheduled daily search fans out to, one child each."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = await db.execute(_daily_search_eligible_ids())
        return [str(user_id) for user_id in rows.scalars().all()]


async def daily_search(payload: StatusCheckTrigger):
    """Daily automated job search based on user preferences.

    Fetches user preferences from memory, searches all platforms + Google Jobs,
    scores matches, and saves top results as applications.

    Honors per-user opt-in: only members who turned on the daily search in
    Settings, have an active LLM model and saved target roles, and are not
    pending deletion get one. The Temporal schedule starts one child
    workflow per member with an explicit user_id; "all" runs every eligible
    member in this one call and is kept for manual runs.
    """
    from sqlalchemy import select

    from app.agents.memory.manager import MemoryManager
    from app.core.database import AsyncSessionLocal
    from app.core.model_router import get_llm
    from app.models.db import User as UserModel
    from app.models.db import UserPreferences
    from app.services.indian_platforms_service import search_google_jobs
    from app.services.job_platforms_service import scrape_all_platforms

    jobs_found = 0
    applications_queued = 0
    users_searched = 0
    visible_browser_opted_in = 0  # how many users have prefer_live_browser=True

    async with AsyncSessionLocal() as db:
        query = select(UserModel).where(UserModel.id.in_(_daily_search_eligible_ids()))
        if payload.user_id != "all":
            query = query.where(UserModel.id == uuid.UUID(payload.user_id))
        users = (await db.execute(query)).scalars().all()

        for user in users:
            try:
                user_id = str(user.id)
                llm = await get_llm(user_id, db)
                if llm is None:
                    logger.debug("Skipping daily search for %s: no active model", user_id)
                    continue

                # Read per-user preference so we can log opted-in users and
                # potentially trigger visible prepare-apply for them in a future
                # iteration.  Right now we just report the count.
                prefs_row = (
                    await db.execute(
                        select(UserPreferences).where(UserPreferences.user_id == user.id)
                    )
                ).scalar_one_or_none()
                if prefs_row and getattr(prefs_row, "prefer_live_browser", False):
                    visible_browser_opted_in += 1
                    logger.info(
                        "Daily search: user %s opted into live browser — saved jobs "
                        "will open a visible Chromium when the user clicks Apply.",
                        user_id,
                    )

                # Get user preferences from memory
                mgr = MemoryManager(db_url=settings.DATABASE_URL, redis_url=settings.REDIS_URL)
                await mgr.initialize()
                user_ctx = await mgr.get_user_context(user_id)
                await mgr.close()

                search_term = user_ctx.get("target_roles") or (prefs_row and prefs_row.target_roles)
                location = user_ctx.get("preferred_locations") or (
                    prefs_row and prefs_row.preferred_locations
                )
                if isinstance(search_term, list):
                    search_term = search_term[0] if search_term else None
                if isinstance(location, list):
                    location = location[0] if location else None
                # No invented defaults: search only for what the member saved.
                location = location or (
                    "Remote" if prefs_row and prefs_row.work_mode == "remote" else None
                )
                if not search_term or not location:
                    logger.debug("Skipping daily search for %s: missing prefs", user_id)
                    continue

                users_searched += 1

                # Search all platforms
                all_jobs = await scrape_all_platforms(
                    search_term=search_term,
                    location=location,
                    results_wanted=15,
                    include_indian=True,
                    llm=llm,
                    user_id=user_id,
                )

                # Also search Google Jobs for company-only postings
                google_jobs = await search_google_jobs(
                    llm=llm,
                    user_id=user_id,
                    search_term=search_term,
                    location=location,
                    results_wanted=10,
                )
                all_jobs.extend(google_jobs)
                jobs_found += len(all_jobs)

                # Normalize once: shared with other users' searches via the
                # catalog (never fails the run) and scored for this member.
                from app.agents.job_search import _persist_saved_jobs
                from app.services.job_catalog import write_through
                from app.services.job_matching import rank_jobs
                from app.services.job_search_service import _normalize

                found = [
                    _normalize(
                        {
                            "url": j.job_url,
                            "title": j.title,
                            "company": j.company,
                            "location": j.location,
                            "description": j.description,
                            "posted_at": j.date_posted,
                            "salary_text": j.salary or "",
                        },
                        "scheduled",
                    )
                    for j in all_jobs
                    if j.job_url
                ]
                await write_through(found, "scheduled")

                # Same scoring and save rule as an interactive search: every
                # match >= SAVE_MIN_SCORE becomes a saved application.
                matches, _ = await rank_jobs(
                    user_id, {"titles": [search_term], "location": location}, found
                )
                applications_queued += await asyncio.to_thread(
                    _persist_saved_jobs, user_id, matches
                )
            except Exception as exc:
                logger.warning("Daily search failed for user %s: %s", user.id, exc)
                # One member's failed write must not poison the session for
                # everyone after them.
                await db.rollback()

    logger.info(
        "Daily search complete: %d users searched (%d opted into live browser), "
        "%d jobs found, %d queued",
        users_searched,
        visible_browser_opted_in,
        jobs_found,
        applications_queued,
    )
    return {
        "status": "ok",
        "users_searched": users_searched,
        "visible_browser_opted_in": visible_browser_opted_in,
        "jobs_found": jobs_found,
        "applications_queued": applications_queued,
    }
