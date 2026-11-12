import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.state import AgentState


@pytest.fixture(autouse=True)
def _stub_parse_back():
    # These tests render with a fake PDF; test_resume_parseback covers real files.
    with patch("app.agents.resume_agent._parse_back", return_value=None):
        yield


def make_state(jd_text: str = "Python engineer at Stripe") -> AgentState:
    return AgentState(
        user_id="usr_test123",
        run_id=str(uuid.uuid4()),
        task_type="resume_optimize",
        messages=[HumanMessage(content=jd_text)],
        context={"jd_text": jd_text},
        status="running",
        pending_action=None,
        result=None,
        error=None,
    )


def _valid_resume_json() -> str:
    return json.dumps(
        {
            "resume_markdown": "Jane Doe\n\nSUMMARY\n5 years Python experience",
            "summary": "Backend engineer",
            "ats_score": 70,
            "keywords_matched": ["Python"],
            "keywords_missing": ["AWS"],
            "changes_made": ["Reordered sections"],
            "warnings": [],
        }
    )


def test_general_score_replaces_model_self_grade():
    from app.agents.prompts.resume_prompt import OUTPUT_SCHEMA
    from app.agents.resume_agent import _score_parsed_resume
    from app.services.ats_estimator import estimate_resume

    parsed = OUTPUT_SCHEMA.model_validate_json(_valid_resume_json())
    parsed.ats_score = 99
    expected = estimate_resume(parsed.resume_markdown)["composite_score"]
    assert _score_parsed_resume(parsed, "").ats_score == expected


def test_general_generation_persists_estimate_without_job(monkeypatch):
    from app.agents.prompts.resume_prompt import OUTPUT_SCHEMA
    from app.agents.resume_agent import _persist_resume_document, _score_parsed_resume

    parsed = _score_parsed_resume(OUTPUT_SCHEMA.model_validate_json(_valid_resume_json()), "")
    session = MagicMock()
    monkeypatch.setattr("app.core.sync_db._get_sync_factory", lambda: lambda: session)
    monkeypatch.setattr("app.agents.resume_agent.upload_file", lambda *args: "test/resume.pdf")
    _persist_resume_document(str(uuid.uuid4()), None, "modern", parsed, "", b"%PDF-test")
    document = session.__enter__.return_value.add.call_args.args[0]
    assert document.ats_data["estimate"]["mode"] == "general"
    assert document.ats_score == document.ats_data["estimate"]["composite_score"]


def test_resume_agent_pauses_for_approval(mock_llm):
    from app.agents.resume_agent import resume_agent_node

    mock_llm.responses = [_valid_resume_json()]
    mock_chunks = [MagicMock(page_content="5 years Python experience")]

    with (
        patch(
            "app.core.sync_db.fetch_model_settings",
            return_value=MagicMock(provider="openai"),
        ),
        patch("app.agents.resume_agent.retrieve", return_value=mock_chunks),
        patch("app.agents.resume_agent.generate_resume_pdf", return_value=b"%PDF-fake"),
        patch(
            "app.agents.resume_agent._persist_resume_document", return_value="doc-123"
        ) as persist,
        patch("app.core.sync_db.fetch_user_full_name", return_value="Test User"),
        patch("app.core.llm_gateway.build_gateway_llm", return_value=mock_llm),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=({}, {})),
        patch("app.core.event_bus.emit"),
    ):
        result = resume_agent_node(make_state())

    persist.assert_called_once()
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"] is not None
    assert result["pending_action"]["type"] == "resume_ready"
    assert "resume_markdown" in result["pending_action"]
    assert result["pending_action"]["pdf_document_id"] == "doc-123"
    assert "pdf_b64" not in result["pending_action"]
    assert result["pending_action"]["ats_score"] >= 0


def test_resume_agent_fails_without_fabricating_a_draft():
    from app.agents.resume_agent import resume_agent_node

    with (
        patch("app.core.sync_db.fetch_model_settings", return_value=MagicMock()),
        patch("app.core.sync_db.fetch_user_full_name", return_value="Test"),
        patch("app.agents.resume_agent.retrieve", side_effect=Exception("pgvector down")),
        patch("app.agents.resume_agent.generate_resume_pdf", return_value=b"%PDF-fake"),
        patch("app.agents.resume_agent._persist_resume_document", return_value="doc-456"),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=({}, {})),
        patch("app.core.event_bus.emit"),
    ):
        result = resume_agent_node(make_state())

    assert result["status"] == "failed"
    assert "Resume generation failed" in result["error"]


def test_resume_agent_degrades_without_pdf_storage(mock_llm):
    from app.agents.resume_agent import resume_agent_node

    mock_llm.responses = [_valid_resume_json()]
    mock_chunks = [MagicMock(page_content="5 years Python experience")]

    with (
        patch(
            "app.core.sync_db.fetch_model_settings",
            return_value=MagicMock(provider="openai"),
        ),
        patch("app.agents.resume_agent.retrieve", return_value=mock_chunks),
        patch("app.agents.resume_agent.generate_resume_pdf", return_value=b"%PDF-fake"),
        patch(
            "app.agents.resume_agent._persist_resume_document",
            side_effect=Exception("storage down"),
        ),
        patch("app.core.sync_db.fetch_user_full_name", return_value="Test User"),
        patch("app.core.llm_gateway.build_gateway_llm", return_value=mock_llm),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=({}, {})),
        patch("app.core.event_bus.emit"),
    ):
        result = resume_agent_node(make_state())

    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["pdf_document_id"] is None
    assert "resume_markdown" in result["pending_action"]


def test_call_llm_json_retries_once_on_invalid():
    from langchain_core.messages import AIMessage

    from app.agents._llm_json import call_llm_json
    from app.agents.prompts.resume_prompt import OUTPUT_SCHEMA

    calls = {"n": 0}

    class FakeLLM:
        def invoke(self, messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return AIMessage(content="not json at all")
            return AIMessage(content=_valid_resume_json())

    parsed = call_llm_json(FakeLLM(), "sys", "human", OUTPUT_SCHEMA)
    assert parsed.ats_score == 70
    assert calls["n"] == 2


DATES_WARNING = "Employment dates were not provided for the internship."
AZURE_WARNING = "The JD asks for Azure experience, which the resume does not show."


def _resume_json(markdown: str, warnings: list[str] | None = None) -> str:
    return json.dumps(
        {
            "resume_markdown": markdown,
            "summary": "Backend engineer",
            "ats_score": 55,
            "keywords_matched": ["Python"],
            "keywords_missing": [],
            "changes_made": [],
            "warnings": warnings or [],
        }
    )


def _run_agent(mock_llm, llm_json, *, facts=({}, {}), ats=None, unsupported=None):
    """Run the node with every I/O boundary patched; returns (result, mocks)."""
    from app.agents.resume_agent import resume_agent_node

    mock_llm.responses = [llm_json]
    render = MagicMock(return_value=b"%PDF-fake")
    persist = MagicMock(return_value="doc-789")
    settings = MagicMock(provider="openai")
    with (
        patch("app.core.sync_db.fetch_model_settings", return_value=settings),
        patch(
            "app.agents.resume_agent.retrieve",
            return_value=[MagicMock(page_content="source resume")],
        ),
        patch("app.agents.resume_agent.generate_resume_pdf", render),
        patch("app.agents.resume_agent._persist_resume_document", persist),
        patch("app.core.sync_db.fetch_user_full_name", return_value="Jane Doe"),
        patch("app.core.llm_gateway.build_gateway_llm", return_value=mock_llm),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=facts),
        patch(
            "app.agents.resume_agent.compute_ats_score",
            ats or MagicMock(return_value=MagicMock(composite_score=77, missing_keywords=[])),
        ) as score,
        patch("app.core.event_bus.emit"),
        # The fixtures' one-line source is not meant to ground a whole draft;
        # grounding itself is covered in test_resume_grounding and below.
        patch(
            "app.agents.resume_agent.unsupported_claims", side_effect=unsupported or (lambda *_: [])
        ),
    ):
        result = resume_agent_node(make_state("Python engineer, Azure"))
    return result, render, persist, score


def test_empty_source_draft_is_not_rendered_or_scored(mock_llm):
    result, render, persist, score = _run_agent(
        mock_llm, _resume_json("NOT_PROVIDED", ["The resume source was empty."])
    )

    assert result["status"] == "awaiting_approval"
    pending = result["pending_action"]
    assert pending["review"] is None
    assert pending["resume_markdown"] == ""
    assert pending["ats_score"] == 0
    assert pending["pdf_document_id"] is None
    assert pending["warnings"] == ["The resume source was empty."]
    render.assert_not_called()
    persist.assert_not_called()
    score.assert_not_called()


def test_saved_facts_and_verified_contact_are_applied_to_the_draft(mock_llm):
    draft = (
        "# Jane Doe\n## EXPERIENCE\n### Prompt Engineer Intern | Acme Labs\n"
        "- Built prompt evaluation pipelines\n## SKILLS\n**Languages:** Python\n"
    )
    contact = {"email": "jane@example.com", "phone": "+91 98765 43210"}
    facts = {
        "experience": [
            {
                "role": "Prompt Engineer Intern",
                "employer_match": "Acme Labs",
                "start": "Jun 2025",
                "end": "Present",
                "submitted": ["start", "end"],
            }
        ],
        "education": [
            {
                "degree": "B.Tech Computer Science",
                "institution": "SPPU",
                "end": "May 2025",
            }
        ],
    }

    result, render, persist, _ = _run_agent(mock_llm, _resume_json(draft), facts=(contact, facts))

    rendered = render.call_args.args[0]
    for expected in (
        "jane@example.com",
        "+91 98765 43210",
        "Jun 2025 - Present",
        "## EDUCATION",
        "B.Tech Computer Science",
        "SPPU",
    ):
        assert expected in rendered
    assert result["pending_action"]["resume_markdown"] == rendered
    assert result["pending_action"]["review"]["issues"] == []
    persist.assert_called_once()


def test_resolved_warnings_are_filtered_but_persisted_in_full(mock_llm):
    draft = (
        "# Jane Doe\njane@example.com | +91 98765 43210\n## EXPERIENCE\n"
        "### Prompt Engineer Intern | Acme Labs | Remote | Jun 2025 - Present\n"
        "- Built prompt evaluation pipelines\n## EDUCATION\n"
        "### B.Tech Computer Science | SPPU | Pune | Aug 2021 - May 2025\n"
    )

    result, _, persist, _ = _run_agent(
        mock_llm, _resume_json(draft, [DATES_WARNING, AZURE_WARNING])
    )

    assert result["pending_action"]["warnings"] == [AZURE_WARNING]
    # The document keeps the model's original list; readers filter it.
    assert persist.call_args.kwargs["warnings"] == [DATES_WARNING, AZURE_WARNING]


def test_failing_saved_facts_do_not_skip_contact_or_name(mock_llm):
    draft = (
        "## EXPERIENCE\n### Prompt Engineer Intern | Acme Labs\n"
        "- Built prompt evaluation pipelines\n## SKILLS\n**Languages:** Python\n"
    )
    contact = {"email": "jane@example.com", "phone": "+91 98765 43210"}
    facts = {"experience": [{"role": "Prompt Engineer Intern", "start": "Jun 2025"}]}

    with patch(
        "app.agents.resume_agent.apply_saved_facts",
        side_effect=ValueError("No experience entry matches"),
    ) as apply_saved:
        result, render, _, _ = _run_agent(mock_llm, _resume_json(draft), facts=(contact, facts))

    apply_saved.assert_called_once()
    rendered = render.call_args.args[0]
    assert "jane@example.com" in rendered and "+91 98765 43210" in rendered
    assert rendered.startswith("# Jane Doe\n")
    assert result["pending_action"]["resume_markdown"] == rendered


def test_resume_agent_keeps_template_render_when_fit_overflows(mock_llm):
    from app.agents.resume_agent import resume_agent_node
    from app.services.resume_export import PageOverflow

    mock_llm.responses = [_valid_resume_json()]
    calls = []

    def render(*args, **kwargs):
        calls.append(kwargs.get("page_target"))
        if kwargs.get("page_target"):
            raise PageOverflow(3, 2)
        return b"%PDF-fallback"

    persist = MagicMock(return_value="doc-789")
    with (
        patch(
            "app.core.sync_db.fetch_model_settings",
            return_value=MagicMock(provider="openai"),
        ),
        patch("app.agents.resume_agent.retrieve", return_value=[]),
        patch("app.agents.resume_agent.generate_resume_pdf", render),
        patch("app.agents.resume_agent._persist_resume_document", persist),
        patch("app.core.sync_db.fetch_user_full_name", return_value="Test User"),
        patch("app.core.llm_gateway.build_gateway_llm", return_value=mock_llm),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=({}, {})),
        patch("app.core.event_bus.emit"),
    ):
        result = resume_agent_node(make_state())

    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["pdf_document_id"] == "doc-789"
    assert calls == [2, None]
    assert persist.call_args.args[5] == b"%PDF-fallback"
    assert persist.call_args.kwargs["page_target"] is None
    assert any("pages" in w for w in persist.call_args.kwargs["warnings"])


def test_unsupported_details_are_removed_after_failed_repair(mock_llm):
    draft = "# Jane Doe\n## EXPERIENCE\n### Engineer | Acme\n- Cut costs 60% with Kubernetes\n"
    # first draft and the repair attempt both still contain Kubernetes
    mock_llm.responses = [_resume_json(draft)]

    result, _, _, _ = _run_agent(
        mock_llm,
        _resume_json(draft),
    )

    pending = result["pending_action"]
    assert pending["grounding"]["checked"] is True
    assert pending["grounding"]["unsupported"] == []
    assert "Acme" in pending["grounding"]["removed"]
    assert "Kubernetes" not in pending["resume_markdown"]
    assert any("Removed because" in w for w in pending["warnings"])
