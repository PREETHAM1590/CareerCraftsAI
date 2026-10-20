"""
cover_letter_agent.py — LangGraph node for personalized cover letter generation.

All prompt text lives in app/agents/prompts/cover_letter_prompt.py. The node
retrieves RAG context, parses the LLM output into OUTPUT_SCHEMA (one retry),
optionally persists a version row, and returns awaiting_approval with the
draft for human review. Cover letters are never sent automatically.
"""

from __future__ import annotations

import logging
import time
import uuid

from langchain_core.messages import AIMessage

from app.agents._llm_json import call_llm_json
from app.agents.memory_prompt import memory_prompt
from app.agents.prompts.cover_letter_prompt import OUTPUT_SCHEMA as CoverLetterOutput
from app.agents.prompts.cover_letter_prompt import SYSTEM_PROMPT as COVER_SYSTEM_PROMPT
from app.agents.prompts.cover_letter_prompt import VALID_TONES
from app.agents.prompts.cover_letter_prompt import build_user_prompt as build_cover_prompt
from app.agents.state import AgentState
from app.core.model_router import build_agent_llm
from app.core.sync_db import _get_sync_factory, _to_uuid, fetch_model_settings
from app.services.rag_service import retrieve

logger = logging.getLogger(__name__)


def _store_cover_letter(
    user_id: str,
    job_application_id: str,
    parsed: CoverLetterOutput,
    tone: str,
) -> dict:
    """Persist the draft to user_documents + cover_letter_versions.

    Verifies the application belongs to the user (IDOR guard) and returns
    {document_id, version_number}. Cover letter text lives in the DB row
    (raw_text); no Storage upload — there is no binary artifact.
    """
    from sqlalchemy import func, select, update

    from app.models.db import CoverLetterVersion, JobApplication, UserDocument

    factory = _get_sync_factory()
    with factory() as db:
        app_row = db.execute(
            select(JobApplication.id).where(
                JobApplication.id == _to_uuid(job_application_id),
                JobApplication.user_id == _to_uuid(user_id),
            )
        ).scalar_one_or_none()
        if app_row is None:
            raise ValueError("Job application not found for this user")

        doc_id = uuid.uuid4()
        db.add(
            UserDocument(
                id=doc_id,
                user_id=_to_uuid(user_id),
                doc_type="cover_letter",
                filename=f"cover_letter_{tone}_{doc_id.hex[:8]}.md",
                storage_path="",  # Generated text artifact; no binary object.
                raw_text=parsed.cover_letter_markdown,
                is_primary=False,
            )
        )

        # NOTE: max+1 is not atomic; concurrent generates for the same
        # application could duplicate a version number. Accepted: cover
        # letter generation is low-volume and human-paced. If this ever
        # runs concurrently, add a UNIQUE(job_application_id,
        # version_number) constraint and retry on conflict.
        next_version = (
            db.execute(
                select(func.coalesce(func.max(CoverLetterVersion.version_number), 0)).where(
                    CoverLetterVersion.job_application_id == _to_uuid(job_application_id)
                )
            ).scalar()
            or 0
        ) + 1

        db.add(
            CoverLetterVersion(
                id=uuid.uuid4(),
                user_id=_to_uuid(user_id),
                job_application_id=_to_uuid(job_application_id),
                document_id=doc_id,
                tone=tone,
                version_number=next_version,
            )
        )
        db.execute(
            update(JobApplication)
            .where(
                JobApplication.id == _to_uuid(job_application_id),
                JobApplication.user_id == _to_uuid(user_id),
            )
            .values(cover_letter_id=doc_id)
        )
        db.commit()
        return {"document_id": str(doc_id), "version_number": next_version}


def cover_letter_node(state: AgentState) -> AgentState:
    """Standard-shape LangGraph node: RAG -> LLM JSON -> optional persist."""
    start_ts = time.monotonic()
    run_id = state["run_id"]
    user_id = state["user_id"]
    ctx = state.get("context", {}) or {}
    # Accept both key spellings: the /generate endpoint sends application_id.
    tone = ctx.get("tone", "formal")
    job_application_id = ctx.get("job_application_id") or ctx.get("application_id")
    jd_text = ctx.get("jd_text", "")

    from app.core.event_bus import emit

    if tone not in VALID_TONES:
        return {
            **state,
            "status": "failed",
            "error": f"missing/invalid: tone must be one of {sorted(VALID_TONES)}",
        }
    if not (jd_text or "").strip():
        return {**state, "status": "failed", "error": "missing: jd_text"}

    try:
        emit(run_id, "thinking", {"step": "start", "message": "Retrieving resume context..."})
        model_settings = state.get("model_settings") or fetch_model_settings(user_id)
        if not model_settings:
            return {**state, "status": "failed", "error": "missing: active model settings"}

        from app.core.sync_db import fetch_user_profile_text

        # RAG is best-effort: a transient retrieval failure degrades to
        # profile text (or empty context) instead of failing the whole run.
        try:
            resume_chunks = retrieve(user_id, "resume", jd_text, model_settings, k=5)
            chunk_texts = [
                c.page_content if hasattr(c, "page_content") else str(c) for c in resume_chunks
            ]
        except Exception as rag_exc:
            logger.warning("Cover letter RAG retrieval failed, continuing: %s", rag_exc)
            resume_chunks, chunk_texts = [], []
        context_text = "\n\n".join(chunk_texts)
        if not context_text.strip():
            try:
                context_text = fetch_user_profile_text(user_id) or ""
            except Exception as prof_exc:
                logger.warning("Cover letter profile fallback failed, continuing: %s", prof_exc)
        emit(run_id, "tool_call", {"tool": "rag_retrieve", "input": {"k": 5}})
        emit(
            run_id,
            "tool_result",
            {"tool": "rag_retrieve", "output": {"chunks": len(resume_chunks)}},
        )

        llm = build_agent_llm(model_settings)

        emit(run_id, "thinking", {"step": "write", "message": f"Writing {tone} cover letter..."})
        parsed = call_llm_json(
            llm,
            COVER_SYSTEM_PROMPT,
            build_cover_prompt(
                {
                    "jd_text": jd_text,
                    "tone": tone,
                    "company": ctx.get("company", "NOT_PROVIDED"),
                    "target_role": ctx.get("target_role", ctx.get("role", "NOT_PROVIDED")),
                },
                [context_text] if context_text.strip() else None,
            )
            + memory_prompt(ctx),
            CoverLetterOutput,
        )
        tokens_used = 0

        document_id = None
        version_number = None
        persist_warning = None
        if job_application_id:
            try:
                stored = _store_cover_letter(user_id, job_application_id, parsed, tone)
                document_id = stored["document_id"]
                version_number = stored["version_number"]
            except ValueError:
                # Unknown application id or owned by another user: fail
                # loudly (generic message — no existence oracle) instead of
                # silently returning an unlinked draft.
                logger.warning("Cover letter persist refused for run %s", run_id)
                emit(run_id, "error", {"message": "Agent failed"})
                return {**state, "status": "failed", "error": "Agent failed"}
            except Exception as se:
                logger.warning("Cover letter persist failed, returning draft only: %s", se)
                persist_warning = "Versioning failed — draft only, not linked to the application."

        duration_ms = int((time.monotonic() - start_ts) * 1000)
        pending = parsed.model_dump()
        pending.update(
            {
                "type": "cover_letter_review",
                "document_id": document_id,
                "version_number": version_number,
            }
        )
        if persist_warning:
            pending["persist_warning"] = persist_warning
        emit(run_id, "complete", {"result": {"document_id": document_id, "tone": tone}})
        return {
            **state,
            "status": "awaiting_approval",
            "pending_action": pending,
            "result": pending,
            "tokens_used": tokens_used,
            "messages": state.get("messages", [])
            + [AIMessage(content=parsed.cover_letter_markdown[:200])],
            "context": {**ctx, "duration_ms": duration_ms},
        }
    except Exception as exc:
        logger.error("Cover letter agent failed for user %s: %s", state.get("user_id"), exc)
        emit(run_id, "error", {"message": "Agent failed"})
        return {**state, "status": "failed", "error": "Agent failed"}
