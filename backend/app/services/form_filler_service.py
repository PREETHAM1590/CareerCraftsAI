from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from langchain_core.language_models import BaseChatModel

from app.core.sync_db import _get_sync_factory, fetch_user_profile_text
from app.services.browser_control_service import BrowserAutomationRetired
from app.services.browser_control_service import (
    run_browser_task_with_captcha_retry as run_browser_task,
)

logger = logging.getLogger(__name__)


@dataclass
class UserFormProfile:
    """All data needed to fill any job application form."""

    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    current_title: str = ""
    experience_level: str = ""
    experience_years: str = ""
    work_mode: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    preferred_locations: list[str] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    bio: str = ""
    resume_text: str = ""
    skills: str = ""
    education: str = ""
    memory_context: str = ""


def build_user_form_profile(user_id: str) -> UserFormProfile:
    """Fetch all user data from DB + memory for form filling."""
    from sqlalchemy import select
    from app.models.db import User, UserPreferences

    factory = _get_sync_factory()
    with factory() as db:
        user_uuid = None
        try:
            user_uuid = UUID(str(user_id))
        except ValueError:
            pass
        criteria = User.clerk_user_id == str(user_id)
        if user_uuid:
            criteria = (User.id == user_uuid) | criteria
        user = db.execute(select(User).where(criteria)).scalars().first()
        prefs = (
            db.execute(select(UserPreferences).where(UserPreferences.user_id == user.id))
            .scalars()
            .first()
            if user
            else None
        )

    resume_text = fetch_user_profile_text(user_id)
    full_name = user.full_name or "" if user else ""
    parts = full_name.split() if full_name else []

    # Map experience level to years
    exp_level = prefs.experience_level or "" if prefs else ""
    experience_years = (
        str(prefs.years_experience) if prefs and prefs.years_experience is not None else ""
    )

    return UserFormProfile(
        full_name=full_name,
        first_name=parts[0] if parts else "",
        last_name=" ".join(parts[1:]) if len(parts) > 1 else "",
        email=user.email or "" if user else "",
        phone=user.phone or "" if user else "",
        linkedin_url=user.linkedin_url or "" if user else "",
        current_title=prefs.current_title or "" if prefs else "",
        experience_level=exp_level,
        experience_years=experience_years,
        work_mode=prefs.work_mode or "remote" if prefs else "remote",
        salary_min=prefs.salary_min if prefs else None,
        salary_max=prefs.salary_max if prefs else None,
        preferred_locations=prefs.preferred_locations or [] if prefs else [],
        target_roles=prefs.target_roles or [] if prefs else [],
        bio=prefs.bio or "" if prefs else "",
        resume_text=resume_text,
    )


def _build_profile_context(profile: UserFormProfile, job_description: str = "") -> str:
    """Build the full context string the LLM uses to answer form questions."""
    salary = ""
    if profile.salary_min and profile.salary_max:
        salary = f"{profile.salary_min} - {profile.salary_max} per annum"
    elif profile.salary_min:
        salary = f"{profile.salary_min}+ per annum"

    return f"""USER PROFILE (use this to fill form fields):

Name: {profile.full_name}
First Name: {profile.first_name}
Last Name: {profile.last_name}
Email: {profile.email}
Phone: {profile.phone}
LinkedIn: {profile.linkedin_url}
Current Title: {profile.current_title}
Experience: {profile.experience_years} years ({profile.experience_level} level)
Work Preference: {profile.work_mode}
Salary Expectation: {salary or 'Negotiable'}
Preferred Locations: {', '.join(profile.preferred_locations) or 'Flexible'}
Target Roles: {', '.join(profile.target_roles) or profile.current_title}

RESUME/BACKGROUND:
{profile.resume_text[:1500]}

BIO:
{profile.bio[:500] if profile.bio else 'See resume above'}

JOB DESCRIPTION:
{job_description[:1000] if job_description else 'Not provided'}"""


async def fill_and_submit_form(
    llm: BaseChatModel,
    user_id: str,
    job_url: str,
    resume_path: str | None = None,
    cover_letter: str = "",
    job_description: str = "",
    live_browser: bool = False,
    submit: bool = False,
    past_learnings: list[str] | None = None,
) -> dict:
    """Navigate to a job URL and use LLM/agent to fill the form with user data.

    past_learnings — optional list of portal-specific learnings from the harness
    (e.g. ["portal:greenhouse:auto_fill_works", "portal:workday:requires_manual"]).
    Injected into the task prompt so the browser agent adapts its approach based
    on what has worked and failed on previous runs for this user.
    """
    profile = build_user_form_profile(user_id)
    context = _build_profile_context(profile, job_description)

    # Build harness learning hint — surfaces portal outcomes to the browser agent
    learning_hint = ""
    if past_learnings:
        relevant = [line for line in past_learnings if "portal:" in line]
        if relevant:
            learning_hint = (
                "\n\nPAST FORM-FILLING EXPERIENCE (from previous runs):\n"
                + "\n".join(
                    f"- {line.replace('portal:', '').replace(':', ' → ')}" for line in relevant[:8]
                )
                + "\nUse this to adapt: if a portal is marked 'requires_manual', stop early and report REQUIRES_MANUAL."
            )

    # Build the browser-use task with full user context so LLM can answer any field
    task = (  # noqa: S608 - browser task text, not SQL.
        f"""You are filling a job application form. You have the candidate's complete profile below.
Use this data to fill EVERY field on the form accurately.
{learning_hint}
{context}

COVER LETTER (use if there's a cover letter field):
{cover_letter[:1000] if cover_letter else 'Write 2-3 compelling sentences about why this candidate fits the role, based on the resume and JD above.'}

TASK:
1. Go to {job_url}
2. Click "Apply" / "Apply Now" / "Submit Application" (whatever the apply button says)
3. For EVERY form field you see:
   - Text inputs: type the correct value from the profile above
   - Dropdowns: select the closest matching option
   - Radio buttons: select the appropriate option
   - Checkboxes: check if applicable
   - File upload: {'upload resume from ' + resume_path if resume_path else 'skip or use pre-uploaded'}
   - Text areas (cover letter, "why this role?"): use the cover letter above or write a compelling answer
4. If the form has multiple pages, fill each page completely then click Next/Continue
5. {"On the final page, click Submit only after confirming all fields." if submit else "On the final page, STOP before clicking final Submit/Send Application and report READY_FOR_REVIEW."}
6. {"Confirm submission was successful" if submit else "Do not submit. The user must review and submit manually."}

IMPORTANT:
- Fill fields using EXACT profile data (don't make up info)
- For "First Name" use: {profile.first_name}
- For "Last Name" use: {profile.last_name}
- For "Email" use: {profile.email}
- For "Phone" use: {profile.phone}
- For experience/years: {profile.experience_years} years
- For salary: {profile.salary_min or 'negotiable'}
- If you encounter CAPTCHA/OTP/video, stop and report 'REQUIRES_MANUAL'
- If account creation is needed, stop and report 'REQUIRES_ACCOUNT_CREATION'."""  # noqa: S608
    )

    try:
        result = await run_browser_task(llm, task, user_id, max_steps=30, live_browser=live_browser)
        if "REQUIRES_ACCOUNT_CREATION" in (result or ""):
            return {"status": "requires_account_creation", "message": result}
        if "REQUIRES_MANUAL" in (result or ""):
            return {"status": "requires_manual", "message": result}
        marker = "SUBMITTED" if submit else "READY_FOR_REVIEW"
        if marker not in (result or ""):
            return {"status": "failed", "message": "Browser outcome could not be verified"}
        status = "applied" if submit else "ready_for_review"
        return {"status": status, "message": result or "Form prepared for review"}
    except BrowserAutomationRetired:
        return {
            "status": "requires_manual",
            "message": (
                "Server-side form filling has moved to the browser extension. "
                "Open the job in the paired extension to fill it, then review and submit yourself."
            ),
        }
    except Exception as exc:
        logger.error("Form filling failed for %s: %s", job_url, exc)
        return {"status": "failed", "message": "Form filling failed"}
