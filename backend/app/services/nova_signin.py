"""Nova's private browser steps: sign in with a saved login, enter a one-time
code, and create a portal account where the member allowed it.

The model may *propose* these steps by element ref; it never sees, chooses or
receives a secret. The server resolves the saved login by the page's exact
origin, and the computer refuses to type it anywhere else. A one-time code
lives only in Redis, encrypted, for CODE_TTL seconds and is read exactly once:
it never enters the database, the workflow history or a model prompt.

Account creation runs only on sites the member listed (PortalAccountConsent),
at most SIGNUP_DAILY_LIMIT tries per site a day. The server generates the
password and saves the login in the vault *before* typing it, signs up with
the member's application email (phase 4) so verification mail is handled,
and marks the login created_by_nova. Automatic sign-in turns on only once the
site accepted the form; a refusal before typing removes the saved login.
"""

import hashlib
import secrets
import string
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.core import redis_client
from app.core.config import settings
from app.core.security import decrypt_api_key, encrypt_api_key
from app.services import computer_service

CONSENT_VERSION = "2026-11-24"
AUTO_SIGNIN_DAILY_LIMIT = 3
CODE_TTL = 600
SIGNUP_DAILY_LIMIT = 3
NOVA_STEPS = {"sign_in", "enter_code", "create_account"}
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


@dataclass(frozen=True)
class AutoSigninClaim:
    credential_id: uuid.UUID
    consent_at: datetime | None
    consent_version: str | None
    # Saving a replacement login can update the encrypted values on the same row.
    encrypted_login: tuple[str, str] = field(repr=False)


class AutoSigninChanged(ValueError):
    """The saved credential or consent changed after the automatic claim."""


def _auto_signin_claim(row) -> AutoSigninClaim | None:
    if row is None or not row.auto_signin:
        return None
    return AutoSigninClaim(
        row.id, row.consent_at, row.consent_version, (row.username_enc, row.password_enc)
    )


async def claim_auto_signin(user_id, origin: str) -> AutoSigninClaim | None:
    """Return a claim when the member consented and today's automatic sign-ins remain.

    Past the limit Nova asks for approval again: a login that keeps failing
    must not be retried until the portal locks the account."""
    claim = _auto_signin_claim(await saved_login(user_id, origin))
    if claim is None:
        return None
    redis = redis_client.get_redis()
    key = _signin_counter(user_id, origin)
    used = await redis.incr(key)
    if used == 1:
        await redis.expire(key, 2 * 86400)
    return claim if used <= AUTO_SIGNIN_DAILY_LIMIT else None


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


async def sign_in(
    user_id, action, page_url: str | None, *, automatic_claim: AutoSigninClaim | None = None
) -> dict:
    """Type the exact-origin login, verifying any automatic claim before dispatch."""
    origin = page_origin(page_url)
    row = await saved_login(user_id, origin)
    if automatic_claim is not None and _auto_signin_claim(row) != automatic_claim:
        raise AutoSigninChanged(f"The automatic sign-in claim for {origin} is no longer valid.")
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


# ── Accounts Nova creates ───────────────────────────────────────


def new_password(length: int = 20) -> str:
    """Random, with each character class most sign-up forms demand."""
    symbols = "!@#$%^&*-_+="
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in value)
            and any(c.isupper() for c in value)
            and any(c.isdigit() for c in value)
            and any(c in symbols for c in value)
        ):
            return value


async def account_consent(user_id, origin: str):
    from app.core.database import AsyncSessionLocal
    from app.models.db import PortalAccountConsent

    async with AsyncSessionLocal() as db:
        return await db.get(PortalAccountConsent, (uuid.UUID(str(user_id)), origin))


async def account_sites(user_id) -> list[str]:
    """Origins where Nova may create an account, for the model's context."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import PortalAccountConsent

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(PortalAccountConsent.origin).where(
                PortalAccountConsent.user_id == uuid.UUID(str(user_id))
            )
        )
    return list(rows.scalars().all())


async def claim_signup(user_id, origin: str) -> bool:
    redis = redis_client.get_redis()
    day = datetime.now(UTC).strftime("%Y%m%d")
    site = hashlib.sha256(origin.encode()).hexdigest()[:16]
    key = f"nova:signup:{uuid.UUID(str(user_id))}:{site}:{day}"
    used = await redis.incr(key)
    if used == 1:
        await redis.expire(key, 2 * 86400)
    return used <= SIGNUP_DAILY_LIMIT


async def signup_email(user_id) -> str:
    """The member's application address. Nova never signs up with the
    personal account email: verification mail must reach the apply inbox."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import User
    from app.services import apply_inbox

    alias = await apply_inbox.alias_for(user_id)
    if not alias and apply_inbox.configured():
        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(str(user_id)))
            if user is not None:
                try:
                    alias = await apply_inbox.get_or_create_alias(db, user)
                except RuntimeError as exc:
                    raise ValueError("Could not set up your application email; try again.") from exc
    if not alias:
        raise ValueError(
            "Nova signs up only with your application email, and it isn't set up yet. "
            "Take control and sign up yourself."
        )
    return alias


# Refusals from the computer before anything was typed: safe to forget the login.
NOTHING_TYPED = {403, 409, 422}


async def create_account(user_id, action, page_url: str | None) -> dict:
    """Create a portal account on an allowed site: vault first, then type.

    The login is saved with automatic sign-in off and turned on only once the
    site took the form, so a failed sign-up never leaves Nova signing in to an
    account that doesn't exist."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import PortalCredential

    origin = page_origin(page_url)
    consent = await account_consent(user_id, origin)
    if consent is None:
        raise ValueError(f"Allow Nova to create accounts on {origin} first.")
    if await saved_login(user_id, origin) is not None:
        raise ValueError(f"You already have a saved login for {origin}; Nova signs in instead.")
    # Before spending a try or saving anything: Nova types secrets only in the cloud.
    if (await computer_service.computer_backend(user_id))["backend"] != "cloud":
        raise ValueError(CLOUD_ONLY)
    email = await signup_email(user_id)
    # Counted only once nothing local can fail before the site sees a try.
    if not await claim_signup(user_id, origin):
        raise ValueError(f"Nova already tried to create an account on {origin} today.")
    password = new_password()
    sealed_password = encrypt_api_key(password, settings.APP_SECRET_KEY)
    host = urlsplit(origin).hostname or origin
    owner = uuid.UUID(str(user_id))
    async with AsyncSessionLocal() as db:
        db.add(
            PortalCredential(
                user_id=owner,
                origin=origin,
                label=f"{host} (created by Nova)"[:100],
                username_enc=encrypt_api_key(email, settings.APP_SECRET_KEY),
                password_enc=sealed_password,
                auto_signin=False,
                consent_at=consent.consent_at,
                consent_version=consent.consent_version,
                created_by_nova=True,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # Another run saved a login for this site first.
            await db.rollback()
            raise ValueError(
                f"You already have a saved login for {origin}; Nova signs in instead."
            ) from None
    params = action.parameters
    body = {
        "operation": "credentials/fill",
        "computer_run": action.computer_run,
        "actor": "nova",
        "parameters": {
            "origin": origin,
            "usernameRef": params["emailRef"],
            "passwordRef": params["passwordRef"],
            **({"confirmRef": params["confirmRef"]} if params.get("confirmRef") else {}),
            "snapshotId": params["snapshotId"],
            "username": email,
            "password": password,
            "submit": True,
        },
    }
    try:
        result = await _dispatch(user_id, body, "nova_create_account")
    except HTTPException as exc:
        if exc.status_code in NOTHING_TYPED:
            await _forget_login(owner, origin, sealed_password)
        # Otherwise the outcome is unknown: keep the password, sign-in stays off.
        raise
    submitted = bool(result.get("submitted"))
    if submitted:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(PortalCredential)
                .where(
                    PortalCredential.user_id == owner,
                    PortalCredential.origin == origin,
                    PortalCredential.password_enc == sealed_password,
                    # Switched off meanwhile (consent cleared): stays off.
                    PortalCredential.consent_at.is_not(None),
                )
                .values(auto_signin=True)
            )
            await db.commit()
    return {"submitted": submitted, "origin": origin, "email": email}


async def _forget_login(owner: uuid.UUID, origin: str, sealed_password: str) -> None:
    """Remove the login this sign-up saved, never one the member replaced."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import PortalCredential

    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(PortalCredential).where(
                PortalCredential.user_id == owner,
                PortalCredential.origin == origin,
                PortalCredential.created_by_nova.is_(True),
                PortalCredential.password_enc == sealed_password,
            )
        )
        await db.commit()
