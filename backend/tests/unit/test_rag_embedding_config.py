from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def test_native_provider_is_used():
    from app.services import rag_service

    settings = SimpleNamespace(provider="ollama", ollama_url="http://ollama:11434")
    with patch.object(rag_service, "QwenOllamaEmbeddings", return_value="ollama") as embeddings:
        assert rag_service.get_embedding_provider(settings) == "ollama"
        assert rag_service.get_embedding_model(settings).inner == "ollama"
    embeddings.assert_called_once_with(
        model=rag_service.OLLAMA_EMBEDDING_MODEL, base_url="http://ollama:11434"
    )
    assert rag_service.OLLAMA_EMBEDDING_MODEL == "qwen3-embedding:0.6b"


def test_ollama_branch_builds_the_qwen_subclass():
    from app.services import rag_service

    model = rag_service.get_embedding_model(
        SimpleNamespace(provider="ollama", ollama_url="http://ollama:11434")
    )
    assert isinstance(model, rag_service.ContactFreeEmbeddings)
    model = model.inner
    assert isinstance(model, rag_service.QwenOllamaEmbeddings)
    assert isinstance(model, rag_service.OllamaEmbeddings)
    assert model.model == "qwen3-embedding:0.6b"
    assert model.base_url == "http://ollama:11434"


def test_non_native_provider_requires_explicit_fallback(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_PROVIDER", "")
    with pytest.raises(rag_service.EmbeddingUnavailable, match="EMBEDDING_PROVIDER"):
        rag_service.get_embedding_provider(SimpleNamespace(provider="anthropic"))


def test_non_native_provider_uses_effective_collection_provider(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_PROVIDER", "ollama")
    settings = SimpleNamespace(provider="anthropic", ollama_url="http://ollama:11434")
    assert rag_service.get_embedding_provider(settings) == "ollama"


# ── Ollama base URL precedence ──────────────────────────────────────────────


def _ollama_base_url(rag_service, settings):
    with patch.object(rag_service, "QwenOllamaEmbeddings", return_value="ollama") as embeddings:
        rag_service.get_embedding_model(settings)
    return embeddings.call_args.kwargs["base_url"]


@pytest.mark.parametrize(
    "user_url,server_url,expected",
    [
        ("http://my-ollama:11434", "http://10.0.0.182:11434", "http://my-ollama:11434"),
        (None, "http://10.0.0.182:11434", "http://10.0.0.182:11434"),
        ("", "  http://10.0.0.182:11434  ", "http://10.0.0.182:11434"),
        (None, "", "http://localhost:11434"),
    ],
)
def test_ollama_user_base_url_precedence(monkeypatch, user_url, server_url, expected):
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_OLLAMA_URL", server_url)
    settings = SimpleNamespace(provider="ollama", ollama_url=user_url)
    assert _ollama_base_url(rag_service, settings) == expected


def test_fallback_ollama_uses_server_url_not_chat_settings(monkeypatch):
    # A DeepSeek model row has no usable ollama_url; the fallback must reach the
    # operator's Ollama instead of localhost inside the container.
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_OLLAMA_URL", "http://10.0.0.182:11434")
    settings = SimpleNamespace(
        provider="deepseek", ollama_url="http://ignored:11434", api_key_enc="enc"
    )
    assert _ollama_base_url(rag_service, settings) == "http://10.0.0.182:11434"


def test_fallback_ollama_defaults_to_localhost(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_OLLAMA_URL", "")
    settings = SimpleNamespace(provider="deepseek", ollama_url=None, api_key_enc="enc")
    assert _ollama_base_url(rag_service, settings) == "http://localhost:11434"


# ── Qwen3 query instruction ─────────────────────────────────────────────────


def _qwen(rag_service):
    return rag_service.QwenOllamaEmbeddings(
        model=rag_service.OLLAMA_EMBEDDING_MODEL, base_url="http://ollama:11434"
    )


def test_qwen_prefix_is_applied_to_queries_only():
    from app.services import rag_service

    emb = _qwen(rag_service)
    seen: list[list[str]] = []

    def fake_embed_documents(self, texts):
        seen.append(list(texts))
        return [[0.0] for _ in texts]

    with patch.object(rag_service.OllamaEmbeddings, "embed_documents", fake_embed_documents):
        emb.embed_query("Senior backend engineer")
        emb.embed_documents(["Built APIs in FastAPI", "Led a team"])

    task = rag_service.QWEN_QUERY_TASK
    assert seen[0] == [f"Instruct: {task}\nQuery:Senior backend engineer"]
    assert seen[1] == ["Built APIs in FastAPI", "Led a team"]


@pytest.mark.asyncio
async def test_qwen_prefix_is_applied_to_async_queries_only():
    from app.services import rag_service

    emb = _qwen(rag_service)
    seen: list[list[str]] = []

    async def fake_aembed_documents(self, texts):
        seen.append(list(texts))
        return [[0.0] for _ in texts]

    with patch.object(rag_service.OllamaEmbeddings, "aembed_documents", fake_aembed_documents):
        await emb.aembed_query("Data engineer")
        await emb.aembed_documents(["Wrote Spark jobs"])

    assert seen[0] == [f"Instruct: {rag_service.QWEN_QUERY_TASK}\nQuery:Data engineer"]
    assert seen[1] == ["Wrote Spark jobs"]


# ── Google ──────────────────────────────────────────────────────────────────


def test_google_uses_gemini_embedding_001_at_768_dims(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service, "decrypt_api_key", MagicMock(return_value="g-key"))
    settings = SimpleNamespace(provider="google", ollama_url=None, api_key_enc="enc")
    with patch.object(rag_service, "GeminiEmbeddings", return_value="google") as embeddings:
        assert rag_service.get_embedding_model(settings).inner == "google"
    embeddings.assert_called_once_with(model="models/gemini-embedding-001", google_api_key="g-key")
    assert rag_service.EMBEDDING_DIMENSIONS["google"] == 768
    assert rag_service.collection_name("u", "resume", "google") == "u_resume_google_768d"


@pytest.mark.parametrize("method", ["embed_query", "embed_documents"])
def test_gemini_embeddings_request_768_dims(method):
    from app.services import rag_service

    calls: list[dict] = []

    def fake(self, arg, **kwargs):
        calls.append(kwargs)
        return [0.0]

    with patch.object(rag_service.GoogleGenerativeAIEmbeddings, method, fake):
        emb = rag_service.GeminiEmbeddings.model_construct(model="models/gemini-embedding-001")
        getattr(emb, method)("text" if method == "embed_query" else ["text"])
        getattr(emb, method)(
            "text" if method == "embed_query" else ["text"], output_dimensionality=256
        )
    assert calls[0]["output_dimensionality"] == 768
    assert calls[1]["output_dimensionality"] == 256  # explicit value wins


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["aembed_query", "aembed_documents"])
async def test_gemini_embeddings_request_768_dims_async(method):
    from app.services import rag_service

    calls: list[dict] = []

    async def fake(self, arg, **kwargs):
        calls.append(kwargs)
        return [0.0]

    with patch.object(rag_service.GoogleGenerativeAIEmbeddings, method, fake):
        emb = rag_service.GeminiEmbeddings.model_construct(model="models/gemini-embedding-001")
        await getattr(emb, method)("text" if method == "aembed_query" else ["text"])
    assert calls[0]["output_dimensionality"] == 768


def test_dimension_map_only_lists_effective_embedding_providers():
    from app.services import rag_service

    assert set(rag_service.EMBEDDING_DIMENSIONS) == {"openai", "google", "ollama"}


# ── Fallback API keys (never reuse the chat provider's key) ─────────────────


def test_fallback_openai_never_reuses_the_chat_provider_key(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_PROVIDER", "openai")
    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_API_KEY", "")
    decrypt = MagicMock(return_value="deepseek-key")
    monkeypatch.setattr(rag_service, "decrypt_api_key", decrypt)
    settings = SimpleNamespace(provider="deepseek", ollama_url=None, api_key_enc="enc")
    with pytest.raises(rag_service.EmbeddingUnavailable, match="EMBEDDING_API_KEY"):
        rag_service.get_embedding_model(settings)
    decrypt.assert_not_called()

    monkeypatch.setattr(rag_service.app_settings, "EMBEDDING_API_KEY", "sk-embed")
    with patch.object(rag_service, "OpenAIEmbeddings", return_value="openai") as embeddings:
        assert rag_service.get_embedding_model(settings).inner == "openai"
    embeddings.assert_called_once_with(model="text-embedding-3-small", api_key="sk-embed")
    decrypt.assert_not_called()


def test_native_openai_still_uses_the_users_key(monkeypatch):
    from app.services import rag_service

    monkeypatch.setattr(rag_service, "decrypt_api_key", MagicMock(return_value="sk-user"))
    settings = SimpleNamespace(provider="openai", ollama_url=None, api_key_enc="enc")
    with patch.object(rag_service, "OpenAIEmbeddings", return_value="openai") as embeddings:
        assert rag_service.get_embedding_model(settings).inner == "openai"
    embeddings.assert_called_once_with(model="text-embedding-3-small", api_key="sk-user")
