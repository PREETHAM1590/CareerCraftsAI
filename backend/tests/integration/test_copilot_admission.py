"""Chat and workflow admission share the same cross-replica user lock."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1",
    reason="Disposable workflow database required",
)


@pytest.fixture
async def member(monkeypatch):
    from app.core.database import Base
    from app.models.db import User
    from app.services import copilot_history

    engine = create_async_engine(
        "postgresql+asyncpg://workflow_test:workflow_test@127.0.0.1:55439/workflow_test"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    user = User(
        id=uuid.uuid4(),
        clerk_user_id=f"user_{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex}@example.test",
        policy_accepted_at=datetime.now(UTC),
    )
    async with maker() as db:
        db.add(user)
        await db.commit()
    monkeypatch.setattr(copilot_history, "AsyncSessionLocal", maker)
    yield maker, user
    async with maker() as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()
    await engine.dispose()


async def test_distinct_threads_and_workflow_share_atomic_cap(member, monkeypatch):
    from unittest.mock import AsyncMock

    from app.api.v1.run_utils import queue_agent_run
    from app.models.db import AgentRun
    from app.services import copilot_history as history

    maker, user = member
    monkeypatch.setattr("app.workflows.starters.start_agent_run", AsyncMock())

    async def chat(thread):
        try:
            return await history.start_turn(
                user.clerk_user_id,
                thread,
                "wire-run",
                [
                    {"id": thread, "role": "user", "content": "Hello"},
                ],
            )
        except HTTPException as exc:
            return exc.status_code

    async def workflow():
        async with maker() as db:
            try:
                return await queue_agent_run(db, user, "job_search", {"query": "Python"})
            except HTTPException as exc:
                return exc.status_code

    results = await asyncio.gather(chat("a"), chat("b"), workflow())
    assert results.count(429) == 1
    async with maker() as db:
        runs = (
            (await db.execute(select(AgentRun).where(AgentRun.user_id == user.id))).scalars().all()
        )
    assert len(runs) == 2
    assert all(run.status in {"running", "queued"} for run in runs)
    # Finishing one admitted chat releases its slot for a different thread.
    for result, thread in zip(results[:2], ["a", "b"], strict=True):
        if isinstance(result, tuple):
            await history.finish_turn(user.id, thread, "wire-run", execution_id=result[2])
            break
    assert isinstance(await chat("c"), tuple)


async def test_crashed_chat_slot_expires_and_logging_accumulates(member, monkeypatch):
    from langchain_core.messages import AIMessage

    from app.agents.chat_orchestrator import _log_turn, chat_execution_id
    from app.models.db import AgentRun
    from app.services import copilot_history as history

    maker, user = member
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", maker)
    old_id = uuid.uuid4()
    old_outreach_id, durable_email_id = uuid.uuid4(), uuid.uuid4()
    async with maker() as db:
        db.add(
            AgentRun(
                id=old_id,
                user_id=user.id,
                agent_type="chat_orchestrator",
                status="running",
                started_at=datetime.now(UTC) - timedelta(minutes=11),
            )
        )
        db.add_all(
            [
                AgentRun(
                    id=old_outreach_id,
                    user_id=user.id,
                    agent_type="email",
                    status="running",
                    input={"source": "application_outreach"},
                    started_at=datetime.now(UTC) - timedelta(minutes=11),
                ),
                AgentRun(
                    id=durable_email_id,
                    user_id=user.id,
                    agent_type="email",
                    status="running",
                    input={"task_type": "email", "context": {}},
                    started_at=datetime.now(UTC) - timedelta(minutes=11),
                ),
            ]
        )
        await db.commit()
    _, _, execution_id = await history.start_turn(
        user.clerk_user_id,
        "new",
        "wire",
        [
            {"id": "u", "role": "user", "content": "Hello"},
        ],
    )
    token = chat_execution_id.set(execution_id)
    try:
        response = AIMessage(
            content="Ready",
            usage_metadata={
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        )
        await _log_turn(user.clerk_user_id, response)
        await _log_turn(user.clerk_user_id, response, status="failed")
    finally:
        chat_execution_id.reset(token)
    await history.finish_turn(user.id, "new", "wire", execution_id=execution_id)
    async with maker() as db:
        assert (await db.get(AgentRun, old_id)).status == "failed"
        assert (await db.get(AgentRun, old_outreach_id)).status == "failed"
        assert (await db.get(AgentRun, durable_email_id)).status == "running"
        run = await db.get(AgentRun, execution_id)
        assert run.tokens_used == 10
        assert run.status == "failed"
        assert run.completed_at is not None
        assert run.duration_ms >= 0


async def test_request_checkpoints_restore_sql_history_without_duplicates(member, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import httpx
    from fastapi import FastAPI
    from langchain_core.messages import AIMessage

    from app.api.v1.copilot_chat import mount_copilot_chat
    from app.api.v1.deps import get_current_user
    from app.core.request_context import reset_current_user_id, set_current_user_id
    from app.services import copilot_history as history

    maker, user = member
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", maker)
    prompts = []

    async def answer(messages):
        prompts.append(messages)
        return AIMessage(content="Ready")

    model = MagicMock()
    model.bind_tools.return_value.ainvoke = AsyncMock(side_effect=answer)
    monkeypatch.setattr("app.core.llm_gateway.get_chat_gateway_llm", AsyncMock(return_value=model))
    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user.id)
    mount_copilot_chat(app)
    token = set_current_user_id(user.clerk_user_id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for i in range(2):
                response = await client.post(
                    "/api/v1/agents/chat",
                    json={
                        "threadId": "durable",
                        "runId": f"turn{i}",
                        "state": {},
                        "tools": [],
                        "context": [],
                        "messages": [
                            {
                                "id": f"u{i}",
                                "role": "user",
                                "content": f"Question {i}",
                            }
                        ],
                    },
                )
                assert response.status_code == 200
                assert "RUN_ERROR" not in response.text
    finally:
        reset_current_user_id(token)
    assert len(prompts) == 2
    assert [m.content for m in prompts[1] if m.type == "human"] == [
        "Question 0",
        "Question 1",
    ]
    stored = await history.get_thread(user.clerk_user_id, "durable")
    assert len(stored["messages"]) == 4
    assert len({m["id"] for m in stored["messages"]}) == 4


@pytest.mark.parametrize("success", [True, False, "cancel"])
async def test_application_outreach_logs_admitted_gateway_work(member, monkeypatch, success):
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core.model_router import _add_tokens
    from app.core.sync_db import _chosen
    from app.models.db import AgentRun, JobApplication, UserDocument
    from app.services import auto_apply_queue as queue

    maker, user = member
    monkeypatch.setattr(queue, "AsyncSessionLocal", maker)
    document = UserDocument(
        id=uuid.uuid4(),
        user_id=user.id,
        doc_type="resume",
        filename="resume.pdf",
        storage_path="test-only",
    )
    application = JobApplication(
        id=uuid.uuid4(),
        user_id=user.id,
        company="Acme",
        role="Engineer",
        job_url="https://jobs.acme.test/1",
        resume_id=document.id,
    )
    async with maker() as db:
        db.add(document)
        await db.flush()
        db.add(application)
        await db.commit()
    contact = SimpleNamespace(
        name="Recruiter",
        email="recruiter@acme.test",
        verdict="valid",
        source="posting",
        verified_by="test",
    )
    monkeypatch.setattr(
        "app.services.recruiter_email.find_recruiter_contact",
        AsyncMock(return_value=SimpleNamespace(best=contact)),
    )
    monkeypatch.setattr(
        "app.core.sync_db.fetch_model_settings",
        lambda _: SimpleNamespace(user_id=user.id),
    )
    monkeypatch.setattr("app.core.sync_db.fetch_user_profile_text", lambda _: "Profile")
    monkeypatch.setattr("app.core.model_router.build_agent_llm", lambda _: object())

    entered, release = threading.Event(), threading.Event()

    def generated(*args):
        assert _chosen("resume_document_id") == document.id
        if success == "cancel":
            entered.set()
            assert release.wait(5)
        _add_tokens(7)
        return {"subject": "Hello", "body": "Draft"} if success else None

    monkeypatch.setattr("app.agents.auto_apply_pipeline._generate_cold_email", generated)
    queued = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), state="needs_review"))
    monkeypatch.setattr("app.services.outreach_service.queue_outreach", queued)
    if success == "cancel":
        task = asyncio.create_task(queue.draft_outreach(str(user.id), application))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        async with maker() as db:
            active = (
                await db.execute(select(AgentRun).where(AgentRun.user_id == user.id))
            ).scalar_one()
            assert active.status == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        state = await queue.draft_outreach(str(user.id), application)
        assert state == ("needs_review" if success else None)
    async with maker() as db:
        run = (await db.execute(select(AgentRun).where(AgentRun.user_id == user.id))).scalar_one()
        assert run.agent_type == "email"
        assert run.status == ("completed" if success is True else "failed")
        assert run.tokens_used == 7
        assert run.completed_at is not None
        assert run.duration_ms >= 0
        assert run.input["context"]["application_id"] == str(application.id)
        assert "recruiter@" not in str(run.input)
    assert queued.await_count == int(success is True)


async def test_admission_blocks_erasing_and_missing_accounts(member):
    from app.api.v1.run_utils import check_run_admission
    from app.models.db import AgentRun, User
    from app.services import copilot_history as history

    maker, user = member
    async with maker() as db:
        row = await db.get(User, user.id)
        row.deletion_scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    with pytest.raises(HTTPException) as exc:
        await history.start_turn(
            user.clerk_user_id,
            "erasing",
            "wire",
            [
                {"id": "new", "role": "user", "content": "Hello"},
            ],
        )
    assert exc.value.status_code == 403
    async with maker() as db:
        assert (
            await db.execute(select(AgentRun).where(AgentRun.user_id == user.id))
        ).first() is None
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            await check_run_admission(db, user)
    assert exc.value.status_code == 404
