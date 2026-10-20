"""MemoryEmbedder: models, Ollama host, and provider-aware cache keys."""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.embedder import MemoryEmbedder


def test_cache_key_differs_across_providers_and_models():
    emb = MemoryEmbedder({"provider": "openai"})
    keys = {
        emb._cache_key("same text", "openai", "text-embedding-3-small"),
        emb._cache_key("same text", "google", "models/gemini-embedding-001"),
        emb._cache_key("same text", "ollama", "qwen3-embedding:0.6b"),
        emb._cache_key("same text", "ollama", "nomic-embed-text"),
    }
    assert len(keys) == 4
    assert emb._cache_key("same text", "ollama", "qwen3-embedding:0.6b").startswith(
        "emb:ollama:qwen3-embedding:0.6b:"
    )


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value


@pytest.mark.asyncio
async def test_primary_vector_is_cached_under_the_primary_model():
    redis = _FakeRedis()
    emb = MemoryEmbedder({"provider": "openai", "api_key": "k"}, redis_client=redis)
    emb._openai_embed = AsyncMock(return_value=[0.5, 0.5])
    await emb.embed("hello")
    [key] = redis.data
    assert key.startswith("emb:openai:text-embedding-3-small:")


@pytest.mark.asyncio
async def test_fallback_vector_is_cached_under_ollama_not_the_primary():
    redis = _FakeRedis()
    emb = MemoryEmbedder({"provider": "openai", "api_key": "k"}, redis_client=redis)
    emb._openai_embed = AsyncMock(side_effect=RuntimeError("openai down"))
    emb._ollama_embed = AsyncMock(return_value=[0.1, 0.2])

    await emb.embed("hello")
    [key] = redis.data
    assert key.startswith("emb:ollama:qwen3-embedding:0.6b:")

    # The primary provider's next lookup must not be served the Ollama vector.
    emb._openai_embed = AsyncMock(return_value=[0.9, 0.9])
    vec = await emb.embed("hello")
    assert vec[:2] == [0.9, 0.9]
    emb._openai_embed.assert_awaited_once()


@pytest.mark.asyncio
async def test_ollama_embed_uses_qwen_via_the_embed_endpoint(monkeypatch):
    client = MagicMock()
    client.embed = AsyncMock(return_value={"embeddings": [[0.1, 0.2, 0.3]]})
    hosts: list[str] = []

    def fake_async_client(host):
        hosts.append(host)
        return client

    monkeypatch.setitem(sys.modules, "ollama", types.SimpleNamespace(AsyncClient=fake_async_client))
    emb = MemoryEmbedder({"provider": "ollama", "ollama_url": "http://my-ollama:11434"})
    assert await emb._ollama_embed("hi") == [0.1, 0.2, 0.3]
    client.embed.assert_awaited_once_with(model="qwen3-embedding:0.6b", input="hi")
    assert hosts == ["http://my-ollama:11434"]


@pytest.mark.parametrize(
    "user_url,server_url,expected",
    [
        ("http://my-ollama:11434", "http://10.0.0.182:11434", "http://my-ollama:11434"),
        ("", "http://10.0.0.182:11434", "http://10.0.0.182:11434"),
        (None, "", "http://localhost:11434"),
    ],
)
def test_ollama_url_precedence(monkeypatch, user_url, server_url, expected):
    from app.core.config import settings

    monkeypatch.setattr(settings, "EMBEDDING_OLLAMA_URL", server_url)
    emb = MemoryEmbedder({"provider": "anthropic", "ollama_url": user_url})
    assert emb.ollama_url == expected


@pytest.mark.asyncio
async def test_google_embed_uses_gemini_embedding_001_at_768_dims(monkeypatch):
    from google import genai
    from google.genai import types as genai_types

    client = AsyncMock()
    client.models.embed_content.return_value = genai_types.EmbedContentResponse(
        embeddings=[genai_types.ContentEmbedding(values=[0.1] * 768)]
    )
    client.__aenter__.return_value = client
    factory = MagicMock(return_value=MagicMock(aio=client))
    monkeypatch.setattr(genai, "Client", factory)
    emb = MemoryEmbedder({"provider": "google", "api_key": "synthetic-key"})
    vec = await emb._google_embed("text")
    assert len(vec) == 768
    assert factory.call_args.kwargs["api_key"] == "synthetic-key"
    assert factory.call_args.kwargs["http_options"].timeout == 15_000
    args = client.models.embed_content.call_args.kwargs
    assert args["model"] == "models/gemini-embedding-001"
    assert args["contents"] == "text"
    assert args["config"].output_dimensionality == 768
    client.__aexit__.assert_awaited_once()
