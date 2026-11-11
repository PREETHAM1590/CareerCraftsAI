import pytest

from app.services.rag_service import format_context
from tests.evals.harness import (
    HashedBagOfWordsEmbeddings,
    build_index,
    evaluate,
    load_golden,
    real_embeddings_from_env,
    search,
)


def test_golden_set_is_well_formed():
    golden = load_golden()
    doc_ids = {d["id"] for d in golden["documents"]}
    index = build_index(golden, HashedBagOfWordsEmbeddings())
    for q in golden["queries"]:
        for rel in q["relevant"]:
            assert rel["doc"] in doc_ids
            assert any(c.doc_id == rel["doc"] and rel["contains"] in c.text for c, _ in index), q[
                "id"
            ]
    assert sum(1 for q in golden["queries"] if not q["relevant"]) >= 3


def test_stub_embedder_is_deterministic():
    emb = HashedBagOfWordsEmbeddings()
    assert emb.embed_query("Kafka pipeline") == emb.embed_query("Kafka pipeline")
    assert emb.embed_query("the of and") == [0.0] * emb.dimensions


def test_stub_retrieval_metrics_meet_thresholds():
    report = evaluate(HashedBagOfWordsEmbeddings())
    assert report["recall@3"] >= 0.9
    assert report["mrr"] >= 0.85
    assert report["no_answer_rate"] >= 0.75
    assert report["false_abstain_rate"] <= 0.1


def test_injection_text_is_returned_only_as_delimited_data():
    emb = HashedBagOfWordsEmbeddings()
    golden = load_golden()
    index = build_index(golden, emb)
    for q in (q for q in golden["queries"] if q.get("injection")):
        hits = search(index, emb, q["doc_type"], q["query"], 3, 0.1)
        assert any(q["relevant"][0]["contains"] in c.text for _, c in hits)
        block = format_context([c.text for _, c in hits])
        assert block.startswith("<retrieved_context")
        assert block.count("</retrieved_context>") == 1
        assert block.endswith("</retrieved_context>")


@pytest.mark.skipif(
    real_embeddings_from_env() is None,
    reason="set OPENAI_API_KEY or EVAL_OLLAMA_URL to run the real-embedding eval",
)
def test_real_embedding_metrics():
    report = evaluate(real_embeddings_from_env(), min_similarity=0.3)
    assert report["recall@3"] >= 0.8
    assert report["mrr"] >= 0.6
