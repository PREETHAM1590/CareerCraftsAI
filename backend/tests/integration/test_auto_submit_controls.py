"""Auto-submit cap, device pairing and erasure of the new member rows, against a
database built by scripts/migrate.py (so the migration's own FKs and checks apply).

    APPLICATIONS_DB_URL=postgresql+asyncpg://workflow_test:<pw>@127.0.0.1:55439/applications_test
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DB_URL = os.getenv("APPLICATIONS_DB_URL")
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_INTEGRATION") != "1" or not DB_URL,
    reason="Disposable migrated PostgreSQL required (APPLICATIONS_DB_URL)",
)


@pytest.fixture
async def maker():
    engine = create_async_engine(DB_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created: list[uuid.UUID] = []
    yield factory, created
    async with factory() as db:
        for user_id in created:
            await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await db.commit()
    await engine.dispose()


async def _user(factory, created, **fields):
    from app.models.db import User

    async with factory() as db:
        user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.test", **fields)
        db.add(user)
        await db.commit()
    created.append(user.id)
    return user


async def _task_in_review(factory, user, device):
    """An extension task whose review is fresh and whose attempt awaits approval."""
    from app.models.db import AgentRun, ApplicationAttempt, ExtensionTask, JobApplication

    workflow_id = f"apply-{uuid.uuid4()}"
    async with factory() as db:
        application = JobApplication(
            id=uuid.uuid4(),
            user_id=user.id,
            company="Acme",
            role="Backend Engineer",
            job_url="https://boards.greenhouse.io/acme/jobs/1",
        )
        run = AgentRun(id=uuid.uuid4(), user_id=user.id, agent_type="apply", status="running")
        db.add_all([application, run])
        await db.flush()
        attempt = ApplicationAttempt(
            id=uuid.uuid4(),
            user_id=user.id,
            job_application_id=application.id,
            run_id=run.id,
            workflow_id=workflow_id,
            state="awaiting_approval",
        )
        task = ExtensionTask(
            id=uuid.uuid4(),
            user_id=user.id,
            device_id=device.id,
            run_id=run.id,
            job_application_id=application.id,
            workflow_id=workflow_id,
            status="review",
            payload={"attempt_id": str(attempt.id), "job_url": application.job_url},
            review_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            review_url=application.job_url,
            review_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db.add_all([attempt, task])
        await db.commit()
    return task


async def test_concurrent_auto_approvals_cannot_exceed_the_daily_cap(maker, monkeypatch):
    from app.api.v1 import extension
    from app.models.db import ApplicationSubmitApproval, UserPreferences
    from app.services import extension_service
    from app.services.extension_service import pair_device

    # Hold each racer between counting and committing so that, without the
    # row lock, both would count the same usage and both would be approved.
    count_usage = extension_service.auto_submit_status

    async def slow_status(*args, **kwargs):
        status = await count_usage(*args, **kwargs)
        await asyncio.sleep(0.3)
        return status

    monkeypatch.setattr(extension_service, "auto_submit_status", slow_status)
    factory, created = maker
    user = await _user(factory, created, policy_accepted_at=datetime.now(UTC))
    async with factory() as db:
        db.add(UserPreferences(user_id=user.id, auto_submit_enabled=True, auto_submit_daily_cap=2))
        await db.commit()
        device, _ = await pair_device(db, user.id, "Browser")
    # One slot of two is already used today, so only one of the racers may win.
    first = await _task_in_review(factory, user, device)
    async with factory() as db:
        db.add(
            ApplicationSubmitApproval(
                user_id=user.id,
                job_application_id=first.job_application_id,
                device_id=device.id,
                auto_approved=True,
                approved_by="auto_submit_setting",
                review_hash=first.review_hash,
            )
        )
        await db.commit()
    tasks = [await _task_in_review(factory, user, device) for _ in range(2)]

    async def approve(task):
        async with factory() as db:
            try:
                return await extension.approve_submit.__wrapped__(
                    request=None,
                    task_id=task.id,
                    body=extension.SubmitApproval(
                        review_hash=task.review_hash, user_confirmed=True, auto_approved=True
                    ),
                    device=device,
                    db=db,
                )
            except HTTPException as exc:
                return exc

    results = await asyncio.gather(*(approve(task) for task in tasks))
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(refused) == 1 and refused[0].status_code == 403
    assert "limit of 2" in refused[0].detail
    assert sum("submission_token" in r for r in results if isinstance(r, dict)) == 1

    async with factory() as db:
        auto_count = await db.scalar(
            select(func.count())
            .select_from(ApplicationSubmitApproval)
            .where(
                ApplicationSubmitApproval.user_id == user.id,
                ApplicationSubmitApproval.auto_approved.is_(True),
            )
        )
    assert auto_count == 2


async def test_pairing_a_sixth_device_revokes_the_least_recently_used(maker):
    from app.models.db import ExtensionDevice
    from app.services.extension_service import MAX_DEVICES_PER_USER, authenticate, pair_device

    factory, created = maker
    user = await _user(factory, created, policy_accepted_at=datetime.now(UTC))
    now = datetime.now(UTC)
    paired = []
    async with factory() as db:
        for index in range(MAX_DEVICES_PER_USER):
            device, token = await pair_device(db, user.id, f"Browser {index}")
            paired.append((device.id, token))
        # Device 2 was used longest ago; the others were seen more recently.
        for index, (device_id, _) in enumerate(paired):
            minutes = 500 if index == 2 else 10 + index
            await db.execute(
                text("UPDATE extension_devices SET last_seen_at = :at WHERE id = :id"),
                {"at": now - timedelta(minutes=minutes), "id": device_id},
            )
        await db.commit()

        sixth, sixth_token = await pair_device(db, user.id, "Browser 5")

    assert sixth.evicted_ids == [paired[2][0]]
    async with factory() as db:
        active = (
            await db.scalars(
                select(ExtensionDevice.id).where(
                    ExtensionDevice.user_id == user.id, ExtensionDevice.revoked_at.is_(None)
                )
            )
        ).all()
        assert len(active) == MAX_DEVICES_PER_USER
        assert paired[2][0] not in active and sixth.id in active
        assert await authenticate(db, paired[2][1]) is None
        assert (await authenticate(db, sixth_token)).id == sixth.id
        assert (await authenticate(db, paired[0][1])).id == paired[0][0]


async def test_account_deletion_removes_approvals_saved_answers_and_boards(maker, monkeypatch):
    from app.applications.saved_answers import save_answer
    from app.models.db import ApplicationSubmitApproval
    from app.services import account_deletion_service as service
    from app.services.extension_service import pair_device
    from app.services.saved_boards import add_board, resolve_board

    factory, created = maker
    expired = datetime.now(UTC) - timedelta(days=1)
    owner = await _user(factory, created, deletion_scheduled_for=expired)
    bystander = await _user(factory, created)

    board = resolve_board("https://boards.greenhouse.io/stripe")
    for user in (owner, bystander):
        async with factory() as db:
            device, _ = await pair_device(db, user.id, "Browser")
            task = await _task_in_review(factory, user, device)
            db.add(
                ApplicationSubmitApproval(
                    user_id=user.id,
                    job_application_id=task.job_application_id,
                    extension_task_id=task.id,
                    device_id=device.id,
                    approved_by="member",
                    review_hash=task.review_hash,
                )
            )
            await db.commit()
            await save_answer(db, user.id, "authorization.work_authorized", True)
            await add_board(db, user.id, board, "https://boards.greenhouse.io/stripe", "Stripe")
            await db.commit()

    # Files, Clerk, Temporal and vector stores are covered by test_account_erasure.
    monkeypatch.setattr(service, "erase_external_data", AsyncMock())

    async def counts(user_id):
        async with factory() as db:
            return {
                table: await db.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE user_id = :id"),  # nosec B608
                    {"id": user_id},
                )
                for table in (
                    "application_submit_approvals",
                    "candidate_answers",
                    "saved_job_boards",
                    "extension_devices",
                )
            }

    assert set((await counts(owner.id)).values()) == {1}
    async with factory() as db:
        assert await service.reap_expired_account_deletions(db) >= 1
        await db.commit()

    assert set((await counts(owner.id)).values()) == {0}
    assert set((await counts(bystander.id)).values()) == {1}
    async with factory() as db:
        assert (
            await db.scalar(text("SELECT count(*) FROM users WHERE id = :id"), {"id": owner.id})
            == 0
        )
