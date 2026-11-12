"""Unit tests for Auto Apply Pipeline — multi-step flow, HITL checkpoints, error handling."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_auto_apply_returns_structured_result():
    from app.agents.auto_apply_pipeline import run_auto_apply_pipeline

    with (
        patch("app.agents.auto_apply_pipeline.scrape_jobs", return_value=[]),
        patch("app.agents.auto_apply_pipeline.fetch_model_settings", return_value=MagicMock()),
        patch("app.agents.auto_apply_pipeline.fetch_user_profile_text", return_value=""),
    ):
        result = await run_auto_apply_pipeline(
            user_id=str(uuid.uuid4()),
            search_query="Python engineer",
            location="Remote",
            max_applications=5,
        )

    assert "jobs_found" in result
    assert "jobs_scored" in result
    assert "applications_sent" in result
    assert "errors" in result


def test_score_job_quick_returns_0_100():
    from app.agents.auto_apply_pipeline import _score_job_quick
    from app.services.job_platforms_service import JobListing

    mock_llm = MagicMock()

    job = JobListing(
        title="Senior Python Engineer",
        company="Stripe",
        location="Remote",
        description="Build distributed systems",
        job_url="https://stripe.com/jobs/1",
        platform="linkedin",
    )

    with patch(
        "app.agents.thinking.think_about_job_match",
        return_value={"match_level": "HIGH", "decision": "YES"},
    ):
        score = _score_job_quick(mock_llm, job, "Profile text")

    assert isinstance(score, int)
    assert 0 <= score <= 100


def test_generate_cold_email_structure():
    from app.agents.auto_apply_pipeline import _generate_cold_email

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(
        content="Subject: Following up\n\nDear team,\n\nI'm reaching out..."
    )

    result = _generate_cold_email(
        mock_llm,
        "Recruiter Name",
        "recruiter@stripe.com",
        "Stripe",
        "Engineer",
        "Job desc",
        "My profile",
    )
    assert isinstance(result, dict)
    assert "subject" in result
    assert "body" in result


@pytest.mark.asyncio
async def test_get_or_create_job_application_reuses_existing_row_by_url():
    from app.agents.auto_apply_pipeline import _get_or_create_job_application
    from app.models.db import JobApplication
    from app.services.job_platforms_service import JobListing
    from contextlib import asynccontextmanager

    user_id = str(uuid.uuid4())
    job = JobListing(
        title="Engineer",
        company="Acme",
        location="Remote",
        description="desc",
        job_url="https://acme.test/apply",
        platform="linkedin",
    )
    existing = JobApplication(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user_id),
        company="Acme",
        role="Engineer",
        job_url=job.job_url,
    )

    class _FakeDB:
        async def execute(self, *a, **k):
            class _R:
                def scalar_one_or_none(self):
                    return existing

            return _R()

        def add(self, obj):
            raise AssertionError("must not create a second row for an existing job_url")

        async def commit(self):
            pass

    @asynccontextmanager
    async def _cm():
        yield _FakeDB()

    with patch("app.core.database.AsyncSessionLocal", lambda: _cm()):
        job_application_id = await _get_or_create_job_application(user_id, job)

    assert job_application_id == str(existing.id)


@pytest.mark.asyncio
async def test_attach_resume_sets_resume_id_only_for_the_owners_document():
    from app.agents.auto_apply_pipeline import _attach_resume
    from app.models.db import UserDocument

    owner, other = uuid.uuid4(), uuid.uuid4()
    app_row = MagicMock(user_id=owner, resume_id=None)
    own_doc = MagicMock(id=uuid.uuid4(), user_id=owner)
    foreign_doc = MagicMock(id=uuid.uuid4(), user_id=other)
    docs = {own_doc.id: own_doc, foreign_doc.id: foreign_doc}

    db = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(
        side_effect=lambda model, pk: docs.get(pk) if model is UserDocument else app_row
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=db)
    session.__aexit__ = AsyncMock(return_value=False)

    with patch("app.core.database.AsyncSessionLocal", return_value=session):
        assert not await _attach_resume(str(owner), str(uuid.uuid4()), str(foreign_doc.id))
        assert app_row.resume_id is None
        assert await _attach_resume(str(owner), str(uuid.uuid4()), str(own_doc.id))
    assert app_row.resume_id == own_doc.id
