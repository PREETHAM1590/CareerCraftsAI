"""Hermetic end-to-end test of the core job-seeker journey. No secrets, no network.

    upload resume -> search a company board -> tailor the resume (grounded, parse-back
    clean) -> hand the application to the browser extension -> plan, review and
    approve-submit

Everything runs through the app's real FastAPI routes (httpx ASGI client) against a
real throwaway PostgreSQL (see hermetic_postgres.py). Only the outside world is
replaced: the LLM is a deterministic stub, HTTP is a canned Greenhouse board,
file storage is a dict, and Temporal is replaced by calling the same code its
activities run. It never touches the live suite's RUN_LIVE_E2E/RUN_E2E gates.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from .hermetic_postgres import EphemeralPostgres, find_pg_bin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
import indian_resume as candidate  # noqa: E402

SOURCE = candidate.SOURCE_MARKDOWN.replace("हिन्दी, English, தமிழ்", "Hindi, English, Tamil")
BOARD = {
    "jobs": [
        {
            "id": 1,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acmeindia/jobs/1",
            "location": {"name": "Bengaluru, India"},
            "content": candidate.JD,
            "first_published": "2026-11-01T00:00:00Z",
        },
        {
            "id": 2,
            "title": "Marketing Manager",
            "absolute_url": "https://boards.greenhouse.io/acmeindia/jobs/2",
            "location": {"name": "Mumbai, India"},
            "content": "Run campaigns.",
            "first_published": "2026-11-01T00:00:00Z",
        },
    ]
}
TAILORED_SUMMARY = (
    "Senior backend engineer with 6 years of experience building payment and logistics "
    "platforms in Python, FastAPI, Kafka and Kubernetes on AWS."
)


@pytest.fixture(scope="session")
def postgres(tmp_path_factory):
    bin_dir = find_pg_bin()
    if bin_dir is None:
        pytest.skip("PostgreSQL server binaries not found (set HERMETIC_PG_BIN)")
    server = EphemeralPostgres(tmp_path_factory.mktemp("pg"), bin_dir)
    try:
        server.start()
        server.create_schema()
    except Exception as exc:  # pragma: no cover - environment specific
        server.stop()
        pytest.skip(f"could not start a local PostgreSQL: {type(exc).__name__}")
    yield server
    server.stop()


class StubLLM:
    """Deterministic stand-in for the tailoring model; `fabricate` simulates a bad one."""

    def __init__(self, fabricate: bool = False):
        self.fabricate = fabricate
        self.calls = 0

    def invoke(self, messages, **_):
        self.calls += 1
        text = SOURCE.replace(
            "Backend engineer with 6 years of experience building payment and logistics "
            "platforms in Python and Go.",
            TAILORED_SUMMARY,
        )
        if self.fabricate:
            text = text.replace("AWS, Docker", "AWS, Docker, Terraform").replace(
                "Flipkart Internet Pvt Ltd", "Google India"
            )
        return AIMessage(
            content=json.dumps(
                {
                    "resume_markdown": text,
                    "summary": "Tailored for a backend role",
                    "ats_score": 0,
                    "keywords_matched": [],
                    "keywords_missing": [],
                    "changes_made": ["Rewrote the summary for the job"],
                    "warnings": [],
                }
            )
        )


@pytest_asyncio.fixture
async def world(postgres, monkeypatch):
    """The real app wired to the temporary database, with every outside call stubbed."""
    from app.api.v1 import deps
    from app.api.v1.users import POLICY_VERSION
    from app.core import database, sync_db
    from app.core.config import settings
    from app.main import app
    from app.models.db import User, UserModelSettings
    from app.services import extension_service, job_connectors, storage_service

    engine = create_async_engine(postgres.async_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    database.AsyncSessionLocal.configure(bind=engine)
    sync_engine = __import__("sqlalchemy").create_engine(postgres.sync_url)
    monkeypatch.setattr(sync_db, "_sync_engine", sync_engine)
    monkeypatch.setattr(sync_db, "_sync_factory", sessionmaker(sync_engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", False)

    files: dict[str, bytes] = {}

    def upload(user_id, filename, content, content_type):
        path = f"{user_id}/{uuid.uuid4().hex}-{filename}"
        files[path] = content
        return path

    monkeypatch.setattr(storage_service, "upload_file", upload)
    monkeypatch.setattr("app.api.v1.rag.upload_file", upload)
    monkeypatch.setattr("app.agents.resume_agent.upload_file", upload)
    monkeypatch.setattr(storage_service, "download_file", lambda path, owner: files[path])

    async def board(url, **kwargs):
        assert "boards-api.greenhouse.io/v1/boards/acmeindia" in url, url
        return httpx.Response(200, json=BOARD, request=httpx.Request("GET", url))

    monkeypatch.setattr(job_connectors, "public_get", board)

    monkeypatch.setattr("app.core.event_bus.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.core.event_bus.publish", lambda *a, **k: None)
    monkeypatch.setattr("app.core.background.spawn_background", lambda coro: coro.close())
    signals = AsyncMock()
    monkeypatch.setattr("app.workflows.starters.signal_extension_update", signals)
    monkeypatch.setattr("app.api.v1.extension.notify_needs_attention", AsyncMock(), raising=False)
    monkeypatch.setattr("app.core.clerk_auth.verify_token", lambda token: {"sub": "user_aditi"})

    user_id = uuid.uuid4()
    async with sessions() as db:
        await db.execute(delete(User))  # the schema's FKs cascade; each test starts empty
        db.add(
            User(
                id=user_id,
                email=candidate.EMAIL,
                full_name=candidate.NAME,
                phone=candidate.PHONE,
                onboarding_completed=True,
                policy_accepted_at=datetime.now(UTC),
                policy_version=POLICY_VERSION,
            )
        )
        db.add(
            UserModelSettings(
                id=uuid.uuid4(),
                user_id=user_id,
                provider="openai",
                model_name="stub",
                is_active=True,
            )
        )
        await db.commit()
        user = await db.get(User, user_id)
    app.dependency_overrides[deps.get_current_user] = lambda: user

    llm = StubLLM()
    monkeypatch.setattr("app.core.llm_gateway.build_gateway_llm", lambda *a, **k: llm)
    monkeypatch.setattr(
        "app.agents.resume_agent.retrieve",
        lambda *a, **k: [MagicMock(page_content="Razorpay Senior Software Engineer")],
    )

    async def start_agent_run(run_id, user_id_, initial_continuation=None):
        """What the durable workflow's activity does for a resume run."""
        from app.agents.resume_agent import resume_agent_node
        from app.models.db import AgentRun
        from app.workflows.agent_activities import record_run_result

        async with sessions() as db:
            run = await db.get(AgentRun, uuid.UUID(run_id))
            run.status = "running"
            context = (run.input or {}).get("context", {})
            await db.commit()
        state = {
            "user_id": str(user_id_),
            "run_id": run_id,
            "task_type": "resume_optimize",
            "messages": [],
            "context": context,
            "status": "running",
            "model_settings": MagicMock(provider="openai"),
        }
        result = await asyncio.to_thread(resume_agent_node, state)
        await record_run_result(run_id, result)

    monkeypatch.setattr("app.workflows.starters.start_agent_run", start_agent_run)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer user-token"},
    )

    class World:
        pass

    w = World()
    w.client, w.sessions, w.user, w.llm, w.signals, w.files = (
        client,
        sessions,
        user,
        llm,
        signals,
        files,
    )
    w.extension_service = extension_service
    yield w
    await client.aclose()
    app.dependency_overrides.clear()
    database.AsyncSessionLocal.configure(bind=database.engine)
    await engine.dispose()
    sync_engine.dispose()


def _upload_pdf() -> bytes:
    from app.services.pdf_service import generate_resume_pdf

    return generate_resume_pdf(SOURCE, candidate.NAME)


async def _upload_resume(w) -> dict:
    response = await w.client.post(
        "/api/v1/rag/upload",
        data={"doc_type": "resume", "is_primary": "true"},
        files={"file": ("aditi.pdf", io.BytesIO(_upload_pdf()), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _tailor(w) -> dict:
    response = await w.client.post(
        "/api/v1/resume/optimize",
        json={"jd_text": candidate.JD, "template": "modern", "page_target": 2},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_resume_to_approved_submission(world):
    from app.models.db import (
        AgentRun,
        ApplicationAttempt,
        ApplicationSubmitApproval,
        ExtensionTask,
        JobApplication,
        UserDocument,
    )
    from app.services.resume_parseback import check_docx, check_pdf

    w = world

    # 1. Upload and parse the resume (a real PDF, parsed by the real isolated parser).
    uploaded = await _upload_resume(w)
    async with w.sessions() as db:
        stored = await db.get(UserDocument, uuid.UUID(uploaded["id"]))
    for expected in ("Razorpay", "Flipkart", candidate.EMAIL, "Kubernetes", "NIT Trichy"):
        assert expected in stored.raw_text, expected

    # 2. Search a company board through the connector (canned Greenhouse JSON).
    found = await w.client.post(
        "/api/v1/jobs/portal-search",
        json={
            "url": "https://boards.greenhouse.io/acmeindia",
            "query": "backend",
            "max_results": 10,
        },
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["total_found"] == 1 and body["saved_count"] == 1
    job = body["jobs"][0]
    assert job["role"] == "Senior Backend Engineer" and job["company"] == "acmeindia"

    # 3. Tailor the resume: grounded, no warnings, and the files read back clean.
    tailored = await _tailor(w)
    assert tailored["status"] == "awaiting_approval" and tailored["pdf_available"]
    assert TAILORED_SUMMARY in tailored["resume_markdown"]
    assert tailored["warnings"] == []
    assert "terraform" in tailored["keywords_missing"]  # asked for, never claimed
    assert "Terraform" not in tailored["resume_markdown"]
    doc_id = tailored["pdf_document_id"]
    async with w.sessions() as db:
        record = await db.get(UserDocument, uuid.UUID(doc_id))
    assert record.ats_data["parse_back"]["ok"] is True

    pdf = await w.client.get(f"/api/v1/resume/download/{doc_id}")
    docx = await w.client.get(f"/api/v1/resume/download/{doc_id}?format=docx")
    assert pdf.status_code == docx.status_code == 200
    assert check_pdf(pdf.content, tailored["resume_markdown"], candidate.NAME, 2).ok
    assert check_docx(docx.content, tailored["resume_markdown"], candidate.NAME).ok
    assert docx.headers["x-resume-parse-warnings"] == "0"
    reread = (await w.client.get(f"/api/v1/resume/tailored/{doc_id}")).json()
    assert reread["warnings"] == [] and reread["review"]["issues"] == []

    # 4. Plan the application for the extension (Temporal's reserve step, then the device).
    async with w.sessions() as db:
        application = await db.get(JobApplication, uuid.UUID(job["id"]))
        application.resume_id = uuid.UUID(doc_id)
        workflow_id = f"auto-apply/{w.user.id}/{application.id}"
        run = AgentRun(
            id=uuid.uuid4(), user_id=w.user.id, agent_type="auto_apply", status="queued", input={}
        )
        attempt = ApplicationAttempt(
            id=uuid.uuid4(),
            user_id=w.user.id,
            job_application_id=application.id,
            run_id=run.id,
            workflow_id=workflow_id,
            state="preparing",
        )
        db.add_all([run, attempt])
        await db.flush()
        task = ExtensionTask(
            id=uuid.uuid4(),
            user_id=w.user.id,
            run_id=run.id,
            job_application_id=application.id,
            workflow_id=workflow_id,
            kind="apply",
            status="pending",
            payload={
                "job_url": application.job_url,
                "platform": "greenhouse",
                "company": application.company,
                "role": application.role,
                "resume_document_id": doc_id,
                "attempt_id": str(attempt.id),
            },
        )
        db.add(task)
        await db.commit()
        task_id = str(task.id)

    paired = await w.client.post("/api/v1/extension/pair", json={"name": "Chrome"})
    assert paired.status_code == 200, paired.text
    device = {"Authorization": f"Bearer {paired.json()['token']}"}

    claimed = await w.client.post("/api/v1/extension/device/tasks/claim", headers=device)
    assert claimed.status_code == 200 and claimed.json()["id"] == task_id, claimed.text
    event = await w.client.post(
        f"/api/v1/extension/device/tasks/{task_id}/events",
        headers=device,
        json={"stage": "filling"},
    )
    assert event.status_code == 200, event.text

    fields = [
        {
            "id": "first_name",
            "name": "first_name",
            "type": "text",
            "label": "First name",
            "visible": True,
        },
        {
            "id": "email",
            "name": "email",
            "type": "email",
            "label": "Email",
            "visible": True,
            "required": True,
        },
    ]
    planned = await w.client.post(
        f"/api/v1/extension/device/tasks/{task_id}/plan", headers=device, json={"fields": fields}
    )
    assert planned.status_code == 200, planned.text
    plan = {item["label"]: item for item in planned.json()["fields"]}
    assert plan["Email"]["value"] == candidate.EMAIL

    # 5. Review in the browser, then approve-submit: recorded, one-time token issued.
    assert (
        await w.client.post(
            f"/api/v1/extension/device/tasks/{task_id}/events",
            headers=device,
            json={"stage": "review"},
        )
    ).status_code == 200
    review = await w.client.post(
        f"/api/v1/extension/device/tasks/{task_id}/review",
        headers=device,
        json={
            "url": "https://boards.greenhouse.io/acmeindia/jobs/1",
            "fields": [
                {"id": "email", "label": "Email", "type": "email", "value": candidate.EMAIL}
            ],
            "submit_control": "button[type=submit]",
        },
    )
    assert review.status_code == 200, review.text
    approved = await w.client.post(
        f"/api/v1/extension/device/tasks/{task_id}/approve-submit",
        headers=device,
        json={
            "review_hash": review.json()["review_hash"],
            "user_confirmed": True,
            "auto_approved": False,
        },
    )
    assert approved.status_code == 200, approved.text
    token = approved.json()["submission_token"]

    async with w.sessions() as db:
        approvals = (await db.execute(select(ApplicationSubmitApproval))).scalars().all()
        task = await db.get(ExtensionTask, uuid.UUID(task_id))
        attempt = (await db.execute(select(ApplicationAttempt))).scalars().one()
    assert len(approvals) == 1
    assert approvals[0].approved_by == "member" and approvals[0].auto_approved is False
    assert approvals[0].review_hash == review.json()["review_hash"]
    assert str(approvals[0].job_application_id) == job["id"]
    assert task.status == "submitting" and attempt.state == "submitting"

    # The submit outcome is relayed to the workflow, and a replayed approval is refused.
    done = await w.client.post(
        f"/api/v1/extension/device/tasks/{task_id}/events",
        headers=device,
        json={"stage": "submitted", "submission_token": token, "confirmation_text": "Thank you"},
    )
    assert done.status_code == 200, done.text
    assert w.signals.await_args.args[1]["stage"] == "submitted"
    replay = await w.client.post(
        f"/api/v1/extension/device/tasks/{task_id}/approve-submit",
        headers=device,
        json={
            "review_hash": review.json()["review_hash"],
            "user_confirmed": True,
            "auto_approved": False,
        },
    )
    assert replay.status_code == 409


async def test_invented_facts_reach_the_user_as_warnings_not_silently(world):
    w = world
    await _upload_resume(w)
    w.llm.fabricate = True
    tailored = await _tailor(w)
    joined = " ".join(tailored["warnings"])
    assert "Not found in your documents" in joined
    for invented in ("Terraform", "Google"):
        assert invented in joined
