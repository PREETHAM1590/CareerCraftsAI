"""The member's auto-apply rule: new saved job >= their match threshold ->
apply / email outreach / both / notify only.

Apply tailors a resume (per the member's resume preferences), checks it against
their own documents, attaches it and hands the application to the browser
extension; the member reviews and submits. Outreach only queues a draft in the
review queue (/outreach). Limits (daily cap, pacing, score) are enforced where
the attempt is reserved, not here. Every action is written to action_log with
source='rule', and that log is what stops a job firing again on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.db import (
    ActionLog,
    ApplicationAttempt,
    ExtensionDevice,
    JobApplication,
    User,
    UserDocument,
    UserModelSettings,
    UserPreferences,
)
from app.services import apply_limits, india_focus

logger = logging.getLogger(__name__)

RULE_ACTIONS = {
    "apply": ("auto_apply",),
    "outreach": ("outreach_queued",),
    "both": ("auto_apply", "outreach_queued"),
    "notify": ("notify",),
}
NOTIFY_LIMIT = 50


def pick_candidates(applications: list[JobApplication], room: int, minimum: int) -> list:
    """Highest-scoring saved jobs with a real URL, at most `room` of them."""
    eligible = [
        a
        for a in applications
        if a.job_url
        and not a.job_url.startswith("https://example.com")
        and apply_limits.score_error(a.match_score, minimum) is None
    ]
    eligible.sort(key=lambda a: a.match_score or 0, reverse=True)
    return eligible[: max(room, 0)]


async def list_auto_apply_users() -> list[str]:
    """Members with the rule on and an active model. Apply and both also need a
    paired browser extension; outreach and notify do not."""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(User.id)
            .join(UserPreferences, UserPreferences.user_id == User.id)
            .join(UserModelSettings, UserModelSettings.user_id == User.id)
            .outerjoin(
                ExtensionDevice,
                (ExtensionDevice.user_id == User.id) & ExtensionDevice.revoked_at.is_(None),
            )
            .where(
                UserPreferences.auto_apply_enabled == True,  # noqa: E712
                UserModelSettings.is_active == True,  # noqa: E712
                User.deletion_scheduled_for.is_(None),
                or_(
                    UserPreferences.auto_rule_action.in_(("outreach", "notify")),
                    ExtensionDevice.id.is_not(None),
                ),
            )
            .distinct()
        )
        return [str(user_id) for user_id in rows.scalars().all()]


async def load_rule(user_id: uuid.UUID) -> dict | None:
    async with AsyncSessionLocal() as db:
        prefs = (
            await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
        ).scalar_one_or_none()
    if prefs is None or not prefs.auto_apply_enabled:
        return None
    return {
        "min_match": prefs.auto_rule_min_match,
        "action": (prefs.auto_rule_action if prefs.auto_rule_action in RULE_ACTIONS else "apply"),
        "template": prefs.resume_template or "modern",
        "page_target": prefs.resume_page_target,
        "tailor": prefs.resume_tailor_per_job,
        "tone": prefs.resume_tone,
        # "New" jobs: found after the rule was last switched on, never the backlog.
        "since": prefs.auto_rule_enabled_at,
        "india_focus": prefs.india_focus is not False,
    }


def plan(
    saved: list[JobApplication],
    logged: set[tuple[uuid.UUID, str]],
    attempted: set[uuid.UUID],
    action: str,
    minimum: int,
    room: int,
    india: bool = True,
) -> dict[str, list[JobApplication]]:
    """Per rule step, the jobs still to handle: at/above the threshold, not
    already logged for that step (apply also not already attempted). With
    India focus on, roles open only outside India are never queued."""
    out: dict[str, list[JobApplication]] = {}
    saved, _ = india_focus.eligible_listings(saved, india_focus=india)
    for step in RULE_ACTIONS[action]:
        pool = [
            a
            for a in saved
            if (a.id, step) not in logged and (step != "auto_apply" or a.id not in attempted)
        ]
        if step == "auto_apply":  # the reservation enforces its own floor, so match it
            out[step] = pick_candidates(pool, room, max(minimum, settings.AUTO_APPLY_MIN_SCORE))
        else:
            limit = NOTIFY_LIMIT if step == "notify" else settings.AUTO_APPLY_QUEUE_BATCH
            out[step] = pick_candidates(pool, limit, minimum)
    return out


async def _candidates(
    user_id: uuid.UUID, now: datetime, rule: dict
) -> dict[str, list[JobApplication]]:
    async with AsyncSessionLocal() as db:
        started = (
            await db.execute(
                select(func.count()).where(
                    ApplicationAttempt.user_id == user_id,
                    ApplicationAttempt.created_at > now - timedelta(hours=24),
                )
            )
        ).scalar_one()
        room = min(settings.APPLY_DAILY_CAP - started, settings.AUTO_APPLY_QUEUE_BATCH)
        saved = list(
            (
                await db.execute(
                    select(JobApplication).where(
                        JobApplication.user_id == user_id,
                        JobApplication.status == "saved",
                        JobApplication.match_score >= rule["min_match"],
                        JobApplication.found_at >= (rule["since"] or now),
                    )
                )
            )
            .scalars()
            .all()
        )
        ids = [a.id for a in saved]
        logged = {
            (r.job_application_id, r.action)
            for r in (
                await db.execute(
                    select(ActionLog.job_application_id, ActionLog.action).where(
                        ActionLog.user_id == user_id,
                        ActionLog.source == "rule",
                        ActionLog.job_application_id.in_(ids),
                    )
                )
            ).all()
        }
        attempted = set(
            (
                await db.execute(
                    select(ApplicationAttempt.job_application_id).where(
                        ApplicationAttempt.user_id == user_id,
                        ApplicationAttempt.job_application_id.in_(ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    return plan(
        saved,
        logged,
        attempted,
        rule["action"],
        rule["min_match"],
        room,
        india=rule.get("india_focus", True),
    )


async def _log(user_id: uuid.UUID, application_id: uuid.UUID, action: str, detail: dict) -> None:
    async with AsyncSessionLocal() as db:
        db.add(
            ActionLog(
                user_id=user_id,
                job_application_id=application_id,
                action=action,
                source="rule",
                detail=detail,
            )
        )
        await db.commit()


async def _tailor(user_id: str, application: JobApplication, rule: dict) -> dict:
    """Reuse durable, audited agent execution; a retried rule reuses its run.

    Only read the draft checkpoint. This never approves an application or sends
    anything. The extension still presents the final application for review.
    """
    from app.api.v1.run_utils import queue_agent_run
    from app.models.db import AgentRun

    owner = uuid.UUID(user_id)
    generation = str(rule.get("since") or "legacy")
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.id == owner).with_for_update())
        ).scalar_one()
        if user.policy_accepted_at is None or user.deletion_scheduled_for is not None:
            raise ValueError("Account is not eligible for scheduled agent work")
        run = (
            await db.execute(
                select(AgentRun)
                .where(
                    AgentRun.user_id == owner,
                    AgentRun.agent_type == "resume_optimize",
                    AgentRun.input["context"]["rule_application_id"].astext == str(application.id),
                    AgentRun.input["context"]["rule_generation"].astext == generation,
                )
                .order_by(AgentRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            run_id = await queue_agent_run(
                db,
                user,
                "resume_optimize",
                {
                    "jd_text": (
                        application.jd_text or f"{application.role} at {application.company}"
                    )[:3000],
                    "template": rule["template"],
                    "page_target": rule["page_target"],
                    "tone": rule["tone"],
                    "rule_application_id": str(application.id),
                    "rule_generation": generation,
                },
            )
        else:
            run_id = str(run.id)
            if run.status in {"queued", "running"}:
                from app.workflows.starters import start_agent_run

                # Recover the commit-before-start crash window. The starter
                # uses this run's stable workflow id and is idempotent.
                await start_agent_run(run_id, owner)
    # SQL is the user-facing run record. Temporal owns execution and survives
    # this polling activity timing out or its worker restarting.
    while True:
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, uuid.UUID(run_id))
            if run is None or run.status in {"failed", "cancelled", "expired"}:
                return {}
            if run.status in {"awaiting_approval", "completed"}:
                return run.output or {}
        await asyncio.sleep(1)


async def _active_resume_id(owner: uuid.UUID) -> uuid.UUID | None:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(UserDocument.id)
                .where(UserDocument.user_id == owner, UserDocument.doc_type == "resume")
                .order_by(
                    UserDocument.is_primary.desc(),
                    UserDocument.embedded_at.desc().nulls_last(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()


async def _apply_one(owner: uuid.UUID, application: JobApplication, rule: dict) -> bool:
    """Attach a resume and start the extension review flow. False = skipped."""
    from app.workflows.starters import start_auto_apply

    if rule["tailor"]:
        draft = await asyncio.wait_for(_tailor(str(owner), application, rule), timeout=300)
        document_id = draft.get("pdf_document_id")
        if not document_id or (draft.get("grounding") or {}).get("unsupported"):
            return False
        resume_id = uuid.UUID(document_id)
    else:
        resume_id = await _active_resume_id(owner)
        if resume_id is None:
            return False
    async with AsyncSessionLocal() as db:
        row = await db.get(JobApplication, application.id)
        if row is None or row.status != "saved":
            return False
        row.resume_id = resume_id
        await db.commit()
    application.resume_id = resume_id
    await start_auto_apply(owner, application.id, auto=True)
    return True


async def draft_outreach(user_id: str, application: JobApplication) -> str | None:
    """Prepare one draft under the shared admission and audit contract.

    This job-specific generator has different inputs from the thread-based
    email agent. Keep its semantics, but reserve capacity before any work and
    record every outcome. Sending still requires explicit payload approval.
    """
    import time

    import anyio

    from app.agents.auto_apply_pipeline import _generate_cold_email
    from app.api.v1.run_utils import check_run_admission
    from app.core.model_router import (
        begin_token_tracking,
        build_agent_llm,
        get_and_reset_tokens,
    )
    from app.core.sync_db import (
        fetch_model_settings,
        fetch_user_profile_text,
        run_choice,
    )
    from app.models.db import AgentRun
    from app.services.outreach_service import queue_outreach
    from app.services.recruiter_email import employer_domain, find_recruiter_contact

    owner = uuid.UUID(user_id)
    if application.user_id != owner:
        raise ValueError("Application does not belong to this account")
    run_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.id == owner).with_for_update())
        ).scalar_one()
        if user.policy_accepted_at is None or user.deletion_scheduled_for is not None:
            raise ValueError("Account is not eligible for outreach drafts")
        await check_run_admission(db, user)
        db.add(
            AgentRun(
                id=run_id,
                user_id=owner,
                agent_type="email",
                status="running",
                tokens_used=0,
                input={
                    "task_type": "email",
                    "source": "application_outreach",
                    "context": {
                        "application_id": str(application.id),
                        "resume_document_id": (
                            str(application.resume_id) if application.resume_id else None
                        ),
                    },
                },
            )
        )
        await db.commit()

    started = time.monotonic()
    status, output, tokens = "failed", {"error": "Outreach draft failed"}, 0
    generation = None
    try:
        jd = application.jd_text or ""
        domain = employer_domain(application.job_url, jd)
        contact = (
            await find_recruiter_contact(
                application.company,
                domain=domain,
                domain_confirmed=bool(domain),
                posting_text=jd,
            )
        ).best
        if not contact:
            status, output = "completed", {"state": "no_contact"}
            return None

        def generate():
            # All sync lookups and callbacks run in the same worker context.
            with run_choice(
                {
                    "resume_document_id": (
                        str(application.resume_id) if application.resume_id else None
                    )
                }
            ):
                begin_token_tracking()
                model_settings = fetch_model_settings(user_id)
                if not model_settings:
                    return None, 0
                email = _generate_cold_email(
                    build_agent_llm(model_settings),
                    contact.name or "Hiring Manager",
                    contact.email,
                    application.company,
                    application.role,
                    jd,
                    fetch_user_profile_text(user_id),
                )
                return email, get_and_reset_tokens()

        generation = asyncio.create_task(asyncio.to_thread(generate))
        # Cancellation must not release the slot while its provider thread is
        # still executing. The bounded gateway call is awaited in finally.
        email, tokens = await asyncio.shield(generation)
        if not email:
            return None
        queued = await queue_outreach(
            user_id,
            company=application.company,
            role=application.role,
            to_email=contact.email,
            verdict=contact.verdict,
            email_source=contact.source,
            verified_by=contact.verified_by,
            subject=email["subject"],
            body=email["body"],
            job_application_id=str(application.id),
            resume_document_id=(str(application.resume_id) if application.resume_id else None),
        )
        status = "completed"
        output = (
            {"state": queued.state, "outreach_id": str(queued.id)}
            if queued
            else {"state": "duplicate"}
        )
        return queued.state if queued else None
    finally:
        with anyio.CancelScope(shield=True):
            if generation is not None and not generation.done():
                try:
                    _, tokens = await generation
                except Exception as exc:
                    logger.warning("Outreach generation interrupted: %s", type(exc).__name__)
            async with AsyncSessionLocal() as db:
                run = await db.get(AgentRun, run_id)
                if run is not None and run.user_id == owner:
                    run.status, run.output, run.tokens_used = status, output, tokens
                    run.duration_ms = int((time.monotonic() - started) * 1000)
                    run.completed_at = datetime.now(UTC)
                    await db.commit()


async def _notify(owner: uuid.UUID, jobs: list[JobApplication], minimum: int) -> None:
    from app.workflows.starters import start_notification

    # Same path as every other notification: the workflow persists it and emails.
    # The dedupe key makes a retried tick a no-op instead of a second notice.
    await start_notification(
        owner,
        "job_matches",
        f"{len(jobs)} new matches ≥ {minimum}%",
        "Saved jobs that meet your auto-apply rule.",
        f"/applications?min={minimum}&sort=match_desc",
        dedupe_key=f"rule-notify:{owner}:{min(str(j.id) for j in jobs)}:{len(jobs)}",
    )


async def queue_for_member(user_id: str) -> dict:
    """Run the member's rule. One job failing never stops the others; a resume
    with unsupported claims is never used."""
    owner = uuid.UUID(user_id)
    rule = await load_rule(owner)
    if rule is None:
        return {"queued": 0, "skipped": 0}
    queued = skipped = 0
    for step, jobs in (await _candidates(owner, datetime.now(UTC), rule)).items():
        if step == "notify":
            if jobs:
                await _notify(owner, jobs, rule["min_match"])
                for job in jobs:
                    await _log(owner, job.id, step, {"min_match": rule["min_match"]})
                queued += len(jobs)
            continue
        for application in jobs:
            try:
                if step == "auto_apply":
                    done = await _apply_one(owner, application, rule)
                    detail = {"min_match": rule["min_match"]}
                    if done:
                        await _log(owner, application.id, step, detail)
                else:
                    state = await draft_outreach(user_id, application)
                    done = state is not None
                    # Logged either way: a job with no contact must not be looked up every tick.
                    await _log(owner, application.id, step, {"state": state or "no_contact"})
                queued += done
                skipped += not done
            except Exception:
                logger.warning("Auto-apply rule failed for one job", exc_info=True)
                skipped += 1
    return {"queued": queued, "skipped": skipped}
