"""CareerCraft's owner-scoped client for the private, bounded OpenBot relay."""

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings

AGENT_READS = {"read", "snapshot", "files/list", "files/read", "files/write"}
AGENT_WRITES = {"navigate", "click", "type", "scroll", "upload"}
HUMAN_ACTIONS = {
    "upload",
    "navigate",
    "snapshot",
    "screenshot",
    "control",
    "control/request",
    "control/take",
    "control/release",
    "privacy/release",
    "human/click",
    "human/type",
    "human/key",
    "human/scroll",
    "human/navigate",
    "human/secret",
}
PARAMETERS = {
    "upload": {"document_id", "ref", "snapshotId"},
    "read": set(),
    "snapshot": set(),
    "screenshot": set(),
    "control": set(),
    "navigate": {"url"},
    "click": {"ref", "snapshotId"},
    "type": {"ref", "snapshotId", "text"},
    "scroll": {"deltaY"},
    "files/list": {"path"},
    "files/read": {"path"},
    "files/write": {"path", "contents"},
    "control/request": {"reason"},
    "control/take": {"requestId"},
    "control/release": {"requestId"},
    "human/click": {"x", "y"},
    "human/type": {"text"},
    "human/key": {"key"},
    "human/scroll": {"deltaY"},
    "human/secret": {"text"},
    "privacy/release": set(),
    "human/navigate": {"url"},
}


class ComputerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    parameters: dict = Field(default_factory=dict)
    computer_run: str | None = None
    approved_snapshot: int | None = None

    @field_validator("parameters")
    @classmethod
    def bounded(cls, value):
        if len(json.dumps(value)) > 20000:
            raise ValueError("Computer action is too large")
        return value

    def validate_operation(self, allowed):
        if self.operation not in allowed:
            raise ValueError("This operation is not available")
        if set(self.parameters) - PARAMETERS[self.operation]:
            raise ValueError("Unexpected computer action parameters")
        required = PARAMETERS[self.operation] - (
            {"path"} if self.operation == "files/list" else set()
        )
        if not required.issubset(self.parameters):
            raise ValueError("Required computer action parameters are missing")
        if self.operation in {"click", "type", "upload"}:
            if (
                not self.computer_run
                or not isinstance(self.parameters.get("snapshotId"), int)
                or not self.parameters.get("ref")
            ):
                raise ValueError("Use the current computer run, snapshotId and element ref")
        if self.operation == "upload":
            uuid.UUID(str(self.parameters["document_id"]))
            if self.approved_snapshot != self.parameters["snapshotId"]:
                raise ValueError("Upload requires the reviewed snapshot")
        if self.operation == "type" and not isinstance(self.parameters.get("text"), str):
            raise ValueError("Text is required")
        if self.operation in {"navigate", "human/navigate"}:
            value = urlsplit(str(self.parameters.get("url", "")))
            if (
                value.scheme not in {"https", "http"}
                or not value.hostname
                or value.username
                or value.password
            ):
                raise ValueError("Use an HTTP(S) URL without credentials")
        if self.operation.startswith("files/"):
            path = str(self.parameters.get("path", ""))
            if (
                path.startswith(("/", "\\"))
                or ".." in path.replace("\\", "/").split("/")
                or ":" in path
            ):
                raise ValueError("File paths must stay within your workspace")


def credential_origin(value: str):
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Use the exact HTTPS portal origin, for example https://jobs.example.com")
    return "https://" + parsed.hostname.lower().encode("idna").decode()


def is_configured() -> bool:
    return bool(settings.SANDBOX_RELAY_URL and settings.SANDBOX_RELAY_TOKEN)


async def relay(user_id, route="", method="GET", body=None):
    if not is_configured():
        raise HTTPException(503, "Sandbox worker is not configured")
    url = settings.SANDBOX_RELAY_URL.rstrip("/") + "/users/" + str(uuid.UUID(str(user_id))) + route
    try:
        async with httpx.AsyncClient(timeout=75, trust_env=False) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": "Bearer " + settings.SANDBOX_RELAY_TOKEN},
                json=body,
            )
        if response.is_error:
            detail = response.json().get("detail", "Sandbox action failed")
            raise HTTPException(response.status_code, str(detail)[:300])
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Sandbox worker is unavailable") from exc


async def purge_user(user_id) -> None:
    """Strict owner-scoped erasure; failures keep the account for sweep retry."""
    # An unconfigured feature may be skipped only for owners with no recorded
    # computer activity. Removing configuration cannot silently erase an account
    # that previously allocated remote storage.
    if not settings.SANDBOX_RELAY_URL and not settings.SANDBOX_RELAY_TOKEN:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.db import AgentRun

        async with AsyncSessionLocal() as db:
            used = (
                await db.execute(
                    select(AgentRun.id)
                    .where(
                        AgentRun.user_id == uuid.UUID(str(user_id)),
                        AgentRun.agent_type == "computer_action",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        if used is not None:
            raise RuntimeError("Restore sandbox relay configuration to erase this computer owner")
        return
    result = await relay(user_id, method="DELETE")
    if result.get("purged") is not True:
        raise RuntimeError("Computer erasure was not confirmed")


async def audit(user_id, operation, status="completed", duration_ms=0):
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun

    async with AsyncSessionLocal() as db:
        row = AgentRun(
            user_id=user_id,
            agent_type="computer_action",
            status=status,
            input={"operation": operation},
            output={"status": status},
            duration_ms=duration_ms,
            tokens_used=0,
            completed_at=datetime.now(UTC) if status != "running" else None,
        )
        db.add(row)
        await db.commit()
        return row.id


async def finish_audit(row_id, status, duration_ms):
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun

    async with AsyncSessionLocal() as db:
        row = await db.get(AgentRun, row_id)
        row.status = status
        row.output = {"status": status}
        row.duration_ms = duration_ms
        row.completed_at = datetime.now(UTC)
        await db.commit()


async def act(user_id, action: ComputerAction, actor="human"):
    if actor not in {"human", "agent"}:
        raise ValueError("Unknown computer actor")
    action.validate_operation(HUMAN_ACTIONS if actor == "human" else AGENT_READS | AGENT_WRITES)
    if (
        actor == "agent"
        and action.operation in AGENT_WRITES
        and (not action.computer_run or not action.approved_snapshot)
    ):
        raise ValueError("A browser change requires the reviewed computer snapshot")
    started = time.monotonic()
    status = "completed"
    # Fail before dispatch if the action cannot be recorded. A DB outage after
    # dispatch must not turn success into a retry of an external write.
    audit_id = None
    if action.operation not in {"screenshot", "control"}:
        audit_id = await audit(user_id, action.operation, "running")
    try:
        body = {**action.model_dump(), "actor": actor}
        if action.operation == "upload":
            from sqlalchemy import select

            from app.api.v1.rag import MAX_SIZE_BYTES, _sniff_content_type
            from app.core.database import AsyncSessionLocal
            from app.models.db import UserDocument
            from app.services.storage_service import download_file

            async with AsyncSessionLocal() as db:
                doc = (
                    await db.execute(
                        select(UserDocument).where(
                            UserDocument.id == uuid.UUID(str(action.parameters["document_id"])),
                            UserDocument.user_id == uuid.UUID(str(user_id)),
                            UserDocument.doc_type == "resume",
                        )
                    )
                ).scalar_one_or_none()
                if not doc:
                    raise HTTPException(404, "Resume not found")
                content = await asyncio.to_thread(download_file, doc.storage_path, str(user_id))
                mime = _sniff_content_type(content)
                if len(content) > MAX_SIZE_BYTES or mime is None:
                    raise HTTPException(422, "Resume format or size is unsupported")
                body["parameters"] = {
                    "ref": action.parameters["ref"],
                    "snapshotId": action.parameters["snapshotId"],
                    "filename": doc.filename,
                    "mimeType": mime,
                    "base64": base64.b64encode(content).decode("ascii"),
                }
        return await relay(user_id, "/action", "POST", body)
    except Exception:
        status = "failed"
        raise
    finally:
        if audit_id:
            try:
                await finish_audit(audit_id, status, int((time.monotonic() - started) * 1000))
            except Exception:
                logging.getLogger(__name__).error("Computer completion audit unavailable")
