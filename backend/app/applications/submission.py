"""Approved application artifacts and the durable submission ledger."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.db import UserDocument
from app.services.document_lifecycle import RESUME_DOCUMENT_TYPES

SUBMIT_NAME = re.compile(r"^(submit(?: your)? application|send application|submit)$", re.I)
CONFIRMATION = re.compile(
    r"application (?:has been |was )?(?:successfully )?submitted|"
    r"thank you for applying|we have received your application",
    re.I,
)


async def load_resume(user_id: uuid.UUID, document_id: str) -> tuple[bytes, str]:
    from app.services.storage_service import download_file

    async with AsyncSessionLocal() as db:
        document = (
            await db.execute(
                select(UserDocument).where(
                    UserDocument.id == uuid.UUID(document_id),
                    UserDocument.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if not document or document.doc_type not in RESUME_DOCUMENT_TYPES:
            raise ValueError("Approved resume is unavailable")
    content = await asyncio.to_thread(download_file, document.storage_path, str(user_id))
    if len(content) > 10 * 1024 * 1024 or not content.startswith(b"%PDF"):
        raise ValueError("Resume must be a PDF smaller than 10 MB")
    return content, hashlib.sha256(content).hexdigest()


async def claim_attempt_for_submit(attempt_id: str, approved_snapshot_hash: str | None):
    """Atomic awaiting_approval -> submitting transition.

    Returns the claimed ApplicationAttempt, or None if the attempt is not
    (or no longer) in awaiting_approval — meaning a concurrent caller already
    claimed it, or it is in some other state. Callers MUST NOT click Submit
    unless this returns a row: this is the single compare-and-swap guard
    that makes the external submit click at-most-once regardless of how
    many callers reach this point concurrently.
    """
    from app.models.db import ApplicationAttempt

    async with AsyncSessionLocal() as db:
        attempt = await db.get(ApplicationAttempt, uuid.UUID(attempt_id), with_for_update=True)
        if attempt is None or attempt.state != "awaiting_approval":
            return None
        attempt.state = "submitting"
        attempt.submission_token = str(uuid.uuid4())
        attempt.approved_snapshot_hash = approved_snapshot_hash
        await db.commit()
        return attempt
