"""Application email: webhook signature, parsing, and handing codes to Nova."""

import base64
import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import apply_inbox

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-long!!").decode()


def signed_headers(body: bytes, secret=SECRET, stamp=None, msg_id="msg_1"):
    stamp = str(int(stamp if stamp is not None else time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": msg_id,
        "svix-timestamp": stamp,
        "svix-signature": "v1," + base64.b64encode(digest).decode(),
    }


@pytest.fixture
def domain(monkeypatch):
    monkeypatch.setattr(settings, "APPLY_EMAIL_DOMAIN", "apply.careercraftsai.me")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", SECRET)


# ── Signature ───────────────────────────────────────────────────


def test_valid_signature_is_accepted():
    body = b'{"type":"email.received"}'
    assert apply_inbox.verify_signature(body, signed_headers(body), SECRET)


def test_any_listed_v1_signature_may_match():
    body = b"{}"
    headers = signed_headers(body)
    headers["svix-signature"] = "v1,bm90LWl0 " + headers["svix-signature"]
    assert apply_inbox.verify_signature(body, headers, SECRET)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda body, h: (body + b" ", h),
        lambda body, h: (body, {**h, "svix-id": "msg_2"}),
        lambda body, h: (body, {**h, "svix-signature": h["svix-signature"].replace("v1", "v2")}),
        lambda body, h: (body, {k: v for k, v in h.items() if k != "svix-signature"}),
    ],
)
def test_tampered_requests_are_refused(tamper):
    body = b'{"type":"email.received"}'
    body, headers = tamper(body, signed_headers(body))
    assert not apply_inbox.verify_signature(
        b'{"type":"email.received"}' if body is None else body, headers, SECRET
    )


def test_stale_signature_is_refused():
    body = b"{}"
    headers = signed_headers(body, stamp=time.time() - 3600)
    assert not apply_inbox.verify_signature(body, headers, SECRET)


def test_no_secret_refuses_everything():
    body = b"{}"
    assert not apply_inbox.verify_signature(body, signed_headers(body), "")


# ── Parsing ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("Your Naukri OTP is 482913. Valid for 10 minutes.", ["482913"]),
        ("Use verification code A7K9Q2 to confirm your account", ["A7K9Q2"]),
        ("Your sign-in code:\n\n  5520\n", ["5520"]),
        ("Thanks for applying! © 2026 Example Corp, 221B Baker St", []),
        ("Call us on 98765 43210 for help with your application", []),
        ("Order 123456 shipped", []),
    ],
)
def test_codes_only_next_to_code_words(text, codes):
    assert apply_inbox.extract_codes(text) == codes


def test_links_keep_verification_and_skip_unsubscribe():
    html = (
        '<a href="https://accounts.example.com/verify?token=abc&amp;u=1">Verify</a>'
        '<a href="https://example.com/unsubscribe?verify=1">Unsubscribe</a>'
        '<a href="http://example.com/confirm">insecure</a>'
        '<a href="https://example.com/careers">Careers</a>'
    )
    assert apply_inbox.extract_links("", html) == [
        "https://accounts.example.com/verify?token=abc&u=1"
    ]


def test_html_to_text_drops_scripts_and_tags():
    text = apply_inbox.html_to_text(
        "<style>x{}</style><p>Your code is <b>1234</b></p><script>1</script>"
    )
    assert "Your code is 1234" in text
    assert "x{}" not in text


@pytest.mark.parametrize(
    ("host", "site"),
    [
        ("mail.naukri.com", "naukri.com"),
        ("www.naukri.com", "naukri.com"),
        ("jobs.example.co.in", "example.co.in"),
        ("myworkdayjobs.com", "myworkdayjobs.com"),
        (None, ""),
    ],
)
def test_site_key(host, site):
    assert apply_inbox.site_key(host) == site


def test_recipients_only_on_our_domain(domain):
    data = {
        "to": ["Priya <PRIYA.K7F2M@apply.careercraftsai.me>", "other@gmail.com"],
        "received_for": ["bad local@apply.careercraftsai.me", "x.y@evil.example"],
    }
    assert apply_inbox.recipient_local_parts(data) == {"priya.k7f2m"}


# ── Handing codes to Nova ───────────────────────────────────────


def session_with(runs, user=None, message=None):
    db = MagicMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalars.return_value.all.return_value = runs
    db.get = AsyncMock(side_effect=lambda model, _id: user if model.__name__ == "User" else message)
    db.commit = AsyncMock()
    return db


def waiting(page_url, operation="enter_code"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        output={
            "type": "computer_action",
            "page_url": page_url,
            "action": {"operation": operation},
        },
    )


@pytest.mark.asyncio
async def test_emailed_code_fills_the_one_step_waiting_on_that_site():
    run = waiting("https://www.naukri.com/otp")
    message = SimpleNamespace(filled_run_id=None)
    db = session_with(
        [run, waiting("https://boards.greenhouse.io/x")],
        user=SimpleNamespace(id=1),
        message=message,
    )
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=db),
        patch("app.services.nova_signin.store_code", AsyncMock()) as store,
        patch(
            "app.api.v1.agents.decide_run", AsyncMock(return_value={"status": "queued"})
        ) as decide,
    ):
        filled = await apply_inbox.fill_waiting_code(
            uuid.uuid4(), "otp@mail.naukri.com", "482913", uuid.uuid4()
        )
    assert filled == run.id
    assert store.await_args.args == (run.id, "482913")
    assert decide.await_args.args[0] == str(run.id)
    assert decide.await_args.args[1].approved is True
    assert message.filled_run_id == run.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runs",
    [
        [],
        [waiting("https://evil-naukri.example/otp")],
        [waiting("https://www.naukri.com/login", operation="click")],
        [waiting("https://www.naukri.com/otp"), waiting("https://login.naukri.com/otp")],
    ],
)
async def test_emailed_code_is_left_for_the_member_when_not_certain(runs):
    db = session_with(runs)
    with (
        patch("app.core.database.AsyncSessionLocal", return_value=db),
        patch("app.services.nova_signin.store_code", AsyncMock()) as store,
        patch("app.api.v1.agents.decide_run", AsyncMock()) as decide,
    ):
        filled = await apply_inbox.fill_waiting_code(
            uuid.uuid4(), "otp@naukri.com", "482913", uuid.uuid4()
        )
    assert filled is None
    store.assert_not_called()
    decide.assert_not_called()


@pytest.mark.asyncio
async def test_events_for_other_domains_are_ignored(domain):
    with patch.object(apply_inbox, "fetch_received", AsyncMock()) as fetch:
        result = await apply_inbox.handle_event(
            {"type": "email.received", "data": {"email_id": "abcdef1234", "to": ["a@gmail.com"]}}
        )
    assert result == {"status": "no_recipient"}
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_non_received_events_are_ignored(domain):
    assert await apply_inbox.handle_event({"type": "email.sent", "data": {}}) == {
        "status": "ignored"
    }


# ── Webhook route ───────────────────────────────────────────────


def inbound_client():
    from app.api.v1 import apply_email
    from app.core.rate_limit import limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(apply_email.inbound_router)
    return TestClient(app)


def test_webhook_refuses_bad_signature(domain):
    with patch.object(apply_inbox, "handle_event", AsyncMock()) as handle:
        response = inbound_client().post(
            "/inbound/resend", content=b'{"type":"email.received"}', headers={"svix-id": "x"}
        )
    assert response.status_code == 401
    handle.assert_not_called()


def test_webhook_hands_verified_event_to_inbox(domain):
    body = json.dumps({"type": "email.received", "data": {"email_id": "abcdef1234"}}).encode()
    with patch.object(
        apply_inbox, "handle_event", AsyncMock(return_value={"status": "stored"})
    ) as handle:
        response = inbound_client().post(
            "/inbound/resend", content=body, headers=signed_headers(body)
        )
    assert response.status_code == 200
    assert handle.await_args.args[0]["data"]["email_id"] == "abcdef1234"


def test_webhook_is_off_until_configured(monkeypatch):
    monkeypatch.setattr(settings, "APPLY_EMAIL_DOMAIN", "")
    response = inbound_client().post("/inbound/resend", content=b"{}")
    assert response.status_code == 503


def test_webhook_path_skips_session_auth():
    from app.core.request_middleware import PUBLIC_PATHS

    assert "/api/v1/inbound/resend" in PUBLIC_PATHS


def test_alias_local_part_is_safe():
    user = SimpleNamespace(full_name="Priya Kumari-Rao", email="p@x.com")
    assert apply_inbox._slug(user) == "priya"
    assert apply_inbox._slug(SimpleNamespace(full_name="", email="Ünïcode+tag@x.com")) == "ncodetag"
    assert apply_inbox._slug(SimpleNamespace(full_name=None, email=None)) == "me"
