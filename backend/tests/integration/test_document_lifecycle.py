"""Real pgvector dimension indexes and deletion using disposable infrastructure."""

import asyncio
import io
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.embeddings import Embeddings
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1",
    reason="Disposable infrastructure required",
)


@pytest.mark.asyncio
async def test_delete_referenced_document_preserves_history_and_enqueues_cleanup(vector_db):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1.rag import delete_document
    from app.models.db import (
        CandidateProfile,
        CoverLetterVersion,
        JobApplication,
        User,
        UserDocument,
    )

    _, engine = vector_db
    with engine.begin() as connection:
        connection.execute(
            text(
                Path(
                    "../supabase/migrations/20261006102000_document_reference_deletion.sql"
                ).read_text()
            )
        )
    async_engine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    owner, document_id, application_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with maker() as db:
            user = User(id=owner, email=f"{owner}@example.test")
            db.add(user)
            await db.flush()
            db.add(
                UserDocument(
                    id=document_id,
                    user_id=owner,
                    doc_type="resume",
                    filename="resume.pdf",
                    storage_path=f"{owner}/resume.pdf",
                )
            )
            await db.flush()
            db.add(
                JobApplication(
                    id=application_id,
                    user_id=owner,
                    company="Acme",
                    role="Engineer",
                    resume_id=document_id,
                    cover_letter_id=document_id,
                )
            )
            db.add(CandidateProfile(user_id=owner, default_resume_id=document_id))
            await db.flush()
            db.add(
                CoverLetterVersion(
                    user_id=owner,
                    job_application_id=application_id,
                    document_id=document_id,
                    tone="formal",
                )
            )
            await db.commit()
            await delete_document(document_id, db, user)
            await db.commit()
        async with maker() as db:
            assert await db.get(UserDocument, document_id) is None
            app = await db.get(JobApplication, application_id)
            assert app is not None and app.resume_id is None and app.cover_letter_id is None
            assert (await db.get(CandidateProfile, owner)).default_resume_id is None
            assert (
                await db.execute(
                    text("SELECT count(*) FROM cover_letter_versions WHERE document_id=:id"),
                    {"id": document_id},
                )
            ).scalar() == 0
            assert (
                await db.execute(
                    text("SELECT storage_path FROM document_cleanup_queue WHERE document_id=:id"),
                    {"id": document_id},
                )
            ).scalar() == f"{owner}/resume.pdf"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_replacement_owner_guard_serializes_with_eraser(vector_db):
    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.db import User
    from app.services.document_lifecycle import lock_document_owner

    _, engine = vector_db
    async_engine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    owner = uuid.uuid4()
    try:
        async with maker() as db:
            db.add(User(id=owner, email=f"{owner}@example.test"))
            await db.commit()
        async with maker() as writer, maker() as eraser:
            await lock_document_owner(writer, owner)

            async def erase():
                row = (
                    await eraser.execute(select(User).where(User.id == owner).with_for_update())
                ).scalar_one()
                row.deletion_scheduled_for = datetime.now(UTC)
                await eraser.commit()

            pending = asyncio.create_task(erase())
            await asyncio.sleep(0.1)
            assert not pending.done(), "external sweep must wait for writer transaction"
            await writer.commit()
            await asyncio.wait_for(pending, timeout=3)
            with pytest.raises(HTTPException) as rejected:
                await lock_document_owner(writer, owner)
            assert rejected.value.status_code == 403
    finally:
        await async_engine.dispose()


class FakeEmbedding(Embeddings):
    def __init__(self, dimension):
        self.dimension = dimension

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * (self.dimension - 1)


@pytest.fixture
def vector_db(monkeypatch):
    import psycopg
    from psycopg import sql

    import app.models.db  # noqa: F401
    from app.core.database import Base

    # Other workflow tests intentionally use simplified stand-in vector tables.
    # Exercise actual PGVector DDL/indexes in an isolated disposable database.
    database_name = "document_test_" + uuid.uuid4().hex
    admin_url = "postgresql://workflow_test:workflow_test@127.0.0.1:55439/workflow_test"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    engine = create_engine(
        "postgresql+psycopg://workflow_test:workflow_test@127.0.0.1:55439/" + database_name
    )
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr("app.core.sync_db._get_sync_factory", lambda: factory)
        monkeypatch.setattr(
            "app.services.rag_service._psycopg_url",
            lambda: str(engine.url.render_as_string(hide_password=False)),
        )
        with engine.begin() as conn:
            conn.execute(
                text(
                    Path("../supabase/migrations/20261006101000_document_lifecycle.sql").read_text()
                )
            )
        yield factory, engine
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def test_migration_backfills_only_unambiguous_live_ownership(vector_db):
    from langchain_core.documents import Document

    from app.models.db import User, UserDocument
    from app.services.rag_service import get_vector_store

    factory, engine = vector_db
    owner = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(3)]
    with factory() as db:
        db.add(User(id=owner, email=f"{owner}@example.test"))
        db.flush()
        for document_id, name in zip(
            ids, ["unique.txt", "duplicate.txt", "duplicate.txt"], strict=True
        ):
            db.add(
                UserDocument(
                    id=document_id,
                    user_id=owner,
                    doc_type="jd",
                    filename=name,
                    storage_path=f"{owner}/{document_id}.txt",
                )
            )
        db.commit()
    try:
        get_vector_store(str(owner), "jd", FakeEmbedding(768), "google").add_documents(
            [
                Document(
                    page_content=name,
                    metadata={
                        "user_id": str(owner),
                        "doc_type": "jd",
                        "filename": name,
                    },
                )
                for name in ["unique.txt", "duplicate.txt", "deleted.txt"]
            ]
        )
        for provider, dimension in (("ollama", 1024), ("openai", 1536)):
            get_vector_store(str(owner), "jd", FakeEmbedding(dimension), provider).add_documents(
                [
                    Document(
                        page_content="unique.txt",
                        metadata={
                            "user_id": str(owner),
                            "doc_type": "jd",
                            "filename": "unique.txt",
                        },
                    )
                ]
            )
        with engine.begin() as db:
            db.execute(
                text(
                    Path("../supabase/migrations/20261006101000_document_lifecycle.sql").read_text()
                )
            )
            rows = db.execute(
                text(
                    "SELECT document, cmetadata FROM langchain_pg_embedding "
                    "WHERE cmetadata->>'user_id'=:owner"
                ),
                {"owner": str(owner)},
            ).all()
            assert len(rows) == 3
            assert all(row.document == "unique.txt" for row in rows)
            assert all(row.cmetadata["document_id"] == str(ids[0]) for row in rows)
            for dimension in (768, 1024, 1536):
                assert db.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname=:name"),
                    {"name": f"idx_langchain_embedding_hnsw_{dimension}"},
                ).scalar()
    finally:
        with engine.begin() as db:
            db.execute(
                text("DELETE FROM langchain_pg_embedding WHERE cmetadata->>'user_id'=:owner"),
                {"owner": str(owner)},
            )
        with factory() as db:
            db.execute(delete(UserDocument).where(UserDocument.user_id == owner))
            db.delete(db.get(User, owner))
            db.commit()


def test_all_dimensions_indexed_and_deleted_documents_excluded(vector_db):
    from app.models.db import User, UserDocument
    from app.services import rag_service as service

    factory, engine = vector_db
    owner, document_id, retained_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with factory() as db:
        db.add(User(id=owner, email=f"{owner}@example.test"))
        db.flush()
        db.add(
            UserDocument(
                id=document_id,
                user_id=owner,
                doc_type="jd",
                filename="resume.txt",
                storage_path=f"{owner}/test.txt",
                raw_text="Python",
            )
        )
        db.add(
            UserDocument(
                id=retained_id,
                user_id=owner,
                doc_type="jd",
                filename="keep.txt",
                storage_path=f"{owner}/keep.txt",
                raw_text="Retained",
            )
        )
        db.commit()
    try:
        for provider, dimension in service.EMBEDDING_DIMENSIONS.items():
            embeddings = FakeEmbedding(dimension)
            store = service.get_vector_store(str(owner), "jd", embeddings, provider)
            from langchain_core.documents import Document

            store.add_documents(
                [
                    Document(
                        page_content="owned",
                        metadata={
                            "user_id": str(owner),
                            "document_id": str(document_id),
                        },
                    ),
                    Document(
                        page_content="retained",
                        metadata={
                            "user_id": str(owner),
                            "document_id": str(retained_id),
                        },
                    ),
                    Document(page_content="legacy secret", metadata={"user_id": str(owner)}),
                ]
            )
            service._ensure_hnsw_index()
            found = service._search_live_documents(
                str(owner), "jd", provider, embeddings, "Python", 5
            )
            assert {row.page_content for row in found} == {"owned", "retained"}
            with engine.connect() as conn:
                assert conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname=:name"),
                    {"name": f"idx_langchain_embedding_hnsw_{dimension}"},
                ).scalar()
                conn.execute(text("SET LOCAL enable_seqscan = off"))
                plan = conn.execute(
                    text(f"""
                    EXPLAIN SELECT embedding FROM langchain_pg_embedding
                    WHERE vector_dims(embedding) = {dimension}
                    ORDER BY embedding::vector({dimension}) <=> CAST(:q AS vector({dimension}))
                    LIMIT 1
                """),
                    {"q": "[" + ",".join(map(str, embeddings.embed_query("q"))) + "]"},
                )
                assert f"idx_langchain_embedding_hnsw_{dimension}" in "\n".join(r[0] for r in plan)
        with factory() as db:
            db.delete(db.get(UserDocument, document_id))
            db.commit()

        for provider, dimension in service.EMBEDDING_DIMENSIONS.items():
            remaining = service._search_live_documents(
                str(owner), "jd", provider, FakeEmbedding(dimension), "Python", 5
            )
            assert [doc.page_content for doc in remaining] == ["retained"]
            metadata = dict(remaining[0].metadata)
            distance = metadata.pop("distance")
            assert isinstance(distance, float) and distance >= 0
            assert metadata == {"user_id": str(owner), "document_id": str(retained_id)}
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM langchain_pg_embedding WHERE cmetadata->>'user_id'=:owner"),
                {"owner": str(owner)},
            )
        with factory() as db:
            db.execute(delete(UserDocument).where(UserDocument.user_id == owner))
            db.delete(db.get(User, owner))
            db.commit()


@pytest.mark.asyncio
async def test_cleanup_failure_is_retried_without_cross_owner_deletion(vector_db, monkeypatch):
    from langchain_core.documents import Document
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.db import User, UserDocument
    from app.services.document_cleanup import (
        enqueue_document_cleanup,
        sweep_document_cleanup,
    )
    from app.services.rag_service import get_vector_store

    _, engine = vector_db
    async_engine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", maker)
    owner, other = uuid.uuid4(), uuid.uuid4()
    target, keep = uuid.uuid4(), uuid.uuid4()
    async with maker() as db:
        db.add_all([User(id=o, email=f"{o}@example.test") for o in (owner, other)])
        await db.flush()
        docs = [
            UserDocument(
                id=i,
                user_id=o,
                doc_type="jd",
                filename="same.txt",
                storage_path=f"{o}/{i}.txt",
            )
            for i, o in ((target, owner), (keep, other))
        ]
        db.add_all(docs)
        await db.flush()
        await enqueue_document_cleanup(db, docs[0])
        await db.delete(docs[0])
        await db.commit()
    for o, i in ((owner, target), (other, keep)):
        get_vector_store(str(o), "jd", FakeEmbedding(768), "google").add_documents(
            [
                Document(
                    page_content="secret",
                    metadata={"user_id": str(o), "document_id": str(i)},
                )
            ]
        )

    def broken_file(*args):
        raise OSError("retry")

    monkeypatch.setattr("app.services.document_cleanup.delete_file", broken_file)
    try:
        assert await sweep_document_cleanup() == 0
        with engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT count(*) FROM document_cleanup_queue WHERE document_id=:id"),
                    {"id": target},
                ).scalar()
                == 1
            )
        monkeypatch.setattr("app.services.document_cleanup.delete_file", lambda *args: None)
        with engine.begin() as db:
            db.execute(
                text(
                    "UPDATE document_cleanup_queue SET next_attempt_at = now() "
                    "WHERE document_id=:id"
                ),
                {"id": target},
            )
        assert await sweep_document_cleanup() >= 1
        with engine.connect() as db:
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM langchain_pg_embedding "
                        "WHERE cmetadata->>'user_id'=:owner"
                    ),
                    {"owner": str(owner)},
                ).scalar()
                == 0
            )
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM langchain_pg_embedding "
                        "WHERE cmetadata->>'user_id'=:owner"
                    ),
                    {"owner": str(other)},
                ).scalar()
                == 1
            )
    finally:
        async with maker() as db:
            await db.execute(delete(UserDocument).where(UserDocument.user_id.in_([owner, other])))
            for o in (owner, other):
                user = await db.get(User, o)
                await db.delete(user)
            await db.commit()
        with engine.begin() as db:
            db.execute(
                text(
                    "DELETE FROM langchain_pg_embedding "
                    "WHERE cmetadata->>'user_id' IN (:owner,:other)"
                ),
                {"owner": str(owner), "other": str(other)},
            )
            db.execute(
                text("DELETE FROM document_cleanup_queue WHERE document_id=:id"),
                {"id": target},
            )
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_inflight_upload_rechecks_erasure_and_primary_changes_serialize(
    vector_db, monkeypatch
):
    from unittest.mock import Mock

    from fastapi import HTTPException, Request, UploadFile
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.v1.rag import activate_resume, upload_document
    from app.models.db import User, UserDocument

    _, engine = vector_db
    async_engine = create_async_engine(engine.url.set(drivername="postgresql+asyncpg"))
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    owner = uuid.uuid4()
    docs = [uuid.uuid4(), uuid.uuid4()]
    async with maker() as db:
        user = User(id=owner, email=f"{owner}@example.test")
        db.add(user)
        await db.flush()
        db.add_all(
            [
                UserDocument(
                    id=i,
                    user_id=owner,
                    doc_type="resume",
                    filename="resume.txt",
                    storage_path=f"{owner}/{i}.txt",
                )
                for i in docs
            ]
        )
        await db.commit()
    try:

        async def activate(i):
            async with maker() as db:
                await activate_resume(i, db, user)
                await db.commit()

        await asyncio.gather(*(activate(i) for i in docs))
        async with maker() as db:
            assert (
                await db.execute(
                    text("SELECT count(*) FROM user_documents WHERE user_id=:id AND is_primary"),
                    {"id": owner},
                )
            ).scalar() == 1
            # Simulate a request admitted before its owner's deletion deadline.
            stale = await db.get(User, owner)
            async with maker() as eraser:
                due = await eraser.get(User, owner)
                due.deletion_scheduled_for = datetime.now(UTC)
                await eraser.commit()
            writer = Mock()
            monkeypatch.setattr("app.api.v1.rag.upload_file", writer)
            monkeypatch.setattr(
                "app.services.document_parsing.parse_isolated", lambda *args: "Hello"
            )
            with pytest.raises(HTTPException) as rejected:
                await upload_document.__wrapped__(
                    request=Request({"type": "http"}),
                    file=UploadFile(io.BytesIO(b"Hello"), filename="resume.txt"),
                    doc_type="jd",
                    is_primary=False,
                    db=db,
                    current_user=stale,
                )
            assert rejected.value.status_code == 403
            writer.assert_not_called()
    finally:
        async with maker() as db:
            await db.execute(delete(UserDocument).where(UserDocument.user_id == owner))
            await db.execute(delete(User).where(User.id == owner))
            await db.commit()
        await async_engine.dispose()
