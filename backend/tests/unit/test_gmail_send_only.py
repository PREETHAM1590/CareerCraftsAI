"""Send-only Gmail: outreach without inbox access, and never a double send."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.v1.integrations import _account_email, _sync_gmail_account_email
from app.integrations.exceptions import (
    ConnectionNotFoundError,
    IntegrationActionError,
    ProviderUnavailableError,
)
from app.integrations.providers import SUPPORTED_PROVIDERS
from app.integrations.schemas import IntegrationProxyResponse
from app.models.db import IntegrationConnection
from app.services import gmail_service
from app.services.gmail_service import GmailMCPClient, GmailSendError

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
SENT = IntegrationProxyResponse(status_code=200, data={"id": "msg-1"})


def sender(*outcomes):
    """A proxy_request stand-in returning or raising each outcome in turn."""
    calls = []

    def proxy_request(**kwargs):
        calls.append(kwargs["provider"])
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return proxy_request, calls


def test_send_only_provider_is_registered_on_google_mail():
    assert SUPPORTED_PROVIDERS["gmail_send"].nango_provider == "google-mail"


def test_full_gmail_connection_is_used_first():
    proxy, calls = sender(SENT)
    with patch.object(gmail_service, "proxy_request", proxy):
        assert GmailMCPClient(str(USER_ID)).send_message("a@b.co", "Hi", "Body") == {"id": "msg-1"}
    assert calls == ["gmail"]


@pytest.mark.parametrize(
    "missing",
    [ConnectionNotFoundError("none"), ValueError("Nango provider config key is not configured")],
)
def test_send_only_connection_is_used_when_gmail_is_absent(missing):
    proxy, calls = sender(missing, SENT)
    with patch.object(gmail_service, "proxy_request", proxy):
        assert GmailMCPClient(str(USER_ID)).send_message("a@b.co", "Hi", "Body") == {"id": "msg-1"}
    assert calls == ["gmail", "gmail_send"]


@pytest.mark.parametrize(
    "failure",
    [IntegrationActionError("rejected"), ProviderUnavailableError("down"), RuntimeError("?")],
)
def test_a_failed_send_never_retries_on_the_other_connection(failure):
    proxy, calls = sender(failure, SENT)
    with patch.object(gmail_service, "proxy_request", proxy):
        with pytest.raises(GmailSendError):
            GmailMCPClient(str(USER_ID)).send_message("a@b.co", "Hi", "Body")
    assert calls == ["gmail"]


def test_no_connection_at_all_is_a_clear_error():
    proxy, calls = sender(ConnectionNotFoundError("none"), ConnectionNotFoundError("none"))
    with patch.object(gmail_service, "proxy_request", proxy):
        with pytest.raises(GmailSendError, match="not connected"):
            GmailMCPClient(str(USER_ID)).send_message("a@b.co", "Hi", "Body")
    assert calls == ["gmail", "gmail_send"]


def send_only_connection():
    return IntegrationConnection(
        user_id=USER_ID,
        provider="gmail_send",
        provider_config_key="career-gmail-send",
        external_connection_id="conn-2",
        status="connected",
    )


@pytest.mark.asyncio
async def test_send_only_account_email_comes_from_userinfo():
    connection = send_only_connection()
    calls = []

    class Gateway:
        async def proxy_request(self, **kwargs):
            calls.append(kwargs)
            return IntegrationProxyResponse(status_code=200, data={"email": "me@gmail.com"})

    await _sync_gmail_account_email(
        connection,
        db=SimpleNamespace(),
        current_user=SimpleNamespace(id=USER_ID),
        gateway=Gateway(),
        login_email=None,
    )
    assert calls == [
        {
            "user_id": USER_ID,
            "provider": "gmail_send",
            "method": "GET",
            "path": "oauth2/v3/userinfo",
            "headers": {"Base-Url-Override": "https://www.googleapis.com"},
        }
    ]
    assert _account_email(connection) == "me@gmail.com"


@pytest.mark.asyncio
async def test_send_only_connection_to_another_gmail_is_revoked():
    connection = send_only_connection()
    gateway = MagicMock()
    gateway.proxy_request = AsyncMock(
        return_value=IntegrationProxyResponse(status_code=200, data={"email": "other@gmail.com"})
    )
    gateway.revoke_connection = AsyncMock()
    with patch("app.api.v1.integrations.mark_revoked", AsyncMock()) as revoked:
        await _sync_gmail_account_email(
            connection,
            db=SimpleNamespace(),
            current_user=SimpleNamespace(id=USER_ID),
            gateway=gateway,
            login_email="me@gmail.com",
        )
    gateway.revoke_connection.assert_awaited_once_with(user_id=USER_ID, provider="gmail_send")
    assert revoked.await_args.kwargs["provider"] == "gmail_send"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connected", "google", "can_send"),
    [
        ({"gmail"}, True, True),
        ({"gmail_send"}, False, True),
        ({"gmail", "gmail_send"}, True, True),
        (set(), False, False),
    ],
)
async def test_connected_accounts_separate_inbox_from_sending(connected, google, can_send):
    from app.api.v1.users import get_connected_accounts

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.scalars.return_value.all.return_value = list(connected)
    result = await get_connected_accounts(db=db, current_user=SimpleNamespace(id=USER_ID))
    assert (result.google, result.gmail_send) == (google, can_send)
