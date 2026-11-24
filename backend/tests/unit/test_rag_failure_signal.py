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


# ── Agents surface a failing vector store as a warning ──────────────────────
import json  # noqa: E402
from unittest.mock import patch as _patch  # noqa: E402

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402


@pytest.fixture
def failing_store():
    with (
        _patch.object(rag_service, "get_embedding_model", return_value=MagicMock()),
        _patch.object(rag_service, "get_embedding_provider", return_value="openai"),
        _patch.object(rag_service, "_search_live_documents", side_effect=RuntimeError("pg down")),
        _patch("app.core.sync_db.fetch_chosen_resume", return_value=None),
        _patch("app.core.sync_db.fetch_user_profile_text", return_value=""),
    ):
        yield


def test_interview_coach_warns_when_company_intel_search_fails(failing_store, mock_llm):
    from tests.unit.test_interview_coach import make_start_state

    mock_llm.responses = [
        json.dumps([{"id": 1, "type": "behavioral", "text": "Tell me about yourself"}])
    ]
    with (
        _patch("app.agents.interview_coach_agent.fetch_model_settings", return_value=MagicMock()),
        _patch("app.agents.interview_coach_agent.build_agent_llm", return_value=mock_llm),
        _patch("app.agents.interview_coach_agent._log_agent_run"),
        _patch("app.agents.interview_coach_agent._save_interview_session"),
    ):
        from app.agents.interview_coach_agent import start_session_node

        result = start_session_node(make_start_state())

    assert result["status"] == "completed"
    assert "Company research was unavailable" in result["result"]["context_warning"]


def test_interview_coach_has_no_warning_when_intel_search_succeeds(mock_llm):
    from tests.unit.test_interview_coach import make_start_state

    mock_llm.responses = [
        json.dumps([{"id": 1, "type": "behavioral", "text": "Tell me about yourself"}])
    ]
    with (
        _patch("app.agents.interview_coach_agent.fetch_model_settings", return_value=MagicMock()),
        _patch("app.agents.interview_coach_agent.build_agent_llm", return_value=mock_llm),
        _patch("app.agents.interview_coach_agent.retrieve", return_value=[]),
        _patch("app.agents.interview_coach_agent._log_agent_run"),
        _patch("app.agents.interview_coach_agent._save_interview_session"),
    ):
        from app.agents.interview_coach_agent import start_session_node

        result = start_session_node(make_start_state())

    assert "context_warning" not in result["result"]


def test_linkedin_agent_warns_when_search_fails(failing_store):
    from app.agents.linkedin_agent import linkedin_agent_node
    from tests.unit.test_linkedin_agent import make_state

    llm = MagicMock()
    llm.invoke.side_effect = [
        AIMessage(
            content=json.dumps(
                {
                    "headline": "H",
                    "about": "A",
                    "experiences": [],
                    "target_keywords": [],
                    "before_after_notes": [],
                }
            )
        )
    ]
    with (
        _patch("app.agents.linkedin_agent.fetch_model_settings", return_value=MagicMock()),
        _patch("app.agents.linkedin_agent.build_agent_llm", return_value=llm),
        _patch("app.agents.linkedin_agent.think_and_select", return_value="x"),
    ):
        result = linkedin_agent_node(make_state())

    assert "context_warning" in result.get("pending_action", {})


def test_cover_letter_warns_when_search_fails(failing_store, mock_llm):
    from app.agents import cover_letter_agent as cla
    from tests.unit.test_cover_letter_agent import _patches, make_state

    patches = _patches(mock_llm)
    patches[1] = _patch.object(cla, "retrieve", rag_service.retrieve)
    with patches[0], patches[1], patches[2], patches[3]:
        result = cla.cover_letter_node(make_state())

    assert result["status"] == "awaiting_approval"
    assert "not grounded in your resume" in result["pending_action"]["context_warning"]


def test_interview_prep_warns_when_search_fails(failing_store, mock_llm):
    from app.agents.interview_prep_agent import interview_prep_agent_node
    from app.agents.state import AgentState

    mock_llm.responses = [
        json.dumps(
            {
                "questions": [{"q": "Why us?", "type": "behavioral", "intent": "fit"}],
                "study_plan": [],
                "topics_ranked": ["Python"],
                "questions_to_ask": ["How is success measured?"],
                "video_search_queries": [],
            }
        )
    ]
    state = AgentState(
        user_id="usr_test123",
        run_id="r1",
        task_type="interview_prep",
        messages=[],
        context={"role": "Engineer", "company": "Example"},
        status="running",
        pending_action=None,
        result=None,
        error=None,
    )
    with (
        _patch("app.agents.interview_prep_agent.fetch_model_settings", return_value=MagicMock()),
        _patch("app.agents.interview_prep_agent.build_agent_llm", return_value=mock_llm),
        _patch("app.agents.thinking.think_and_select", return_value="x"),
        _patch("app.core.event_bus.emit"),
    ):
        result = interview_prep_agent_node(state)

    assert "context_warning" in result["pending_action"]
