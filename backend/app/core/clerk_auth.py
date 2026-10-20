"""Clerk Auth verification — JWKS-based JWT decode (RS256).

Clerk signs session tokens with RS256 using keys published at
``{CLERK_ISSUER}/.well-known/jwks.json``. The ``sub`` claim is a *text* id such
as ``user_2abc...`` — not a UUID — and is stored in ``users.clerk_user_id``,
which is a TEXT column. The RLS policies in
``supabase/migrations/0028_clerk_third_party_auth_rls.sql`` (and renamed by
``0041_rename_supabase_uid_to_clerk_user_id.sql``) match on that exact column
via ``auth.jwt() ->> 'sub'``.

The JWKS client is built lazily on first verification, never at import time —
an unset or unreachable JWKS URL must not break importing the app (or
collecting the test suite).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)
_CLOCK_SKEW_LEEWAY = 60
_ALGORITHMS = ["RS256"]


class ClerkIdentityError(Exception):
    """Clerk could not confirm the user's verified primary email."""


# Built lazily by _get_jwks_client(); tests patch this attribute directly.
_jwks_client: jwt.PyJWKClient | None = None


def _jwks_url() -> str:
    """Resolve the JWKS endpoint from explicit config or from the issuer."""
    if settings.CLERK_JWKS_URL:
        return settings.CLERK_JWKS_URL
    if settings.CLERK_ISSUER:
        return f"{settings.CLERK_ISSUER.rstrip('/')}/.well-known/jwks.json"
    return ""


def _get_jwks_client() -> jwt.PyJWKClient:
    """Return the process-wide JWKS client, constructing it on first use.

    Constructing at import time would make a bad/missing URL a hard import
    failure for every module that touches auth.
    """
    global _jwks_client
    if _jwks_client is None:
        url = _jwks_url()
        if not url:
            logger.error("Clerk auth is not configured — set CLERK_JWKS_URL or CLERK_ISSUER")
            raise HTTPException(status_code=401, detail="Authentication not configured")
        _jwks_client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
    return _jwks_client


def verify_token(token: str) -> dict[str, Any]:
    """Decode and validate a Clerk JWT using JWKS (RS256). Returns payload dict."""
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        options: dict[str, Any] = {"require": ["exp", "sub"]}
        kwargs: dict[str, Any] = {}
        if settings.CLERK_AUDIENCE:
            kwargs["audience"] = settings.CLERK_AUDIENCE
        else:
            # Clerk session tokens have no fixed `aud` — don't demand one.
            options["verify_aud"] = False
        if settings.CLERK_ISSUER:
            kwargs["issuer"] = settings.CLERK_ISSUER
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=_ALGORITHMS,
            leeway=_CLOCK_SKEW_LEEWAY,
            options=options,
            **kwargs,
        )
        return payload
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


verify_auth_jwt = verify_token


# ── Verified-token cache ─────────────────────────────────────────
# A browser reuses one Clerk session token (~60s lifetime) for every request
# it makes, and the app polls every 5-30s, so most requests carry a token
# this process already verified. Re-checking its RS256 signature each time is
# pure CPU. Only successful verifications are cached, keyed by a hash of the
# whole token, and an entry is never served past the token's own `exp` (no
# leeway), so the cache can only ever reject sooner than verify_token would.
_TOKEN_CACHE_MAX = 10_000
_token_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


async def verify_token_cached(token: str) -> dict[str, Any]:
    """verify_token, skipping the signature check for a token seen recently."""
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _token_cache.get(key)
    if hit is not None:
        if time.time() < hit[0]:
            _token_cache.move_to_end(key)
            return hit[1]
        del _token_cache[key]
    # verify_token can block on a synchronous JWKS HTTP fetch on cache miss
    # (jwt.PyJWKClient uses urllib internally) — run it off the event loop
    # so one slow/cold JWKS fetch can't stall every other in-flight request.
    payload = await asyncio.to_thread(verify_token, token)
    expires = payload.get("exp")
    if isinstance(expires, (int, float)):
        _token_cache[key] = (float(expires), payload)
        if len(_token_cache) > _TOKEN_CACHE_MAX:
            _token_cache.popitem(last=False)
    return payload


async def get_verified_primary_email(
    subject: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """Resolve the verified primary email for a Clerk user via the Backend API."""
    if not settings.CLERK_SECRET_KEY:
        raise ClerkIdentityError("Clerk Backend API is not configured")

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=5, trust_env=False)
    try:
        try:
            response = await client.get(
                f"https://api.clerk.com/v1/users/{quote(subject, safe='')}",
                headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
            )
        except httpx.RequestError as exc:
            raise ClerkIdentityError("Clerk user lookup failed") from exc
        if response.status_code != 200:
            raise ClerkIdentityError("Clerk user lookup failed")

        try:
            data = response.json()
        except ValueError as exc:
            raise ClerkIdentityError("Clerk user lookup returned invalid data") from exc
        primary_id = data.get("primary_email_address_id") if isinstance(data, dict) else None
        addresses = data.get("email_addresses") if isinstance(data, dict) else None
        if not isinstance(primary_id, str) or not isinstance(addresses, list):
            raise ClerkIdentityError("Clerk primary email is unavailable")

        primary = next(
            (item for item in addresses if isinstance(item, dict) and item.get("id") == primary_id),
            None,
        )
        email = primary.get("email_address") if primary else None
        verification = primary.get("verification") if primary else None
        if (
            not isinstance(email, str)
            or not email.strip()
            or not isinstance(verification, dict)
            or verification.get("status") != "verified"
        ):
            raise ClerkIdentityError("Clerk primary email is not verified")
        return email.strip().casefold()
    finally:
        if owns_client:
            await client.aclose()


PLACEHOLDER_EMAIL_DOMAIN = "@users.noreply.clerk"


async def fetch_clerk_profile(subject: str) -> dict[str, Any] | None:
    """Verified email, name and avatar from the Clerk Backend API, or None.

    Clerk session tokens carry no email/name unless the instance adds custom
    claims, so without this every user was stored with a placeholder email —
    which then landed in job applications and follow-up emails.
    """
    if not settings.CLERK_SECRET_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            email = await get_verified_primary_email(subject, client=client)
            response = await client.get(
                f"https://api.clerk.com/v1/users/{quote(subject, safe='')}",
                headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
            )
        data = response.json() if response.status_code == 200 else {}
    except (ClerkIdentityError, httpx.HTTPError, ValueError) as exc:
        logger.info("Clerk profile lookup unavailable for %s: %s", subject, exc)
        return None
    name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p) or None
    return {"email": email, "full_name": name, "avatar_url": data.get("image_url") or None}


# After a failed repair, wait this long before asking Clerk again, so a
# member whose email cannot be repaired does not cost two Clerk calls on
# every request.
_REPAIR_RETRY_SECONDS = 3600


def _repair_backoff_key(user: User) -> str:
    return f"clerk_profile_repair:{user.id}"


async def _repair_recently_failed(user: User) -> bool:
    from app.core.redis_client import get_redis

    try:
        return bool(await get_redis().exists(_repair_backoff_key(user)))
    except Exception:
        return False  # Redis down: try the repair as before


async def _remember_failed_repair(user: User) -> None:
    from app.core.redis_client import get_redis

    try:
        await get_redis().set(_repair_backoff_key(user), "1", ex=_REPAIR_RETRY_SECONDS)
    except Exception:
        logger.debug("Could not record failed profile repair for %s", user.id)


async def _repair_placeholder_profile(db: AsyncSession, user: User) -> None:
    """Replace a placeholder email (and empty name) from Clerk, once."""
    if not (user.email or "").endswith(PLACEHOLDER_EMAIL_DOMAIN) or not user.clerk_user_id:
        return
    if await _repair_recently_failed(user):
        return
    profile = await fetch_clerk_profile(user.clerk_user_id)
    if not profile:
        await _remember_failed_repair(user)
        return
    user.email = profile["email"]
    user.full_name = user.full_name or profile["full_name"]
    user.avatar_url = user.avatar_url or profile["avatar_url"]
    try:
        await db.commit()
    except IntegrityError:
        # Another row already owns that email; keep the placeholder.
        await db.rollback()
        logger.warning("Could not repair placeholder email for %s: email in use", user.id)
        await _remember_failed_repair(user)
    await db.refresh(user)


async def delete_clerk_user(
    subject: str, *, client: httpx.AsyncClient | None = None, strict: bool = False
) -> None:
    """Delete the Clerk user via the Backend API. A user Clerk no longer has
    counts as deleted. Other failures are logged, or raised when strict, so
    the account sweep can keep the row and retry.

    Called from account deletion so a removed local row can't be re-provisioned
    by the same Clerk identity signing back in.
    """
    if not settings.CLERK_SECRET_KEY:
        if strict:
            raise RuntimeError("CLERK_SECRET_KEY is required for account erasure")
        logger.warning(
            "Skipping Clerk user deletion for %s — CLERK_SECRET_KEY not configured", subject
        )
        return

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=5, trust_env=False)
    try:
        response = await client.delete(
            f"https://api.clerk.com/v1/users/{quote(subject, safe='')}",
            headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
        )
        if response.status_code not in (200, 404):
            logger.warning("Clerk user deletion for %s returned %s", subject, response.status_code)
            if strict:
                raise RuntimeError(f"Clerk user deletion returned {response.status_code}")
    except httpx.RequestError as exc:
        logger.warning("Clerk user deletion request failed for %s: %s", subject, exc)
        if strict:
            raise
    finally:
        if owns_client:
            await client.aclose()


def subject_from_payload(payload: dict[str, Any]) -> str:
    """Pull the auth subject out of a verified payload, or raise 401."""
    subject = payload.get("sub") or payload.get("user_id") or payload.get("supabase_uid")
    if not subject:
        raise HTTPException(status_code=401, detail="Invalid token — missing sub claim")
    return str(subject)


def _profile_from_payload(payload: dict[str, Any], subject: str) -> dict[str, Any]:
    """Map Clerk claims onto User columns.

    ``users.email`` is NOT NULL, but Clerk only emits an ``email`` claim when the
    instance is configured to add it as a custom session claim. Fall back to a
    deterministic, non-deliverable placeholder so first-sight provisioning never
    fails; the profile endpoints can correct it later.
    """
    meta = payload.get("user_metadata") or {}
    email = (
        payload.get("email")
        or payload.get("primary_email_address")
        or meta.get("email")
        or f"{subject}{PLACEHOLDER_EMAIL_DOMAIN}"
    )
    return {
        "clerk_user_id": subject,
        "email": email,
        "full_name": payload.get("full_name") or payload.get("name") or meta.get("full_name"),
        "avatar_url": payload.get("image_url") or payload.get("picture") or meta.get("avatar_url"),
    }


async def _select_by_uid(db: AsyncSession, subject: str) -> User | None:
    result = await db.execute(select(User).where(User.clerk_user_id == subject))
    return result.scalars().first()


async def get_or_provision_user(
    db: AsyncSession,
    subject: str,
    payload: dict[str, Any] | None = None,
) -> User:
    """Load the User for a Clerk subject, creating the row on first sight.

    Nothing else creates ``public.users`` rows any more — the Supabase
    ``on_auth_user_created`` trigger lived on ``auth.users``, which does not
    exist here. So the first authenticated request for a subject provisions it.

    Race handling: two concurrent first requests for the same subject both miss
    the SELECT. The INSERT uses ``ON CONFLICT (clerk_user_id) DO NOTHING`` so the
    loser is a no-op instead of a unique violation, and any other integrity
    error (e.g. the unique ``email`` index) is caught, rolled back, and followed
    by a re-SELECT. Either way both requests end up with the same row.
    """
    payload = payload or {}
    user = await _select_by_uid(db, subject)
    if user is not None:
        await _repair_placeholder_profile(db, user)
        return user

    values = _profile_from_payload(payload, subject)
    if values["email"].endswith(PLACEHOLDER_EMAIL_DOMAIN):
        profile = await fetch_clerk_profile(subject)
        if profile:
            values["email"] = profile["email"]
            values["full_name"] = values["full_name"] or profile["full_name"]
            values["avatar_url"] = values["avatar_url"] or profile["avatar_url"]
    try:
        await db.execute(
            pg_insert(User)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["clerk_user_id"])
        )
        await db.commit()
    except IntegrityError as exc:
        # Concurrent insert won the race, or the email collides with an existing
        # row. Roll back and fall through to the re-SELECT below.
        await db.rollback()
        logger.info("User provisioning conflict for %s: %s", subject, exc.orig)

    user = await _select_by_uid(db, subject)
    if user is not None:
        return user

    # Not a clerk_user_id race — an existing row already owns this email. Adopt
    # it only if it has never been bound to an auth subject (pre-Clerk rows);
    # otherwise refuse rather than hand over somebody else's account.
    result = await db.execute(select(User).where(User.email == values["email"]))
    existing = result.scalars().first()
    if existing is not None and not existing.clerk_user_id:
        existing.clerk_user_id = subject
        await db.commit()
        await db.refresh(existing)
        logger.info("Bound pre-existing user %s to auth subject %s", existing.id, subject)
        return existing

    logger.error("User provisioning failed for subject %s", subject)
    raise HTTPException(status_code=500, detail="User provisioning failed")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract Bearer token, verify locally, load (or provision) User by subject."""
    if not token:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    payload = verify_token(token)
    subject = subject_from_payload(payload)
    return await get_or_provision_user(db, subject, payload)
