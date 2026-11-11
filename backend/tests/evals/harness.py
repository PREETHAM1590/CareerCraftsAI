"""Offline retrieval eval: real chunker + in-memory cosine search over a golden set.

The Postgres search in rag_service is not exercised here; embeddings are any
object with embed_documents/embed_query, so a deterministic stub (default) or
the real provider can be plugged in.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.rag_service import chunk_text

GOLDEN_PATH = Path(__file__).parent / "golden_set.json"
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the to was we "
    "which with you your will our did what how when who after across".split()
)


class HashedBagOfWordsEmbeddings:
    """Deterministic, network-free embeddings: signed hashed unigram counts, L2-normalised."""

    def __init__(self, dimensions: int = 512):
        self.dimensions = dimensions

    @staticmethod
    def _tokens(text: str) -> list[str]:
        out = []
        for word in re.findall(r"[a-z0-9+#]+", text.lower()):
            if word in _STOPWORDS:
                continue
            if len(word) > 3 and word.endswith("s"):
                word = word[:-1]
            out.append(word)
        return out

    def embed_query(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token in self._tokens(text):
            digest = hashlib.md5(token.encode(), usedforsecurity=False).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    doc_type: str
    text: str


def load_golden(path: Path = GOLDEN_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_index(golden: dict, embeddings) -> list[tuple[Chunk, list[float]]]:
    chunks = [
        Chunk(doc["id"], doc["doc_type"], piece)
        for doc in golden["documents"]
        for piece in chunk_text("\n\n".join(doc["paragraphs"]))
    ]
    vectors = embeddings.embed_documents([c.text for c in chunks])
    return list(zip(chunks, vectors, strict=True))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search(index, embeddings, doc_type: str, query: str, k: int, min_similarity: float):
    """Top-k chunks of one doc type, best first, dropping those below min_similarity."""
    qv = embeddings.embed_query(query)
    scored = sorted(
        ((_cosine(qv, vec), chunk) for chunk, vec in index if chunk.doc_type == doc_type),
        key=lambda pair: (-pair[0], pair[1].doc_id, pair[1].text),
    )
    return [(s, c) for s, c in scored[:k] if s >= min_similarity]


def is_relevant(chunk: Chunk, relevant: list[dict]) -> bool:
    return any(r["doc"] == chunk.doc_id and r["contains"] in chunk.text for r in relevant)


def evaluate(embeddings, k: int = 3, min_similarity: float = 0.1, golden: dict | None = None):
    """Recall@k, precision@k and MRR over answerable queries; abstention on unanswerable ones.

    recall is the share of queries with a relevant chunk in the top k (hit rate).
    precision@k divides relevant hits by k. no_answer_rate is the share of
    unanswerable queries that correctly return nothing above min_similarity;
    false_abstain_rate is the share of answerable queries that return nothing.
    """
    golden = golden or load_golden()
    index = build_index(golden, embeddings)
    hits = precision = rr = answerable = abstain_ok = unanswerable = false_abstain = 0
    per_query = {}
    for q in golden["queries"]:
        results = search(index, embeddings, q["doc_type"], q["query"], k, min_similarity)
        flags = [is_relevant(c, q["relevant"]) for _, c in results]
        per_query[q["id"]] = {"retrieved": len(results), "relevant_flags": flags}
        if not q["relevant"]:
            unanswerable += 1
            abstain_ok += not results
            continue
        answerable += 1
        false_abstain += not results
        hits += any(flags)
        precision += sum(flags) / k
        rank = next((i for i, f in enumerate(flags, 1) if f), None)
        rr += 1 / rank if rank else 0.0
    return {
        "k": k,
        "min_similarity": min_similarity,
        "queries_answerable": answerable,
        "queries_unanswerable": unanswerable,
        f"recall@{k}": hits / answerable,
        f"precision@{k}": precision / answerable,
        "mrr": rr / answerable,
        "no_answer_rate": abstain_ok / unanswerable if unanswerable else None,
        "false_abstain_rate": false_abstain / answerable,
        "per_query": per_query,
    }


def real_embeddings_from_env():
    """Real provider embeddings from env vars, or None when no credentials are set."""
    import os

    if os.environ.get("OPENAI_API_KEY"):
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model="text-embedding-3-small")
    if os.environ.get("EVAL_OLLAMA_URL"):
        from app.services.rag_service import OLLAMA_EMBEDDING_MODEL, QwenOllamaEmbeddings

        return QwenOllamaEmbeddings(
            model=OLLAMA_EMBEDDING_MODEL, base_url=os.environ["EVAL_OLLAMA_URL"]
        )
    return None


if __name__ == "__main__":
    import os
    import sys

    real = "--real" in sys.argv
    emb = real_embeddings_from_env() if real else HashedBagOfWordsEmbeddings()
    if emb is None:
        sys.exit("--real needs OPENAI_API_KEY or EVAL_OLLAMA_URL")
    min_sim = float(os.environ.get("EVAL_MIN_SIMILARITY", "0.1" if not real else "0.3"))
    report = evaluate(emb, k=3, min_similarity=min_sim)
    report.pop("per_query")
    print(json.dumps(report, indent=2))
