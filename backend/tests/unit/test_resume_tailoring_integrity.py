"""Tailoring end to end with a stub model: grounding, parse-back and honest ATS scoring."""

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import indian_resume as candidate  # noqa: E402

from app.agents.state import AgentState  # noqa: E402
from app.services.ats_estimator import estimate_resume  # noqa: E402

SRC = candidate.SOURCE_MARKDOWN.replace("हिन्दी, English, தமிழ்", "Hindi, English, Tamil")


def _run(mock_llm, tailored_markdown, *, full_text=SRC, chunks=("one small chunk",)):
    from app.agents.resume_agent import resume_agent_node

    mock_llm.responses = [
        json.dumps(
            {
                "resume_markdown": tailored_markdown,
                "summary": "s",
                "ats_score": 0,
                "keywords_matched": [],
                "keywords_missing": [],
                "changes_made": [],
                "warnings": [],
            }
        )
    ] * 3
    persist = MagicMock(return_value="doc-1")
    state = AgentState(
        user_id="usr_1",
        run_id=str(uuid.uuid4()),
        task_type="resume_optimize",
        messages=[HumanMessage(content=candidate.JD)],
        context={"jd_text": candidate.JD, "page_target": 2},
        status="running",
        pending_action=None,
        result=None,
        error=None,
    )
    contact = {"email": candidate.EMAIL, "phone": candidate.PHONE}
    with (
        patch("app.core.sync_db.fetch_model_settings", return_value=MagicMock(provider="openai")),
        patch(
            "app.agents.resume_agent.retrieve",
            return_value=[MagicMock(page_content=c) for c in chunks],
        ),
        patch("app.agents.resume_agent._full_resume_text", return_value=full_text),
        patch("app.agents.resume_agent._persist_resume_document", persist),
        patch("app.core.sync_db.fetch_user_full_name", return_value=candidate.NAME),
        patch("app.core.llm_gateway.build_gateway_llm", return_value=mock_llm),
        patch("app.services.resume_facts.fetch_resume_facts_sync", return_value=(contact, {})),
        patch("app.core.event_bus.emit"),
    ):
        return resume_agent_node(state), persist


def test_faithful_tailoring_is_clean_and_parse_back_runs_on_the_real_pdf(mock_llm):
    result, persist = _run(mock_llm, SRC)
    pending = result["pending_action"]
    assert pending["grounding"]["unsupported"] == []
    assert pending["warnings"] == []
    report = persist.call_args.kwargs["parse_back"]
    assert report["ok"] is True and report["pages"] <= 2


def test_invented_facts_are_surfaced_to_the_user(mock_llm):
    forged = (
        SRC.replace("Flipkart Internet Pvt Ltd", "Google India")
        .replace("by 40%", "by 70%")
        .replace("AWS, Docker", "AWS, Docker, Terraform")
    )
    result, _ = _run(mock_llm, forged)
    pending = result["pending_action"]
    unsupported = " ".join(pending["grounding"]["unsupported"])
    for item in ("Google", "70%", "Terraform"):
        assert item in unsupported
    assert any("Not found in your documents" in w for w in pending["warnings"])


def test_a_long_resume_is_grounded_on_the_whole_document_not_eight_chunks(mock_llm):
    # Only one chunk is retrieved; Flipkart exists solely in the full text.
    result, _ = _run(mock_llm, SRC, chunks=("Razorpay Senior Software Engineer",))
    assert result["pending_action"]["grounding"]["unsupported"] == []


def test_a_dropped_job_and_reversed_dates_become_warnings(mock_llm):
    cut = SRC.split("### Software Engineer | Flipkart")[0] + "## Education" + SRC.split("## Education")[1]
    result, _ = _run(mock_llm, cut.replace("Jan 2021 - Present", "Dec 2022 - Jan 2021"))
    warnings = " ".join(result["pending_action"]["warnings"])
    assert "Left out of this tailored version: Flipkart Internet Pvt Ltd" in warnings
    assert "end before they start" in warnings


def test_keyword_stuffing_scores_lower_than_the_honest_resume():
    honest = estimate_resume(SRC, candidate.JD)
    stuffed_text = SRC + "\n## Skills\n" + ", ".join(["Terraform", "GraphQL", "Kubernetes"] * 6)
    stuffed = estimate_resume(stuffed_text, candidate.JD)
    assert stuffed["composite_score"] < honest["composite_score"]
    assert any(i["code"] == "format_keyword_stuffing" for i in stuffed["issues"])


def test_skills_only_keywords_earn_less_than_keywords_shown_in_experience():
    jd = "Backend Engineer\nRequired: Terraform"
    used = "## Experience\n- Provisioned infrastructure with Terraform for 12 services"
    listed = "## Skills\nTerraform"
    assert (
        estimate_resume(used, jd)["sub_scores"]["match"]["score"]
        > estimate_resume(listed, jd)["sub_scores"]["match"]["score"]
    )


def test_location_words_are_not_reported_as_missing_skills():
    missing = estimate_resume(SRC, candidate.JD)["missing_keywords"]
    assert not {"india", "remote", "within", "need"} & set(missing)
    assert {"graphql", "terraform"} <= set(missing)


def test_indian_salary_and_scale_units_count_as_measured_impact():
    bullets = "## Experience\n- Saved ₹2 crore yearly\n- Cut costs by 15 lakh\n- Improved things"
    assert estimate_resume(bullets)["sub_scores"]["impact"]["score"] == 67
