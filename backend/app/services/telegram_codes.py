"""One-time codes over Telegram: Nova asks, the member replies with the code.

* Linking: the member opens a one-time deep link (t.me/<bot>?start=<token>,
  15 minutes, single use) from CareerCraft; the bot stores their private
  chat id. /stop unlinks.
* Asking: when a Nova run waits on an enter_code step, the bot messages the
  linked chat and remembers which run that chat may answer, for CODE_TTL.
* Answering: a reply shaped like a code is sealed for that run exactly as
  the in-app code box does (nova_signin.store_code: Redis, encrypted, read
  once), the step is approved for the member, and the bot deletes the
  member's message from the chat. Codes are never logged, stored in the
  database, or given to a model.

The webhook is authenticated by Telegram's secret-token header; only
private chats are handled.
"""

import hmac
import logging
import re
import secrets
import uuid

import httpx
from sqlalchemy import delete, select

from app.core import redis_client
from app.core.config import settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
LINK_TTL = 900
CODE = re.compile(r"[A-Za-z0-9-]{4,12}")
HELP = (
    "I only take one-time codes that Nova asks you for. "
    "Open CareerCraft for everything else. Send /stop to disconnect."
)


def configured() -> bool:
    return bool(
        settings.TELEGRAM_BOT_TOKEN
        and settings.TELEGRAM_BOT_USERNAME
        and settings.TELEGRAM_WEBHOOK_SECRET
    )


def secret_ok(header: str | None) -> bool:
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    return bool(expected and header and hmac.compare_digest(header, expected))


async def call(method: str, payload: dict) -> bool:
    """Bot API call. The token is in the URL, so the URL is never logged."""
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                f"{API}/bot{settings.TELEGRAM_BOT_TOKEN}/{method}", json=payload
            )
        return response.status_code == 200
    except httpx.HTTPError:
        logger.warning("telegram_call_failed", extra={"method": method})
        return False


# ── Linking ─────────────────────────────────────────────────────


async def link_url(user_id) -> str:
    token = secrets.token_urlsafe(24)
    await redis_client.get_redis().set(f"tg:link:{token}", str(user_id), ex=LINK_TTL)
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={token}"


async def chat_for(user_id) -> int | None:
    from app.core.database import AsyncSessionLocal
    from app.models.db import TelegramLink

    async with AsyncSessionLocal() as db:
        row = await db.get(TelegramLink, uuid.UUID(str(user_id)))
    return row.chat_id if row else None


async def link(token: str, chat_id: int) -> bool:
    from app.core.database import AsyncSessionLocal
    from app.models.db import TelegramLink

    owner = await redis_client.get_redis().getdel(f"tg:link:{token}")
    if not owner:
        return False
    owner = uuid.UUID(owner.decode() if isinstance(owner, bytes) else owner)
    async with AsyncSessionLocal() as db:
        # One chat per member and one member per chat.
        await db.execute(
            delete(TelegramLink).where(
                (TelegramLink.user_id == owner) | (TelegramLink.chat_id == chat_id)
            )
        )
        db.add(TelegramLink(user_id=owner, chat_id=chat_id))
        await db.commit()
    return True


async def unlink_user(user_id) -> None:
    from app.core.database import AsyncSessionLocal
    from app.models.db import TelegramLink

    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(TelegramLink).where(TelegramLink.user_id == uuid.UUID(str(user_id)))
        )
        await db.commit()


async def unlink_chat(chat_id: int) -> None:
    from app.core.database import AsyncSessionLocal
    from app.models.db import TelegramLink

    async with AsyncSessionLocal() as db:
        await db.execute(delete(TelegramLink).where(TelegramLink.chat_id == chat_id))
        await db.commit()
    await redis_client.get_redis().delete(f"tg:await:{chat_id}")


# ── Asking for a code ───────────────────────────────────────────


async def ask_for_code(user_id, run_id, site: str) -> bool:
    """Best effort: never raises; the in-app code box always works too."""
    from app.services.nova_signin import CODE_TTL

    if not configured():
        return False
    try:
        chat_id = await chat_for(user_id)
        if chat_id is None:
            return False
        await redis_client.get_redis().set(f"tg:await:{chat_id}", str(run_id), ex=CODE_TTL)
        return await call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"Nova is waiting for the one-time code that {site} just sent you. "
                    "Reply here with only the code within 10 minutes. "
                    "Never send passwords here."
                ),
            },
        )
    except Exception:
        logger.warning("telegram_code_request_failed", exc_info=True)
        return False


# ── Updates from Telegram ───────────────────────────────────────


async def handle_update(update: dict) -> dict:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = str(message.get("text") or "").strip()
    if chat.get("type") != "private" or not isinstance(chat_id, int) or not text:
        return {"status": "ignored"}
    update_id = update.get("update_id")
    if isinstance(update_id, int) and not await redis_client.get_redis().set(
        f"tg:update:{update_id}", "1", nx=True, ex=86400
    ):
        return {"status": "duplicate"}

    if text.startswith("/start"):
        token = text.split(maxsplit=1)[1] if " " in text else ""
        if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token) and await link(token, chat_id):
            await call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Connected. Nova will ask you here for one-time codes. "
                    "Send /stop to disconnect.",
                },
            )
            return {"status": "linked"}
        await call(
            "sendMessage",
            {"chat_id": chat_id, "text": "That link expired. Open CareerCraft and connect again."},
        )
        return {"status": "link_failed"}
    if text == "/stop":
        await unlink_chat(chat_id)
        await call("sendMessage", {"chat_id": chat_id, "text": "Disconnected."})
        return {"status": "unlinked"}

    code = re.sub(r"\s+", "", text)
    run_id = await redis_client.get_redis().getdel(f"tg:await:{chat_id}")
    if not run_id or not CODE.fullmatch(code):
        if run_id:  # not a code: keep waiting for one
            from app.services.nova_signin import CODE_TTL

            await redis_client.get_redis().set(f"tg:await:{chat_id}", run_id, ex=CODE_TTL)
        await call("sendMessage", {"chat_id": chat_id, "text": HELP})
        return {"status": "help"}
    run_id = run_id.decode() if isinstance(run_id, bytes) else run_id
    accepted = await deliver_code(chat_id, uuid.UUID(run_id), code)
    # Don't leave the code sitting in the chat history.
    await call("deleteMessage", {"chat_id": chat_id, "message_id": message.get("message_id")})
    await call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Got it. Nova is entering the code."
            if accepted
            else "Nova isn't waiting for that code any more. Open CareerCraft to check.",
        },
    )
    return {"status": "code_accepted" if accepted else "code_rejected"}


async def deliver_code(chat_id: int, run_id: uuid.UUID, code: str) -> bool:
    from app.api.v1.agents import ApproveRequest, decide_run
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun, TelegramLink, User
    from app.services import nova_signin

    try:
        async with AsyncSessionLocal() as db:
            owner = (
                await db.execute(
                    select(TelegramLink.user_id).where(TelegramLink.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            run = await db.get(AgentRun, run_id)
            pending = (run.output or {}) if run else {}
            if (
                owner is None
                or run is None
                or run.user_id != owner
                or run.status != "awaiting_approval"
                or ((pending.get("action") or {}).get("operation")) != "enter_code"
            ):
                return False
            user = await db.get(User, owner)
            await nova_signin.store_code(run_id, code)
            await decide_run(
                str(run_id), ApproveRequest(approved=True, action_type="computer_action"), db, user
            )
            return True
    except Exception:
        logger.warning("telegram_code_delivery_failed", exc_info=False)
        return False
