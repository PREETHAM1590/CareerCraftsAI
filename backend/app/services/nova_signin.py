"""Nova's private browser steps: sign in with a saved login, enter a one-time code.

The model may *propose* these steps by element ref; it never sees, chooses or
receives a secret. The server resolves the saved login by the page's exact
origin, and the computer refuses to type it anywhere else. A one-time code
lives only in Redis, encrypted, for CODE_TTL seconds and is read exactly once:
it never enters the database, the workflow history or a model prompt.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.core import redis_client
from app.core.config import settings
from app.core.security import decrypt_api_key, encrypt_api_key
from app.services import computer_service

CONSENT_VERSION = "2026-11-24"
AUTO_SIGNIN_DAILY_LIMIT = 3
CODE_TTL = 600
NOVA_STEPS = {"sign_in", "enter_code"}
CLOUD_ONLY = "Nova signs in and enters codes only in the cloud computer. Switch to Cloud."


def page_origin(url: str | None) -> str:
    """The exact origin of a page URL, in the vault's canonical form."""
    parts = urlsplit(str(url or ""))
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("Nova signs in only on HTTPS pages")
    return computer_service.credential_origin(f"https://{parts.netloc.rsplit('@', 1)[-1]}")


def _code_key(run_id) -> str:
    return f"nova:code:{uuid.UUID(str(run_id))}"


async def store_code(run_id, code: str) -> None:
    sealed = encrypt_api_key(code, settings.APP_SECRET_KEY)
    await redis_client.get_redis().set(_code_key(run_id), sealed, ex=CODE_TTL)


async def has_code(run_id) -> bool:
    return bool(await redis_client.get_redis().exists(_code_key(run_id)))


async def take_code(run_id) -> str | None:
    """Read and delete in one step, so a code can be typed at most once."""
    sealed = await redis_client.get_redis().getdel(_code_key(run_id))
    if not sealed:
        return None
    if isinstance(sealed, bytes):
        sealed = sealed.decode()
    return decrypt_api_key(sealed, settings.APP_SECRET_KEY)


async def saved_login(user_id, origin: str):
    from app.core.database import AsyncSessionLocal
    from app.models.db import PortalCredential

    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(PortalCredential).where(
                    PortalCredential.user_id == uuid.UUID(str(user_id)),
                    PortalCredential.origin == origin,
                )
            )
        ).scalar_one_or_none()


async def saved_logins(user_id) -> list[dict]:
    """Origins only, for the model's context. No usernames, no labels."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import PortalCredential

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(PortalCredential.origin, PortalCredential.auto_signin).where(
                    PortalCredential.user_id == uuid.UUID(str(user_id))
                )
            )
        ).all()
    return [{"origin": row.origin, "automatic": bool(row.auto_signin)} for row in rows]


def _signin_counter(user_id, origin: str) -> str:
    day = datetime.now(UTC).strftime("%Y%m%d")
    site = hashlib.sha256(origin.encode()).hexdigest()[:16]
    return f"nova:signin:{uuid.UUID(str(user_id))}:{site}:{day}"


async def claim_auto_signin(user_id, origin: str) -> bool:
    """True when the member consented and today's automatic sign-ins remain.

    Past the limit Nova asks for approval again: a login that keeps failing
    must not be retried until the portal locks the account."""
    row = await saved_login(user_id, origin)
    if row is None or not row.auto_signin:
        return False
    redis = redis_client.get_redis()
    key = _signin_counter(user_id, origin)
    used = await redis.incr(key)
    if used == 1:
        await redis.expire(key, 2 * 86400)
    return used <= AUTO_SIGNIN_DAILY_LIMIT


async def _dispatch(user_id, body: dict, audit_name: str) -> dict:
    state = await computer_service.computer_backend(user_id)
    if state["backend"] != "cloud":
        raise HTTPException(409, CLOUD_ONLY)
    audit_id = await computer_service.audit(user_id, audit_name, "running", backend="cloud")
    status = "failed"
    try:
        result = await computer_service.dispatch(user_id, "action", body, state)
        status = "completed"
        return result
    finally:
        await computer_service.finish_audit(audit_id, status, 0)


async def sign_in(user_id, action, page_url: str | None) -> dict:
    """Type the saved login for this exact origin and submit the form."""
    origin = page_origin(page_url)
    row = await saved_login(user_id, origin)
    if row is None:
        raise ValueError(f"No saved login for {origin}. Save one in the computer panel first.")
    params = action.parameters
    body = {
        "operation": "credentials/fill",
        "computer_run": action.computer_run,
        "actor": "nova",
        "parameters": {
            "origin": origin,
            "usernameRef": params["usernameRef"],
            "passwordRef": params["passwordRef"],
            "snapshotId": params["snapshotId"],
            "username": decrypt_api_key(row.username_enc, settings.APP_SECRET_KEY),
            "password": decrypt_api_key(row.password_enc, settings.APP_SECRET_KEY),
            "submit": True,
        },
    }
    result = await _dispatch(user_id, body, "nova_sign_in")
    return {"signed_in": bool(result.get("submitted")), "origin": origin}


async def enter_code(user_id, run_id, action, page_url: str | None) -> dict:
    """Type the member's one-time code into the reviewed field and submit."""
    origin = page_origin(page_url)
    code = await take_code(run_id)
    if code is None:
        raise ValueError("No code was entered, or it expired. Ask Nova to try again.")
    params = action.parameters
    body = {
        "operation": "codes/fill",
        "computer_run": action.computer_run,
        "actor": "nova",
        "parameters": {
            "origin": origin,
            "ref": params["ref"],
            "snapshotId": params["snapshotId"],
            "code": code,
        },
    }
    result = await _dispatch(user_id, body, "nova_enter_code")
    return {"entered": bool(result.get("submitted")), "origin": origin}
