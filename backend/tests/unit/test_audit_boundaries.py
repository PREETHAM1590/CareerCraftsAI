"""Regressions for shared artifact, identity, and durable API boundaries."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


@pytest.mark.parametrize("doc_type", ["resume", "resume_tailored"])
async def test_submission_accepts_owned_uploaded_and_generated_pdfs(monkeypatch, doc_type):
    from app.applications import submission

    owner, document_id = uuid.uuid4(), uuid.uuid4()
    doc = SimpleNamespace(doc_type=doc_type, storage_path=f"{owner}/resume.pdf")
    db = AsyncMock()

    async def execute(query):
        values = query.compile().params.values()
        assert owner in values and document_id in values
        return SimpleNamespace(scalar_one_or_none=lambda: doc)

    db.execute = execute
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(submission, "AsyncSessionLocal", lambda: context)
    download = MagicMock(return_value=b"%PDF-valid")
    monkeypatch.setattr("app.services.storage_service.download_file", download)
    content, digest = await submission.load_resume(owner, str(document_id))
    assert content == b"%PDF-valid" and len(digest) == 64
    download.assert_called_once_with(doc.storage_path, str(owner))


@pytest.mark.parametrize("kind", [None, "cover_letter"])
async def test_submission_rejects_missing_or_nonresume_before_storage(monkeypatch, kind):
    from app.applications import submission

    context = MagicMock()
    context.__aenter__ = AsyncMock(
        return_value=SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    scalar_one_or_none=lambda: (SimpleNamespace(doc_type=kind) if kind else None)
                )
            )
        )
    )
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(submission, "AsyncSessionLocal", lambda: context)
    download = MagicMock()
    monkeypatch.setattr("app.services.storage_service.download_file", download)
    with pytest.raises(ValueError, match="unavailable"):
        await submission.load_resume(uuid.uuid4(), str(uuid.uuid4()))
    download.assert_not_called()


async def test_document_owner_guard_refreshes_and_refuses_due_erasure():
    from app.services.document_lifecycle import lock_document_owner

    owner = uuid.uuid4()
    row = SimpleNamespace(deletion_scheduled_for=datetime.now(UTC) - timedelta(seconds=1))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row))
    )
    with pytest.raises(HTTPException) as exc:
        await lock_document_owner(db, owner)
    assert exc.value.status_code == 403
    query = db.execute.await_args.args[0]
    assert query.get_execution_options()["populate_existing"]
    assert query._for_update_arg is not None
    assert owner in query.compile().params.values()


async def test_idle_sse_uses_native_wait_and_emits_keepalive(monkeypatch):
    from app.core import event_bus

    pubsub = SimpleNamespace(
        subscribe=AsyncMock(),
        unsubscribe=AsyncMock(),
        aclose=AsyncMock(),
        get_message=AsyncMock(return_value=None),
    )
    redis = SimpleNamespace(pubsub=lambda: pubsub, aclose=AsyncMock())
    monkeypatch.setattr(event_bus.aioredis, "from_url", lambda *a, **k: redis)
    stream = event_bus.stream_events("idle-run")
    assert "event: ping" in await anext(stream)
    pubsub.get_message.assert_awaited_once_with(ignore_subscribe_messages=True, timeout=5.0)
    await stream.aclose()
    pubsub.aclose.assert_awaited_once()
    redis.aclose.assert_awaited_once()


async def test_email_compose_dispatches_durable_custom_draft(monkeypatch):
    from app.api.v1 import email, run_utils

    user, db = SimpleNamespace(id=uuid.uuid4()), object()
    queue = AsyncMock(return_value=str(uuid.uuid4()))
    draft = {"type": "send_email", "subject": "Custom", "body": "User draft"}
    wait = AsyncMock(return_value=SimpleNamespace(status="awaiting_approval", output=draft))
    monkeypatch.setattr(run_utils, "queue_agent_run", queue)
    monkeypatch.setattr(run_utils, "wait_for_agent_result", wait)
    response = await email.compose_email.__wrapped__(
        None,
        email.ComposeRequest(
            company="Acme",
            role="Engineer",
            recipient_email="hr@example.com",
            subject="Custom",
            body="User draft",
        ),
        db,
        user,
    )
    assert response["draft"] == draft
    assert queue.await_args.args[2] == "email"
    assert queue.await_args.args[3]["body"] == "User draft"
    wait.assert_awaited_once_with(db, user, response["run_id"])


async def test_result_waiter_refreshes_owned_run_and_releases_transactions():
    from app.api.v1.run_utils import wait_for_agent_result

    owner, run_id = uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(status="awaiting_approval", output={"body": "Ready"})
    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)),
    )
    assert await wait_for_agent_result(db, SimpleNamespace(id=owner), str(run_id)) is run
    query = db.execute.await_args.args[0]
    assert {owner, run_id} <= set(query.compile().params.values())
    assert query.get_execution_options()["populate_existing"]
    db.commit.assert_awaited_once()


async def test_linkedin_approval_reports_manual_handoff_without_sending(monkeypatch):
    from app.api.v1 import linkedin

    owner, run_id = uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(status="awaiting_approval")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run))
    )
    with pytest.raises(HTTPException) as exc:
        await linkedin.approve_outreach(
            run_id, linkedin.OutreachApproveRequest(approved=True), db, SimpleNamespace(id=owner)
        )
    assert exc.value.status_code == 409 and "Copy the draft" in exc.value.detail
    assert run.status == "awaiting_approval"
    assert not hasattr(
        __import__("app.services.browser_control_service", fromlist=["x"]),
        "linkedin_send_connection",
    )


async def test_profile_can_clear_optional_fields_without_clearing_omitted_ones():
    from app.api.v1.users import update_profile
    from app.models.schemas import UserProfileUpdate

    user = SimpleNamespace(
        phone="123", headline="Engineer", linkedin_url="https://linkedin.com/in/a"
    )
    db = SimpleNamespace(flush=AsyncMock())
    await update_profile(UserProfileUpdate(phone=None, linkedin_url=None), db, user)
    assert user.phone is None and user.linkedin_url is None and user.headline == "Engineer"
