import asyncio
import json
import logging
import re
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.api.v1.job_basis import router as basis_router
from app.core.rate_limit import limiter
from app.models.db import (
    ActionLog,
    AgentRun,
    JobApplication,
    User,
    UserDocument,
    UserPreferences,
)
from app.schemas.jobs import JobSearchQuerySchema
from app.workflows.starters import WorkflowUnavailable

router = APIRouter(prefix="/jobs", tags=["jobs"])

router.include_router(basis_router)
logger = logging.getLogger(__name__)

VALID_STATUSES = {"saved", "applied", "viewed", "interview", "offer", "rejected"}


class JobSearchRequest(JobSearchQuerySchema):
    search_query: str = Field(default="", max_length=200)
    location: str = "Remote"
    max_results: int = Field(10, ge=1, le=25)
    # Default to False: the free keyless job-board APIs (Remotive / Arbeitnow
    # / Jobicy) and JobSpy are fast and return real apply links. live_browser
    # opens a visible Chromium for the demo, but it triggers CAPTCHAs and
    # frequently returns zero jobs. Flip on explicitly via the UI.
    live_browser: bool = False
    work_mode: str | None = None
    experience_level: str | None = None
    years_experience: int | None = Field(None, ge=0, le=60)
    job_type: str | None = None
    target_roles: list[str] | None = Field(None, max_length=20)
    preferred_locations: list[str] | None = Field(None, max_length=20)


def make_job_search_id(
    user_id: str,
    search_query: str,
    location: str,
    max_results: int,
    filters: dict | None = None,
) -> str:
    """Content hash of a search — identical repeat clicks reuse the run
    that is already in flight instead of starting another."""
    import hashlib
    import json

    digest = hashlib.sha256(
        (
            f"{user_id}:{search_query}:{location}:{max_results}:"
            f"{json.dumps(filters or {}, sort_keys=True)}"
        ).encode()
    ).hexdigest()[:16]
    return f"{user_id}:job_search:{digest}"


class JobSearchResponse(BaseModel):
    run_id: str
    queue_job_id: str
    status: str = "queued"
    queued: bool = True


class ApplicationResponse(BaseModel):
    id: uuid.UUID
    company: str
    role: str
    location: str | None
    job_url: str | None
    jd_text: str | None = None
    match_score: int | None
    status: str
    applied_at: datetime | None
    # Real follow-up schedule (day-5 / day-12 nudges), so the tracker can show
    # an actual next-follow-up date instead of leaving the field blank.
    followup_day5: datetime | None = None
    followup_day12: datetime | None = None
    notes: str | None = None
    source: str | None = None
    posted_at: datetime | None = None
    # Which resume went out (file name and a short content id), and how the
    # recruiter email for this application is doing.
    resume_label: str | None = None
    outreach_status: str | None = None
    outreach_to: str | None = None
    found_at: datetime | None = None
    apply_state: str | None = None

    model_config = {"from_attributes": True}


class ApplicationIdsBody(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class ApplyStateBody(BaseModel):
    state: Literal["opened", "applied", "failed"]


class JobDescriptionResponse(BaseModel):
    jd_text: str
    role: str
    company: str
    source: Literal["application", "catalog"]


class JobSearchProfileResponse(BaseModel):
    resume_found: bool
    resume_id: uuid.UUID | None = None
    resume_filename: str | None = None
    role_suggestions: list[str] = []
    skills: list[str] = []
    inferred_years_experience: int | None = None
    inferred_experience_level: str | None = None
    saved_preferences: dict = {}
    search_query_preview: str
    location_preview: str
    work_mode_preview: str | None = None
    missing_fields: list[str] = []
    analysis_notes: list[str] = []


class StatusUpdateBody(BaseModel):
    status: str


class PrepareApplyBody(BaseModel):
    live_browser: bool = False


# Old and new city names both appear in postings; a filter on either matches both.
_CITY_PAIRS = (
    ("bengaluru", "bangalore"),
    ("gurugram", "gurgaon"),
    ("mumbai", "bombay"),
    ("chennai", "madras"),
    ("kolkata", "calcutta"),
)
_CITY_NAMES = {name: pair for pair in _CITY_PAIRS for name in pair}


def _matches_location_filter(app: JobApplication, location_filter: str | None) -> bool:
    if not location_filter:
        return True
    filters = {item.strip().lower() for item in location_filter.split(",") if item.strip()}
    if not filters:
        return True
    location = (app.location or "").lower()
    if not location:
        return False

    mode_filters = filters & {"remote", "hybrid", "onsite"}
    if "remote" in mode_filters and "remote" not in location:
        return False
    if "hybrid" in mode_filters and "hybrid" not in location:
        return False
    if "onsite" in mode_filters and "remote" in location:
        return False

    city_filters = filters - {"remote", "hybrid", "onsite"}
    if city_filters and not any(
        name in location for city in city_filters for name in _CITY_NAMES.get(city, (city,))
    ):
        return False

    return True


def _split_pref_values(value: str | None) -> list[str]:
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


ROLE_HINTS = (
    "frontend engineer",
    "backend engineer",
    "full stack engineer",
    "full-stack developer",
    "software engineer",
    "python developer",
    "react developer",
    "data analyst",
    "data scientist",
    "machine learning engineer",
    "devops engineer",
    "qa engineer",
    "product manager",
    "ui ux designer",
)

SKILL_HINTS = (
    "python",
    "javascript",
    "typescript",
    "react",
    "next.js",
    "node.js",
    "fastapi",
    "django",
    "postgresql",
    "sql",
    "aws",
    "azure",
    "docker",
    "kubernetes",
    "machine learning",
    "data analysis",
    "pandas",
    "tensorflow",
    "figma",
    "product strategy",
)


def _extract_skills_from_resume(resume_text: str | None) -> list[str]:
    text = (resume_text or "").lower()
    found: list[str] = []
    for skill in SKILL_HINTS:
        if skill in text and skill.title().replace("Sql", "SQL") not in found:
            found.append(skill.title().replace("Sql", "SQL").replace("Aws", "AWS"))
    return found[:10]


def _infer_years_experience(resume_text: str | None) -> int | None:
    text = (resume_text or "").lower()
    explicit = [
        int(match.group(1))
        for match in re.finditer(
            r"(\d{1,2})\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:professional\s+)?experience",
            text,
        )
    ]
    if explicit:
        return max(explicit)
    if any(word in text for word in ("fresher", "new graduate", "recent graduate", "student")):
        return 0
    date_years = [int(y) for y in re.findall(r"\b(20\d{2}|19\d{2})\b", text)]
    current_year = datetime.now(UTC).year
    plausible_starts = [year for year in date_years if 1990 <= year <= current_year]
    if plausible_starts:
        oldest = min(plausible_starts)
        inferred = current_year - oldest
        if 0 <= inferred <= 60:
            return inferred
    return None


def _experience_level_from_years(years: int | None) -> str | None:
    if years is None:
        return None
    if years <= 1:
        return "fresher"
    if years <= 3:
        return "junior"
    if years <= 6:
        return "mid"
    if years <= 10:
        return "senior"
    return "lead"


def _derive_role_from_resume(resume_text: str | None) -> str:
    text = (resume_text or "").lower()
    for role in ROLE_HINTS:
        if role in text:
            return role.title().replace("Ui Ux", "UI UX")
    return ""


def _derive_roles_from_resume(resume_text: str | None, limit: int = 5) -> list[str]:
    text = (resume_text or "").lower()
    roles: list[str] = []
    for role in ROLE_HINTS:
        if role in text:
            label = role.title().replace("Ui Ux", "UI UX")
            if label not in roles:
                roles.append(label)
    skills = set(_extract_skills_from_resume(resume_text))
    skill_roles = [
        ("React Developer", {"React", "Javascript", "Typescript", "Next.Js"}),
        ("Python Developer", {"Python", "Fastapi", "Django"}),
        ("Data Analyst", {"SQL", "Pandas", "Data Analysis"}),
        ("ML Engineer", {"Machine Learning", "Tensorflow"}),
        ("DevOps Engineer", {"Docker", "Kubernetes", "AWS"}),
    ]
    for label, required in skill_roles:
        if skills & required and label not in roles:
            roles.append(label)
    return roles[:limit]


async def _resolve_search_context(
    db: AsyncSession,
    current_user: User,
    payload: JobSearchRequest,
) -> tuple[str, str, str, dict]:
    prefs_result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == current_user.id)
    )
    prefs = prefs_result.scalar_one_or_none()

    from app.services.search_basis import resolve_basis

    resume, _ = await resolve_basis(db, current_user.id, payload.resume_id, payload.persona_id)

    role_source = "custom"
    role = payload.search_query.strip()
    payload_roles = [str(r).strip() for r in (payload.target_roles or []) if str(r).strip()]
    if not role and payload_roles:
        role = payload_roles[0]
        role_source = "manual.target_roles"
    if not role and prefs and prefs.target_roles:
        role = str(prefs.target_roles[0]).strip()
        role_source = "preferences.target_roles"
    if not role and prefs and prefs.current_title:
        role = prefs.current_title.strip()
        role_source = "preferences.current_title"
    if not role and current_user.headline:
        role = current_user.headline.strip()
        role_source = "profile.headline"
    if not role:
        role = _derive_role_from_resume(resume.raw_text if resume else None)
        role_source = "resume"
    if not role:
        role = "Software Engineer"
        role_source = "default"

    skills = _extract_skills_from_resume(resume.raw_text if resume else None)
    inferred_years = _infer_years_experience(resume.raw_text if resume else None)
    years_experience = (
        payload.years_experience
        if payload.years_experience is not None
        else (
            prefs.years_experience
            if prefs and prefs.years_experience is not None
            else inferred_years
        )
    )
    experience_level = (
        payload.experience_level
        or (prefs.experience_level if prefs else None)
        or _experience_level_from_years(years_experience)
    )
    # The role alone: every word of the query must appear in a job title
    # (job_catalog._role_patterns), so adding the experience level or resume
    # skills here ("Fresher Entry-level Software Engineer Python React")
    # matched no job at all. Resume skills still steer ranking (job_matching).
    search_query = role[:200]

    requested_location = payload.location.strip()
    location = requested_location or "Remote"
    work_modes = _split_pref_values(payload.work_mode)
    preferred_locations = [
        str(loc).strip() for loc in (payload.preferred_locations or []) if str(loc).strip()
    ]
    if prefs or preferred_locations:
        if not preferred_locations and prefs:
            preferred_locations = [
                str(loc).strip() for loc in (prefs.preferred_locations or []) if str(loc).strip()
            ]
        if not work_modes and prefs:
            work_modes = _split_pref_values(prefs.work_mode)
        primary_work_mode = work_modes[0] if work_modes else ""
        if primary_work_mode == "remote":
            location = "Remote"
        elif (
            primary_work_mode in {"hybrid", "onsite"}
            and requested_location in {"", "Any", "Remote"}
            and preferred_locations
        ):
            location = preferred_locations[0]
    work_mode = ",".join(work_modes)

    source = {
        "role_source": role_source,
        "location_source": (
            "preferences" if prefs and location != requested_location else "custom"
        ),
        "work_mode": work_mode or None,
        "experience_level": experience_level,
        "years_experience": years_experience,
        "job_type": payload.job_type or (prefs.job_type if prefs else None),
        "resume_skills": skills,
        "preference_id": str(prefs.id) if prefs else None,
        "resume_id": str(resume.id) if resume else None,
    }
    return search_query[:200], location[:200], work_mode, source


@router.get("/search/profile", response_model=JobSearchProfileResponse)
async def get_job_search_profile(
    resume_id: uuid.UUID | None = None,
    persona_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    prefs_result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == current_user.id)
    )
    prefs = prefs_result.scalar_one_or_none()
    from app.services.search_basis import resolve_basis

    resume, _ = await resolve_basis(db, current_user.id, resume_id, persona_id)
    resume_text = resume.raw_text if resume else None

    resume_roles = _derive_roles_from_resume(resume_text)
    skills = _extract_skills_from_resume(resume_text)
    inferred_years = _infer_years_experience(resume_text)
    saved_years = prefs.years_experience if prefs and prefs.years_experience is not None else None
    years = saved_years if saved_years is not None else inferred_years
    level = (prefs.experience_level if prefs else None) or _experience_level_from_years(years)

    saved_roles = (
        [str(role).strip() for role in (prefs.target_roles or []) if str(role).strip()]
        if prefs
        else []
    )
    role = (
        saved_roles[0]
        if saved_roles
        else (prefs.current_title.strip() if prefs and prefs.current_title else "")
        or (resume_roles[0] if resume_roles else "")
        or "Software Engineer"
    )

    work_mode = (prefs.work_mode if prefs else None) or "remote"
    work_modes = _split_pref_values(work_mode)
    primary_work_mode = work_modes[0] if work_modes else "remote"
    locations = (
        [str(loc).strip() for loc in (prefs.preferred_locations or []) if str(loc).strip()]
        if prefs
        else []
    )
    location = "Remote" if primary_work_mode == "remote" else (locations[0] if locations else "Any")

    missing_fields: list[str] = []
    if not resume:
        missing_fields.append("resume")
    if not saved_roles and not (prefs and prefs.current_title) and not resume_roles:
        missing_fields.append("target role")
    if years is None:
        missing_fields.append("years of experience")
    if any(mode in {"hybrid", "onsite"} for mode in work_modes) and not locations:
        missing_fields.append("preferred city")
    if not prefs or not _split_pref_values(prefs.job_type):
        missing_fields.append("job type")

    notes: list[str] = []
    if resume_roles:
        notes.append(f"Resume suggests: {', '.join(resume_roles[:3])}.")
    if skills:
        notes.append(f"Top skills found: {', '.join(skills[:5])}.")
    if inferred_years is not None and saved_years is None:
        notes.append(f"Inferred {inferred_years} year(s) from resume; confirm if wrong.")
    elif saved_years is not None:
        notes.append(f"Using saved {saved_years} year(s) experience.")

    saved_preferences = {
        "current_title": prefs.current_title if prefs else None,
        "experience_level": prefs.experience_level if prefs else None,
        "years_experience": prefs.years_experience if prefs else None,
        "job_type": prefs.job_type if prefs else None,
        "work_mode": prefs.work_mode if prefs else None,
        "target_roles": prefs.target_roles or [] if prefs else [],
        "preferred_locations": prefs.preferred_locations or [] if prefs else [],
        "salary_min": prefs.salary_min if prefs else None,
        "salary_max": prefs.salary_max if prefs else None,
        "bio": prefs.bio if prefs else None,
    }

    return JobSearchProfileResponse(
        resume_found=resume is not None,
        resume_id=resume.id if resume else None,
        resume_filename=resume.filename if resume else None,
        role_suggestions=list(
            dict.fromkeys(
                saved_roles
                + ([prefs.current_title.strip()] if prefs and prefs.current_title else [])
                + resume_roles
            )
        )[:6],
        skills=skills,
        inferred_years_experience=inferred_years,
        inferred_experience_level=level,
        saved_preferences=saved_preferences,
        search_query_preview=role,
        location_preview=location,
        work_mode_preview=work_mode,
        missing_fields=missing_fields,
        analysis_notes=notes,
    )


# ---------------------------------------------------------------------------
# Strategy preview endpoints — show the user what the system WOULD search.
#
# These don't actually fire searches. They surface the catalogs from
# ``app.services.search_presets`` so the UI can display the query plan
# (Google dorks, company career pages, ready-to-fetch job-board URLs)
# before the user clicks "Run search".
# ---------------------------------------------------------------------------


@router.get("/search/dorks")
async def list_search_dorks(
    q: str = "GenAI Python fresher",
    location: str = "India",
    region: str = "india",
    limit: int = 10,
    current_user: User = Depends(get_current_user),
):
    """Return Google dork strings the system would fire for a query.

    Region defaults to ``"india"`` (filters to India-targeted dorks).
    Pass ``region="global"`` for the full 17-dork list.
    """
    from app.services.search_presets import GOOGLE_DORKS

    if region.lower() == "global":
        dorks = list(GOOGLE_DORKS)
    else:
        # All current dorks are google_cse-tagged, but include any
        # engine so future additions to non-Google dorks also flow through.
        dorks = list(GOOGLE_DORKS)

    out: list[dict] = []
    for d in dorks[: max(1, min(limit, 50))]:
        raw_dork = d.get("dork", "")
        # Substitute {q} / {loc} / {location} placeholders if present.
        for needle, repl in (
            ("{q}", q),
            ("{loc}", location),
            ("{location}", location),
        ):
            raw_dork = raw_dork.replace(needle, repl)
        out.append(
            {
                "name": d.get("name", ""),
                "dork": raw_dork,
                "use_for": d.get("use_for", ""),
                "engine": d.get("engine", "google_cse"),
                "url": f"https://www.google.com/search?q={urllib.parse.quote_plus(raw_dork)}",
            }
        )
    return {
        "q": q,
        "location": location,
        "region": region,
        "count": len(out),
        "dorks": out,
    }


@router.get("/search/companies")
async def list_company_careers(
    region: str = "all",
    current_user: User = Depends(get_current_user),
):
    """Return the 16 company career pages the system can scrape directly."""
    from app.services.search_presets import COMPANY_CAREER_PAGES

    pages = list(COMPANY_CAREER_PAGES)
    indian_keywords = (
        "tcs",
        "infosys",
        "wipro",
        "hcl",
        "tech mahindra",
        "cognizant",
        "capgemini",
        "accenture",
        "ltimindtree",
        "mindtree",
        "mphasis",
    )
    if region.lower() == "india":
        pages = [
            c for c in pages if any(kw in c.get("name", "").lower() for kw in indian_keywords)
        ] or pages
    elif region.lower() == "global":
        pages = [
            c for c in pages if not any(kw in c.get("name", "").lower() for kw in indian_keywords)
        ]

    out: list[dict] = []
    for c in pages:
        out.append(
            {
                "name": c.get("name", ""),
                "url": c.get("url", ""),
                "apply_via": c.get("apply_via", "browser_use"),
                "notes": c.get("notes", ""),
            }
        )
    return {"region": region, "count": len(out), "companies": out}


@router.get("/search/presets")
async def list_search_presets(
    q: str = "GenAI Python fresher",
    location: str = "India",
    region: str = "all",
    limit: int = 25,
    current_user: User = Depends(get_current_user),
):
    """Return ready-to-fetch job-board URLs for a query.

    These are the 27 SEARCH_PRESETS from ``search_presets.py`` with
    ``{q}`` and ``{loc}`` substituted.  Some presets have fully-baked
    URLs (e.g. Naukri's direct search) — those pass through unchanged.
    """
    from app.services.search_presets import (
        SEARCH_PRESETS,
        build_url,
        presets_for_region,
    )

    if region.lower() in ("india", "global", "remote"):
        presets = presets_for_region(region)
    else:
        presets = list(SEARCH_PRESETS)

    out: list[dict] = []
    for preset in presets[: max(1, min(limit, 50))]:
        try:
            url = build_url(preset, q=q, loc=location, location=location)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Failed to build URL for preset %s: %s", preset.get("name", "?"), exc
            )
            url = preset.get("url", "")
        out.append(
            {
                "name": preset.get("name", ""),
                "url": url,
                "method": preset.get("method", "fetch"),
                "region": preset.get("region", "global"),
                "date_filter": preset.get("date_filter", ""),
                "exp_filter": preset.get("exp_filter", ""),
                "notes": preset.get("notes", ""),
            }
        )
    return {
        "q": q,
        "location": location,
        "region": region,
        "count": len(out),
        "presets": out,
    }


async def _resolve_live_browser(
    db: AsyncSession,
    current_user: User,
    request_value: bool,
) -> bool:
    """Merge per-user preference with the request.

    The request flag wins when the caller explicitly opted in (True) — this
    lets a UI button force the visible browser for a one-off run.  When the
    caller didn't override (False), the user's saved ``prefer_live_browser``
    preference decides.  Defaulting at the schema layer stays False so
    BYOK-no-preference users still get the fast headless waterfall.
    """
    if request_value:
        return True
    prefs_result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == current_user.id)
    )
    prefs = prefs_result.scalar_one_or_none()
    return bool(prefs and getattr(prefs, "prefer_live_browser", False))


@router.post("/search", response_model=JobSearchResponse)
@limiter.limit("10/minute")
async def search_jobs(
    request: Request,
    payload: JobSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    search_query, location, work_mode, search_source = await _resolve_search_context(
        db, current_user, payload
    )
    live_browser = await _resolve_live_browser(db, current_user, payload.live_browser)

    titles = [t.strip() for t in (payload.titles or []) if t.strip()]
    structured_locations = [loc.strip() for loc in (payload.locations or []) if loc.strip()]
    if titles:
        search_query = " ".join(titles)
    if structured_locations:
        location = structured_locations[0]
    # Leave unset so job_search_service uses DEFAULT_PLATFORMS
    # (open_apis, jobspy, ats, remoteok).
    #
    # This used to default to ["linkedin", "indeed", "naukri"], and
    # job_search.LEGACY_PLATFORM_MAP maps all three onto the single "jobspy"
    # adapter — so every UI-initiated search collapsed to one scraper, skipping
    # the keyless job APIs, the ATS scrapers and RemoteOK entirely. When JobSpy
    # was blocked or errored the run completed with "no results from any
    # platform" and the page had nothing real to show.
    platforms = payload.platforms or []

    remote = (payload.remote or "").strip() or work_mode or "any"
    from app.services.search_basis import resolve_basis

    resume, persona = await resolve_basis(
        db, current_user.id, payload.resume_id, payload.persona_id
    )
    basis = {
        "resume_id": str(resume.id) if resume else None,
        "persona_id": str(persona.id) if persona else None,
        "posted_within_days": payload.posted_within_days,
        "titles": sorted(set(titles)),
        "locations": sorted(set(structured_locations)),
        "platforms": sorted(platforms),
        "remote": remote,
    }
    stable_job_id = make_job_search_id(
        str(current_user.id), search_query, location, payload.max_results, basis
    )

    # A duplicate request (same query/location/count) reuses the run that is
    # already in flight rather than starting a second identical search.
    # Locked so two concurrent duplicate requests serialize on this check
    # rather than both slipping past it.
    from app.models.db import User

    await db.execute(select(User.id).where(User.id == current_user.id).with_for_update())
    existing_result = await db.execute(
        select(AgentRun)
        .where(
            AgentRun.user_id == current_user.id,
            AgentRun.agent_type == "job_search",
            AgentRun.status.in_(("queued", "running")),
            AgentRun.input["queue_job_id"].astext == stable_job_id,
        )
        .with_for_update()
    )
    existing_run = existing_result.scalars().first()
    if existing_run is not None:
        agent_run = existing_run
        run_id = str(existing_run.id)
    else:
        from app.api.v1.run_utils import check_run_admission

        await check_run_admission(db, current_user)
        run_id = str(uuid.uuid4())
        agent_run = AgentRun(
            id=uuid.UUID(run_id),
            user_id=current_user.id,
            agent_type="job_search",
            status="running",
            input={
                **basis,
                "search_query": search_query,
                "location": location,
                "max_results": payload.max_results,
                "live_browser": live_browser,
                "work_mode": work_mode,
                "search_source": search_source,
                "titles": titles,
                "platforms": platforms,
                "queue_job_id": stable_job_id,
            },
        )
        db.add(agent_run)
    # Reuse still ensures dispatch: recover a crash between commit and start.
    await db.commit()

    from app.workflows.starters import start_job_search

    try:
        await start_job_search(
            run_id,
            current_user.id,
            {
                **basis,
                "search_query": search_query,
                "location": location,
                "max_results": payload.max_results,
                "live_browser": live_browser,
                "work_mode": work_mode,
                "platforms": platforms,
                "remote": remote,
            },
        )
    except WorkflowUnavailable as exc:
        logger.warning("Job search start failed for run %s: %s", run_id, exc)
        agent_run.status = "failed"
        agent_run.output = {"error": "Job search service unavailable"}
        await db.commit()
        raise HTTPException(status_code=503, detail="Job search service unavailable") from exc
    return JobSearchResponse(run_id=run_id, queue_job_id=stable_job_id, queued=True)


_SORTS = {
    "found_desc": (JobApplication.found_at.desc(),),
    "found_asc": (JobApplication.found_at.asc(),),
    "match_desc": (
        JobApplication.match_score.desc().nulls_last(),
        JobApplication.found_at.desc(),
    ),
    "match_asc": (
        JobApplication.match_score.asc().nulls_last(),
        JobApplication.found_at.desc(),
    ),
}


def _application_query(
    user_id: uuid.UUID,
    *,
    status: str | None = None,
    location: str | None = None,
    source: str | None = None,
    posted_within_days: int | None = None,
    min_match: int | None = None,
    found_after: datetime | None = None,
    found_before: datetime | None = None,
    sort: str | None = None,
    q: str | None = None,
):
    """Filters owned rows in SQL for paging, counts and exports."""
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {VALID_STATUSES}")

    query = select(JobApplication).where(JobApplication.user_id == user_id)
    host = func.substring(
        func.coalesce(JobApplication.job_url, ""),
        r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)?//(?:[^/?#]*@)?([^:/?#]+)",
    )
    query = query.where(~func.coalesce(func.lower(host).op("~")(r"(^|\.)example\.com$"), False))
    filters = {item.strip().lower() for item in (location or "").split(",") if item.strip()}
    for mode in filters & {"remote", "hybrid"}:
        query = query.where(JobApplication.location.icontains(mode, autoescape=True))
    if "onsite" in filters:
        query = query.where(~JobApplication.location.icontains("remote", autoescape=True))
    cities = filters - {"remote", "hybrid", "onsite"}
    if cities:
        query = query.where(
            or_(
                *(
                    JobApplication.location.icontains(name, autoescape=True)
                    for city in cities
                    for name in _CITY_NAMES.get(city, (city,))
                )
            )
        )
    if filters:
        query = query.where(JobApplication.location.is_not(None), JobApplication.location != "")
    if status:
        query = query.where(JobApplication.status == status)
    if source:
        query = query.where(JobApplication.source == source)
    if posted_within_days:
        query = query.where(
            JobApplication.posted_at
            >= datetime.now(UTC) - timedelta(days=posted_within_days)  # noqa: UP017
        )
    if min_match is not None:
        query = query.where(JobApplication.match_score >= min_match)
    if found_after:
        query = query.where(JobApplication.found_at >= found_after)
    if found_before:
        query = query.where(JobApplication.found_at <= found_before)
    if q and q.strip():
        term = q.strip()
        query = query.where(
            or_(
                JobApplication.company.icontains(term, autoescape=True),
                JobApplication.role.icontains(term, autoescape=True),
                JobApplication.location.icontains(term, autoescape=True),
            )
        )
    if sort:
        order = _SORTS[sort]
    elif status == "saved":
        order = (JobApplication.match_score.desc().nulls_last(),)
    else:
        order = (
            JobApplication.applied_at.desc().nulls_last(),
            JobApplication.found_at.desc(),
        )
    return query.order_by(*order, JobApplication.id.asc())


async def _filtered_applications(
    db: AsyncSession, user_id: uuid.UUID, **filters
) -> list[JobApplication]:
    """Exports deliberately request all matching rows, using the list's filters."""
    result = await db.execute(_application_query(user_id, **filters))
    return list(result.scalars().all())


@router.get("/applications", response_model=list[ApplicationResponse])
async def list_applications(
    response: Response,
    status: str | None = None,
    location: str | None = None,
    source: str | None = Query(None, max_length=100),
    posted_within_days: int | None = Query(None, ge=1, le=90),
    min_match: int | None = Query(None, ge=0, le=100),
    found_after: datetime | None = None,
    found_before: datetime | None = None,
    sort: Literal["found_desc", "found_asc", "match_desc", "match_asc"] | None = None,
    q: str | None = Query(None, max_length=200),
    offset: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Filtered, sorted, paginated list. The total row count (before
    offset/limit) is returned in the ``X-Total-Count`` header and per-stage
    counts in ``X-Stage-Counts`` (JSON), so the body stays a plain list for
    existing callers and a paged view can still show whole-list stats."""
    query = _application_query(
        current_user.id,
        status=status,
        location=location,
        source=source,
        posted_within_days=posted_within_days,
        min_match=min_match,
        found_after=found_after,
        found_before=found_before,
        sort=sort,
        q=q,
    )
    counts = (
        await db.execute(
            query.order_by(None)
            .with_only_columns(JobApplication.status, func.count())
            .group_by(JobApplication.status)
        )
    ).all()
    response.headers["X-Total-Count"] = str(sum(count for _, count in counts))
    response.headers["X-Stage-Counts"] = json.dumps(dict(counts))
    page = query.offset(offset)
    if limit:
        page = page.limit(limit)
    apps = list((await db.execute(page)).scalars().all())
    return await _with_tracking(db, current_user.id, apps)


def _csv_cell(value: object) -> str:
    """CSV-escape; a leading = + - @ gets a ' prefix so Sheets never runs
    scraped job text as a formula."""
    text_value = "" if value is None else str(value)
    if text_value[:1] in ("=", "+", "-", "@"):
        text_value = "'" + text_value
    return '"' + text_value.replace('"', '""') + '"'


@router.post("/applications/export-sheet")
async def export_applications_sheet(
    status: str | None = None,
    location: str | None = None,
    source: str | None = Query(None, max_length=100),
    posted_within_days: int | None = Query(None, ge=1, le=90),
    min_match: int | None = Query(None, ge=0, le=100),
    found_after: datetime | None = None,
    found_before: datetime | None = None,
    sort: Literal["found_desc", "found_asc", "match_desc", "match_asc"] | None = None,
    q: str | None = Query(None, max_length=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export the filtered list to a new Google Sheet in the member's Drive
    (Drive converts the CSV upload; drive.file scope only). Each call makes a
    new sheet: appending to an existing one isn't supported."""
    from app.services.drive_service import DriveError, upload_to_drive

    apps = await _filtered_applications(
        db,
        current_user.id,
        status=status,
        location=location,
        source=source,
        posted_within_days=posted_within_days,
        min_match=min_match,
        found_after=found_after,
        found_before=found_before,
        sort=sort,
        q=q,
    )
    rows = [["Company", "Role", "Location", "Match", "Status", "Found", "URL", "Source"]]
    rows += [
        [
            a.company,
            a.role,
            a.location,
            a.match_score,
            a.status,
            a.found_at,
            a.job_url,
            a.source,
        ]
        for a in apps
    ]
    csv = "\r\n".join(",".join(_csv_cell(c) for c in row) for row in rows)
    name = f"CareerCraft jobs {datetime.now(UTC).date().isoformat()}"
    try:
        # upload_to_drive is a blocking HTTP call; keep it off the event loop.
        result = await asyncio.to_thread(
            upload_to_drive,
            str(current_user.id),
            name,
            csv.encode("utf-8"),
            "text/csv",
            "application/vnd.google-apps.spreadsheet",
        )
    except DriveError as exc:
        if exc.not_connected:  # the UI starts the Drive connect flow only for this
            raise HTTPException(status_code=409, detail="google_drive_not_connected") from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.add(
        ActionLog(
            user_id=current_user.id,
            action="export_sheet",
            detail={"rows": len(apps), "file_id": result.get("id")},
        )
    )
    await db.commit()
    return {"url": result.get("webViewLink"), "rows": len(apps)}


async def _owned_applications(
    db: AsyncSession,
    user_id: uuid.UUID,
    ids: list[uuid.UUID],
    *,
    include_deleted: bool = False,
) -> list[JobApplication]:
    rows = await db.execute(
        select(JobApplication)
        .where(JobApplication.user_id == user_id, JobApplication.id.in_(ids))
        .execution_options(include_deleted=include_deleted)
    )
    return list(rows.scalars().all())


@router.post("/applications/delete")
async def delete_applications(
    body: ApplicationIdsBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft delete (undoable via /applications/restore)."""
    apps = await _owned_applications(db, current_user.id, body.ids)
    now = datetime.now(UTC)
    for app in apps:
        app.deleted_at = now
        db.add(ActionLog(user_id=current_user.id, job_application_id=app.id, action="delete"))
    await db.commit()
    return {"deleted": [str(a.id) for a in apps]}


@router.post("/applications/restore")
async def restore_applications(
    body: ApplicationIdsBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    apps = await _owned_applications(db, current_user.id, body.ids, include_deleted=True)
    for app in apps:
        app.deleted_at = None
        db.add(ActionLog(user_id=current_user.id, job_application_id=app.id, action="restore"))
    await db.commit()
    return {"restored": [str(a.id) for a in apps]}


@router.get("/applications/{application_id}/jd", response_model=JobDescriptionResponse)
async def get_application_jd(
    application_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The job description to tailor a resume against. Saved rows hold a
    copy capped at 4000 chars (and some sources save none), so the shared
    job catalog's full description wins when it is longer."""
    apps = await _owned_applications(db, current_user.id, [application_id])
    if not apps:
        raise HTTPException(status_code=404, detail="Application not found")
    app = apps[0]
    saved = (app.jd_text or "").strip()
    catalog = ""
    if app.job_url:
        from app.services.job_connectors import canonical_url

        try:  # the catalog keys jobs by canonical URL; saved rows keep the raw one
            urls = list({app.job_url, canonical_url(app.job_url)})
        except ValueError:
            urls = [app.job_url]
        catalog = (
            (
                await db.execute(
                    text(
                        "SELECT data->>'description' FROM job_catalog WHERE url = ANY(:urls) "
                        "ORDER BY length(data->>'description') DESC NULLS LAST LIMIT 1"
                    ),
                    {"urls": urls},
                )
            ).scalar_one_or_none()
            or ""
        ).strip()
    if not saved and not catalog:
        raise HTTPException(status_code=404, detail="No job description saved for this role")
    use_catalog = len(catalog) > len(saved)
    return JobDescriptionResponse(
        jd_text=catalog if use_catalog else saved,
        role=app.role,
        company=app.company,
        source="catalog" if use_catalog else "application",
    )


@router.post("/applications/{application_id}/apply-state", response_model=ApplicationResponse)
async def set_apply_state(
    application_id: uuid.UUID,
    body: ApplyStateBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Assisted apply in the member's own browser: the client opens the job
    page (``opened``), then the member confirms ``applied`` or ``failed``.
    ``applied`` also moves a saved row to status=applied (follow-ups start)."""
    apps = await _owned_applications(db, current_user.id, [application_id])
    if not apps:
        raise HTTPException(status_code=404, detail="Application not found")
    app = apps[0]
    app.apply_state = body.state
    db.add(
        ActionLog(
            user_id=current_user.id,
            job_application_id=app.id,
            action=f"apply_{body.state}",
        )
    )
    await db.commit()
    if body.state == "applied" and app.status == "saved":
        await update_application_status(
            application_id, StatusUpdateBody(status="applied"), db, current_user
        )
    await db.refresh(app)
    return (await _with_tracking(db, current_user.id, [app]))[0]


async def _with_tracking(db: AsyncSession, user_id: uuid.UUID, apps: list) -> list:
    """Applications plus the resume that went out and the recruiter email's
    status, fetched in two queries rather than one per row."""
    from app.models.db import RecruiterOutreach
    from app.services.outreach_service import summarize_outreach
    from app.services.resume_version import content_version

    resume_ids = {a.resume_id for a in apps if a.resume_id}
    documents = {}
    if resume_ids:
        rows = await db.execute(
            select(UserDocument.id, UserDocument.filename, UserDocument.raw_text).where(
                UserDocument.id.in_(resume_ids), UserDocument.user_id == user_id
            )
        )
        documents = {row.id: row for row in rows}
    outreach: dict = {}
    if apps:
        rows = await db.execute(
            select(RecruiterOutreach).where(
                RecruiterOutreach.user_id == user_id,
                RecruiterOutreach.job_application_id.in_([a.id for a in apps]),
            )
        )
        for row in rows.scalars().all():
            outreach.setdefault(row.job_application_id, []).append(row)

    out = []
    for app in apps:
        item = ApplicationResponse.model_validate(app)
        document = documents.get(app.resume_id)
        if document is not None:
            short = content_version(document.raw_text or "")[:8]
            item.resume_label = f"{document.filename} · {short}"
        summary = summarize_outreach(outreach.get(app.id, []))
        if summary:
            item.outreach_status = summary["status"]
            item.outreach_to = summary["to_email"]
        out.append(item)
    return out


@router.patch("/applications/{application_id}/status", response_model=ApplicationResponse)
async def update_application_status(
    application_id: uuid.UUID,
    body: StatusUpdateBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {VALID_STATUSES}")

    result = await db.execute(
        select(JobApplication).where(
            JobApplication.id == application_id,
            JobApplication.user_id == current_user.id,
        )
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    app.status = body.status
    if body.status == "applied":
        if not app.applied_at:
            app.applied_at = datetime.now(UTC)
        # Shown by the tracker and dashboard; FollowupWorkflow drafts the
        # emails on these same dates.
        if not app.followup_day5:
            app.followup_day5 = app.applied_at + timedelta(days=5)
        if not app.followup_day12:
            app.followup_day12 = app.applied_at + timedelta(days=12)
        await db.commit()
        from app.workflows.starters import start_followups

        try:
            await start_followups(current_user.id, app.id, app.applied_at)
        except WorkflowUnavailable:
            # The status change stands; the follow-ups are a convenience.
            logger.warning("Could not schedule follow-ups for application %s", app.id)
        await db.refresh(app)
    return app


async def _start_temporal_auto_apply(user_id: uuid.UUID, application_id: uuid.UUID) -> dict:
    """Start (or reuse) the application's AutoApplyWorkflow. Ownership,
    job-url and resume checks already happened in the caller; the workflow's
    reserve_application_attempt activity repeats the state checks and makes
    the reservation, since that must be safe under at-least-once execution.
    """
    from app.workflows.starters import start_auto_apply

    try:
        started = await start_auto_apply(user_id, application_id)
    except WorkflowUnavailable as exc:
        raise HTTPException(status_code=503, detail="Application service unavailable") from exc

    # The reserve activity creates the AgentRun row within its own first
    # (sub-second, no browser involved) step; wait briefly for it so the
    # client can open the run's event stream right away.
    run_id = await _await_temporal_run_id(user_id, application_id, started.get("run_id"))
    return {
        "run_id": run_id,
        "workflow_id": started["workflow_id"],
        "engine": "temporal",
        "mode": started["mode"],
        "status": started["status"],
    }


async def _await_temporal_run_id(
    user_id: uuid.UUID,
    application_id: uuid.UUID,
    run_id: str | None = None,
    attempts: int = 10,
    delay_s: float = 0.3,
) -> str | None:
    """The run of a just-started workflow is known up front (wait for its
    row); for an already-running one, read it from the attempt. Never read
    the attempt for a new workflow: until the reserve activity runs it still
    points at the previous attempt's run."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import ApplicationAttempt

    if run_id is not None:
        for _ in range(attempts):
            async with AsyncSessionLocal() as db:
                if await db.get(AgentRun, uuid.UUID(run_id)) is not None:
                    break
            await asyncio.sleep(delay_s)
        return run_id

    for _ in range(attempts):
        async with AsyncSessionLocal() as db:
            attempt = (
                await db.execute(
                    select(ApplicationAttempt).where(
                        ApplicationAttempt.user_id == user_id,
                        ApplicationAttempt.job_application_id == application_id,
                    )
                )
            ).scalar_one_or_none()
            if attempt and attempt.run_id:
                return str(attempt.run_id)
        await asyncio.sleep(delay_s)
    return None


@router.post("/applications/{application_id}/prepare-apply")
@limiter.limit("5/hour")
async def prepare_application_apply(
    application_id: uuid.UUID,
    body: PrepareApplyBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start the application's AutoApplyWorkflow. In extension mode the
    user's own browser fills the form and the user submits from the
    extension's review panel. Never a route-owned background task.
    """
    # Locked for the duration of this transaction so a second concurrent
    # prepare-apply call for the same application serializes behind this one
    # instead of racing it to create a second ApplicationAttempt.
    result = await db.execute(
        select(JobApplication)
        .where(
            JobApplication.id == application_id,
            JobApplication.user_id == current_user.id,
        )
        .with_for_update()
    )
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    if not app.job_url:
        raise HTTPException(status_code=400, detail="Application has no job URL")
    if app.status == "applied":
        raise HTTPException(status_code=400, detail="Already applied to this job")
    if not app.resume_id:
        raise HTTPException(
            status_code=400,
            detail="Attach an approved resume to this application before applying",
        )

    from app.models.db import ApplicationAttempt
    from app.services.extension_service import has_active_device
    from app.services.workflow_service import ACTIVE_SUBMISSION_STATES

    attempt = (
        await db.execute(
            select(ApplicationAttempt).where(
                ApplicationAttempt.user_id == current_user.id,
                ApplicationAttempt.job_application_id == application_id,
            )
        )
    ).scalar_one_or_none()
    if attempt and attempt.state in ACTIVE_SUBMISSION_STATES:
        raise HTTPException(
            status_code=409,
            detail="Verify the existing application before applying again",
        )

    if not await has_active_device(db, current_user.id):
        raise HTTPException(
            status_code=409,
            detail=(
                "Connect the CareerCraft browser extension first (Settings → Integrations), "
                "then stay signed in to the job site in that browser."
            ),
        )

    # Release the row lock before the workflow's reserve activity locks it.
    await db.commit()
    return await _start_temporal_auto_apply(current_user.id, application_id)


class OutreachBody(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=10)


@router.post("/applications/outreach")
@limiter.limit("10/hour")
async def queue_applications_outreach(
    body: OutreachBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Draft recruiter emails for the member's own saved jobs. Drafts land in
    the review queue (/outreach); they only go out unreviewed if the member
    turned on auto-send."""
    from app.services.auto_apply_queue import draft_outreach

    apps = await _owned_applications(db, current_user.id, body.ids)
    queued: list[str] = []
    skipped: list[str] = []
    for app in apps:
        state = await draft_outreach(str(current_user.id), app)
        (queued if state else skipped).append(str(app.id))
        db.add(
            ActionLog(
                user_id=current_user.id,
                job_application_id=app.id,
                action="outreach_queued",
                source="user",
                detail={"state": state or "no_contact"},
            )
        )
    await db.commit()
    return {"queued": queued, "skipped": skipped}
