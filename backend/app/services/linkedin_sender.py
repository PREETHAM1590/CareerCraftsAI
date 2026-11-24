"""Send approved LinkedIn outreach from the member's own browser (beta).

Off unless LINKEDIN_OUTREACH_ENABLED. LinkedIn's User Agreement prohibits
automated activity and accounts can be restricted, so this only ever sends
messages the member approved, under small daily caps.

No third party holds LinkedIn access: the member's paired CareerCraft
extension does the sending in their own Chrome, with the LinkedIn session
already signed in there, through the device command channel
(computer_service.send_command). For each approved item:

1. "probe" (read only): open the profile and report sign-in state, degree,
   and whether Message / Connect are offered.
2. Claim today's cap for the kind of send, then "send": a first-degree
   connection gets a message, anyone else a connection request with the note
   (LinkedIn's 300-character limit). The extension reports success only once
   LinkedIn shows it (message in the thread, request pending).

Each item moves through sending -> invited/messaged/failed/limit_reached/
outcome_unknown. Sent or maybe-sent items are never sent again; failed and
limit_reached ones can be resent. A batch runs in the background so the
approval returns at once; the outreach page shows progress.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.core import redis_client
from app.core.config import settings

logger = logging.getLogger(__name__)

NOTE_LIMIT = 300
# A send can take ~70 s in the browser (page load, then menu, dialog, note,
# Send and the confirmation), longer than the default 60 s command wait.
# Must stay under redis_client.BLOCKING_SOCKET_TIMEOUT (90 s).
COMMAND_TIMEOUT = 85
PROFILE_URL = re.compile(
    r"^https://(?:www\.|[a-z]{2}\.)?linkedin\.com/in/([A-Za-z0-9\-_%]{3,100})/?(?:\?.*)?$"
)
DONE = {"invited", "messaged"}
# Sent, being sent, or maybe sent: never sent again automatically.
FINAL = DONE | {"sending", "outcome_unknown"}
# A send or batch older than this was interrupted (e.g. a restart): the batch
# lock has expired, so it can no longer be running.
STALE_AFTER = timedelta(minutes=30)


def sending_stale(output: dict | None) -> bool:
    """True when output.sending is set but its batch can no longer be running."""
    output = output or {}
    if not output.get("sending"):
        return False
    try:
        started = datetime.fromisoformat(str(output.get("sending_started_at")))
    except ValueError:
        return True  # no start time: set before this field existed
    return datetime.now(UTC) - started > STALE_AFTER


_tasks: set[asyncio.Task] = set()


class LinkedInUnavailable(RuntimeError):
    """Sending is off, or the member's extension isn't online."""


def enabled() -> bool:
    return bool(settings.LINKEDIN_OUTREACH_ENABLED)


def profile_url(url: str | None) -> str | None:
    """Canonical https://www.linkedin.com/in/<id>/ or None."""
    match = PROFILE_URL.match((url or "").strip())
    return f"https://www.linkedin.com/in/{match.group(1)}/" if match else None


async def device_for(user_id) -> str | None:
    """The member's online extension device, or None."""
    from app.services import computer_service

    try:
        return (await computer_service.computer_backend(user_id))["device"]
    except Exception:
        # Treated as offline: callers fall back to the manual handoff.
        logger.warning("linkedin_device_lookup_failed", exc_info=True)
        return None


def _cap_key(user_id, kind: str) -> str:
    return f"li:{kind}:{uuid.UUID(str(user_id))}:{datetime.now(UTC):%Y%m%d}"


async def claim(user_id, kind: str) -> bool:
    limit = (
        settings.LINKEDIN_INVITES_PER_DAY
        if kind == "invite"
        else settings.LINKEDIN_MESSAGES_PER_DAY
    )
    redis = redis_client.get_redis()
    key = _cap_key(user_id, kind)
    used = await redis.incr(key)
    if used == 1:
        await redis.expire(key, 2 * 86400)
    if used > limit:
        await release(user_id, kind)  # over the cap: this try isn't counted
        return False
    return True


async def release(user_id, kind: str) -> None:
    """Give back a claimed send that LinkedIn confirmed never went out."""
    try:
        await redis_client.get_redis().decr(_cap_key(user_id, kind))
    except Exception:
        logger.warning("linkedin_cap_release_failed", exc_info=True)


async def _command(device: str, body: dict) -> dict:
    from app.services import computer_service

    return await computer_service.send_command(device, "linkedin", body, timeout=COMMAND_TIMEOUT)


async def deliver(user_id, device: str, url: str, message: str) -> tuple[str, str | None]:
    """One send through the member's extension. Returns (delivery, error)."""
    try:
        probe = await _command(device, {"op": "probe", "profile_url": url})
    except HTTPException as exc:
        # Read-only step: whatever happened, nothing was sent.
        return "failed", f"Your browser couldn't open the profile ({exc.detail}); nothing was sent."
    if probe.get("signed_out"):
        return "failed", "Sign in to LinkedIn in Chrome, then send again."
    if probe.get("not_found"):
        return "failed", "That LinkedIn profile could not be opened; nothing was sent."
    if probe.get("pending"):
        return "failed", "A connection request to this person is already pending."
    if probe.get("degree") == "1st" and probe.get("can_message"):
        kind = "message"
    elif probe.get("can_connect"):
        kind = "invite"
    else:
        return "failed", "LinkedIn offers neither Message nor Connect for this profile."
    if kind == "invite" and len(message) > NOTE_LIMIT:
        # Never send a cut-off note the member didn't approve.
        return "failed", (
            f"This person isn't a connection yet, and LinkedIn limits a connection note to "
            f"{NOTE_LIMIT} characters. Shorten the draft to {NOTE_LIMIT}, then resend."
        )
    try:
        claimed = await claim(user_id, kind)
    except Exception:
        logger.warning("linkedin_cap_claim_failed", exc_info=True)
        return "failed", "Today's LinkedIn limit couldn't be checked; nothing was sent."
    if not claimed:
        return "limit_reached", f"Today's LinkedIn {kind} limit is used up; send it tomorrow."
    try:
        result = await _command(
            device, {"op": "send", "as": kind, "profile_url": url, "message": message}
        )
    except HTTPException as exc:
        if exc.status_code in (403, 409, 422):
            await release(user_id, kind)
            return "failed", f"Your browser refused the send ({exc.detail}); nothing was sent."
        # Timed out or broke mid-way: it may have gone out.
        return "outcome_unknown", "Your browser stopped mid-send; check LinkedIn before resending."
    except Exception:
        logger.warning("linkedin_send_command_failed", exc_info=True)
        return "outcome_unknown", "Your browser stopped mid-send; check LinkedIn before resending."
    delivery = result.get("delivery")
    if delivery in DONE:
        return delivery, None
    reason = result.get("reason")
    if reason == "unconfirmed":
        return "outcome_unknown", "LinkedIn didn't confirm the send; check before resending."
    # Confirmed not sent: today's slot is given back.
    await release(user_id, kind)
    if reason == "limit":
        return "limit_reached", "LinkedIn's own limit was reached; try again later."
    return "failed", str(result.get("error") or "LinkedIn refused this send.")[:300]


async def send_item(user_id, device: str, item_id) -> str:
    """Send one approved queue item at most once. Returns its delivery state."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import LinkedInOutreachQueue

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
        if (
            item.delivery == "sending"
            and item.delivery_started_at
            and datetime.now(UTC) - item.delivery_started_at > STALE_AFTER
        ):
            # Interrupted mid-send (restart): it may have gone out, so never
            # resend; record it as unknown instead of "sending" forever.
            item.delivery = "outcome_unknown"
            item.delivery_error = "Sending was interrupted; check LinkedIn before resending."
            await db.commit()
            return item.delivery
        if item.delivery in FINAL:
            return item.delivery  # sending, sent or maybe sent: never twice
        url = profile_url(item.contact_linkedin_url)
        if url is None:
            item.delivery, item.delivery_error = (
                "failed",
                "No LinkedIn profile link for this contact.",
            )
            await db.commit()
            return item.delivery
        item.delivery, item.delivery_error = "sending", None
        item.delivery_started_at = datetime.now(UTC)
        message = item.message
        await db.commit()
    try:
        delivery, error = await deliver(user_id, device, url, message)
    except Exception:
        # deliver() maps every pre-send failure to "failed" itself; anything
        # left may have happened mid-send.
        logger.warning("linkedin_deliver_failed", exc_info=True)
        delivery, error = (
            "outcome_unknown",
            "Sending failed unexpectedly; check LinkedIn before resending.",
        )
    async with AsyncSessionLocal() as db:
        item = await db.get(LinkedInOutreachQueue, item_id)
        item.delivery, item.delivery_error = delivery, error
        if delivery in DONE:
            item.status = "sent"
            item.sent_at = datetime.now(UTC)
        await db.commit()
    return delivery


async def send_run(user_id, run_id) -> None:
    """Send every approved item of one outreach run, one at a time."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun

    redis = redis_client.get_redis()
    lock = _lock_key(run_id)
    token = uuid.uuid4().hex
    ttl = int(STALE_AFTER.total_seconds())
    if not await redis.set(lock, token, nx=True, ex=ttl):
        return  # already sending this run
    started = None
    try:
        async with AsyncSessionLocal() as db:
            run = await db.get(AgentRun, run_id)
            output = (run.output or {}) if run else {}
            queue_ids = list(output.get("queue_ids") or [])
            started = output.get("sending_started_at")
        for queue_id in queue_ids:
            try:
                device = await device_for(user_id)
                if device is None:
                    delivery = "failed"  # browser went offline: nothing sent, resend later
                else:
                    delivery = await send_item(user_id, device, uuid.UUID(queue_id))
            except Exception:
                # send_item records its own outcome once it starts a send, so
                # this item was never marked sending: it can be resent.
                logger.warning("linkedin_send_item_failed", exc_info=True)
                delivery = "failed"
            async with AsyncSessionLocal() as db:
                run = await db.get(AgentRun, run_id, with_for_update=True)
                output = dict(run.output or {})
                output["deliveries"] = {**(output.get("deliveries") or {}), queue_id: delivery}
                run.output = output
                await db.commit()
            # A long batch keeps its lock: no second batch while this one runs.
            if await _lock_value(redis, lock) == token:
                await redis.expire(lock, ttl)
    except Exception:
        logger.warning("linkedin_send_run_failed", exc_info=True)
    finally:
        try:
            async with AsyncSessionLocal() as db:
                run = await db.get(AgentRun, run_id, with_for_update=True)
                # Clear the flag only for this batch, never a newer one's.
                if run is not None and (run.output or {}).get("sending_started_at") == started:
                    run.output = {**(run.output or {}), "sending": False}
                    await db.commit()
        finally:
            if await _lock_value(redis, lock) == token:
                await redis.delete(lock)


def _lock_key(run_id) -> str:
    return f"li:sending:{uuid.UUID(str(run_id))}"


async def _lock_value(redis, lock: str) -> str | None:
    value = await redis.get(lock)
    return value.decode() if isinstance(value, bytes) else value


async def batch_running(run_id) -> bool:
    """True while a send batch for this run holds its lock."""
    return bool(await redis_client.get_redis().exists(_lock_key(run_id)))


def start_run(user_id, run_id) -> None:
    """Send a run in the background; the approval request returns at once."""
    task = asyncio.get_running_loop().create_task(send_run(user_id, run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
