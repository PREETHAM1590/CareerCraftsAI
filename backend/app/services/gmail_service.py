"""Gmail operations through the Nango credential proxy."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from urllib.parse import quote, urlencode

from app.integrations.exceptions import ConnectionNotFoundError, IntegrationActionError
from app.services.integration_proxy_service import proxy_request

# Sending tries the full Gmail connection, then the send-only one. Only a
# missing or unconfigured connection falls through; a rejection or an
# unavailable provider never retries on the other (no double sends).
SEND_PROVIDERS = ("gmail", "gmail_send")


def send_only_verified(user_id: str) -> bool:
    """The send-only connection is used only once its Gmail address was read
    and matched to the member's sign-in (a mismatch revokes it)."""
    from uuid import UUID

    from sqlalchemy import select

    from app.core.database import AsyncSessionLocal
    from app.models.db import IntegrationConnection
    from app.services.integration_proxy_service import _run

    async def check() -> bool:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(IntegrationConnection).where(
                        IntegrationConnection.user_id == UUID(str(user_id)),
                        IntegrationConnection.provider == "gmail_send",
                        IntegrationConnection.status == "connected",
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return False
        from app.api.v1.integrations import _account_email

        return bool(_account_email(row))

    try:
        return _run(check())
    except Exception:
        return False


class GmailSendError(RuntimeError):
    """Raised when Nango or Gmail cannot send an approved message."""


class GmailMCPClient:
    """Compatibility surface for agents; credentials stay exclusively in Nango."""

    def __init__(self, user_id: str):
        self.user_id = user_id

    def search_threads(self, query: str, max_results: int = 10) -> list[dict]:
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="GET",
                path=(
                    "gmail/v1/users/me/messages?"
                    f"{urlencode({'q': query, 'maxResults': max_results})}"
                ),
            )
        except Exception:
            return []
        return result.data.get("messages", []) if isinstance(result.data, dict) else []

    def get_message_summary(self, message_id: str) -> dict:
        """Sender, subject, date and Gmail's own snippet for one message.

        Gmail's search lists only message ids; this fetches headers (not the
        body) so a scan can classify mail without downloading it.
        """
        query = urlencode(
            [
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date"),
            ]
        )
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="GET",
                path=f"gmail/v1/users/me/messages/{quote(message_id, safe='')}?{query}",
            )
        except Exception:
            return {}
        data = result.data if isinstance(result.data, dict) else {}
        headers = {
            str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in (data.get("payload") or {}).get("headers", [])
            if isinstance(h, dict)
        }
        return {
            "id": data.get("id") or message_id,
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": str(data.get("snippet", "")),
        }

    def get_thread(self, thread_id: str) -> dict:
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="GET",
                path=f"gmail/v1/users/me/threads/{quote(thread_id, safe='')}",
            )
        except Exception:
            return {}
        return result.data if isinstance(result.data, dict) else {}

    def get_thread_headers(self, thread_id: str) -> list[dict]:
        """Sender, subject and date of every message in a thread (no bodies)."""
        query = urlencode(
            [
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date"),
            ]
        )
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="GET",
                path=f"gmail/v1/users/me/threads/{quote(thread_id, safe='')}?{query}",
            )
        except Exception:
            return []
        data = result.data if isinstance(result.data, dict) else {}
        messages = []
        for message in data.get("messages", []):
            headers = {
                str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in (message.get("payload") or {}).get("headers", [])
                if isinstance(h, dict)
            }
            messages.append(
                {
                    "id": message.get("id", ""),
                    "from": headers.get("from", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                }
            )
        return messages

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        html: str | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> dict:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        for filename, content in attachments or []:
            message.add_attachment(
                content, maintype="application", subtype="pdf", filename=filename
            )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        result = None
        for provider in SEND_PROVIDERS:
            if provider == "gmail_send" and not send_only_verified(self.user_id):
                continue  # its address hasn't been checked against the sign-in yet
            try:
                result = proxy_request(
                    user_id=self.user_id,
                    provider=provider,
                    method="POST",
                    path="gmail/v1/users/me/messages/send",
                    json_data={"raw": raw},
                )
                break
            except IntegrationActionError as exc:
                raise GmailSendError("Gmail rejected the approved message") from exc
            except (ConnectionNotFoundError, ValueError):
                continue  # not connected / not configured: try the next one
            except Exception as exc:
                raise GmailSendError("Gmail is not connected through Nango") from exc
        if result is None:
            raise GmailSendError("Gmail is not connected through Nango")
        if not isinstance(result.data, dict):
            raise GmailSendError("Gmail returned an invalid response")
        return result.data

    def get_message_metadata(self, message_id: str) -> dict:
        """Fetch header metadata only (From/Subject/Date/List-Unsubscribe).

        Read path used opportunistically while listing inbox-cleanup
        candidates, so a failure degrades to `{}` (same pattern as
        `search_threads`/`get_thread`) rather than raising and aborting the
        whole list. Gmail's `format=metadata` never includes the message
        body, but its `snippet` field is a short body preview — stripped
        here so this method can never surface any message content.
        """
        query = urlencode(
            {
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date", "List-Unsubscribe"],
            },
            doseq=True,
        )
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="GET",
                path=f"gmail/v1/users/me/messages/{quote(message_id, safe='')}?{query}",
            )
        except Exception:
            return {}
        if not isinstance(result.data, dict):
            return {}
        data = dict(result.data)
        data.pop("snippet", None)
        return data

    def archive_message(self, message_id: str) -> dict:
        """Remove INBOX label via Gmail's messages.modify.

        Batch cleanup action over a list of already-known message IDs, so —
        like the read paths above — a single failed ID degrades to `{}`
        instead of raising and aborting every other message in the batch.
        """
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="POST",
                path=f"gmail/v1/users/me/messages/{quote(message_id, safe='')}/modify",
                json_data={"removeLabelIds": ["INBOX"]},
            )
        except Exception:
            return {}
        return result.data if isinstance(result.data, dict) else {}

    def save_draft(self, to: str, subject: str, body: str) -> dict:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        try:
            result = proxy_request(
                user_id=self.user_id,
                provider="gmail",
                method="POST",
                path="gmail/v1/users/me/drafts",
                json_data={"message": {"raw": raw}},
            )
        except IntegrationActionError as exc:
            raise GmailSendError("Gmail rejected the draft") from exc
        except Exception as exc:
            raise GmailSendError("Gmail is not connected through Nango") from exc
        if not isinstance(result.data, dict):
            raise GmailSendError("Gmail returned an invalid response")
        return result.data
