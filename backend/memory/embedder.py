"""
memory/embedder.py — Routes embedding requests to the user's configured provider.

Falls back to qwen3-embedding:0.6b via Ollama if the primary provider fails.
Uses Redis to cache embeddings for 1 hour to minimise duplicate API calls;
cache keys include the provider and model that produced the vector.
"""

import hashlib
import json
import logging
from typing import Optional, Protocol

import redis

logger = logging.getLogger(__name__)

# Target dimensionality for pgvector column (vector(1536))
_DIMS = 1536

# Keep in sync with app.services.rag_service.OLLAMA_EMBEDDING_MODEL.
OLLAMA_EMBEDDING_MODEL = "qwen3-embedding:0.6b"
GOOGLE_EMBEDDING_MODEL = "models/gemini-embedding-001"
GOOGLE_EMBEDDING_DIMENSIONS = 768
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
NVIDIA_EMBEDDING_MODEL = "nvidia/nv-embedqa-e5-v5"
DEFAULT_OLLAMA_URL = "http://localhost:11434"


def _server_ollama_url() -> str:
    """Operator-configured Ollama host (settings.EMBEDDING_OLLAMA_URL)."""
    # memory.routes already depends on app.core.config; imported lazily so
    # `import memory` stays usable without the app settings loaded.
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001 — settings unavailable (e.g. bare scripts)
        return ""
    return (settings.EMBEDDING_OLLAMA_URL or "").strip()


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class MemoryEmbedder:
    """
    Embed text using the user's active LLM provider.

    Supported providers: openai, google, nvidia_nim, ollama, anthropic.
    Anthropic does not expose an embeddings API so it routes to the Ollama fallback.
    """

    def __init__(
        self,
        user_settings: dict,
        redis_client: Optional[redis.Redis] = None,
    ) -> None:
        self.user_id = user_settings.get("user_id")
        self.provider: str = user_settings.get("provider", "openai")
        self.api_key: str = user_settings.get("api_key", "")
        self.ollama_url: str = (
            user_settings.get("ollama_url") or _server_ollama_url() or DEFAULT_OLLAMA_URL
        )
        self.redis = redis_client
        self.DIMS = _DIMS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """
        Embed a single text string.

        Checks Redis cache first; stores result for 1 hour on success, keyed
        by the provider/model that actually produced the vector (so an Ollama
        fallback vector is never served as the primary provider's).
        If all providers fail, returns a zero vector so callers can still
        save the memory row with NULL embedding.
        """
        primary = self._primary_model()
        cache_key = self._cache_key(text, *primary)

        if self.redis:
            try:
                cached = self.redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception as e:
                logger.debug(f"Redis cache read failed: {e}")

        try:
            vec = await self._embed_with_provider(text)
        except Exception as e:
            logger.warning(
                f"Primary embedding failed (provider={self.provider}): {e}. "
                f"Trying Ollama {OLLAMA_EMBEDDING_MODEL} fallback."
            )
            try:
                vec = await self._ollama_embed(text)
            except Exception as e2:
                logger.error(f"All embedding providers failed: {e2}. Returning zero vector.")
                return [0.0] * self.DIMS
            cache_key = self._cache_key(text, "ollama", OLLAMA_EMBEDDING_MODEL)

        vec = self._pad_to_dims(vec)

        if self.redis:
            try:
                self.redis.setex(cache_key, 3600, json.dumps(vec))
            except Exception as e:
                logger.debug(f"Redis cache write failed: {e}")

        return vec

    # ------------------------------------------------------------------
    # Internal routing
    # ------------------------------------------------------------------

    async def _embed_with_provider(self, text: str) -> list[float]:
        if self.provider == "openai":
            return await self._openai_embed(text)
        elif self.provider == "google":
            return await self._google_embed(text)
        elif self.provider == "nvidia_nim":
            return await self._nvidia_embed(text)
        else:
            # anthropic, ollama, unknown — all use Ollama
            return await self._ollama_embed(text)

    def _primary_model(self) -> tuple[str, str]:
        """(provider, model) that _embed_with_provider uses for this user."""
        if self.provider == "openai":
            return "openai", OPENAI_EMBEDDING_MODEL
        if self.provider == "google":
            return "google", GOOGLE_EMBEDDING_MODEL
        if self.provider == "nvidia_nim":
            return "nvidia_nim", NVIDIA_EMBEDDING_MODEL
        return "ollama", OLLAMA_EMBEDDING_MODEL

    async def _openai_embed(self, text: str) -> list[float]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        resp = await client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=text)
        return resp.data[0].embedding

    async def _google_embed(self, text: str) -> list[float]:
        from google import genai
        from google.genai import types

        async with genai.Client(
            api_key=self.api_key, http_options=types.HttpOptions(timeout=15_000)
        ).aio as client:
            result = await client.models.embed_content(
                model=GOOGLE_EMBEDDING_MODEL,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=GOOGLE_EMBEDDING_DIMENSIONS),
            )
        if not result.embeddings or not result.embeddings[0].values:
            raise ValueError("Google returned no embedding")
        return list(result.embeddings[0].values)

    async def _ollama_embed(self, text: str) -> list[float]:
        import ollama

        # /api/embed (ollama>=0.4); the legacy /api/embeddings endpoint is deprecated.
        resp = await ollama.AsyncClient(host=self.ollama_url).embed(
            model=OLLAMA_EMBEDDING_MODEL, input=text
        )
        return list(resp["embeddings"][0])

    async def _nvidia_embed(self, text: str) -> list[float]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url="https://integrate.api.nvidia.com/v1",
        )
        resp = await client.embeddings.create(
            model=NVIDIA_EMBEDDING_MODEL,
            input=text,
            extra_body={"input_type": "query", "truncate": "END"},
        )
        return resp.data[0].embedding

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cache_key(self, text: str, provider: str, model: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        scope = f"memory-emb:{self.user_id}:" if self.user_id else "emb:"
        return f"{scope}{provider}:{model}:{digest}"

    def _pad_to_dims(self, vec: list[float]) -> list[float]:
        """Truncate or zero-pad vector to self.DIMS dimensions."""
        if len(vec) >= self.DIMS:
            return vec[: self.DIMS]
        return vec + [0.0] * (self.DIMS - len(vec))
