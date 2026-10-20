"""Installed Google SDK uses separate async clients for concurrent members."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google import genai

from memory.embedder import MemoryEmbedder


@pytest.mark.asyncio
async def test_google_credentials_are_request_scoped(monkeypatch):
    clients = []

    def factory(*, api_key, http_options):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[1.0 if api_key == "a" else 2.0])]
        )
        clients.append(client)
        return SimpleNamespace(aio=client)

    monkeypatch.setattr(genai, "Client", factory)
    result = await asyncio.gather(
        *[
            MemoryEmbedder({"provider": "google", "api_key": key})._google_embed("hello")
            for key in ("a", "b")
        ]
    )
    assert result == [[1.0], [2.0]]
    assert len(clients) == 2
    for client in clients:
        client.__aexit__.assert_awaited_once()
