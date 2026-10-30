"""Regression coverage for review findings at real application boundaries."""

import inspect
import io
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_saved_cover_letter_exports_as_text():
    from app.models.db import User, UserDocument
    from app.services.data_export_service import build_user_data_export

    user = User(id=uuid.uuid4(), email="audit@example.invalid")
    doc = UserDocument(
        id=uuid.uuid4(),
        user_id=user.id,
        doc_type="cover_letter",
        filename="cover.md",
        raw_text="Synthetic draft",
    )
    doc.storage_path = f"cover_letters/{user.id}/{doc.id}.md"

    async def execute(query, params=None):
        result = MagicMock()
        result.scalar.return_value = None
        result.scalars.return_value.all.return_value = (
            [doc] if "FROM user_documents" in str(query) else []
        )
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    archive = await build_user_data_export(db, user)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert zf.read(f"documents/{doc.id}.md") == b"Synthetic draft"


@pytest.mark.asyncio
async def test_search_preserves_roles_locations_and_identity(monkeypatch):
    from app.api.v1 import jobs, run_utils
    from app.models.db import User
    from app.services import search_basis
    from app.workflows import starters

    monkeypatch.setattr(
        jobs,
        "_resolve_search_context",
        AsyncMock(return_value=("Backend Engineer", "Remote", "any", {})),
    )
    monkeypatch.setattr(jobs, "_resolve_live_browser", AsyncMock(return_value=False))
    monkeypatch.setattr(search_basis, "resolve_basis", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(run_utils, "check_run_admission", AsyncMock())
    start = AsyncMock()
    monkeypatch.setattr(starters, "start_job_search", start)
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    user = User(id=uuid.uuid4(), email="audit@example.invalid")
    payload = jobs.JobSearchRequest(
        titles=["Backend Engineer"], locations=["Bengaluru", "Hyderabad"]
    )
    await inspect.unwrap(jobs.search_jobs)(request=None, payload=payload, db=db, current_user=user)
    params = start.await_args.args[2]
    assert params["location"] == "Bengaluru"
    assert params["locations"] == ["Bengaluru", "Hyderabad"]
    assert params["titles"] == ["Backend Engineer"]
    from app.services.scheduled_jobs import JobSearchTrigger

    trigger = JobSearchTrigger(user_id=str(user.id), run_id=str(uuid.uuid4()), **params)
    assert trigger.locations == payload.locations
    first = db.add.call_args.args[0].input["queue_job_id"]
    payload.locations = ["Bengaluru", "Mumbai"]
    await inspect.unwrap(jobs.search_jobs)(request=None, payload=payload, db=db, current_user=user)
    assert db.add.call_args.args[0].input["queue_job_id"] != first


def test_multiple_roles_are_alternatives():
    from app.services.job_catalog import matches_query

    q = {"titles": ["Backend Engineer", "Data Scientist"], "locations": ["Remote"]}
    for title in q["titles"]:
        job = {"title": title, "location": "Remote"}
        assert matches_query(job, q)
        assert matches_query(job, {**q, "titles": [title]})


def test_cover_letter_agent_consumes_memory_in_actual_prompt(monkeypatch):
    from app.agents import cover_letter_agent as cla
    from app.agents.prompts.cover_letter_prompt import CoverLetterOutput

    prompts = []

    def fake_call(llm, system, prompt, schema):
        prompts.append(prompt)
        return CoverLetterOutput(
            cover_letter_markdown="Synthetic letter",
            hook_used="Synthetic",
            word_count=2,
            tone="formal",
        )

    monkeypatch.setattr(cla, "call_llm_json", fake_call)
    monkeypatch.setattr(cla, "retrieve", lambda *a, **kw: ["Candidate source"])
    monkeypatch.setattr(cla, "build_agent_llm", lambda *a: object())
    monkeypatch.setattr("app.core.event_bus.emit", lambda *a, **kw: None)
    base = {
        "user_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "messages": [],
        "model_settings": object(),
    }
    for marker in ["MEMORY_SENTINEL_ALPHA", "MEMORY_SENTINEL_BETA"]:
        result = cla.cover_letter_node(
            {
                **base,
                "context": {
                    "tone": "formal",
                    "jd_text": "Backend Engineer",
                    "_strategy": marker,
                    "_memory": {"preferences": {"style": marker}},
                    "_pgvector_memory": {"preferences": [marker]},
                    "_procedures": [marker],
                    "_avoid": [marker],
                },
            }
        )
        assert result["status"] == "awaiting_approval"
    assert prompts[0] != prompts[1]
    assert "MEMORY_SENTINEL_ALPHA" in prompts[0]
    assert "MEMORY_SENTINEL_BETA" in prompts[1]
    assert "untrusted data" in prompts[0]


@pytest.mark.asyncio
async def test_strict_identity_erasure_requires_key(monkeypatch):
    from app.core import clerk_auth

    monkeypatch.setattr(clerk_auth.settings, "CLERK_SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="CLERK_SECRET_KEY"):
        await clerk_auth.delete_clerk_user("user_synthetic", strict=True)


@pytest.mark.asyncio
async def test_memory_cache_erasure_is_owner_scoped_and_batched(monkeypatch):
    from app.core import redis_client
    from app.services.memory_lifecycle import delete_memory_cache

    seen = []
    yielded = []

    async def scan_iter(*, match, count):
        seen.append(match)
        for i in range(205):
            key = match.replace("*", str(i))
            yielded.append(key)
            yield key

    redis = MagicMock()
    redis.scan_iter = scan_iter
    redis.delete = AsyncMock()
    monkeypatch.setattr(redis_client, "get_redis", lambda: redis)
    await delete_memory_cache("owner")
    assert seen == ["session:owner:*", "memory-emb:owner:*"]
    assert len(redis.delete.await_args_list) == 6
    assert all(len(call.args) <= 100 for call in redis.delete.await_args_list)
    deleted = [k for call in redis.delete.await_args_list for k in call.args]
    assert deleted == yielded


def test_memory_hints_are_bounded():
    from app.agents.memory_prompt import memory_prompt

    prompt = memory_prompt({"_memory": {"preferences": "x" * 20000}, "_avoid": ["y" * 20000]})
    assert len(prompt) < 5000


def test_rag_chunk_settings_are_applied(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "RAG_CHUNK_SIZE", 100)
    monkeypatch.setattr(rag_service.app_settings, "RAG_CHUNK_OVERLAP", 10)
    chunks = rag_service.chunk_text("word " * 150)
    assert len(chunks) > 5
    assert all(len(c) <= 100 for c in chunks)


@pytest.mark.asyncio
async def test_generated_letter_cleanup_has_no_binary_path():
    from app.models.db import UserDocument
    from app.services.document_cleanup import enqueue_document_cleanup

    owner, document_id = uuid.uuid4(), uuid.uuid4()
    doc = UserDocument(
        id=document_id,
        user_id=owner,
        doc_type="cover_letter",
        raw_text="Generated",
        filename="letter.md",
        storage_path=f"cover_letters/{owner}/{document_id}.md",
    )
    db = AsyncMock()
    await enqueue_document_cleanup(db, doc)
    assert db.execute.await_args.args[1]["storage_path"] == ""


@pytest.mark.asyncio
async def test_resolved_search_query_still_matches_plain_titles(monkeypatch):
    # Regression: the query used to grow into "Fresher Entry-level Software
    # Engineer Python React", and every word must be in a job title, so a
    # fresher with resume skills found 0 jobs on every search.
    from types import SimpleNamespace

    from app.api.v1 import jobs
    from app.models.db import User
    from app.services import search_basis
    from app.services.job_catalog import matches_query

    resume = SimpleNamespace(id=uuid.uuid4(), raw_text="Projects in Python, React and FastAPI.")
    monkeypatch.setattr(search_basis, "resolve_basis", AsyncMock(return_value=(resume, None)))
    prefs = SimpleNamespace(
        id=uuid.uuid4(),
        target_roles=[],
        current_title=None,
        experience_level="fresher",
        years_experience=0,
        preferred_locations=[],
        work_mode="remote",
        job_type="full-time",
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = prefs
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    user = User(id=uuid.uuid4(), email="audit@example.invalid", headline=None)
    cases = (
        ("", "Software Engineer", "Software Engineer I"),
        ("Data Scientist", "Data Scientist", "Data Scientist II"),
    )
    for typed, expected, title in cases:
        query, location, *_ = await jobs._resolve_search_context(
            db, user, jobs.JobSearchRequest(search_query=typed)
        )
        assert query == expected
        job = {"title": title, "location": "Remote", "remote": "remote"}
        assert matches_query(job, {"search_query": query, "location": location})
