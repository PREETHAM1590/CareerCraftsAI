"""Selected-resume matching; no job description can issue model/tool instructions."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import uuid

from sqlalchemy import select, text

from app.models.db import UserModelSettings
from app.services.jobs_database import AsyncSessionLocal
from app.services.search_basis import basis_text


def rule_score(
    job: dict, basis: str, query: str, location: str | list = "", github_skills=()
) -> tuple[int, list[str]]:
    def tokens(value):
        return set(re.findall(r"[a-z][a-z0-9+#.]{2,}", value.lower()))

    wanted, title, profile, description = (
        tokens(query),
        tokens(job["title"]),
        tokens(basis),
        tokens(job.get("description", "")),
    )
    role = len(wanted & title) / max(1, len(wanted))
    overlap = len(profile & (title | description)) / max(1, min(len(profile), 40))
    score = 55 * role + 30 * min(1, overlap)
    reasons = [f"Role keyword match: {round(role * 100)}%"]
    places = [location] if isinstance(location, str) else list(location or [])
    job_place = str(job.get("location") or "").lower()
    if any(
        place
        and place.lower() != "any"
        and (
            place.lower() in job_place
            or (place.lower() == "remote" and job.get("remote") == "remote")
        )
        for place in map(str, places)
    ):
        score += 10
        reasons.append("Matches preferred location")
    evidence = tokens(" ".join(github_skills)) & (title | description)
    if evidence:
        score += min(5, len(evidence))
        reasons.append("Public GitHub evidence: " + ", ".join(sorted(evidence)[:5]))
    if job.get("posted_at"):
        score += 5
        reasons.append("Publication date available")
    return min(100, round(score)), reasons


async def semantic_scores(user_id: str, basis: str, jobs: list[dict]):
    from app.services.rag_service import get_embedding_model, get_embedding_provider

    async with AsyncSessionLocal() as db:
        model_settings = (
            await db.execute(
                select(UserModelSettings)
                .where(
                    UserModelSettings.user_id == uuid.UUID(user_id),
                    UserModelSettings.is_active.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if not model_settings or not basis or not jobs:
            return {}
        model = get_embedding_model(model_settings)
        provider = (
            get_embedding_provider(model_settings) + ":" + str(getattr(model, "model", "default"))
        )
        query = await asyncio.to_thread(model.embed_query, basis[:16000])
        dimensions = len(query)
        if dimensions not in {768, 1024, 1536} or not all(math.isfinite(x) for x in query):
            raise ValueError("Unsupported embedding dimensions")
        cached = (
            await db.execute(
                text("""SELECT job_id,content_hash FROM job_match_embeddings
            WHERE user_id=:uid AND provider=:provider AND dimensions=:dimensions
            AND job_id=ANY(:ids)"""),
                {
                    "uid": uuid.UUID(user_id),
                    "provider": provider,
                    "dimensions": dimensions,
                    "ids": [j["job_id"] for j in jobs],
                },
            )
        ).all()
        hashes = dict(cached)
        strings = {j["job_id"]: (j["title"] + "\n" + j.get("description", ""))[:6000] for j in jobs}
        missing = [
            j
            for j in jobs
            if hashes.get(j["job_id"]) != hashlib.sha256(strings[j["job_id"]].encode()).hexdigest()
        ]
        if missing:
            vectors = await asyncio.to_thread(
                model.embed_documents, [strings[j["job_id"]] for j in missing]
            )
            for job, vector in zip(missing, vectors, strict=True):
                if len(vector) != dimensions or not all(math.isfinite(x) for x in vector):
                    raise ValueError("Invalid embedding")
                await db.execute(
                    text("""INSERT INTO job_match_embeddings
                    (user_id,job_id,provider,dimensions,content_hash,embedding)
                    VALUES(:uid,:id,:provider,:dimensions,:hash,CAST(:vector AS vector))
                    ON CONFLICT(user_id,job_id,provider,dimensions) DO UPDATE SET
                    content_hash=EXCLUDED.content_hash,embedding=EXCLUDED.embedding"""),
                    {
                        "uid": uuid.UUID(user_id),
                        "id": job["job_id"],
                        "provider": provider,
                        "dimensions": dimensions,
                        "hash": hashlib.sha256(strings[job["job_id"]].encode()).hexdigest(),
                        "vector": str(vector),
                    },
                )
        # Dimension is checked above, so interpolated cast syntax cannot be user supplied.
        rows = (
            await db.execute(
                text(f"""SELECT job_id, 1-(embedding::vector({dimensions})
                    <=> CAST(:query AS vector({dimensions}))) AS score
            FROM job_match_embeddings WHERE user_id=:uid AND provider=:provider
            AND dimensions={dimensions} AND job_id=ANY(:ids)
            ORDER BY embedding::vector({dimensions})
            <=> CAST(:query AS vector({dimensions})) LIMIT 100"""),  # noqa: S608  # nosec B608
                {
                    "uid": uuid.UUID(user_id),
                    "provider": provider,
                    "query": str(query),
                    "ids": [j["job_id"] for j in jobs],
                },
            )
        ).all()
        await db.commit()
        return {id_: max(0, min(1, float(score))) for id_, score in rows}


async def rank_jobs(user_id: str, context: dict, jobs: list[dict]):
    query = " ".join(context.get("titles") or []) or context.get("search_query", "")
    basis, document_id = await basis_text(
        user_id, context.get("resume_id"), context.get("persona_id"), query
    )
    warnings, skills = [], []
    try:
        from app.services.github_profile import get_profile

        profile = await get_profile(uuid.UUID(user_id))
        skills = [s["name"] for s in profile["skills"]] if profile else []
    except Exception:
        # Optional evidence must never prevent a search.
        skills = []
    from app.services.funding_signals import funded_among, with_bonus
    from app.services.job_connectors import _squash

    funded = await funded_among([job.get("company", "") for job in jobs])
    matches = []
    for job in jobs:
        score, reasons = rule_score(
            job, basis, query, context.get("locations") or context.get("location", ""), skills
        )
        if _squash(job.get("company", "")) in funded:
            score = with_bonus(score)
            reasons.append("Company raised funding recently")
        matches.append(
            {
                **job,
                "match_score": score,
                "reasons": reasons,
                "red_flags": [],
                "missing_skills": [],
                "resume_id": document_id,
                "ranking_mode": "rules",
            }
        )
    matches.sort(key=lambda job: job["match_score"], reverse=True)
    try:
        semantic = await asyncio.wait_for(semantic_scores(user_id, basis, matches[:100]), 60)
        if not semantic:
            warnings.append("Semantic ranking unavailable; using resume keyword and role rules")
        for job in matches:
            if job["job_id"] in semantic:
                job["match_score"] = round(
                    0.55 * job["match_score"] + 0.45 * semantic[job["job_id"]] * 100
                )
                job["ranking_mode"] = "semantic+rules"
                job["reasons"].append("Similarity to selected resume")
    except Exception:
        warnings.append("Semantic ranking unavailable; using resume keyword and role rules")
    matches.sort(key=lambda job: (job["match_score"], job.get("posted_at") or ""), reverse=True)
    return matches, warnings  # no cap: every relevant match is returned
