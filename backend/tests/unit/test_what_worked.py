"""What worked: outcome feedback aggregation over application statuses."""

from app.agents.prompts import resume_prompt
from app.services import what_worked


def _row(status, source, resume_id, score=80):
    return (status, source, resume_id, score)


def test_summarize_rates_and_ranking():
    resume_a = "11111111-1111-1111-1111-111111111111"
    resume_b = "22222222-2222-2222-2222-222222222222"
    rows = (
        [("applied", "linkedin", resume_a, 80)] * 8
        + [("interview", "linkedin", resume_a, 90)] * 2
        + [("applied", "naukri-extension", resume_b, 70)] * 4
        + [("rejected", "naukri-extension", resume_b, 65)] * 6
    )
    out = what_worked.summarize(rows)
    assert out["total"] == 20
    assert out["applied"] == 20
    assert out["interviews"] == 2
    assert out["interview_rate"] == 0.1
    linkedin = next(g for g in out["by_source"] if g["key"] == "linkedin")
    naukri = next(g for g in out["by_source"] if g["key"] == "naukri-extension")
    assert linkedin == {"key": "linkedin", "total": 10, "interviews": 2, "rate": 0.2}
    assert naukri["rate"] == 0.0
    best_resume = out["by_resume"][0]
    assert best_resume["key"] == resume_a and best_resume["rate"] == 0.2
    assert out["by_status"] == {"applied": 12, "interview": 2, "rejected": 6}
    assert out["median_match_score"] == 80
    assert out["note"] is None


def test_summarize_empty():
    out = what_worked.summarize([])
    assert out["total"] == 0
    assert out["interview_rate"] is None
    assert out["median_match_score"] is None
    assert out["by_source"] == []


def test_small_samples_flag_the_note():
    out = what_worked.summarize([_row("applied", "linkedin", None)])
    assert out["total"] == 1
    assert out["note"] and "informative only" in out["note"]


def test_tailoring_prompt_includes_outcome_feedback():
    prompt = resume_prompt.build_user_prompt(
        {
            "jd_text": "jd",
            "target_title": "Engineer",
            "outcome_feedback": "Interviews came from LinkedIn, resume A.",
        }
    )
    assert "WHAT_WORKED" in prompt
    assert "LinkedIn, resume A" in prompt


def test_tailoring_prompt_omits_empty_feedback():
    prompt = resume_prompt.build_user_prompt({"jd_text": "jd", "target_title": "Engineer"})
    assert "WHAT_WORKED" not in prompt
