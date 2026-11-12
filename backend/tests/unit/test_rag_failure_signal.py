"""A failing vector store must surface as a warning, not as silent empty context."""

from unittest.mock import MagicMock, patch

from app.services import rag_service
from app.services.rag_service import RetrievalResult, retrieval_unavailable


def test_retrieve_flags_failed_non_resume_search():
    with (
        patch.object(rag_service, "get_embedding_model", side_effect=RuntimeError("db down")),
        patch.object(rag_service, "get_embedding_provider", return_value="openai"),
    ):
        result = rag_service.retrieve("u1", "company", "Acme", MagicMock(), k=3)
    assert list(result) == []
    assert isinstance(result, RetrievalResult)
    assert retrieval_unavailable(result)


def test_empty_successful_search_is_not_flagged():
    assert not retrieval_unavailable([])
    assert not retrieval_unavailable(RetrievalResult())


def test_resume_profile_fallback_is_flagged():
    doc = MagicMock(page_content="x", metadata={"rag_unavailable": True})
    assert retrieval_unavailable([doc])
