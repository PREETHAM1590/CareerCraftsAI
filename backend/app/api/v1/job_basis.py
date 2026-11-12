"""Search basis list and default; all queries are user scoped."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.api.v1.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.db import ResumePersona, UserDocument
from app.services.search_basis import SearchDefault, resolve_basis

router = APIRouter()


class DefaultRequest(BaseModel):
    kind: Literal["resume", "persona"]
    id: uuid.UUID


@router.get("/search-bases")
@router.get("/search/bases", include_in_schema=False)
async def bases(db=Depends(get_db), user=Depends(get_current_user)):
    resumes = (
        (
            await db.execute(
                select(UserDocument)
                .where(
                    UserDocument.user_id == user.id,
                    UserDocument.doc_type == "resume",
                )
                .order_by(
                    UserDocument.is_primary.desc(),
                    UserDocument.embedded_at.desc().nulls_last(),
                )
            )
        )
        .scalars()
        .all()
    )
    personas = (
        (await db.execute(select(ResumePersona).where(ResumePersona.user_id == user.id)))
        .scalars()
        .all()
    )
    document, persona = await resolve_basis(db, user.id)
    owned_ids = {r.id for r in resumes}
    response = {
        "options": [
            {"kind": "resume", "id": str(r.id), "label": r.filename, "available": True}
            for r in resumes
        ]
        + [
            {
                "kind": "persona",
                "id": str(p.id),
                "label": p.name,
                "available": p.primary_resume_id in owned_ids,
            }
            for p in personas
        ],
        "default": (
            {
                "kind": "persona" if persona else "resume",
                "id": str(persona.id if persona else document.id),
            }
            if document
            else None
        ),
    }
    for option in response["options"]:
        option["ready"] = option["available"]
    response["default_basis"] = response["default"]
    return response


@router.patch("/search/basis-default", include_in_schema=False)
async def set_default(body: DefaultRequest, db=Depends(get_db), user=Depends(get_current_user)):
    from app.models.db import User

    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    await resolve_basis(
        db,
        user.id,
        resume_id=body.id if body.kind == "resume" else None,
        persona_id=body.id if body.kind == "persona" else None,
    )
    row = await db.get(SearchDefault, user.id, with_for_update=True)
    if row is None:
        row = SearchDefault(user_id=user.id, kind=body.kind, basis_id=body.id)
        db.add(row)
    else:
        row.kind, row.basis_id = body.kind, body.id
    await db.commit()
    return body.model_dump()


class DefaultPatch(BaseModel):
    basis: DefaultRequest | None


@router.patch("/search-basis")
async def patch_default(body: DefaultPatch, db=Depends(get_db), user=Depends(get_current_user)):
    if body.basis is not None:
        return await set_default(body.basis, db, user)
    row = await db.get(SearchDefault, user.id, with_for_update=True)
    if row is not None:
        await db.delete(row)
    await db.commit()
    return {"basis": None}


@router.get("/sources")
async def source_health(db=Depends(get_db), user=Depends(get_current_user)):
    from sqlalchemy import text

    from app.services.job_catalog import sources

    records = (
        (
            await db.execute(
                text("SELECT source_id,status,checked_at,failures FROM job_source_health")
            )
        )
        .mappings()
        .all()
    )
    health = {row["source_id"]: dict(row) for row in records}
    from app.services.indian_platforms_service import platform_status

    return {
        "sources": [
            {
                "id": s.id,
                "family": s.family,
                "attribution": s.company or s.tenant or s.family,
                "country": s.country or None,
                "company_country": s.company_country or None,
                **health.get(s.id, {"status": "not_refreshed"}),
            }
            for s in sources()
        ],
        # Boards that are deliberately not read, with the reason.
        "unavailable": platform_status(),
    }


@router.get("/catalog")
@limiter.limit("10/minute")
async def catalog(
    request: Request,
    q: str = Query(default="software engineer", min_length=1, max_length=200),
    location: str = Query(default="", max_length=200),
    source: str | None = Query(default=None, max_length=200),
    posted_within_days: int = Query(default=30, ge=1, le=90),
    resume_id: uuid.UUID | None = None,
    persona_id: uuid.UUID | None = None,
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=25, ge=1, le=100),
    india_focus: bool | None = Query(
        default=None,
        description="India-first results (default: on unless the location is outside India)",
    ),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    import base64
    import hashlib
    import json

    from app.services.india_focus import wants_india
    from app.services.job_catalog import search_catalog
    from app.services.job_matching import rank_jobs

    document, persona = await resolve_basis(db, user.id, resume_id, persona_id)
    context = {
        "search_query": q,
        "location": location,
        "posted_within_days": posted_within_days,
        "resume_id": str(document.id) if document else None,
        "persona_id": str(persona.id) if persona else None,
        "max_results": 300,
    }
    if india_focus is None:
        from sqlalchemy import select

        from app.models.db import UserPreferences

        saved = (
            await db.execute(
                select(UserPreferences.india_focus).where(UserPreferences.user_id == user.id)
            )
        ).scalar_one_or_none()
        india_focus = False if saved is False else None
    if india_focus is not None:
        context["india_focus"] = india_focus
    fingerprint = hashlib.sha256(
        json.dumps([str(user.id), context, source], sort_keys=True).encode()
    ).hexdigest()
    offset = 0
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (
                value["query"] != fingerprint
                or type(value["offset"]) is not int
                or not 0 <= value["offset"] <= 300
            ):
                raise ValueError("Invalid cursor")
            offset = value["offset"]
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "Invalid search cursor") from exc
    stats: dict = {}
    jobs, warnings = await search_catalog(context, [source] if source else None, stats=stats)
    if hidden := len(stats.get("india_hidden_ids", [])):
        warnings = [*warnings, f"Hid {hidden} matching roles open only outside India"]
    ranked, ranking_warnings = await rank_jobs(str(user.id), context, jobs)
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"query": fingerprint, "offset": offset + limit}).encode()
        ).decode()
        if offset + limit < len(ranked)
        else None
    )
    return {
        "jobs": ranked[offset : offset + limit],
        "next_cursor": next_cursor,
        "warnings": warnings + ranking_warnings,
        "india_focus": wants_india(context),
        "ranking_mode": (
            "semantic+rules" if any(j["ranking_mode"] != "rules" for j in ranked) else "rules"
        ),
    }
