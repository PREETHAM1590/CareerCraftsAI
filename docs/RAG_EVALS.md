# RAG retrieval evals

## What exists
- `backend/tests/evals/golden_set.json`: 8 synthetic documents (2 resumes, 2 job descriptions, 2 company notes, plus one resume and one company note containing prompt-injection text) and 33 queries. Relevance is `{doc, contains}`: a retrieved chunk is relevant if it comes from that document and contains the phrase, so it survives chunk-size changes. 4 queries have no relevant answer; 2 target injection text.
- `backend/tests/evals/harness.py`: runs the real `chunk_text` over the documents, embeds, and ranks by cosine similarity in memory (filtered by `doc_type`, as production collections are). It does not touch Postgres/pgvector, so SQL scoping is covered by `backend/tests/unit/test_rag_hardening.py`, not here.
- Metrics: recall@k (share of answerable queries with a relevant chunk in the top k), precision@k, MRR, `no_answer_rate` (share of unanswerable queries that correctly return nothing above `min_similarity`) and `false_abstain_rate`.

## Run
Offline, deterministic (hashed bag-of-words stub, no network or keys); also run in CI:

    cd backend && pytest tests/evals -v
    python -m tests.evals.harness    # prints metrics; needs APP_SECRET_KEY, DATABASE_URL, REDIS_URL set

Real embeddings (opt-in; the test is skipped when unset): set `OPENAI_API_KEY` (text-embedding-3-small) or `EVAL_OLLAMA_URL` (qwen3-embedding), then `pytest tests/evals -k real` or `python -m tests.evals.harness --real`. `EVAL_MIN_SIMILARITY` overrides the abstention threshold (stub 0.1, real 0.3; the real value is a placeholder to calibrate).

## Interpreting scores
Stub scores (k=3, min_similarity 0.1): recall@3 1.00, precision@3 0.33, MRR 0.98, no_answer_rate 1.00, false_abstain_rate 0.00. The stub only matches shared words, so these validate chunking and pipeline plumbing, not semantic quality. The threshold was chosen on this same small set. Real-model scores have not been measured.

## Adding cases
Add a document (`id`, `doc_type`, `paragraphs`) and queries (`id`, `doc_type`, `query`, `relevant: [{doc, contains}]`; empty list for a no-answer query; `"injection": true` for injection checks) to `golden_set.json`. `test_golden_set_is_well_formed` fails if a phrase matches no chunk. Add hard negatives that share vocabulary with the target.

## Prompt safety
Agent prompts insert retrieved chunks through `untrusted()` in `backend/app/agents/prompts/__init__.py`, which neutralises fence-closing lines and tags inside the chunk text. Agents must use it when inserting retrieved text into prompts.
