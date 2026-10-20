import logging
import re

from langchain_core.documents import Document
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_ollama import OllamaEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings as app_settings
from app.core.security import decrypt_api_key

logger = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    pass


OLLAMA_EMBEDDING_MODEL = "qwen3-embedding:0.6b"
GOOGLE_EMBEDDING_MODEL = "models/gemini-embedding-001"
GOOGLE_EMBEDDING_DIMENSIONS = 768
# Qwen3-Embedding is instruction-aware: queries carry a one-line task, documents
# are embedded bare (per the model card).
QWEN_QUERY_TASK = (
    "Given a job description or role, retrieve relevant passages from the candidate's documents"
)
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Vector size per *effective* embedding provider -- collection_name() is always
# called with get_embedding_provider()'s result (openai, google or ollama),
# never with a chat-only provider such as anthropic or deepseek.
EMBEDDING_DIMENSIONS: dict[str, int] = {
    "openai": 1536,  # text-embedding-3-small
    "google": GOOGLE_EMBEDDING_DIMENSIONS,  # gemini-embedding-001, truncated to 768
    "ollama": 1024,  # qwen3-embedding:0.6b
}


class QwenOllamaEmbeddings(OllamaEmbeddings):
    """OllamaEmbeddings that adds Qwen3-Embedding's query instruction."""

    query_task: str = QWEN_QUERY_TASK

    def _instruct(self, text: str) -> str:
        return f"Instruct: {self.query_task}\nQuery:{text}"

    def embed_query(self, text: str) -> list[float]:
        return super().embed_query(self._instruct(text))

    async def aembed_query(self, text: str) -> list[float]:
        return await super().aembed_query(self._instruct(text))


class GeminiEmbeddings(GoogleGenerativeAIEmbeddings):
    """gemini-embedding-001 truncated to GOOGLE_EMBEDDING_DIMENSIONS.

    langchain-google-genai 2.1.x only takes output_dimensionality per call, so
    it is defaulted here to keep the existing 768-d google collections valid.
    """

    def embed_documents(self, texts, **kwargs):
        kwargs.setdefault("output_dimensionality", GOOGLE_EMBEDDING_DIMENSIONS)
        return super().embed_documents(texts, **kwargs)

    def embed_query(self, text, **kwargs):
        kwargs.setdefault("output_dimensionality", GOOGLE_EMBEDDING_DIMENSIONS)
        return super().embed_query(text, **kwargs)

    async def aembed_documents(self, texts, **kwargs):
        kwargs.setdefault("output_dimensionality", GOOGLE_EMBEDDING_DIMENSIONS)
        return await super().aembed_documents(texts, **kwargs)

    async def aembed_query(self, text, **kwargs):
        kwargs.setdefault("output_dimensionality", GOOGLE_EMBEDDING_DIMENSIONS)
        return await super().aembed_query(text, **kwargs)


def collection_name(user_id: str, doc_type: str, provider: str = "openai") -> str:
    """Generate collection name namespaced by user, doc_type, and embedding provider.

    Provider namespacing prevents dimension mismatch when users switch between
    providers with different embedding dimensions (e.g., OpenAI 1536-d vs Google 768-d).
    """
    # Map provider to dimension to ensure collections are separated by embedding size
    dimension = EMBEDDING_DIMENSIONS.get(provider, 768)
    return f"{user_id}_{doc_type}_{provider}_{dimension}d"


def extract_text(content: bytes, filename: str) -> str:
    # The filename is descriptive only; verified bytes select the parser.
    from app.services.document_parsing import extract_verified_text, sniff_content_type

    content_type = sniff_content_type(content)
    if content_type is None:
        raise ValueError("Unsupported or unsafe document")
    return extract_verified_text(content, content_type)


def chunk_text(text: str) -> list[str]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=app_settings.RAG_CHUNK_SIZE, chunk_overlap=app_settings.RAG_CHUNK_OVERLAP
    )
    return splitter.split_text(text)


def _relevant_chunks(chunks: list[str], query: str, k: int) -> list[str]:
    """The k chunks sharing the most words with the query, kept in resume order.

    Taking the first k cut a two-page resume off after ~4K characters, so later
    roles, education and skills never reached the prompt."""
    if len(chunks) <= k:
        return chunks
    words = {w for w in re.findall(r"[a-z0-9+#]+", query.lower()) if len(w) > 2}

    def overlap(chunk: str) -> int:
        return len(words & set(re.findall(r"[a-z0-9+#]+", chunk.lower())))

    best = sorted(range(len(chunks)), key=lambda i: overlap(chunks[i]), reverse=True)[:k]
    return [chunks[i] for i in sorted(best)]


def get_embedding_model(model_settings):
    """Embeddings for the user's active model, or the deployment fallback.

    Providers without an embeddings API (DeepSeek, Anthropic, OpenRouter,
    NVIDIA NIM) use EMBEDDING_PROVIDER. The fallback never reuses the chat
    provider's API key -- a DeepSeek key sent to OpenAI would just 401 -- so
    it needs its own EMBEDDING_API_KEY (openai/google) or EMBEDDING_OLLAMA_URL.
    """
    provider = get_embedding_provider(model_settings)
    native = model_settings.provider == provider
    if provider in {"openai", "google"}:
        if native:
            api_key = decrypt_api_key(model_settings.api_key_enc, app_settings.APP_SECRET_KEY)
        else:
            api_key = app_settings.EMBEDDING_API_KEY.strip()
            if not api_key:
                raise EmbeddingUnavailable(
                    f"EMBEDDING_PROVIDER={provider} needs EMBEDDING_API_KEY for "
                    f"'{model_settings.provider}' models."
                )
        if provider == "openai":
            return OpenAIEmbeddings(model="text-embedding-3-small", api_key=api_key)
        return GeminiEmbeddings(model=GOOGLE_EMBEDDING_MODEL, google_api_key=api_key)
    if provider == "ollama":
        # The user's own Ollama when they chose it; otherwise the operator's
        # EMBEDDING_OLLAMA_URL (a trusted server setting, so not subject to the
        # per-user OLLAMA_ALLOWED_HOSTS check).
        base_url = (
            (model_settings.ollama_url if native else None)
            or app_settings.EMBEDDING_OLLAMA_URL.strip()
            or DEFAULT_OLLAMA_URL
        )
        return QwenOllamaEmbeddings(model=OLLAMA_EMBEDDING_MODEL, base_url=base_url)
    raise EmbeddingUnavailable(f"Unsupported embedding provider: {provider}")


def get_embedding_provider(model_settings) -> str:
    provider = model_settings.provider
    if provider in {"openai", "google", "ollama"}:
        return provider
    fallback = app_settings.EMBEDDING_PROVIDER.strip().lower()
    if fallback not in {"openai", "google", "ollama"}:
        raise EmbeddingUnavailable(
            f"Provider '{provider}' has no embeddings API and EMBEDDING_PROVIDER is unset. "
            "Set EMBEDDING_PROVIDER to openai, google, or ollama."
        )
    return fallback


def _psycopg_url() -> str:
    """Convert asyncpg URL to psycopg3 URL for langchain-postgres PGVector."""
    url = app_settings.DATABASE_URL
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+psycopg")
    if "postgresql://" in url and "+psycopg" not in url:
        logger.warning(
            "DATABASE_URL does not contain '+asyncpg' driver prefix. "
            "Assuming psycopg3 compatibility — verify your connection string."
        )
        return url.replace("postgresql://", "postgresql+psycopg://")
    return url


def get_vector_store(user_id: str, doc_type: str, embeddings, provider: str = "openai"):
    """Get or create PGVector store for a user+doc_type+provider collection.

    Provider is included in the collection name to prevent dimension mismatch
    when users switch between embedding providers.
    """
    from langchain_postgres import PGVector

    table = collection_name(user_id, doc_type, provider)
    return PGVector(
        connection=_psycopg_url(),
        collection_name=table,
        embeddings=embeddings,
        use_jsonb=True,
    )


def _ensure_hnsw_index() -> None:
    """Index each supported dimension; retrieval uses the same cast/predicate."""
    from sqlalchemy import text

    from app.core.sync_db import _get_sync_factory

    with _get_sync_factory()() as db:
        for dimension in sorted(set(EMBEDDING_DIMENSIONS.values())):
            db.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_langchain_embedding_hnsw_{dimension}
                ON public.langchain_pg_embedding
                USING hnsw ((embedding::vector({dimension})) vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE vector_dims(embedding) = {dimension}
            """))
        db.commit()


def _search_live_documents(user_id, doc_type, provider, embeddings, query, k):
    """Legacy/unowned/orphaned vectors never enter a model prompt."""
    from sqlalchemy import text

    from app.core.sync_db import _get_sync_factory

    dimension = EMBEDDING_DIMENSIONS[provider]
    vector = embeddings.embed_query(query)
    if len(vector) != dimension:
        raise ValueError("Embedding dimension mismatch")
    # Company research is persisted in company_intel, not user_documents.
    # The trusted type selects a fixed SQL clause; IDs and ownership stay bound.
    live_record = (
        """SELECT 1 FROM company_intel d
           WHERE d.id::text = e.cmetadata->>'company_intel_id'
             AND d.user_id::text = :owner"""
        if doc_type == "company"
        else """SELECT 1 FROM user_documents d
           WHERE d.id::text = e.cmetadata->>'document_id'
             AND d.user_id::text = :owner AND d.doc_type = :doc_type"""
    )
    with _get_sync_factory()() as db:
        rows = db.execute(
            text(f"""
            SELECT e.document, e.cmetadata
            FROM langchain_pg_embedding e
            JOIN langchain_pg_collection c ON c.uuid = e.collection_id
            WHERE c.name = :collection
              AND vector_dims(e.embedding) = {dimension}
              AND e.cmetadata->>'user_id' = :owner
              AND EXISTS (
                  {live_record}
              )
            ORDER BY e.embedding::vector({dimension}) <=> CAST(:query AS vector({dimension}))
            LIMIT :limit
        """),  # noqa: S608 # nosec B608
            {
                "collection": collection_name(user_id, doc_type, provider),
                "owner": str(user_id),
                "doc_type": doc_type,
                "query": "[" + ",".join(str(float(v)) for v in vector) + "]",
                "limit": max(1, min(k, 100)),
            },
        ).all()
    return [Document(page_content=row.document, metadata=row.cmetadata or {}) for row in rows]


def ingest_document(
    user_id: str,
    doc_type: str,
    text: str,
    metadata: dict,
    model_settings,
) -> int:
    """Chunk, embed, store. Returns chunk count."""
    if not metadata.get("document_id"):
        raise ValueError("document_id is required for owned vector ingestion")
    metadata = {**metadata, "user_id": str(user_id), "doc_type": doc_type}
    chunks = chunk_text(text)
    embeddings = get_embedding_model(model_settings)
    docs = [
        Document(page_content=chunk, metadata={**metadata, "chunk_index": i})
        for i, chunk in enumerate(chunks)
    ]
    provider = get_embedding_provider(model_settings)
    store = get_vector_store(user_id, doc_type, embeddings, provider=provider)
    store.add_documents(
        docs,
        ids=[f"{metadata['document_id']}:{provider}:{i}" for i in range(len(docs))],
    )
    _ensure_hnsw_index()
    return len(docs)


def retrieve(
    user_id: str,
    doc_type: str,
    query: str,
    model_settings,
    k: int | None = None,
) -> list[Document]:
    """Retrieve top-k relevant chunks."""
    k = app_settings.RAG_TOP_K if k is None else k
    if doc_type == "resume":
        # A resume the member picked for this run replaces similarity search:
        # Restrict context to that selection regardless of similarity ranking.
        from app.core.sync_db import fetch_chosen_resume

        chosen = fetch_chosen_resume(user_id)
        if chosen is not None and chosen.raw_text:
            return [
                Document(page_content=c, metadata={"doc_type": "resume"})
                for c in _relevant_chunks(chunk_text(chosen.raw_text), query, k)
            ]
    try:
        embeddings = get_embedding_model(model_settings)
        return _search_live_documents(
            user_id,
            doc_type,
            get_embedding_provider(model_settings),
            embeddings,
            query,
            k,
        )
    except Exception as exc:
        logger.warning("Vector retrieval failed for %s/%s: %s", user_id, doc_type, exc)
        if doc_type == "resume":
            from app.core.sync_db import fetch_user_profile_text

            profile_text = fetch_user_profile_text(user_id)
            if profile_text:
                return [
                    Document(
                        page_content=profile_text,
                        metadata={
                            "fallback": "raw_resume",
                            "rag_unavailable": True,
                            "doc_type": doc_type,
                        },
                    )
                ]
        return []
