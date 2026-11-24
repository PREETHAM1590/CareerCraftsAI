"""Extension board capture: Naukri/Instahyre/LinkedIn normalizer + endpoint."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
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


def collect_client(monkeypatch, saved=(["id-1"], 1)):
    app = FastAPI()
    app.include_router(extension.router)
    device = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), revoked_at=None)
    app.dependency_overrides[extension.get_device] = lambda: device
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    saved_boards = MagicMock()
    saved_boards.save_jobs = AsyncMock(return_value=saved)
    monkeypatch.setitem(__import__("sys").modules, "app.services.saved_boards", saved_boards)
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
