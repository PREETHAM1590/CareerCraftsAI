"""What worked: outcome feedback aggregation over application statuses."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

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
    assert linkedin == {
        "key": "linkedin",
        "total": 10,
        "interviews": 2,
        "rate": 0.2,
        "small_sample": False,
    }
    assert naukri["rate"] == 0.0
    best_resume = out["by_resume"][0]
    assert best_resume["key"] == resume_a and best_resume["rate"] == 0.2
    assert out["by_status"] == {"applied": 12, "interview": 2, "rejected": 6}
    assert out["median_match_score"] == 75
    assert out["note"] is None


def test_summarize_empty():
    out = what_worked.summarize([])
    assert out["total"] == 0
    assert out["interview_rate"] is None
    assert out["median_match_score"] is None
    assert out["by_source"] == []


def test_small_samples_flag_the_note():
    rows = (
        [_row("applied", "large-source", "resume-a")] * what_worked.MIN_GROUP_FOR_RATE
        + [_row("interview", "small-source", "resume-b")]
        + [_row("saved", "small-source", "resume-b")] * 10
        + [_row("saved", "saved-only", None)]
    )
    out = what_worked.summarize(rows)
    for groups in (out["by_source"], out["by_resume"]):
        assert groups[0]["total"] == 11
        assert groups[0]["rate"] == 1.0
        assert groups[0]["small_sample"] is True
        assert groups[1]["small_sample"] is False
    assert out["by_source"][2]["rate"] == 0.0
    assert out["by_source"][2]["small_sample"] is True
    assert out["note"] == (
        f"Rates are unreliable below {what_worked.MIN_GROUP_FOR_RATE} applications per group."
    )


@pytest.mark.parametrize(
    "rows, applied, rate, score",
    [([], 0, None, None), ([_row("interview", "linkedin", None, 81)], 1, 1.0, 81)],
)
async def test_what_worked_executes_and_consumes_buffered_rows(rows, applied, rate, score):
    result = Mock()
    result.all.return_value = rows
    db = Mock(execute=AsyncMock(return_value=result))

    out = await what_worked.what_worked(db, uuid4())

    db.execute.assert_awaited_once()
    result.all.assert_called_once_with()
    assert out["applied"] == applied
    assert out["interview_rate"] == rate
    assert out["median_match_score"] == score


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
