"""The member's application email: <local_part>@APPLY_EMAIL_DOMAIN.

Resend receives mail for the domain (MX record) and calls our signed webhook
with metadata only; we fetch the message, keep it for the member, and pull out
verification codes and links. When Nova is waiting on a code step for the same
site the email came from, and the email holds exactly one code, the code is
handed to that step the same way a member-typed code is (sealed, read once).

All email content is untrusted: it is shown to the member and given to Nova
only as data. Links are never opened automatically; Nova may propose opening
one, and the member approves it like any other navigation.
"""

import base64
import hashlib
import hmac
import html as html_lib
import logging
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.config import settings

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com"
SIGNATURE_TOLERANCE = 300
MAX_TEXT = 20000
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

_CODE_WORDS = re.compile(
    r"\b(code|otp|one[- ]time|verification|verify|passcode|pin|security|login|sign[- ]?in)\b",
    re.I,
)
_DIGITS = re.compile(r"(?<![\d-])(\d{4,8})(?![\d-])")
_ALNUM = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])([A-Z0-9]{6,8})\b")
_URL = re.compile(r"https://[^\s<>\"')\]]+")
_HREF = re.compile(r"""href\s*=\s*["'](https://[^"']+)["']""", re.I)
_LINK_HINT = re.compile(
    r"verif|confirm|activat|validat|magic|login|signin|sign-in|token|reset", re.I
)
_LINK_SKIP = re.compile(r"unsubscribe|preferences|privacy|optout|opt-out", re.I)
_SECOND_LEVEL = {"co", "com", "ac", "org", "net", "gov", "edu"}


# ── Address ─────────────────────────────────────────────────────


def configured() -> bool:
    return bool(settings.APPLY_EMAIL_DOMAIN and settings.RESEND_API_KEY)


def _slug(user) -> str:
    seed = (getattr(user, "full_name", None) or "").split(" ")[0] or (
        getattr(user, "email", None) or ""
    ).split("@")[0]
    slug = re.sub(r"[^a-z0-9]", "", seed.lower())[:20]
    return slug or "me"


def address(local_part: str) -> str:
    return f"{local_part}@{settings.APPLY_EMAIL_DOMAIN.lower()}"


async def get_or_create_alias(db, user) -> str | None:
    """The member's address, created on first use. None when not configured."""
    from app.models.db import ApplyEmailAlias

    if not settings.APPLY_EMAIL_DOMAIN:
        return None
    row = await db.get(ApplyEmailAlias, user.id)
    for _ in range(5):
        if row is not None:
            return address(row.local_part)
        suffix = "".join(secrets.choice(_ALPHABET) for _ in range(5))
        candidate = ApplyEmailAlias(user_id=user.id, local_part=f"{_slug(user)}.{suffix}")
        try:
            async with db.begin_nested():
                db.add(candidate)
            await db.commit()
            row = candidate
        except IntegrityError:
            # Taken local part, or a concurrent first call created ours.
            await db.rollback()
            row = await db.get(ApplyEmailAlias, user.id)
    raise RuntimeError("Could not allocate an application email address")


async def alias_for(user_id) -> str | None:
    from app.core.database import AsyncSessionLocal
    from app.models.db import ApplyEmailAlias

    if not settings.APPLY_EMAIL_DOMAIN:
        return None
    async with AsyncSessionLocal() as db:
        row = await db.get(ApplyEmailAlias, uuid.UUID(str(user_id)))
    return address(row.local_part) if row else None


# ── Webhook signature (Svix, as used by Resend) ─────────────────


def verify_signature(body: bytes, headers, secret: str, now: float | None = None) -> bool:
    msg_id = headers.get("svix-id")
    stamp = headers.get("svix-timestamp")
    offered = headers.get("svix-signature")
    if not (secret and msg_id and stamp and offered):
        return False
    try:
        sent = int(stamp)
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - sent) > SIGNATURE_TOLERANCE:
        return False
    signed = f"{msg_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in offered.split():
        version, _, signature = part.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return True
    return False


# ── Parsing ─────────────────────────────────────────────────────


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"[ \t\r\f\v]+", " ", html_lib.unescape(value)).strip()


def extract_codes(text: str) -> list[str]:
    """Codes that sit next to words like 'code', 'OTP' or 'verification'."""
    found: list[str] = []
    for pattern in (_DIGITS, _ALNUM):
        for match in pattern.finditer(text):
            value = match.group(1)
            if len(value) == 4 and value.isdigit() and 1900 <= int(value) <= 2099:
                continue  # a year, not a code
            window = text[max(0, match.start() - 120) : match.end() + 40]
            if _CODE_WORDS.search(window) and value not in found:
                found.append(value)
    return found[:3]


def extract_links(text: str, html: str | None) -> list[str]:
    links: list[str] = []
    for url in [*_HREF.findall(html or ""), *_URL.findall(text)]:
        url = html_lib.unescape(url).rstrip(".,;")
        if len(url) > 2000 or url in links or _LINK_SKIP.search(url):
            continue
        if _LINK_HINT.search(url):
            links.append(url)
    return links[:5]


def site_key(host: str | None) -> str:
    """Registrable domain, roughly: mail.naukri.com and www.naukri.com match."""
    labels = [label for label in (host or "").lower().strip(".").split(".") if label]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def sender_site(sender: str) -> str:
    return site_key(sender.rsplit("@", 1)[-1].strip(" >"))


# ── Ingest ──────────────────────────────────────────────────────


def recipient_local_parts(data: dict) -> set[str]:
    domain = settings.APPLY_EMAIL_DOMAIN.lower()
    parts = set()
    for value in [*(data.get("to") or []), *(data.get("received_for") or [])]:
        value = str(value).strip().lower()
        if "<" in value:
            value = value.rsplit("<", 1)[-1].rstrip(">")
        local, _, host = value.partition("@")
        if host == domain and re.fullmatch(r"[a-z0-9.]{1,64}", local):
            parts.add(local)
    return parts


async def fetch_received(email_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        response = await client.get(
            f"{RESEND_API}/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
        )
    response.raise_for_status()
    return response.json()


async def handle_event(payload: dict) -> dict:
    """Store an email.received event for every member it was addressed to."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import ApplyEmailAlias, ApplyInboxMessage

    if payload.get("type") != "email.received" or not settings.APPLY_EMAIL_DOMAIN:
        return {"status": "ignored"}
    data = payload.get("data") or {}
    email_id = str(data.get("email_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,100}", email_id):
        return {"status": "ignored"}
    locals_ = recipient_local_parts(data)
    if not locals_:
        return {"status": "no_recipient"}
    async with AsyncSessionLocal() as db:
        owners = (
            (
                await db.execute(
                    select(ApplyEmailAlias.user_id).where(ApplyEmailAlias.local_part.in_(locals_))
                )
            )
            .scalars()
            .all()
        )
    if not owners:
        return {"status": "no_recipient"}
    email = await fetch_received(email_id)
    sender = str(email.get("from") or data.get("from") or "")[:320]
    subject = str(email.get("subject") or data.get("subject") or "")[:500]
    text = (email.get("text") or html_to_text(email.get("html")))[:MAX_TEXT]
    codes = extract_codes(f"{subject}\n{text}")
    links = extract_links(text, email.get("html"))
    stored = 0
    for owner in owners:
        async with AsyncSessionLocal() as db:
            message_id = (
                await db.execute(
                    insert(ApplyInboxMessage)
                    .values(
                        id=uuid.uuid4(),
                        user_id=owner,
                        provider_email_id=email_id,
                        sender=sender,
                        subject=subject,
                        text_body=text,
                        codes=codes,
                        links=links,
                    )
                    .on_conflict_do_nothing(constraint="apply_inbox_messages_provider_user")
                    .returning(ApplyInboxMessage.id)
                )
            ).scalar_one_or_none()
            await db.commit()
        if message_id is None:
            continue  # redelivered webhook
        stored += 1
        if len(codes) == 1:
            await fill_waiting_code(owner, sender, codes[0], message_id)
    return {"status": "stored", "messages": stored}


async def fill_waiting_code(user_id, sender: str, code: str, message_id) -> uuid.UUID | None:
    """Give an emailed code to the one Nova code step waiting on that site."""
    from app.api.v1.agents import ApproveRequest, decide_run
    from app.core.database import AsyncSessionLocal
    from app.models.db import AgentRun, ApplyInboxMessage, User
    from app.services import nova_signin

    site = sender_site(sender)
    if not site or "." not in site:
        return None
    try:
        async with AsyncSessionLocal() as db:
            runs = (
                (
                    await db.execute(
                        select(AgentRun).where(
                            AgentRun.user_id == user_id,
                            AgentRun.agent_type == "computer_task",
                            AgentRun.status == "awaiting_approval",
                            AgentRun.started_at >= datetime.now(UTC) - timedelta(hours=6),
                        )
                    )
                )
                .scalars()
                .all()
            )
            waiting = [
                run
                for run in runs
                if (run.output or {}).get("type") == "computer_action"
                and ((run.output or {}).get("action") or {}).get("operation") == "enter_code"
                and site_key(urlsplit(str((run.output or {}).get("page_url") or "")).hostname)
                == site
            ]
            if len(waiting) != 1:
                return None  # none, or ambiguous: the member enters it
            run = waiting[0]
            user = await db.get(User, user_id)
            await nova_signin.store_code(run.id, code)
            await decide_run(
                str(run.id), ApproveRequest(approved=True, action_type="computer_action"), db, user
            )
            message = await db.get(ApplyInboxMessage, message_id)
            if message is not None:
                message.filled_run_id = run.id
                await db.commit()
            return run.id
    except Exception:
        logger.warning("Emailed code could not be handed to the waiting Nova step", exc_info=True)
        return None


async def recent_links(user_id, minutes: int = 30) -> list[dict]:
    """Verification links from the last few minutes, for Nova's context."""
    from app.core.database import AsyncSessionLocal
    from app.models.db import ApplyInboxMessage

    if not settings.APPLY_EMAIL_DOMAIN:
        return []
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(ApplyInboxMessage)
                    .where(
                        ApplyInboxMessage.user_id == uuid.UUID(str(user_id)),
                        ApplyInboxMessage.received_at
                        >= datetime.now(UTC) - timedelta(minutes=minutes),
                    )
                    .order_by(ApplyInboxMessage.received_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
    return [
        {"from": row.sender, "subject": row.subject, "links": row.links}
        for row in rows
        if row.links
    ]
