"""Moves job applications forward from replies found in the member's inbox.

The email monitor classifies mail; this module decides what that means for a
member's applications and records it. Rules:

- A Gmail message is acted on once (unique per member and message id).
- Status only moves forward (applied, viewed, interview), or to rejected from
  any of those. An offer or a rejection is never overwritten by later mail.
- Mail is matched to an application only when exactly one of the member's
  open applications fits its company; otherwise it is recorded unmatched and
  nothing changes.
- Mail changes a status on its own only when its sender's domain belongs to
  the company or to a known job platform. Anyone can write "we are pleased to
  offer you"; mail from anywhere else is recorded and the member is told to
  check it, but nothing moves.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import AsyncSessionLocal
from app.models.db import ApplicationStatusEvent, JobApplication

logger = logging.getLogger(__name__)

# Applications still waiting on a reply. "saved" jobs were never applied to,
# so mail from that company says nothing about them.
OPEN_STATUSES = ("applied", "viewed", "interview")
_RANK = {"applied": 1, "viewed": 2, "interview": 3}
_CATEGORY_STATUS = {
    "VIEWED": "viewed",
    "SHORTLISTED": "viewed",
    "INTERVIEW": "interview",
    "REJECTED": "rejected",
}
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|technologies|technology|"
    r"solutions|labs|india)\b\.?",
    re.IGNORECASE,
)


# Registrable domains of applicant-tracking systems and job boards that send
# status mail on a company's behalf.
_PLATFORM_DOMAINS = frozenset(
    {
        "greenhouse.io", "greenhouse-mail.io", "lever.co", "myworkdayjobs.com",
        "myworkday.com", "workday.com", "ashbyhq.com", "smartrecruiters.com",
        "icims.com", "workable.com", "jobvite.com", "taleo.net", "breezy.hr",
        "recruitee.com", "teamtailor.com", "bamboohr.com", "linkedin.com",
        "indeed.com", "naukri.com", "glassdoor.com", "wellfound.com", "instahyre.com",
    }
)  # fmt: skip
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})
_MAIL_ROLE_WORDS = ("careers", "jobs", "hr", "recruiting", "recruitment", "talent", "hiring")
_ADDRESS = re.compile(r"[\w.+'-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def sender_domain(sender: str) -> str:
    """Registrable domain of the address in a From header, or ""."""
    found = _ADDRESS.findall(str(sender or ""))
    if not found:
        return ""
    labels = found[-1].lower().strip(".").split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def sender_is_trusted(application: JobApplication, sender: str) -> bool:
    """The sender's domain is the company's own or a known job platform."""
    domain = sender_domain(sender)
    if not domain:
        return False
    if domain in _PLATFORM_DOMAINS:
        return True
    company = normalize_company(application.company or "").replace(" ", "")
    if len(company) < 3:
        return False
    name = domain.split(".")[0].replace("-", "")
    return name == company or any(
        name in (company + word, word + company) for word in _MAIL_ROLE_WORDS
    )


def next_status(current: str, category: str) -> str | None:
    """The status an email of this category moves an application to, or None
    when it should not move (no mapping, not an open application, or not
    forward)."""
    target = _CATEGORY_STATUS.get(category)
    if target is None or current not in _RANK:
        return None
    if target == "rejected":
        return target
    return target if _RANK[target] > _RANK[current] else None


def normalize_company(name: str) -> str:
    cleaned = _COMPANY_SUFFIX.sub(" ", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def match_application(applications: list[JobApplication], update: dict) -> JobApplication | None:
    """The one open application the email is about, else None.

    The company counts as mentioned when its normalized name appears in the
    email's company, sender or subject. Two open applications at the same
    company are ambiguous (which role replied?), so they match nothing.
    """
    haystack = " ".join(
        normalize_company(str(update.get(key) or "")) for key in ("company", "sender", "subject")
    )
    haystack = f" {haystack} "
    hits = []
    for application in applications:
        company = normalize_company(application.company or "")
        if len(company) >= 3 and f" {company} " in haystack:
            hits.append(application)
    return hits[0] if len(hits) == 1 else None


async def processed_message_ids(user_id: str, message_ids: list[str]) -> set[str]:
    """Which of these Gmail messages were already acted on."""
    if not message_ids:
        return set()
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(ApplicationStatusEvent.gmail_message_id).where(
                ApplicationStatusEvent.user_id == uuid.UUID(user_id),
                ApplicationStatusEvent.gmail_message_id.in_(message_ids),
            )
        )
        return set(rows.scalars().all())


async def apply_inbox_updates(user_id: str, updates: list[dict]) -> list[dict]:
    """Record each classified email and advance the application it belongs to.

    Returns the changes made: {application_id, company, role, from_status,
    to_status, subject}. Safe to repeat: a message already recorded is skipped.
    """
    owner = uuid.UUID(user_id)
    changes: list[dict] = []
    unverified: list[tuple] = []
    async with AsyncSessionLocal() as db:
        applications = list(
            (
                await db.execute(
                    select(JobApplication)
                    .where(
                        JobApplication.user_id == owner,
                        JobApplication.status.in_(OPEN_STATUSES),
                    )
                    .order_by(JobApplication.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for update in updates:
            message_id = update.get("message_id")
            if not message_id:
                continue
            application = match_application(applications, update)
            previous = application.status if application else None
            new_status = next_status(previous, update["category"]) if application else None
            suggested = None
            if new_status and not sender_is_trusted(application, update.get("sender", "")):
                suggested, new_status = new_status, None
            inserted = await db.execute(
                pg_insert(ApplicationStatusEvent)
                .values(
                    id=uuid.uuid4(),
                    user_id=owner,
                    job_application_id=application.id if application else None,
                    gmail_message_id=message_id,
                    category=update["category"],
                    company=(update.get("company") or None),
                    subject=(update.get("subject") or "")[:200] or None,
                    previous_status=previous,
                    new_status=new_status,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "gmail_message_id"])
                .returning(ApplicationStatusEvent.id)
            )
            if inserted.scalar() is None:
                continue  # seen on an earlier scan
            if suggested:
                unverified.append((application.company, application.role, suggested))
            if application and new_status:
                application.status = new_status
                changes.append(
                    {
                        "application_id": str(application.id),
                        "company": application.company,
                        "role": application.role,
                        "from_status": previous,
                        "to_status": new_status,
                        "subject": update.get("subject"),
                    }
                )
        await db.commit()
    for company, role, suggested in unverified:
        await _suggest_status_check(owner, company, role, suggested)
    if changes:
        logger.info("Inbox moved %d application(s) for user %s", len(changes), user_id)
    return changes


async def _suggest_status_check(owner: uuid.UUID, company, role, suggested: str) -> None:
    """Tell the member about mail that looks like an update but came from a
    sender we cannot tie to the company. Best effort."""
    from app.workflows.starters import start_notification

    where = f"{role} at {company}" if company and role else (company or "an application")
    try:
        await start_notification(
            owner,
            type="application_status_suggestion",
            title=f"Unverified email about {where}",
            body=(
                f"An email suggests this application is now '{suggested}', but it did not "
                "come from the company or a known job platform, so nothing was changed. "
                "Check your inbox and update the status yourself if it is genuine."
            ),
            link="/applications",
        )
    except Exception:
        logger.warning("Could not start status-suggestion notification", exc_info=True)


def inbox_tracking_member_ids():
    """Members who opted into inbox tracking, have Gmail connected and an
    active model, and have not asked for their account to be deleted."""
    from app.models.db import IntegrationConnection, UserModelSettings, UserPreferences
    from app.models.db import User as UserModel

    return (
        select(UserModel.id)
        .join(UserPreferences, UserPreferences.user_id == UserModel.id)
        .join(UserModelSettings, UserModelSettings.user_id == UserModel.id)
        .join(IntegrationConnection, IntegrationConnection.user_id == UserModel.id)
        .where(
            UserPreferences.inbox_tracking_enabled == True,  # noqa: E712
            UserModelSettings.is_active == True,  # noqa: E712
            IntegrationConnection.provider == "gmail",
            IntegrationConnection.status == "connected",
            UserModel.deletion_scheduled_for.is_(None),
        )
        .distinct()
    )


async def list_inbox_tracking_users() -> list[str]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(inbox_tracking_member_ids())
        return [str(user_id) for user_id in rows.scalars().all()]


def scan_inbox_for_member(user_id: str) -> dict:
    """One member's scan, run on a worker thread (the monitor is synchronous)."""
    from app.agents.email_monitor_agent import email_monitor_node

    state = {
        "user_id": user_id,
        "run_id": "",
        "task_type": "email_monitor",
        "context": {},
        "messages": [],
        "status": "running",
        "pending_action": None,
        "result": None,
        "error": None,
    }
    final = email_monitor_node(state)  # type: ignore[arg-type]
    return {"status": final.get("status"), "result": final.get("result")}
