import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.api.v1.computer import SaveCredential, metadata, router
from app.api.v1.deps import get_current_user, get_db
from app.services.computer_service import AGENT_WRITES, ComputerAction, credential_origin
from app.services.workflow_service import validate_approval


@pytest.mark.parametrize(
    "origin",
    [
        "http://portal.example",
        "https://portal.example/path",
        "https://u:p@portal.example",
        "https://portal.example:444",
        "https://portal.example?key=secret",
    ],
)
def test_exact_credential_origin(origin):
    with pytest.raises(ValueError):
        credential_origin(origin)


def test_secret_model_and_metadata():
    credential = SaveCredential(
        origin="https://PORTAL.example/",
        label="Test",
        username="privateuser",
        password="privatepassword",
    )
    assert credential.origin == "https://portal.example"
    assert "privatepassword" not in repr(credential)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        origin=credential.origin,
        label="Test",
        username_enc="encrypted",
        password_enc="encrypted",
    )
    assert set(metadata(row)) == {"id", "origin", "label"}


def test_private_validation_never_echoes_secret():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    app.dependency_overrides[get_db] = lambda: None
    response = TestClient(app).post(
        "/computer/credentials",
        json={
            "origin": "bad",
            "label": "test",
            "username": "privateuser",
            "password": "privatepassword",
        },
    )
    assert response.status_code == 422
    assert "privatepassword" not in response.text
    assert "privateuser" not in response.text


def test_saved_login_fill_is_owner_scoped():
    app = FastAPI()
    app.include_router(router)
    owner = uuid.uuid4()
    foreign_login = uuid.uuid4()
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner)
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.api.v1.computer.decrypt_api_key") as decrypt:
        response = TestClient(app).post(
            "/computer/credentials/fill",
            json={
                "credential_id": str(foreign_login),
                "username_ref": "e1",
                "password_ref": "e2",
                "snapshot_id": 1,
                "computer_run": "run1",
            },
        )
    assert response.status_code == 404
    decrypt.assert_not_called()
    query = db.execute.await_args.args[0].compile()
    assert owner in query.params.values()
    assert foreign_login in query.params.values()


@pytest.mark.parametrize(
    "operation", ["credentials/fill", "screenshot", "exec", "evaluate", "human/type"]
)
def test_model_has_no_private_capability(operation):
    with pytest.raises(ValueError):
        ComputerAction(operation=operation).validate_operation(AGENT_WRITES)


def test_upload_requires_stored_document_and_reviewed_snapshot():
    valid = ComputerAction(
        operation="upload",
        computer_run="run",
        approved_snapshot=3,
        parameters={"document_id": str(uuid.uuid4()), "ref": "e1", "snapshotId": 3},
    )
    valid.validate_operation(AGENT_WRITES)
    for changes in [{"approved_snapshot": 2}, {"computer_run": None}]:
        with pytest.raises(ValueError):
            valid.model_copy(update=changes).validate_operation(AGENT_WRITES)
    with pytest.raises(ValueError):
        ComputerAction(operation="upload", parameters={"path": "/etc/passwd"}).validate_operation(
            AGENT_WRITES
        )


@pytest.mark.asyncio
async def test_upload_cannot_read_another_members_resume():
    from app.services.computer_service import act

    owner, document = uuid.uuid4(), uuid.uuid4()
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    context = AsyncMock()
    context.__aenter__.return_value = db
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=context),
        patch("app.services.computer_service.audit", new=AsyncMock(return_value="audit")),
        patch("app.services.computer_service.finish_audit", new=AsyncMock()),
        patch("app.services.computer_service.relay", new=AsyncMock()) as relay,
        patch("app.services.storage_service.download_file") as read,
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await act(
                owner,
                ComputerAction(
                    operation="upload",
                    computer_run="run",
                    approved_snapshot=3,
                    parameters={"document_id": str(document), "ref": "e1", "snapshotId": 3},
                ),
                "agent",
            )
        assert exc.value.status_code == 404
        read.assert_not_called()
        relay.assert_not_called()
        query = db.execute.await_args.args[0].compile()
        assert owner in query.params.values() and document in query.params.values()


def test_approval_payload_immutable():
    original = {
        "type": "computer_action",
        "action": {"operation": "click", "parameters": {"ref": "e1", "snapshotId": 1}},
    }
    assert validate_approval(original, {}) == original
    with pytest.raises(ValueError):
        validate_approval(original, {"body": "change target"})


@pytest.mark.asyncio
async def test_private_browser_lock_reports_required_input_without_reading_page():
    from fastapi import HTTPException

    from app.agents import computer_agent as agent

    run = SimpleNamespace(user_id=uuid.uuid4(), input={"context": {"task": "Open employer page"}})
    with (
        patch.object(
            agent, "act", AsyncMock(side_effect=HTTPException(409, "Private input locked"))
        ) as act,
        patch.object(agent, "get_chat_gateway_llm", AsyncMock()) as gateway,
    ):
        result = await agent.plan(run)
    assert result["result"]["input_required"] == "browser_handoff"
    assert act.await_count == 1
    gateway.assert_not_called()


@pytest.mark.asyncio
async def test_planner_cannot_execute_own_proposal():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(
        user_id=uuid.uuid4(),
        id=uuid.uuid4(),
        input={"context": {"task": "Open employer page"}},
        tokens_used=0,
    )
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "c1",
                    "name": "propose_browser_step",
                    "args": {
                        "operation": "navigate",
                        "parameters": {"url": "https://employer.example"},
                        "summary": "Open employer",
                    },
                }
            ],
        )
    )
    snapshot = {"snapshotId": 1, "computer_run": "run1", "elements": []}
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get = AsyncMock(
        return_value=SimpleNamespace(
            full_name="Test Candidate", email="test@example.com", phone=None, linkedin_url=None
        )
    )
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalars.return_value.first.return_value = None
    with (
        patch.object(agent, "act", AsyncMock(return_value=snapshot)) as act,
        patch.object(agent, "AsyncSessionLocal", return_value=db),
        patch.object(agent, "get_chat_gateway_llm", AsyncMock(return_value=llm)),
    ):
        result = await agent.plan(run)
    assert result["status"] == "awaiting_approval"
    assert result["pending_action"]["action"]["approved_snapshot"] == 1
    assert act.await_count == 1
    assert act.await_args.args[1].operation == "snapshot"


@pytest.mark.asyncio
async def test_unknown_outcome_is_never_replanned():
    from app.agents import computer_agent as agent

    run = SimpleNamespace(user_id=uuid.uuid4(), agent_type="computer_task")
    pending = {
        "action": {
            "operation": "click",
            "parameters": {"ref": "e1", "snapshotId": 1},
            "computer_run": "r",
            "approved_snapshot": 1,
        }
    }
    with (
        patch.object(agent, "act", AsyncMock(side_effect=TimeoutError)),
        patch.object(agent, "plan", AsyncMock()) as plan,
    ):
        with pytest.raises(TimeoutError):
            await agent.continue_step(run, pending)
    plan.assert_not_called()


@pytest.mark.asyncio
async def test_site_permission_refusal_is_reproposed_not_failed():
    from fastapi import HTTPException

    from app.agents import computer_agent as agent

    run = SimpleNamespace(user_id=uuid.uuid4(), agent_type="computer_task")
    pending = {"action": {"operation": "navigate", "parameters": {"url": "https://jobs.example"}}}
    refused = HTTPException(403, f"{agent.PERMISSION_PREFIX}https://jobs.example to continue.")
    with (
        patch.object(agent, "act", AsyncMock(side_effect=refused)),
        patch.object(
            agent, "plan", AsyncMock(return_value={"status": "awaiting_approval"})
        ) as plan,
    ):
        assert (await agent.continue_step(run, pending))["status"] == "awaiting_approval"
    assert "https://jobs.example" in plan.await_args.args[1]


def test_unconfigured_sandbox_reports_unavailable_but_actions_still_refuse(monkeypatch):
    from app.services import computer_service

    monkeypatch.setattr(computer_service.settings, "SANDBOX_RELAY_URL", "")
    monkeypatch.setattr(computer_service.settings, "SANDBOX_RELAY_TOKEN", "")
    cloud = {"mode": "cloud", "backend": "cloud", "paired": False, "device": None}
    monkeypatch.setattr(computer_service, "computer_backend", AsyncMock(return_value=cloud))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    client = TestClient(app)

    status = client.get("/computer")
    assert status.status_code == 200
    assert status.json() == {"available": False, "running": False, "backend": "cloud"}
    # The page shows "not available"; anything that needs the relay still fails.
    assert client.post("/computer/stop").status_code == 503
