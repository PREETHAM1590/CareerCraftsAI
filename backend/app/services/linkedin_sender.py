"""Send approved LinkedIn outreach from the member's own account (beta).

Off unless LINKEDIN_OUTREACH_ENABLED and Unipile are configured. LinkedIn's
User Agreement prohibits automated activity and accounts can be restricted,
so this only ever sends messages the member approved, under small daily caps.

* Connecting: the member opens a Unipile hosted-login link (UniLogin: the
  LinkedIn already signed in to their browser, or Unipile's own form).
  CareerCraft never sees a LinkedIn password or session cookie; it stores
  only Unipile's account id. The callback is matched by a single-use random
  token in the notify URL, not by the user id Unipile echoes back.
* Sending one approved item: resolve the profile; a first-degree connection
  gets a message, anyone else a connection request carrying the note
  (LinkedIn's 300-character limit). Each item moves through
  sending -> invited/messaged/failed/limit_reached/outcome_unknown and is
  never retried automatically, so nothing is sent twice.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.core import redis_client
from app.core.config import settings

logger = logging.getLogger(__name__)

LINK_TTL = 30 * 60
NOTE_LIMIT = 300
PROFILE_URL = re.compile(
    r"^https://(?:www\.|[a-z]{2}\.)?linkedin\.com/in/([A-Za-z0-9\-_%]{3,100})/?(?:\?.*)?$"
)
DONE = {"invited", "messaged"}
# Sent, being sent, or maybe sent: never sent again automatically.
FINAL = DONE | {"sending", "outcome_unknown"}


class LinkedInUnavailable(RuntimeError):
    """Sending is off, or the member hasn't connected LinkedIn."""


def enabled() -> bool:
    return bool(
        settings.LINKEDIN_OUTREACH_ENABLED and settings.UNIPILE_DSN and settings.UNIPILE_API_KEY
    )


def _url(path: str) -> str:
    return settings.UNIPILE_DSN.rstrip("/") + "/api/v1/" + path.lstrip("/")


def _headers() -> dict:
    return {"X-API-KEY": settings.UNIPILE_API_KEY, "accept": "application/json"}


# ── Connecting ──────────────────────────────────────────────────


async def link_url(user_id) -> str:
    """A Unipile hosted-login link for this member, valid for 30 minutes."""
    if not enabled():
        raise LinkedInUnavailable("LinkedIn sending is not enabled on this site")
    if not settings.PUBLIC_API_URL:
        raise LinkedInUnavailable("PUBLIC_API_URL must be set for the LinkedIn callback")
    state = secrets.token_urlsafe(24)
    await redis_client.get_redis().set(f"li:link:{state}", str(user_id), ex=LINK_TTL)
    expires = (datetime.now(UTC) + timedelta(seconds=LINK_TTL)).isoformat().replace("+00:00", "Z")
    app = settings.FRONTEND_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        response = await client.post(
            _url("hosted/accounts/link"),
            headers=_headers(),
            json={
                "type": "create",
                "providers": ["LINKEDIN"],
                "api_url": settings.UNIPILE_DSN.rstrip("/"),
                "expiresOn": expires,
                "name": str(user_id),
                "notify_url": settings.PUBLIC_API_URL.rstrip("/")
                + f"/api/v1/inbound/unipile?state={state}",
                "success_redirect_url": f"{app}/linkedin/outreach?linkedin=connected",
                "failure_redirect_url": f"{app}/linkedin/outreach?linkedin=failed",
                # The LinkedIn already signed in to the member's browser,
                # without CareerCraft ever receiving its cookies.
                "unilogin": {"publisher_name": "CareerCraft"},
            },
        )
    response.raise_for_status()
    return response.json()["url"]


async def handle_notify(state: str, payload: dict) -> dict:
    """Unipile's callback after a successful hosted login."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import LinkedInAccount

    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", state or ""):
        return {"status": "ignored"}
    owner = await redis_client.get_redis().getdel(f"li:link:{state}")
    if not owner:
        return {"status": "expired"}
    owner = owner.decode() if isinstance(owner, bytes) else owner
    account_id = str(payload.get("account_id") or "")
    if (
        payload.get("status") not in {"CREATION_SUCCESS", "RECONNECTED"}
        or str(payload.get("name") or "") != owner
        or not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", account_id)
    ):
        return {"status": "rejected"}
    async with AsyncSessionLocal() as db:
        row = await db.get(LinkedInAccount, uuid.UUID(owner))
        if row is None:
            db.add(LinkedInAccount(user_id=uuid.UUID(owner), unipile_account_id=account_id))
        else:
            row.unipile_account_id = account_id
            row.connected_at = datetime.now(UTC)
        await db.commit()
    return {"status": "connected"}


async def account_for(user_id) -> str | None:
    from app.core.database import AsyncSessionLocal
    from app.models.db import LinkedInAccount

    async with AsyncSessionLocal() as db:
        row = await db.get(LinkedInAccount, uuid.UUID(str(user_id)))
    return row.unipile_account_id if row else None


async def disconnect(user_id) -> None:
    """Unlink at Unipile (revoking its session) and forget the account id."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import LinkedInAccount

    async with AsyncSessionLocal() as db:
        row = await db.get(LinkedInAccount, uuid.UUID(str(user_id)))
        if row is None:
            return
        account_id = row.unipile_account_id
        await db.delete(row)
        await db.commit()
    if enabled():
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                await client.delete(_url(f"accounts/{account_id}"), headers=_headers())
        except httpx.HTTPError:
            logger.warning("unipile_disconnect_failed")


# ── Sending ─────────────────────────────────────────────────────


async def claim(user_id, kind: str) -> bool:
    limit = (
        settings.LINKEDIN_INVITES_PER_DAY
        if kind == "invite"
        else settings.LINKEDIN_MESSAGES_PER_DAY
    )
    redis = redis_client.get_redis()
    key = f"li:{kind}:{uuid.UUID(str(user_id))}:{datetime.now(UTC):%Y%m%d}"
    used = await redis.incr(key)
    if used == 1:
        await redis.expire(key, 2 * 86400)
    return used <= limit


def public_identifier(url: str | None) -> str | None:
    match = PROFILE_URL.match((url or "").strip())
    return match.group(1) if match else None


def first_degree(profile: dict) -> bool:
    distance = str(profile.get("network_distance") or "").upper()
    return distance in {"FIRST_DEGREE", "DISTANCE_1"} or profile.get("is_relationship") is True


def _limit_error(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 422:
        return False
    try:
        kind = str((response.json() or {}).get("type") or "")
    except ValueError:
        kind = ""
    return "cannot_resend" in kind or "limit" in kind


async def deliver(
    user_id, account_id: str, identifier: str, message: str
) -> tuple[str, str | None]:
    """One send. Returns (delivery, error). Never retries."""
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        try:
            profile = await client.get(
                _url(f"users/{identifier}"), params={"account_id": account_id}, headers=_headers()
            )
        except httpx.HTTPError:
            return "failed", "LinkedIn profile lookup failed; nothing was sent."
        if profile.status_code in (401, 403):
            return "failed", "Reconnect LinkedIn: the connection expired."
        if profile.status_code != 200:
            return "failed", "That LinkedIn profile could not be found; nothing was sent."
        data = profile.json()
        provider_id = str(data.get("provider_id") or "")
        if not provider_id:
            return "failed", "That LinkedIn profile could not be found; nothing was sent."
        kind = "message" if first_degree(data) else "invite"
        if not await claim(user_id, kind):
            return "limit_reached", f"Today's LinkedIn {kind} limit is used up; send it tomorrow."
        try:
            if kind == "message":
                response = await client.post(
                    _url("chats"),
                    headers=_headers(),
                    # Unipile's start-chat endpoint takes multipart/form-data.
                    files={
                        "account_id": (None, account_id),
                        "attendees_ids": (None, provider_id),
                        "text": (None, message),
                    },
                )
            else:
                response = await client.post(
                    _url("users/invite"),
                    headers=_headers(),
                    json={
                        "account_id": account_id,
                        "provider_id": provider_id,
                        "message": message[:NOTE_LIMIT],
                    },
                )
        except httpx.TimeoutException:
            return "outcome_unknown", "LinkedIn didn't answer in time; check before resending."
        except httpx.HTTPError:
            return "failed", "Could not reach LinkedIn; nothing was sent."
    if response.status_code in (200, 201):
        return ("messaged" if kind == "message" else "invited"), None
    if _limit_error(response):
        return "limit_reached", "LinkedIn's own limit was reached; try again later."
    if response.status_code in (401, 403):
        return "failed", "Reconnect LinkedIn: the connection expired."
    if response.status_code >= 500:
        return "outcome_unknown", "LinkedIn returned an error; check before resending."
    return "failed", "LinkedIn refused this send."


async def send_item(user_id, item_id) -> str:
    """Send one approved queue item at most once. Returns its delivery state."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import LinkedInOutreachQueue

    account_id = await account_for(user_id)
    if not enabled() or not account_id:
        raise LinkedInUnavailable("Connect LinkedIn first")
    owner = uuid.UUID(str(user_id))
    async with AsyncSessionLocal() as db:
        item = (
            await db.execute(
                select(LinkedInOutreachQueue)
                .where(LinkedInOutreachQueue.id == item_id, LinkedInOutreachQueue.user_id == owner)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            return "missing"
        if item.delivery in FINAL:
            return item.delivery  # sending, sent or maybe sent: never twice
        identifier = public_identifier(item.contact_linkedin_url)
        if identifier is None:
            item.delivery, item.delivery_error = (
                "failed",
                "No LinkedIn profile link for this contact.",
            )
            await db.commit()
            return item.delivery
        item.delivery = "sending"
        item.approved_at = datetime.now(UTC)
        message = item.message
        await db.commit()
    delivery, error = await deliver(user_id, account_id, identifier, message)
    async with AsyncSessionLocal() as db:
        item = await db.get(LinkedInOutreachQueue, item_id)
        item.delivery, item.delivery_error = delivery, error
        if delivery in DONE:
            item.status = "sent"
            item.sent_at = datetime.now(UTC)
        await db.commit()
    return delivery
