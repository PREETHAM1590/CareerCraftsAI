"""The approved resume for an application. Submission itself happens in the
member's browser extension (see api/v1/extension.py approve-submit)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.db import UserDocument
from app.services.document_lifecycle import RESUME_DOCUMENT_TYPES


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
