"""Regression tests for Fix 4: RLS + pgvector isolation (2026-06-13 audit).

Isolation model:
  - Backend connects as the table owner via SQLAlchemy — this bypasses all
    Postgres RLS policies by design (see deploy/oracle-vm/postgres-bootstrap.sql).
  - User isolation is enforced ENTIRELY at the application layer:
      1. Every authenticated endpoint filters by current_user.id in WHERE clauses.
      2. pgvector collections are namespaced by user_id in the collection name.
      3. Background tasks that write to user-owned rows now receive user_id explicitly
         and include it in the WHERE clause (Fix 4: rag.py _score_resume_background).

These tests verify the application-layer isolation properties WITHOUT a live DB
(unit-level, mock DB). A real cross-user isolation test requires live infrastructure
and is documented in the manual test plan at the bottom of this file.

Bugs caught:
  1. _score_resume_background(doc_id, raw_text) — only filtered by doc_id, not user_id.
     A malicious race could cause a background score update to land on a different user's
     document if doc UUIDs were predictable.  Fix: added user_id param and WHERE clause.

  2. pgvector collection_name namespacing — verified user_id is embedded in every
     collection name so no cross-user retrieval is possible through the LangChain API.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. _score_resume_background now includes user_id in WHERE clause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_resume_background_filters_by_user_id():
    """OLD bug: WHERE doc_id only — no user_id guard.
    NEW fix: WHERE doc_id AND user_id — ownership enforced.
    """
    from app.api.v1.rag import _score_resume_background

    captured_queries: list[object] = []

    mock_db = AsyncMock()

    async def _capture_execute(stmt, *args, **kwargs):
        captured_queries.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        return result

    mock_db.execute = _capture_execute
    mock_db.commit = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=None)

    doc_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    # AsyncSessionLocal is a lazy import inside the function
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=mock_db),
        patch(
            "app.services.ats_service.compute_ats_score",
            return_value=MagicMock(
                composite_score=75,
                keyword_score=80,
                readability_score=70,
                format_score=72,
                matched_keywords=[],
                missing_keywords=[],
                suggestions=[],
                flesch_kincaid=None,
                avg_sentence_length=None,
                format_checks={},
            ),
        ),
    ):
        await _score_resume_background(doc_id, user_id, "resume text")

    # Verify the signature now accepts user_id (covered by test_score_resume_background_signature_requires_user_id)
    # and that the query was attempted
    assert captured_queries, "background scorer must query the owner-scoped document"
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter
    comparisons = []
    for statement in captured_queries:
        for node in visitors.iterate(statement.whereclause):
            if isinstance(node, BinaryExpression) and isinstance(node.right, BindParameter):
                comparisons.append((str(node.left), node.right.effective_value))
    assert ("user_documents.id", uuid.UUID(doc_id)) in comparisons
    assert ("user_documents.user_id", uuid.UUID(user_id)) in comparisons


@pytest.mark.asyncio
async def test_score_resume_background_signature_requires_user_id():
    """Verify the function signature was updated to require user_id param."""
    import inspect
    from app.api.v1.rag import _score_resume_background

    sig = inspect.signature(_score_resume_background)
    params = list(sig.parameters.keys())
    assert (
        "user_id" in params
    ), f"_score_resume_background missing user_id param. Current params: {params}"
    assert params.index("user_id") < params.index(
        "raw_text"
    ), "user_id must appear before raw_text in the signature"


# ---------------------------------------------------------------------------
# 2. pgvector collection namespacing — user_id in every collection name
# ---------------------------------------------------------------------------


def test_collection_name_includes_user_id():
    """Every pgvector collection name must start with the user_id.

    This is the primary isolation mechanism: LangChain PGVector filters all
    queries by collection_name, which contains the user_id. User A's collection
    "usr_abc_resume_openai_1536d" is structurally unreachable from User B's
    queries which use "usr_xyz_resume_openai_1536d".
    """
    from app.services.rag_service import collection_name

    user_a = "00000000-0000-0000-0000-000000000001"
    user_b = "00000000-0000-0000-0000-000000000002"

    coll_a = collection_name(user_a, "resume", "openai")
    coll_b = collection_name(user_b, "resume", "openai")

    assert coll_a.startswith(user_a), f"Collection for user A must start with user_a: {coll_a}"
    assert coll_b.startswith(user_b), f"Collection for user B must start with user_b: {coll_b}"
    assert coll_a != coll_b, "Different users must produce different collection names"


def test_collection_name_different_doc_types_are_isolated():
    """Different doc_types for the same user must produce different collections."""
    from app.services.rag_service import collection_name

    uid = "00000000-0000-0000-0000-000000000001"
    assert collection_name(uid, "resume", "openai") != collection_name(uid, "jd", "openai")


def test_collection_name_different_providers_are_isolated():
    """Different embedding providers must produce different collections (dimension safety)."""
    from app.services.rag_service import collection_name

    uid = "00000000-0000-0000-0000-000000000001"
    openai_coll = collection_name(uid, "resume", "openai")
    google_coll = collection_name(uid, "resume", "google")
    assert (
        openai_coll != google_coll
    ), "OpenAI (1536-d) and Google (768-d) collections must be separate to avoid dimension mismatch"


def test_cross_user_collection_names_never_collide():
    """10 random user pairs must all produce distinct collection names."""
    from app.services.rag_service import collection_name

    users = [str(uuid.uuid4()) for _ in range(10)]
    names = [collection_name(u, "resume", "openai") for u in users]
    assert len(set(names)) == len(names), "Collection name collision between different users"


# ---------------------------------------------------------------------------
# 3. Agent runs: stream and approve endpoints filter by user_id
# ---------------------------------------------------------------------------


def test_stream_endpoint_rejects_other_users_run():
    """GET /agents/{run_id}/stream isolation: query must filter by BOTH run_id AND user_id.

    Verifies the WHERE clause in the endpoint source contains user_id — the
    structural guarantee that User A cannot stream User B's run even if they
    know the UUID.  (Full HTTP test requires live infrastructure — see manual plan.)
    """
    import inspect
    from app.api.v1 import agents

    source = inspect.getsource(agents.stream_agent)
    # Both conditions must appear in the where clause
    assert "AgentRun.id ==" in source or "AgentRun.id ==" in source.replace(
        "run_uuid", "run_id"
    ), "stream_agent must filter by run_id"
    assert (
        "AgentRun.user_id == current_user.id" in source
    ), "stream_agent MUST filter by current_user.id — without this, any user can stream any run"


def test_approve_endpoint_rejects_other_users_run():
    """POST /agents/{run_id}/approve isolation: query must filter by BOTH run_id AND user_id."""
    import inspect
    from app.api.v1 import agents

    # The route delegates to decide_run, which Nova's code route and the
    # application inbox also call; the owner filter must live there.
    assert "decide_run(run_id, payload, db, current_user)" in inspect.getsource(
        agents.approve_or_cancel
    )
    source = inspect.getsource(agents.decide_run)
    assert "AgentRun.user_id == current_user.id" in source, (
        "decide_run MUST filter by current_user.id — "
        "without this, any user can approve any run"
    )


# ---------------------------------------------------------------------------
# 4. HNSW index migration file exists and is valid SQL
# ---------------------------------------------------------------------------


def test_hnsw_migration_file_exists_and_contains_index():
    """The applied migrations create the HNSW and collection indexes."""
    from pathlib import Path

    migrations = Path(__file__).parents[3] / "supabase" / "migrations"
    content = (migrations / "0025_hnsw_index_embeddings.sql").read_text()
    assert "hnsw" in content.lower(), "Migration must contain HNSW index creation"
    assert "langchain_pg_embedding" in content, "Migration must target langchain_pg_embedding table"
    assert (
        "vector_cosine_ops" in content
    ), "Migration must use cosine ops to match LangChain retrieval"
    collection = (migrations / "20261001101000_langchain_collection_index.sql").read_text()
    assert "idx_langchain_embedding_collection_id" in collection
    assert "CREATE INDEX IF NOT EXISTS" in content, "Migration must be idempotent (IF NOT EXISTS)"


# ---------------------------------------------------------------------------
# MANUAL TEST PLAN (requires live DB + two real user accounts)
# ---------------------------------------------------------------------------
# To verify cross-user isolation with a real database:
#
# 1. Create users A and B via /api/v1/auth/signup
# 2. As user A: POST /api/v1/rag/upload with a resume PDF
# 3. Record the doc_id returned
# 4. As user B: GET /api/v1/rag/documents — verify doc_id is NOT in the response
# 5. As user B: attempt to fetch /api/v1/rag/documents/{doc_id} — expect 404
# 6. As user A: POST /api/v1/agents/run (job_search task) — record run_id
# 7. As user B: GET /api/v1/agents/{run_id}/stream — expect 404
# 8. As user B: POST /api/v1/agents/{run_id}/approve — expect 404
#
# All of these should return 404 (not 403) to avoid leaking resource existence.
# ---------------------------------------------------------------------------
