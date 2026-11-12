import json
import logging

from langchain_core.messages import AIMessage

from app.agents._llm_json import call_llm_json
from app.agents.prompts.resume_prompt import OUTPUT_SCHEMA as ResumeOutput
from app.agents.prompts.resume_prompt import SYSTEM_PROMPT as RESUME_JSON_SYSTEM_PROMPT
from app.agents.prompts.resume_prompt import (
    build_user_prompt as build_resume_json_prompt,
)
from app.agents.state import AgentState
from app.core.pii_vault import strip_contact_details
from app.services.ats_estimator import estimate_resume
from app.services.ats_service import compute_ats_score
from app.services.pdf_service import generate_resume_pdf
from app.services.rag_service import retrieval_unavailable, retrieve
from app.services.resume_export import PageOverflow
from app.services.resume_grounding import (
    date_issues,
    omitted_entries,
    remove_unsupported,
    source_text,
    unsupported_claims,
)
from app.services.resume_parseback import check_pdf
from app.services.resume_structure import (
    apply_fixes,
    apply_saved_facts,
    clean_placeholders,
    ensure_contact,
    filter_resolved_warnings,
    parse_contact,
    review_resume,
)
from app.services.storage_service import upload_file

logger = logging.getLogger(__name__)


MAX_SOURCE_CHARS = 30000
RESUME_UNAVAILABLE = (
    "Your resume could not be read right now (document search is unavailable). "
    "Nothing was generated; please try again in a few minutes."
)
RESUME_SEARCH_DEGRADED = (
    "Your documents could not be searched, so this version was tailored from your "
    "profile text only. Check it carefully before using it."
)


def _full_resume_text(user_id: str) -> str:
    """The whole stored resume (picked one, else primary), or "" when unavailable.

    Retrieval returns a handful of 500-character chunks: roughly one page. A
    longer resume would lose whole employers before the model ever saw them.
    """
    try:
        from sqlalchemy import select

        from app.core.sync_db import _get_sync_factory, _to_uuid, fetch_chosen_resume
        from app.models.db import UserDocument

        doc = fetch_chosen_resume(user_id)
        if doc is None:
            with _get_sync_factory()() as session:
                doc = (
                    session.execute(
                        select(UserDocument)
                        .where(
                            UserDocument.user_id == _to_uuid(user_id),
                            UserDocument.doc_type == "resume",
                        )
                        .order_by(UserDocument.is_primary.desc())
                    )
                    .scalars()
                    .first()
                )
        text = getattr(doc, "raw_text", None)
        return text if isinstance(text, str) and len(text) <= MAX_SOURCE_CHARS else ""
    except Exception as exc:
        logger.info("Full resume text unavailable, using retrieved chunks: %s", type(exc).__name__)
        return ""


def _parse_back(
    pdf_bytes: bytes, markdown: str, full_name: str | None, page_target: int | None
) -> dict | None:
    """ATS read-back of the PDF just produced; a failing check never fails the run."""
    try:
        return check_pdf(pdf_bytes, markdown, full_name or "", page_target).to_dict()
    except Exception as exc:
        logger.warning("Resume parse-back check failed: %s", type(exc).__name__)
        return None


def _persist_resume_document(
    user_id: str,
    full_name: str | None,
    template: str,
    parsed: "ResumeOutput",
    jd_text: str,
    pdf_bytes: bytes,
    warnings: list[str] | None = None,
    page_target: int | None = 2,
    parse_back: dict | None = None,
) -> str:
    """Upload the tailored PDF to storage and record a UserDocument row.

    ``warnings`` is the model's original list (defaults to parsed.warnings);
    readers filter out the ones the resume has since resolved, so fixing a
    gap later never loses a warning permanently.

    Returns the new document id (str). No base64 is ever returned or stored —
    downloads go through GET /resume/download/{document_id}.
    """
    from app.core.sync_db import _get_sync_factory, _to_uuid
    from app.models.db import UserDocument
    from app.services.storage_service import delete_file

    storage_path = upload_file(user_id, "resume.pdf", pdf_bytes, "application/pdf")
    factory = _get_sync_factory()
    try:
        with factory() as session:
            doc = UserDocument(
                user_id=_to_uuid(user_id),
                doc_type="resume_tailored",
                filename="resume.pdf",
                storage_path=storage_path,
                raw_text=parsed.resume_markdown,
                ats_score=parsed.ats_score,
                ats_data={
                    "page_target": page_target,
                    "estimate": estimate_resume(parsed.resume_markdown, jd_text),
                    "template": template,
                    "parse_back": parse_back,
                    "keywords_matched": parsed.keywords_matched,
                    "keywords_missing": parsed.keywords_missing,
                    "warnings": list((parsed.warnings if warnings is None else warnings) or []),
                    "summary": parsed.summary,
                    "changes_made": list(parsed.changes_made or []),
                    # Kept so fixes applied later can be re-scored against
                    # the same job. It can be scraped third-party text (the
                    # auto-apply pipeline passes job-board descriptions), so
                    # it is capped and never returned by GET /rag/documents.
                    "jd_text": (jd_text or "")[:20000],
                },
            )
            session.add(doc)
            session.commit()
            return str(doc.id)
    except Exception:
        try:
            delete_file(storage_path, user_id)
        except Exception as cleanup_exc:
            logger.warning("Orphan storage cleanup failed for %s: %s", storage_path, cleanup_exc)
        raise


def _finalize_markdown(
    parsed: "ResumeOutput",
    full_name: str | None,
    verified_contact: dict,
    saved_facts: dict,
) -> tuple["ResumeOutput", dict | None]:
    """Deterministic clean-up after the model: strip placeholders, then fill
    gaps from facts the user typed in (contact, saved dates/education).

    Returns (parsed, review). review is None when there is no resume at all
    (the model returned NOT_PROVIDED for an empty source); the draft is then
    emptied and scored 0 so nothing is rendered or scored.

    parsed.warnings is left as the model wrote it: the caller persists that
    list and filters it against ``review`` for display.
    """
    markdown = clean_placeholders(parsed.resume_markdown or "")
    if not markdown.strip():
        parsed.resume_markdown = ""
        parsed.ats_score = 0
        return parsed, None
    # Independent steps: one that cannot be applied must not skip the others.
    try:
        markdown = apply_saved_facts(markdown, saved_facts)
    except ValueError as exc:
        logger.warning("Saved resume facts could not be applied: %s", exc)
    try:
        markdown = ensure_contact(markdown, verified_contact)
    except ValueError as exc:
        logger.warning("Verified contact details could not be applied: %s", exc)
    if full_name:
        try:
            markdown = apply_fixes(markdown, full_name=full_name)
        except ValueError as exc:
            logger.warning("Resume name heading could not be applied: %s", exc)
    parsed.resume_markdown = markdown
    return parsed, review_resume(markdown)


def _ground_resume(
    llm,
    parsed: "ResumeOutput",
    source: str,
    full_name: str | None,
    verified_contact: dict,
    saved_facts: dict,
    jd_text: str = "",
    prompt_source: str | None = None,
) -> tuple["ResumeOutput", list[str]]:
    """Make sure the tailored text only says what the candidate's own
    documents say. One repair pass asks the model to remove what is not in the
    source; whatever is still unsupported is returned for the caller to act on.

    ``source`` (contact details included) is what the draft is checked
    against; the repair prompt gets ``prompt_source`` and the draft with
    contact details removed, and the contact line is put back afterwards.
    """
    unsupported = unsupported_claims(parsed.resume_markdown, source, jd_text)
    if not unsupported:
        return parsed, []
    repair_prompt = (
        "Your resume draft contains details that do not appear in the candidate's source "
        "documents. Remove or reword every one so the resume states only what the source "
        "states. Do not add anything new. Contact details have been removed and are added "
        "back afterwards; do not add any.\n"
        f"UNSUPPORTED: {', '.join(unsupported[:30])}\n\n"
        f"DRAFT_RESUME_MARKDOWN:\n{strip_contact_details(parsed.resume_markdown)}\n\n"
        f"CANDIDATE_SOURCE:\n{strip_contact_details(prompt_source or source)[:12000]}"
    )
    try:
        repaired = call_llm_json(llm, RESUME_JSON_SYSTEM_PROMPT, repair_prompt, ResumeOutput)
    except Exception as exc:  # the first draft stays and is reported as unsupported
        logger.warning("Resume grounding repair failed: %s", type(exc).__name__)
        return parsed, unsupported
    # The draft's own contact line (from the candidate's resume) fills what the
    # verified profile lacks, so a repair never drops the header.
    draft_contact = parse_contact(parsed.resume_markdown)
    contact = {**draft_contact, **{k: v for k, v in verified_contact.items() if v}}
    repaired, _ = _finalize_markdown(repaired, full_name, contact, saved_facts)
    remaining = unsupported_claims(repaired.resume_markdown, source, jd_text)
    if len(remaining) <= len(unsupported):
        return repaired, remaining
    return parsed, unsupported


def _score_parsed_resume(parsed: "ResumeOutput", jd_text: str) -> "ResumeOutput":
    """Overwrite the self-graded ATS score with the real computed score."""
    if not jd_text.strip():
        estimate = estimate_resume(parsed.resume_markdown)
        parsed.ats_score = estimate["composite_score"]
        parsed.keywords_missing = []
        parsed.keywords_matched = []
        return parsed
    ats = compute_ats_score(parsed.resume_markdown, jd_text)
    parsed.ats_score = ats.composite_score
    parsed.keywords_missing = list(ats.missing_keywords[:10])
    parsed.keywords_matched = list(ats.matched_keywords)
    return parsed


def _resume_pending_action(parsed: "ResumeOutput", pdf_document_id: str | None) -> dict:
    """Pending/review payload: schema dump + document id. No binary data."""
    result: dict = parsed.model_dump()
    result["type"] = "resume_ready"
    result["pdf_document_id"] = pdf_document_id
    return result


# ── LangGraph node (registered in the orchestrator) ──
def resume_agent_node(state: AgentState) -> AgentState:
    """LangGraph node: tailors the resume via prompts/resume_prompt + JSON parsing.

    All prompt text lives in app/agents/prompts/resume_prompt.py. The result
    equals OUTPUT_SCHEMA.model_dump() + {"pdf_document_id"}. No binary data
    is placed in state, SSE events, or the DB — the PDF is stored on local
    disk and downloaded via GET /resume/download/{document_id}.
    """
    from app.core.event_bus import emit
    from app.core.llm_gateway import build_gateway_llm
    from app.core.sync_db import fetch_model_settings, fetch_user_full_name

    run_id = state["run_id"]
    user_id = state["user_id"]
    ctx = state.get("context", {})
    jd_text = ctx.get("jd_text", ctx.get("job_description", ""))
    tone = ctx.get("tone", "professional")
    full_name = None
    template = ctx.get("template", "modern")

    try:
        emit(
            run_id,
            "thinking",
            {"step": "start", "message": "Retrieving resume context from RAG..."},
        )
        model_settings = state.get("model_settings") or fetch_model_settings(user_id)
        if not model_settings:
            raise ValueError("No active model settings configured for user")

        full_name = fetch_user_full_name(user_id)
        from app.services.resume_facts import fetch_resume_facts_sync

        verified_contact, saved_facts = fetch_resume_facts_sync(user_id)

        emit(
            run_id,
            "tool_call",
            {
                "tool": "rag_retrieve",
                "input": {"doc_type": "resume", "query_len": len(jd_text)},
            },
        )
        resume_chunks = retrieve(user_id, "resume", jd_text, model_settings, k=8)
        rag_failed = retrieval_unavailable(resume_chunks)
        chunk_texts = [
            c.page_content if hasattr(c, "page_content") else str(c) for c in resume_chunks
        ]
        full_text = _full_resume_text(user_id)
        if full_text.strip():
            chunk_texts = [full_text]
            rag_failed = False  # the whole stored resume needs no search
        elif rag_failed and not any(t.strip() for t in chunk_texts):
            # A failed search is not an empty resume: generating now would
            # invent one from the job description alone.
            emit(run_id, "error", {"message": RESUME_UNAVAILABLE})
            return {**state, "status": "failed", "error": RESUME_UNAVAILABLE}
        emit(
            run_id,
            "tool_result",
            {"tool": "rag_retrieve", "output": {"chunks": len(resume_chunks)}},
        )

        llm = build_gateway_llm(model_settings, user_id)

        emit(
            run_id,
            "thinking",
            {"step": "tailor", "message": "Tailoring resume to job description..."},
        )
        parsed = call_llm_json(
            llm,
            RESUME_JSON_SYSTEM_PROMPT,
            build_resume_json_prompt(
                {
                    "jd_text": jd_text,
                    "tone": tone,
                    "template": template,
                    "verified_facts": saved_facts,
                    "page_target": ctx.get("page_target", 2),
                },
                chunk_texts,
            ),
            ResumeOutput,
        )
        parsed, review = _finalize_markdown(parsed, full_name, verified_contact, saved_facts)
        source = source_text(chunk_texts, saved_facts, full_name, json.dumps(verified_contact))
        prompt_facts = {k: v for k, v in (saved_facts or {}).items() if k != "contact"}
        parsed, unsupported = _ground_resume(
            llm,
            parsed,
            source,
            full_name,
            verified_contact,
            saved_facts,
            jd_text,
            prompt_source=source_text(chunk_texts, prompt_facts),
        )
        removed: list[str] = []
        if unsupported and parsed.resume_markdown.strip():
            # Still unsupported after the repair: taken out, never shipped. What
            # the member confirmed (saved facts) is part of `source` already.
            parsed.resume_markdown, removed = remove_unsupported(
                parsed.resume_markdown, source, jd_text
            )
            unsupported = unsupported_claims(parsed.resume_markdown, source, jd_text)
        if removed:
            parsed.warnings = list(parsed.warnings or []) + [
                "Removed because your documents do not support it: " + ", ".join(removed[:10])
            ]
        if rag_failed:
            parsed.warnings = list(parsed.warnings or []) + [RESUME_SEARCH_DEGRADED]
        if parsed.resume_markdown.strip():
            review = review_resume(parsed.resume_markdown)
        if unsupported:
            parsed.warnings = list(parsed.warnings or []) + [
                "Not found in your documents, please check or remove: "
                + ", ".join(unsupported[:10])
            ]
        if parsed.resume_markdown.strip():
            parsed.warnings = list(parsed.warnings or []) + [
                *date_issues(parsed.resume_markdown),
                *(
                    f"Left out of this tailored version: {name}"
                    for name in omitted_entries(parsed.resume_markdown, source, saved_facts)
                ),
            ]
        model_warnings = list(parsed.warnings or [])

        pdf_document_id = None
        if review is None:
            # The source was empty/unreadable: there is no resume to render
            # or score.
            emit(
                run_id,
                "tool_result",
                {"tool": "pdf_store", "output": {"pdf_document_id": None}},
            )
        else:
            parsed = _score_parsed_resume(parsed, jd_text)
            parsed.warnings = filter_resolved_warnings(model_warnings, review)
            emit(
                run_id,
                "thinking",
                {"step": "pdf", "message": "Generating PDF and storing..."},
            )
            emit(
                run_id,
                "tool_call",
                {"tool": "pdf_store", "input": {"template": template}},
            )
            page_target = ctx.get("page_target", 2)
            try:
                pdf_bytes = generate_resume_pdf(
                    parsed.resume_markdown,
                    full_name=full_name,
                    template=template,
                    page_target=page_target,
                )
            except ValueError as fit_error:
                # Fitting is strict (readable fonts, every glyph). A resume that
                # can't meet it still gets the template's own render instead of
                # failing the run; the editor can re-fit it later.
                logger.info("Resume fit failed, using template layout: %s", fit_error)
                pdf_bytes = generate_resume_pdf(
                    parsed.resume_markdown, full_name=full_name, template=template
                )
                page_target = None
                fit_warning = (
                    str(fit_error)
                    if isinstance(fit_error, PageOverflow)
                    else "Some characters could not be rendered in this template's fonts."
                )
                model_warnings = list(model_warnings or []) + [fit_warning]
                parsed.warnings = list(parsed.warnings or []) + [fit_warning]
            parse_back = _parse_back(pdf_bytes, parsed.resume_markdown, full_name, page_target)
            if parse_back:
                parsed.warnings = list(parsed.warnings or []) + parse_back["warnings"]
            try:
                pdf_document_id = _persist_resume_document(
                    user_id,
                    full_name,
                    template,
                    parsed,
                    jd_text,
                    pdf_bytes,
                    warnings=model_warnings,
                    page_target=page_target,
                    parse_back=parse_back,
                )
            except Exception as se:
                logger.warning("Resume PDF persist failed, continuing without download: %s", se)
                pdf_document_id = None
                parsed.warnings = list(parsed.warnings or []) + [
                    "PDF storage unavailable — preview only."
                ]
            emit(
                run_id,
                "tool_result",
                {"tool": "pdf_store", "output": {"pdf_document_id": pdf_document_id}},
            )

        pending = _resume_pending_action(parsed, pdf_document_id)
        pending["review"] = review
        pending["grounding"] = {"checked": True, "unsupported": unsupported, "removed": removed}
        pending["template"] = template
        emit(run_id, "complete", {"result": pending})
        return {
            **state,
            "status": "awaiting_approval",
            "pending_action": pending,
            "result": pending,
            "messages": state.get("messages", [])
            + [AIMessage(content=parsed.resume_markdown[:200])],
        }
    except Exception as exc:
        logger.warning("resume_agent_failed user_id=%s error_type=%s", user_id, type(exc).__name__)
        return {
            **state,
            "status": "failed",
            "error": "Resume generation failed. Check your model settings and try again.",
        }
