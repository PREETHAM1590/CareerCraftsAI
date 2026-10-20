import asyncio
import logging
import os
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.db import User, UserDocument, UserModelSettings
from app.services.document_lifecycle import lock_document_owner as _lock_document_owner
from app.services.drive_service import DriveError, upload_to_drive
from app.services.rag_service import EmbeddingUnavailable, ingest_document
from app.services.storage_service import download_file, upload_file

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
VALID_DOC_TYPES = {"resume", "jd", "cert", "portfolio", "cover_letter"}

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
# Signatures that must never pass as "text/plain" regardless of what the
# client declares — a security review found the caller-declared Content-Type
# was trusted outright, letting arbitrary bytes in under a false label.
_BINARY_MAGICS = (
    _PDF_MAGIC,
    *_ZIP_MAGICS,
    b"MZ",  # Windows PE/EXE
    b"\x7fELF",
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG",
    b"GIF8",
)


def _sniff_content_type(content: bytes) -> str | None:
    from app.services.document_parsing import sniff_content_type

    return sniff_content_type(content)


async def _read_bounded(file: UploadFile) -> bytes:
    content = bytearray()
    while True:
        piece = await file.read(min(65536, MAX_SIZE_BYTES + 1 - len(content)))
        if not piece:
            return bytes(content)
        content.extend(piece)
        if len(content) > MAX_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="File too large — max 10 MB")


_parser_slots = asyncio.Semaphore(2)


def _safe_filename(filename: str | None) -> str:
    """Strip directory components and unsafe characters — a review found
    the raw client filename (e.g. ``../../etc/passwd.txt``) was stored and
    echoed back unchanged."""
    name = os.path.basename((filename or "upload.bin").replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return (name or "upload.bin")[:255]


_PRIVATE_ATS_KEYS = frozenset({"jd_text"})


def _public_ats_data(data: dict | None) -> dict | None:
    """ats_data without internal keys. Tailored resumes keep the job
    description they were scored against (up to 20 KB of possibly scraped
    text) for re-scoring; it is not part of the document listing."""
    if data is None:
        return None
    return {k: v for k, v in data.items() if k not in _PRIVATE_ATS_KEYS}


class DocumentResponse(BaseModel):
    id: uuid.UUID
    doc_type: str
    filename: str
    is_primary: bool
    embedded_at: datetime | None
    ats_score: int | None = None
    ats_data: dict | None = None
    warning: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("ats_data")
    @classmethod
    def _strip_private_ats_data(cls, value: dict | None) -> dict | None:
        return _public_ats_data(value)


async def _score_resume_background(doc_id: str, user_id: str, raw_text: str) -> None:
    """Compute ATS score asynchronously after resume upload.

    Keywords are measured against up to three jobs the user saved with a
    description; with none saved the score covers readability and format
    only. user_id is passed explicitly so the update query can scope to the
    owner — prevents a latent IDOR if the background task is ever called
    with untrusted input.
    """
    try:
        from app.core.database import AsyncSessionLocal
        from app.models.db import JobApplication, UserDocument
        from app.services.ats_service import score_resume_baseline

        owner = uuid.UUID(str(user_id))
        async with AsyncSessionLocal() as db:
            target_jds = (
                (
                    await db.execute(
                        select(JobApplication.jd_text)
                        .where(
                            JobApplication.user_id == owner,
                            func.length(JobApplication.jd_text) > 200,
                        )
                        .order_by(
                            JobApplication.applied_at.desc().nullslast(),
                            JobApplication.match_score.desc().nullslast(),
                        )
                        .limit(3)
                    )
                )
                .scalars()
                .all()
            )
            score, ats_data = score_resume_baseline(raw_text, "\n\n".join(target_jds) or None)

            res = await db.execute(
                select(UserDocument).where(
                    UserDocument.id == uuid.UUID(doc_id),
                    UserDocument.user_id == owner,
                )
            )
            doc = res.scalar_one_or_none()
            if doc:
                doc.ats_score = score
                previous = {k: v for k, v in (doc.ats_data or {}).items() if k != "score_error"}
                doc.ats_data = {**previous, **ats_data}
                await db.commit()
    except Exception as exc:
        logger.warning("Background ATS scoring failed for doc %s: %s", doc_id, exc)
        await _mark_score_failed(doc_id, user_id, "scoring_failed")


async def _mark_score_failed(doc_id: str, user_id: str, reason: str) -> None:
    """ats_score stays null; ats_data.score_error tells the UI to stop waiting."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import UserDocument

    try:
        async with AsyncSessionLocal() as db:
            doc = (
                await db.execute(
                    select(UserDocument).where(
                        UserDocument.id == uuid.UUID(doc_id),
                        UserDocument.user_id == uuid.UUID(str(user_id)),
                    )
                )
            ).scalar_one_or_none()
            if doc and doc.ats_score is None:
                doc.ats_data = {**(doc.ats_data or {}), "score_error": reason}
                await db.commit()
    except Exception:
        logger.warning("Could not record the scoring failure for doc %s", doc_id)


router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/upload", response_model=DocumentResponse, status_code=201)
@limiter.limit(settings.RATE_LIMIT_UPLOAD)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    doc_type: str = Form(...),
    is_primary: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid doc_type. Must be one of: {VALID_DOC_TYPES}",
        )
    content = await _read_bounded(file)
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="File too large — max 10 MB")

    # Validate the file's actual bytes, not the client-declared Content-Type
    # or filename extension — either can claim anything.
    sniffed_type = _sniff_content_type(content)
    if sniffed_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Only PDF, DOCX, and TXT files are supported",
        )
    safe_filename = _safe_filename(file.filename)

    from app.services.document_parsing import parse_isolated

    if _parser_slots.locked():
        raise HTTPException(status_code=429, detail="Document parsers are busy; retry shortly")
    try:
        async with _parser_slots:
            raw_text = await asyncio.to_thread(parse_isolated, content, sniffed_type)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="Could not read this document within parsing limits"
        ) from exc

    current_user = await _lock_document_owner(db, current_user.id)
    storage_path = upload_file(
        str(current_user.id),
        safe_filename,
        content,
        sniffed_type,
    )
    upload_warning: str | None = None
    if safe_filename.lower().endswith(".pdf") and len(raw_text.strip()) < 100:
        upload_warning = (
            "Scanned or image-only PDF detected — text extraction yielded little content. "
            "Re-upload a text-based PDF for best results."
        )

    result = await db.execute(
        select(UserModelSettings).where(
            UserModelSettings.user_id == current_user.id,
            UserModelSettings.is_active == True,  # noqa: E712
        )
    )
    model_settings = result.scalars().first()

    document_id = uuid.uuid4()
    embedded_at = None
    if model_settings:
        try:
            await asyncio.to_thread(
                ingest_document,
                str(current_user.id),
                doc_type,
                raw_text,
                {
                    "user_id": str(current_user.id),
                    "doc_type": doc_type,
                    "filename": safe_filename,
                    "document_id": str(document_id),
                },
                model_settings,
            )
            embedded_at = datetime.now(UTC)
        except Exception as exc:
            logger.warning(
                "Embedding failed for doc_type=%s user=%s: %s",
                doc_type,
                current_user.id,
                exc,
            )
            if isinstance(exc, EmbeddingUnavailable):
                reason = (
                    f"your {model_settings.provider} model can't create search embeddings "
                    "and no embedding fallback is configured (EMBEDDING_PROVIDER)."
                )
            else:
                reason = "the embedding service failed — check Settings → Models."
            upload_warning = (
                (upload_warning + " ") if upload_warning else ""
            ) + f"Document saved but not indexed for AI search: {reason}"
            embedded_at = None

    if is_primary and doc_type == "resume":
        await _clear_primary_resume(db, current_user.id)
    doc = UserDocument(
        id=document_id,
        user_id=current_user.id,
        doc_type=doc_type,
        filename=safe_filename,
        storage_path=storage_path,
        raw_text=raw_text,
        embedded_at=embedded_at,
        is_primary=is_primary,
        # Image-only PDFs extract no text, so there is nothing to score.
        ats_data=(None if raw_text.strip() or doc_type != "resume" else {"score_error": "no_text"}),
    )
    db.add(doc)
    await db.flush()

    # Trigger ATS scoring in background for resumes. Commit first: the task
    # updates this row from its own session and must be able to see it.
    if doc_type == "resume" and raw_text:
        await db.commit()
        from app.core.background import spawn_background

        spawn_background(_score_resume_background(str(doc.id), str(current_user.id), raw_text))

    return DocumentResponse(
        id=doc.id,
        doc_type=doc.doc_type,
        filename=doc.filename,
        is_primary=doc.is_primary,
        embedded_at=doc.embedded_at,
        ats_score=doc.ats_score if hasattr(doc, "ats_score") else None,
        ats_data=doc.ats_data if hasattr(doc, "ats_data") else None,
        warning=upload_warning,
    )


@router.get("/documents", response_model=list[DocumentResponse])
async def list_documents(
    doc_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(UserDocument).where(UserDocument.user_id == current_user.id)
    if doc_type:
        if doc_type not in VALID_DOC_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid doc_type. Must be one of: {VALID_DOC_TYPES}",
            )
        query = query.where(UserDocument.doc_type == doc_type)
    result = await db.execute(query)
    docs = result.scalars().all()
    await _refresh_stale_resume_scores(db, current_user.id, docs)
    return docs


async def _refresh_stale_resume_scores(db: AsyncSession, user_id, docs) -> None:
    """Re-score resumes whose keywords were measured against nothing real.

    Covers scores from before keyword_basis existed (they used a generic
    placeholder JD) and readability-only scores once the user has saved
    jobs to measure against. Runs in the background; the next load shows it.
    """
    from app.core.background import spawn_background
    from app.models.db import JobApplication

    candidates = [
        doc
        for doc in docs
        if doc.doc_type == "resume"
        and doc.raw_text
        and isinstance(doc.ats_data, dict)
        and doc.ats_data.get("keyword_basis") is None
    ]
    if not candidates:
        return
    has_saved_jobs = None
    for doc in candidates:
        if "keyword_basis" in doc.ats_data:
            if has_saved_jobs is None:
                has_saved_jobs = bool(
                    (
                        await db.execute(
                            select(JobApplication.id)
                            .where(
                                JobApplication.user_id == user_id,
                                func.length(JobApplication.jd_text) > 200,
                            )
                            .limit(1)
                        )
                    ).first()
                )
            if not has_saved_jobs:
                continue
        spawn_background(_score_resume_background(str(doc.id), str(user_id), doc.raw_text))


async def _clear_primary_resume(db: AsyncSession, user_id) -> None:
    await db.execute(
        update(UserDocument)
        .where(
            UserDocument.user_id == user_id,
            UserDocument.doc_type == "resume",
            UserDocument.is_primary.is_(True),
        )
        .values(is_primary=False)
    )


@router.post("/documents/{document_id}/activate", response_model=DocumentResponse)
async def activate_resume(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Make this resume the member's one active resume, then re-score it so
    the dashboard and resume page (both read ``ats_score``) follow it."""
    from app.core.background import spawn_background

    await _lock_document_owner(db, current_user.id)
    doc = (
        await db.execute(
            select(UserDocument)
            .where(
                UserDocument.id == document_id,
                UserDocument.user_id == current_user.id,
                UserDocument.doc_type == "resume",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not doc.is_primary:
        await _clear_primary_resume(db, current_user.id)
        doc.is_primary = True
        await db.commit()
        await db.refresh(doc)
        if doc.raw_text:
            spawn_background(
                _score_resume_background(str(doc.id), str(current_user.id), doc.raw_text)
            )
    return doc


@router.get("/documents/{document_id}/ats", response_model=dict)
async def get_ats_score(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(UserDocument).where(
            UserDocument.id == document_id,
            UserDocument.user_id == current_user.id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "ats_score": doc.ats_score,
        "ats_data": _public_ats_data(doc.ats_data),
    }


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _lock_document_owner(db, current_user.id)
    result = await db.execute(
        select(UserDocument)
        .where(
            UserDocument.id == document_id,
            UserDocument.user_id == current_user.id,
        )
        .with_for_update()
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    from app.services.document_cleanup import enqueue_document_cleanup

    await enqueue_document_cleanup(db, doc)
    await db.delete(doc)
    await db.flush()


_DRIVE_MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "txt": "text/plain",
    "md": "text/markdown",
}


class SaveToDriveResponse(BaseModel):
    id: str
    name: str
    web_view_link: str | None = None


@router.post("/documents/{document_id}/save-to-drive", response_model=SaveToDriveResponse)
async def save_document_to_drive(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Push a stored document (e.g. resume) to the user's connected Google Drive."""
    result = await db.execute(
        select(UserDocument).where(
            UserDocument.id == document_id,
            UserDocument.user_id == current_user.id,
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    owner_id = str(current_user.id)
    ext = doc.filename.rsplit(".", 1)[-1].lower() if "." in doc.filename else ""
    mime_type = _DRIVE_MIME_BY_EXT.get(ext, "application/octet-stream")

    try:
        from app.services.document_lifecycle import is_text_artifact

        content = (
            doc.raw_text.encode("utf-8")
            if is_text_artifact(doc)
            else await asyncio.to_thread(download_file, doc.storage_path, owner_id)
        )
    except Exception as exc:
        logger.warning("Drive save: download failed for %s: %s", doc.storage_path, exc)
        raise HTTPException(status_code=502, detail="Could not read the stored document") from exc

    try:
        uploaded = await asyncio.to_thread(
            upload_to_drive, owner_id, doc.filename, content, mime_type
        )
    except DriveError as exc:
        # Actionable Google reason (scopes / API disabled / token) — safe to show.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Drive save: upload failed for %s: %s", doc.filename, exc)
        raise HTTPException(status_code=502, detail="Drive upload failed") from exc

    return SaveToDriveResponse(
        id=uploaded.get("id", ""),
        name=uploaded.get("name", doc.filename),
        web_view_link=uploaded.get("webViewLink"),
    )
