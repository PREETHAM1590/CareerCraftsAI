"""Extension board capture: Naukri/Instahyre/LinkedIn normalizer + endpoint."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import extension
from app.api.v1.deps import get_db
from app.services import extension_service

# ── Normalizer ──────────────────────────────────────────────────────────


def test_normalize_naukri_listing():
    job = extension_service.normalize_collected_job(
        {
            "url": "https://www.naukri.com/joblisting-python-developer?utm_source=x",
            "title": "Senior Python Developer",
            "company": "Acme India",
            "location": "Bengaluru",
            "description": "Build services " * 10,
            "salary": "15-25 LPA",
            "posted_at": "5 Days Ago",
            "skills": ["Python", " FastAPI ", ""],
        }
    )
    assert job["title"] == "Senior Python Developer"
    assert job["company"] == "Acme India"
    assert job["platform"] == "naukri"
    assert job["source"] == "naukri-extension"
    assert "utm_source" not in job["url"]
    assert job["skills"] == ["Python", "FastAPI"]
    assert job["posted_at"].endswith(("Z", "+00:00")) or "T" in job["posted_at"]


def test_normalize_instahyre_listing():
    job = extension_service.normalize_collected_job(
        {
            "url": "https://www.instahyre.com/job/12345/",
            "title": "Backend Engineer",
            "company": "StartupCo",
            "posted_at": "just now",
        }
    )
    assert job["platform"] == "instahyre"
    assert job.get("description", "") == ""
    assert "posted_at" not in job or job["posted_at"]


def test_normalize_rejects_unknown_host():
    with pytest.raises(ValueError, match="Unsupported job board"):
        extension_service.normalize_collected_job(
            {"url": "https://example.com/job", "title": "X"}
        )


def test_normalize_rejects_missing_title():
    with pytest.raises(ValueError, match="title and a URL"):
        extension_service.normalize_collected_job(
            {"url": "https://www.naukri.com/j", "title": "  "}
        )


def test_normalize_truncates_oversized_description():
    job = extension_service.normalize_collected_job(
        {
            "url": "https://www.naukri.com/j",
            "title": "T" * 500,
            "description": "d" * 99_999,
        }
    )
    assert len(job["title"]) == 200
    assert len(job["description"]) == 20_000


# ── Endpoint ────────────────────────────────────────────────────────────


def collect_client(monkeypatch, saved=None):
    app = FastAPI()
    app.include_router(extension.router)
    device = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), revoked_at=None)
    app.dependency_overrides[extension.get_device] = lambda: device
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    from app.services import saved_boards as real_saved_boards

    monkeypatch.setattr(
        real_saved_boards,
        "save_jobs",
        AsyncMock(return_value=saved if saved is not None else ([uuid.uuid4()], 1)),
    )
    return TestClient(app)


def test_collect_job_endpoint_saves(monkeypatch):
    client = collect_client(monkeypatch)
    res = client.post(
        "/extension/device/collect-job",
        json={
            "url": "https://www.instahyre.com/job/9/",
            "title": "Platform Engineer",
            "company": "Co",
        },
    )
    assert res.status_code == 200
    assert res.json()["status"] == "saved"
    assert res.json()["created"] == 1


def test_collect_job_endpoint_rejects_bad_board(monkeypatch):
    client = collect_client(monkeypatch)
    res = client.post(
        "/extension/device/collect-job",
        json={"url": "https://evil.example.com/j", "title": "X"},
    )
    assert res.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "javascript://www.naukri.com/%0aalert(1)",
        "http://www.naukri.com/job-1",
        "https://user:pw@www.naukri.com/job-1",
        "https://www.naukri.com:8443/job-1",
    ],
)
def test_normalize_rejects_non_https_urls(url):
    with pytest.raises(ValueError, match="plain https"):
        extension_service.normalize_collected_job({"url": url, "title": "X"})


@pytest.mark.parametrize("raw", ["30+ Days Ago", "a day ago", "Posted 2 weeks ago", "Few Hours Ago"])
def test_parse_board_date_common_forms(raw):
    assert extension_service._parse_board_date(raw)


# ── LLM fallback for unanswered required questions ─────────────────────


def test_llm_fallback_uses_saved_answers_but_never_sensitive_ones(monkeypatch):
    import asyncio

    from app.applications import saved_answers
    from app.core import model_router

    async def fake_list(db, user_id):
        assert db == "db-session"
        return [
            {"question_key": "custom.why_this_role", "value": "I build payment systems."},
            {"question_key": "identity.gender", "value": "Female"},
            {"question_key": "disclosure.disability", "value": "Yes"},
            {"question_key": "custom.empty", "value": None},
        ]

    seen = {}

    class FakeLLM:
        def invoke(self, messages):
            seen["prompt"] = messages[-1].content
            return SimpleNamespace(content="I build payment systems.")

    async def fake_get_llm(*a, **kw):
        return FakeLLM()

    monkeypatch.setattr(saved_answers, "list_answers", fake_list)
    monkeypatch.setattr(model_router, "get_llm", fake_get_llm)
    task = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    plan = [
        {
            "field_id": "f1",
            "label": "Why this role?",
            "input_type": "textarea",
            "required": True,
            "value": None,
        }
    ]
    asyncio.run(extension_service._llm_fallback("db-session", task, plan, ["f1"]))

    assert "payment systems" in seen["prompt"]
    assert "Female" not in seen["prompt"] and "disability" not in seen["prompt"]
    assert plan[0]["value"] == "I build payment systems."
    assert plan[0]["source"] == "ai_draft" and plan[0]["requires_review"] is True


def test_llm_fallback_skips_the_model_without_usable_answers(monkeypatch):
    import asyncio

    from app.applications import saved_answers
    from app.core import model_router

    async def fake_list(db, user_id):
        return [{"question_key": "identity.gender", "value": "Female"}]

    get_llm = AsyncMock()
    monkeypatch.setattr(saved_answers, "list_answers", fake_list)
    monkeypatch.setattr(model_router, "get_llm", get_llm)
    plan = [{"field_id": "f1", "label": "Why?", "input_type": "text", "value": None}]
    task = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    asyncio.run(extension_service._llm_fallback(None, task, plan, ["f1"]))
    get_llm.assert_not_called()
    assert plan[0]["value"] is None
