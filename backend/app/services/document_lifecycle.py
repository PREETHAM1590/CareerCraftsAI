"""Shared document contracts and serialization with account erasure."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app.models.db import User

RESUME_DOCUMENT_TYPES = ("resume", "resume_tailored")


async def lock_document_owner(db, owner_id) -> User:
    """Lock the owner before any document lock or storage publication.

    The account eraser takes this same lock before sweeping storage. Hold it
    until the document transaction commits, and refresh identity-map values.
    """
    owner = (
        await db.execute(
            select(User)
            .where(User.id == owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if owner is None or (
        owner.deletion_scheduled_for is not None
        and owner.deletion_scheduled_for <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=403, detail="Account deletion is in progress")
    return owner


def is_text_artifact(document) -> bool:
    """Generated letters live in raw_text; uploaded letters retain their bytes.

    Recognize the exact legacy placeholder path as well as the empty storage
    path used by new generated artifacts. Neither is a filesystem location.
    """
    return (
        document.doc_type == "cover_letter"
        and document.raw_text is not None
        and document.storage_path in ("", f"cover_letters/{document.user_id}/{document.id}.md")
    )
