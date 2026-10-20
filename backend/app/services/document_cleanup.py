"""Transactional deletion outbox; retained until storage and vectors are gone."""

import asyncio
import logging

from sqlalchemy import text

from app.services.document_lifecycle import is_text_artifact
from app.services.storage_service import delete_file

logger = logging.getLogger(__name__)


async def enqueue_document_cleanup(db, doc) -> None:
    await db.execute(
        text("""
        INSERT INTO document_cleanup_queue (document_id, user_id, storage_path, filename)
        VALUES (:document_id, :user_id, :storage_path, :filename)
        ON CONFLICT (document_id) DO NOTHING
    """),
        {
            "document_id": doc.id,
            "user_id": doc.user_id,
            "storage_path": "" if is_text_artifact(doc) else doc.storage_path,
            "filename": doc.filename,
        },
    )


async def sweep_document_cleanup(limit: int = 100) -> int:
    from app.core.database import AsyncSessionLocal

    cleaned = 0
    # One row/transaction prevents a bad file from rolling back other owners.
    async with AsyncSessionLocal() as db:
        ids = (
            (
                await db.execute(
                    text("""
            SELECT document_id FROM document_cleanup_queue
            WHERE next_attempt_at <= now()
            ORDER BY next_attempt_at, created_at LIMIT :limit
        """),
                    {"limit": max(1, min(limit, 1000))},
                )
            )
            .scalars()
            .all()
        )
        await db.rollback()
        for document_id in ids:
            try:
                row = (
                    (
                        await db.execute(
                            text("""
                    SELECT * FROM document_cleanup_queue WHERE document_id = :id
                    FOR UPDATE SKIP LOCKED
                """),
                            {"id": document_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    await db.rollback()
                    continue
                legacy_text_path = f"cover_letters/{row['user_id']}/{document_id}.md"
                if row["storage_path"] and row["storage_path"] != legacy_text_path:
                    await asyncio.to_thread(delete_file, row["storage_path"], str(row["user_id"]))
                exists = (
                    await db.execute(text("SELECT to_regclass('public.langchain_pg_embedding')"))
                ).scalar()
                if exists:
                    await db.execute(
                        text("""
                        DELETE FROM langchain_pg_embedding
                        WHERE cmetadata->>'user_id' = :owner AND (
                            cmetadata->>'document_id' = :id OR (
                                cmetadata->>'document_id' IS NULL
                                AND cmetadata->>'filename' = :filename
                            )
                        )
                    """),
                        {
                            "owner": str(row["user_id"]),
                            "id": str(document_id),
                            "filename": row["filename"],
                        },
                    )
                await db.execute(
                    text("DELETE FROM document_cleanup_queue WHERE document_id = :id"),
                    {"id": document_id},
                )
                await db.commit()
                cleaned += 1
            except Exception:
                await db.rollback()
                await db.execute(
                    text("""
                        UPDATE document_cleanup_queue
                        SET next_attempt_at = now() + interval '5 minutes'
                        WHERE document_id = :id
                    """),
                    {"id": document_id},
                )
                await db.commit()
                logger.warning("Document cleanup deferred for %s", document_id, exc_info=True)
    return cleaned
