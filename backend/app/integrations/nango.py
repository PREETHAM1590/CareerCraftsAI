"""Small typed client for the documented Nango HTTP API."""

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from app.integrations.exceptions import (
    ConnectionNotFoundError,
    IntegrationActionError,
    ProviderUnavailableError,
)
from app.integrations.gateway import IntegrationGateway
from app.integrations.providers import provider_config_key
from app.integrations.schemas import (
    ConnectSession,
    IntegrationConnectionResult,
    IntegrationProxyResponse,
)
from app.integrations.validation import validate_return_path


class NangoIntegrationGateway(IntegrationGateway):
    """Nango-backed gateway; OAuth credentials never cross this boundary."""

    def __init__(
        self,
        *,
        base_url: str,
        secret_key: str,
        provider_config_keys: dict[str, str],
        timeout_s: float = 15,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret_key = secret_key
        self._provider_config_keys = provider_config_keys
        self._client = client or httpx.AsyncClient(timeout=timeout_s, trust_env=False)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def provider_config_key(self, provider: str) -> str:
        return provider_config_key(provider, self._provider_config_keys)

    async def create_connect_session(
        self, *, user_id: UUID, provider: str, return_path: str
    ) -> ConnectSession:
        config_key = self.provider_config_key(provider)
        validate_return_path(return_path)
        payload = {
            "allowed_integrations": [config_key],
            "tags": {"end_user_id": str(user_id)},
        }
        # Creating a session has no destructive side effect (unlike /action/trigger),
        # so it's safe to retry through this host's occasional transient network drops.
        data = await self._request("POST", "/connect/sessions", json=payload, idempotent=True)
        session = _required_object(data, "data")
        token = _required_string(session, "token")
        expires_at = _parse_datetime(_required_string(session, "expires_at"))
        connect_link = session.get("connect_link")
        if connect_link is not None and not isinstance(connect_link, str):
            raise ProviderUnavailableError("Nango returned an invalid connect session")
        if connect_link is not None:
            # The bundled Connect UI falls back to Nango Cloud's public API
            # (api.nango.dev) whenever the link's apiURL query param is absent,
            # so a self-hosted server's own connect_link is unusable without it.
            separator = "&" if "?" in connect_link else "?"
            connect_link = f"{connect_link}{separator}apiURL={quote(self._base_url, safe='')}"
        return ConnectSession(provider, token, connect_link, expires_at)

    async def get_connection(
        self, *, user_id: UUID, provider: str
    ) -> IntegrationConnectionResult | None:
        config_key = self.provider_config_key(provider)
        connections = await self._list_raw_connections(user_id)
        for connection in connections:
            if connection.get("provider_config_key") == config_key:
                return _connection_result(provider, connection)
        return None

    async def list_connections(self, *, user_id: UUID) -> list[IntegrationConnectionResult]:
        connections = await self._list_raw_connections(user_id)
        result: list[IntegrationConnectionResult] = []
        for provider in (
            "gmail",
            "gmail_send",
            "google_drive",
            "google_calendar",
            "outlook_mail",
            "outlook_calendar",
        ):
            if provider not in self._provider_config_keys:
                continue
            config_key = self.provider_config_key(provider)
            if provider == "gmail_send" and config_key == self._provider_config_keys.get("gmail"):
                continue  # misconfigured: one Nango integration can't be both
            matched = next(
                (item for item in connections if item.get("provider_config_key") == config_key),
                None,
            )
            if matched is not None:
                result.append(_connection_result(provider, matched))
        return result

    async def revoke_connection(self, *, user_id: UUID, provider: str) -> None:
        connection = await self.get_connection(user_id=user_id, provider=provider)
        if connection is None:
            return
        await self._request(
            "DELETE",
            f"/connections/{quote(connection.external_connection_id, safe='')}",
            params={"provider_config_key": connection.provider_config_key},
        )

    async def execute_action(
        self,
        *,
        user_id: UUID,
        provider: str,
        action: str,
        input_data: dict,
        idempotency_key: str | None = None,
    ) -> dict:
        connection = await self.get_connection(user_id=user_id, provider=provider)
        if connection is None:
            raise ConnectionNotFoundError("No connection exists for this provider")
        # Nango's action API has no idempotency header. Callers must persist and
        # enforce idempotency before this non-retried external mutation.
        del idempotency_key
        try:
            data = await self._request(
                "POST",
                "/action/trigger",
                headers={
                    "Connection-Id": connection.external_connection_id,
                    "Provider-Config-Key": connection.provider_config_key,
                },
                json={"action_name": action, "input": input_data},
            )
        except ProviderUnavailableError as exc:
            raise IntegrationActionError("Integration action could not be completed") from exc
        if not isinstance(data, dict):
            raise IntegrationActionError("Nango returned an invalid action response")
        return data

    async def proxy_request(
        self,
        *,
        user_id: UUID,
        provider: str,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_data: dict | None = None,
        content: bytes | None = None,
    ) -> IntegrationProxyResponse:
        """Make one non-retried request through Nango's credential proxy."""
        connection = await self.get_connection(user_id=user_id, provider=provider)
        if connection is None:
            raise ConnectionNotFoundError("No connection exists for provider")

        request_headers = {
            "Authorization": f"Bearer {self._secret_key}",
            "Connection-Id": connection.external_connection_id,
            "Provider-Config-Key": connection.provider_config_key,
        }
        request_headers.update(headers or {})
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}/proxy/{path.lstrip('/')}",
                headers=request_headers,
                json=json_data,
                content=content,
            )
        except httpx.RequestError as exc:
            raise ProviderUnavailableError("Integration provider unavailable") from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderUnavailableError("Integration provider unavailable")
        if response.status_code >= 400:
            raise IntegrationActionError("Integration provider rejected the request")
        try:
            data: dict | list | str | None = response.json()
        except ValueError:
            data = response.text or None
        return IntegrationProxyResponse(status_code=response.status_code, data=data)

    async def _list_raw_connections(self, user_id: UUID) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/connections",
            params={"tags[end_user_id]": str(user_id), "limit": 100},
        )
        connections = data.get("connections") if isinstance(data, dict) else None
        if not isinstance(connections, list) or not all(
            isinstance(item, dict) for item in connections
        ):
            raise ProviderUnavailableError("Nango returned an invalid connections response")
        return connections

    async def _request(
        self, method: str, path: str, *, idempotent: bool = False, **kwargs: Any
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._secret_key}",
            "Accept": "application/json",
        }
        headers.update(kwargs.pop("headers", {}))
        attempts = 2 if (method == "GET" or idempotent) else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, f"{self._base_url}{path}", headers=headers, **kwargs
                )
            except httpx.RequestError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.1)
                    continue
                raise ProviderUnavailableError("Integration provider is unavailable") from exc
            if response.status_code == 404:
                raise ConnectionNotFoundError("Connection was not found")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.1)
                    continue
                raise ProviderUnavailableError("Integration provider is unavailable")
            if response.is_error:
                raise ProviderUnavailableError("Integration provider rejected the request")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderUnavailableError(
                    "Integration provider returned invalid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise ProviderUnavailableError("Integration provider returned invalid JSON")
            return payload
        raise AssertionError("unreachable")


def _required_object(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ProviderUnavailableError("Nango returned an invalid response")
    return result


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProviderUnavailableError("Nango returned an invalid response")
    return result


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderUnavailableError("Nango returned an invalid timestamp") from exc


def _connection_result(provider: str, payload: dict[str, Any]) -> IntegrationConnectionResult:
    connection_id = _required_string(payload, "connection_id")
    config_key = _required_string(payload, "provider_config_key")
    errors = payload.get("errors", [])
    status = "error" if isinstance(errors, list) and errors else "connected"
    created = payload.get("created") or payload.get("created_at")
    connected_at = _parse_datetime(created) if isinstance(created, str) else None
    return IntegrationConnectionResult(provider, config_key, connection_id, status, connected_at)
