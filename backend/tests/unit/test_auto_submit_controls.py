"""Server-side auto-submit control, the approval audit trail, saved answers,
repeated Workday blocks, evidence and device-token hygiene."""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import extension
from app.api.v1.deps import get_db
from app.applications.answer_resolver import _is_sensitive
from app.applications.models import ResolvedAnswer
from app.applications.question_normalizer import normalize_question
from app.models.db import (
    ApplicationAttempt,
    ApplicationSubmitApproval,
    ExtensionDevice,
    ExtensionTask,
)
from app.services import apply_limits, extension_service

AUTO = {"review_hash": "a" * 64, "user_confirmed": True, "auto_approved": True}
MANUAL = {**AUTO, "auto_approved": False}
ENABLED = {"enabled": True, "daily_cap": 3, "used_last_24h": 0}


@pytest.fixture
def gate(monkeypatch):
    device = ExtensionDevice(id=uuid.uuid4(), user_id=uuid.uuid4())
    task = ExtensionTask(
        id=uuid.uuid4(),
        user_id=device.user_id,
        device_id=device.id,
        job_application_id=uuid.uuid4(),
        status="review",
        review_hash="a" * 64,
        review_url="https://boards.greenhouse.io/acme/jobs/1",
        review_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        workflow_id="test",
        payload={
            "job_url": "https://boards.greenhouse.io/acme/jobs/1",
            "platform": "greenhouse",
            "review_fields": [
                {"id": "f1", "label": "Email", "type": "email", "value": "a@example.com"},
                {"id": "f2", "label": "Resume", "type": "file", "value": "", "digest": "b" * 64},
            ],
            "answer_sources": {"Email": "profile"},
            "auto_blockers": [],
        },
    )
    attempt = ApplicationAttempt(id=uuid.uuid4(), state="awaiting_approval")
    added = []
    db = MagicMock()
    db.add = MagicMock(side_effect=added.append)
    db.commit = AsyncMock()
    monkeypatch.setattr(extension, "_device_task", AsyncMock(return_value=task))
    monkeypatch.setattr(extension, "_task_attempt", AsyncMock(return_value=attempt))
    status = AsyncMock(return_value=dict(ENABLED))
    monkeypatch.setattr(extension_service, "auto_submit_status", status)
    app = FastAPI()
    app.include_router(extension.router)
    app.dependency_overrides[extension.get_device] = lambda: device
    app.dependency_overrides[get_db] = lambda: db
    return SimpleNamespace(app=app, task=task, attempt=attempt, added=added, status=status, db=db)


async def _approve(gate, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate.app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/extension/device/tasks/{gate.task.id}/approve-submit", json=body
        )


@pytest.mark.asyncio
async def test_extensions_that_do_not_say_who_approved_must_update(gate):
    response = await _approve(gate, {"review_hash": "a" * 64, "user_confirmed": True})
    assert response.status_code == 426
    assert gate.attempt.state == "awaiting_approval" and not gate.added


@pytest.mark.asyncio
async def test_manual_approval_is_recorded_with_the_submitted_answers(gate):
    gate.status.return_value = {**ENABLED, "enabled": False}
    response = await _approve(gate, MANUAL)
    assert response.status_code == 200
    [approval] = gate.added
    assert isinstance(approval, ApplicationSubmitApproval)
    assert approval.auto_approved is False and approval.approved_by == "member"
    assert approval.job_application_id == gate.task.job_application_id
    assert approval.review_hash == "a" * 64
    assert approval.answers == [
        {"label": "Email", "type": "email", "value": "a@example.com", "source": "profile"},
        {
            "label": "Resume",
            "type": "file",
            "value": None,
            "source": "page",
            "file_digest": "b" * 64,
        },
    ]
    gate.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_approval_is_recorded_when_the_server_allows_it(gate):
    response = await _approve(gate, AUTO)
    assert response.status_code == 200
    [approval] = gate.added
    assert approval.auto_approved is True and approval.approved_by == "auto_submit_setting"
    assert gate.status.await_args.kwargs == {"lock": True}
    assert gate.attempt.state == "submitting"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,detail",
    [
        ({"status": {"enabled": False}}, "off in your CareerCraft settings"),
        ({"status": {"used_last_24h": 3}}, "limit of 3 a day"),
        ({"payload": {"auto_blockers": ["Why Acme?"]}}, "Why Acme?"),
        ({"review_url": "https://www.linkedin.com/jobs/view/1"}, "LinkedIn and Naukri"),
        ({"payload": {"platform": "naukri"}}, "LinkedIn and Naukri"),
    ],
)
async def test_auto_approval_refused_falls_back_to_the_member(gate, change, detail):
    gate.status.return_value = {**ENABLED, **change.get("status", {})}
    gate.task.payload = {**gate.task.payload, **change.get("payload", {})}
    gate.task.review_url = change.get("review_url", gate.task.review_url)
    response = await _approve(gate, AUTO)
    assert response.status_code == 403
    assert detail in response.json()["detail"]
    assert gate.attempt.state == "awaiting_approval"
    assert not gate.added and not gate.task.approved_at


def test_no_auto_submit_hosts_cover_subdomains_only():
    assert apply_limits.auto_submit_host_blocked("www.naukri.com", None)
    assert apply_limits.auto_submit_host_blocked("in.linkedin.com.")
    assert not apply_limits.auto_submit_host_blocked("notlinkedin.com", "acme.myworkdayjobs.com")
    assert apply_limits.auto_submit_cap_error(2, 3) is None
    assert "limit of 3" in apply_limits.auto_submit_cap_error(3, 3)


# ── Evidence ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_screenshot_is_stored_and_attached(gate, monkeypatch):
    from app.services import storage_service

    upload = MagicMock(return_value="user/shot.jpg")
    monkeypatch.setattr(storage_service, "upload_file", upload)
    approval = ApplicationSubmitApproval(review_hash="a" * 64, screenshot_path=None)
    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = approval
    gate.db.execute = AsyncMock(return_value=lookup)
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0fake").decode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate.app), base_url="http://test"
    ) as client:
        url = f"/extension/device/tasks/{gate.task.id}/evidence"
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()
        assert (
            await client.post(url, json={"review_hash": "a" * 64, "screenshot": png})
        ).status_code == 422
        stale = await client.post(url, json={"review_hash": "c" * 64, "screenshot": jpeg})
        assert stale.status_code == 409
        stored = await client.post(url, json={"review_hash": "a" * 64, "screenshot": jpeg})
    assert stored.status_code == 200
    assert gate.task.payload["review_screenshot"] == {
        "review_hash": "a" * 64,
        "path": "user/shot.jpg",
    }
    assert approval.screenshot_path == "user/shot.jpg"
    assert upload.call_args.args[0] == str(gate.task.user_id)


def test_approval_takes_the_screenshot_of_its_own_review_only():
    task = ExtensionTask(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        review_hash="a" * 64,
        payload={"review_screenshot": {"review_hash": "b" * 64, "path": "old.jpg"}},
    )
    device = ExtensionDevice(id=uuid.uuid4())
    db = MagicMock()
    assert extension_service.record_approval(db, task, device, auto=False).screenshot_path is None
    task.payload["review_screenshot"]["review_hash"] = "a" * 64
    assert (
        extension_service.record_approval(db, task, device, auto=False).screenshot_path == "old.jpg"
    )


# ── Planner ─────────────────────────────────────────────────────────────


def _raw(field_id, label, type_="text", **extra):
    return {
        "id": field_id,
        "name": field_id,
        "type": type_,
        "label": label,
        "group_label": "",
        "value": extra.pop("value", ""),
        "checked": False,
        "required": extra.pop("required", False),
        "options": extra.pop("options", []),
        "visible": True,
        "disabled": False,
        **extra,
    }


class _PlanDB:
    def __init__(self, facts=None):
        self.facts = facts

    async def get(self, model, key):
        return None

    async def execute(self, statement):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        row = SimpleNamespace(answer=self.facts) if self.facts is not None else None
        result.scalar_one_or_none.return_value = row
        return result


def _resolver(values):
    async def resolve_fields(db, user_id, fields, **kwargs):
        out = []
        for f in fields:
            value, source = values.get(f.label, (None, "unresolved"))
            out.append(
                ResolvedAnswer(field_id=f.field_id, value=value, source=source, confidence=0.9)
            )
        return out

    return resolve_fields


@pytest.mark.asyncio
async def test_plan_records_sources_and_marks_drafts_as_auto_blockers(monkeypatch):
    monkeypatch.setattr(
        extension_service,
        "resolve_fields",
        _resolver(
            {
                "Notice period": ("30 days", "user"),
                "Email": ("a@example.com", "profile"),
                "Why Acme?": ("Because…", "generated"),
            }
        ),
    )
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={})
    raw = [_raw("n", "Notice period"), _raw("e", "Email"), _raw("w", "Why Acme?", "textarea")]
    await extension_service.plan_fields(_PlanDB(), task, raw)
    assert task.payload["answer_sources"] == {
        "Notice period": "user",
        "Email": "profile",
        "Why Acme?": "generated",
    }
    assert task.payload["auto_blockers"] == ["Why Acme?"]
    # Blockers accumulate across the steps of one form.
    await extension_service.plan_fields(_PlanDB(), task, [_raw("e", "Email")])
    assert task.payload["auto_blockers"] == ["Why Acme?"]
    status = {"enabled": True, "daily_cap": 5, "used_last_24h": 0}
    task.review_url = "https://jobs.lever.co/acme/1/apply"
    assert "Why Acme?" in extension_service.auto_submit_refusal(status, task)


@pytest.mark.asyncio
async def test_plan_accepts_widget_fields_and_reports_unmatched_dropdowns(monkeypatch):
    monkeypatch.setattr(
        extension_service,
        "resolve_fields",
        _resolver(
            {
                "Country": ("India", "user"),
                "Degree": ("PhD", "user"),
                "Source": ("LinkedIn", "user"),
            }
        ),
    )
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={})
    raw = [
        # A listbox whose options the extension could not read: matched in the page.
        _raw("c", "Country", "select", widget="listbox"),
        _raw("d", "Degree", "select", widget="listbox", options=["Bachelor's", "Master's"]),
        _raw("s", "Source", "text", widget="combobox"),
    ]
    plan = await extension_service.plan_fields(_PlanDB(), task, raw)
    by_id = {f["field_id"]: f for f in plan["fields"]}
    assert by_id["c"]["value"] == "India" and by_id["c"]["source"] == "user"
    assert by_id["d"]["value"] is None and by_id["d"]["source"] == "unresolved"
    assert by_id["d"]["missing_reason"].startswith("No option clearly matches")
    assert by_id["s"]["value"] == "LinkedIn"


@pytest.mark.asyncio
async def test_repeated_blocks_are_filled_only_from_the_members_own_facts(monkeypatch):
    monkeypatch.setattr(extension_service, "resolve_fields", _resolver({}))
    facts = {
        "experience": [
            # Already parsed onto the page by the ATS: not used again.
            {"role": "Engineer", "employer": "Acme Labs", "submitted": ["role", "employer"]},
            {
                "role": "Analyst",
                "employer": "Babbage & Co",
                "start": "Mar 2018",
                "end": "2020-06",
                "location": "London",
                "submitted": ["role", "employer", "start", "end"],
            },
            {"role": "Intern", "employer": "Old Co", "start": "2016", "submitted": ["role"]},
            # Never typed by the member (legacy, possibly model-written).
            {"role": "Guess", "employer": "Model Inc"},
        ],
        "education": [{"institution": "University of London", "degree": "BSc", "end": "2016"}],
    }
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={})
    raw = [
        _raw("t1", "Work Experience 1: Job Title*", value="Engineer"),
        _raw("c1", "Work Experience 1: Company*", value="Acme Labs"),
        _raw("t2", "Work Experience 2: Job Title*", required=True),
        _raw("c2", "Work Experience 2: Company*", required=True),
        _raw("l2", "Work Experience 2: Location"),
        _raw("f2", "Work Experience 2: From*", "date"),
        _raw("e2", "Work Experience 2: To", "date"),
        _raw("d2", "Work Experience 2: Role Description", "textarea"),
    ]
    plan = await extension_service.plan_fields(_PlanDB(facts), task, raw)
    values = {f["field_id"]: (f["value"], f["source"]) for f in plan["fields"]}
    assert values["t2"] == ("Analyst", "profile")
    assert values["c2"] == ("Babbage & Co", "profile")
    assert values["f2"] == ("03/2018", "profile")
    assert values["e2"] == ("06/2020", "profile")
    # Location was not typed by the member; descriptions are never drafted.
    assert values["l2"] == (None, "unresolved") and values["d2"] == (None, "unresolved")
    # "Intern" still needs a block; education needs one too.
    assert plan["add_blocks"] == {"work_experience": 1, "education": 1}
    assert task.payload["auto_blockers"] == []


def test_section_labels_and_dates():
    assert extension_service._section_of("Education 1: School or University*") == (
        "education",
        1,
        "institution",
    )
    assert extension_service._section_of("Why Acme?") is None
    assert extension_service._form_date("January 2021") == "01/2021"
    assert extension_service._form_date("Present") is None
    assert extension_service._form_date("summer") is None


@pytest.mark.asyncio
async def test_prefilled_fields_are_never_drafted(monkeypatch):
    task = SimpleNamespace(user_id=uuid.uuid4(), payload={"resume_document_id": None}, run_id=None)
    generate = extension_service._narrative_generator(task.user_id, task, [3])
    from app.applications.models import ApplicationField

    field = ApplicationField(
        field_id="x", label="Summary", input_type="textarea", required=False, value="Parsed"
    )
    assert await generate(field) is None


def test_voluntary_disclosures_have_canonical_sensitive_keys():
    assert normalize_question("Veteran Status*") == "disclosure.veteran_status"
    assert normalize_question("Please select your gender") == "disclosure.gender"
    assert normalize_question("Race/Ethnicity") == "disclosure.race_ethnicity"
    assert normalize_question("Do you have a disability?") == "disclosure.disability_status"
    assert normalize_question("Will you require visa sponsorship?") == (
        "authorization.requires_sponsorship"
    )
    assert _is_sensitive("disclosure.gender")


# ── Device tokens ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pairing_beyond_the_cap_revokes_the_least_recently_used():
    now = datetime.now(UTC)
    old = [
        ExtensionDevice(id=uuid.uuid4(), user_id=uuid.uuid4(), last_seen_at=now - timedelta(days=i))
        for i in range(extension_service.MAX_DEVICES_PER_USER, 0, -1)
    ]
    listing = MagicMock()
    listing.scalars.return_value.all.return_value = old
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[MagicMock(), listing, MagicMock()])
    db.commit = AsyncMock()
    device, token = await extension_service.pair_device(db, uuid.uuid4(), "New")
    assert device.evicted_ids == [old[0].id]
    assert old[0].revoked_at is not None and all(d.revoked_at is None for d in old[1:])
    assert device.expires_at > now + timedelta(days=89)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_rotation_replaces_the_token_and_expired_tokens_are_rejected():
    device = ExtensionDevice(id=uuid.uuid4(), token_hash="x", expires_at=None)
    db = MagicMock()
    db.commit = AsyncMock()
    token = await extension_service.rotate_token(db, device)
    assert token.startswith("ccx_") and device.token_hash == extension_service.hash_token(token)
    assert device.expires_at > datetime.now(UTC) + timedelta(days=89)

    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=lookup)
    assert await extension_service.authenticate(db, token) is None
    sql = str(db.execute.await_args.args[0])
    assert "expires_at" in sql and "revoked_at IS NULL" in sql


# ── Saved answers API and retired LinkedIn passwords ────────────────────


def _users_app(db, user):
    from app.api.v1 import users
    from app.api.v1.deps import get_current_user

    app = FastAPI()
    app.include_router(users.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.mark.asyncio
async def test_saved_answers_api(monkeypatch):
    from app.applications import saved_answers

    saved = AsyncMock()
    deleted = AsyncMock()
    monkeypatch.setattr(saved_answers, "save_answer", saved)
    monkeypatch.setattr(saved_answers, "delete_answer", deleted)
    row = SimpleNamespace(
        question_key="experience.notice_period_days",
        answer={"value": "30 days"},
        normalized_question="Notice?",
        updated_at=None,
    )
    custom = SimpleNamespace(
        question_key="custom.why acme",
        answer={"value": "x"},
        normalized_question="Why Acme?",
        updated_at=None,
    )
    facts = SimpleNamespace(
        question_key="resume.facts", answer={}, normalized_question="", updated_at=None
    )
    listing = MagicMock()
    listing.scalars.return_value.all.return_value = [row, custom, facts]
    db = MagicMock()
    db.execute = AsyncMock(return_value=listing)
    db.flush = AsyncMock()
    user = SimpleNamespace(id=uuid.uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_users_app(db, user)), base_url="http://test"
    ) as client:
        answers = (await client.get("/users/me/saved-answers")).json()["answers"]
        keys = [a["question_key"] for a in answers]
        assert keys[:6] == [
            "authorization.work_authorized",
            "authorization.requires_sponsorship",
            "experience.notice_period_days",
            "compensation.expected_salary",
            "location.willing_to_relocate",
            "availability.start_date",
        ]
        assert "resume.facts" not in keys and keys[-1] == "custom.why acme"
        assert (
            next(a for a in answers if a["question_key"] == row.question_key)["value"] == "30 days"
        )

        put = await client.put(
            "/users/me/saved-answers/authorization.requires_sponsorship", json={"value": " No "}
        )
        assert put.status_code == 200 and put.json()["value"] == "No"
        saved.assert_awaited_once_with(db, user.id, "authorization.requires_sponsorship", "No")
        assert (
            await client.put("/users/me/saved-answers/resume.facts", json={"value": "x"})
        ).status_code == 404
        assert (
            await client.put("/users/me/saved-answers/disclosure.gender", json={"value": " "})
        ).status_code == 422
        assert (await client.delete("/users/me/saved-answers/disclosure.gender")).status_code == 204
        deleted.assert_awaited_once_with(db, user.id, "disclosure.gender")

        gone = await client.post(
            "/users/me/linkedin-credentials", json={"email": "a@b.c", "password": "pw"}
        )
        assert gone.status_code == 405
