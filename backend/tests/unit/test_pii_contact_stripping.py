"""Contact details stay out of embeddings, stored chunks and logs."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import pii_vault
from app.core.pii_vault import strip_contact_details
from app.services import rag_service
from tests.fixtures.indian_resume import verhoeff_check_digit

EMAIL = "preetham.v@gmail.com"
PHONE = "+91 98765 43210"
AADHAAR = "23456789012" + verhoeff_check_digit("23456789012")
RESUME = (
    "PREETHAM VENKATESH\n"
    f"Email: {EMAIL} | Phone: {PHONE} | linkedin.com/in/preetham-venkatesh | Bengaluru\n"
    f"Aadhaar: {AADHAAR}\n"
    "Senior Python engineer at Infosys. Contact the team at careers@acme.in."
)
SECRETS = (EMAIL, "98765", "preetham-venkatesh", AADHAAR)


def test_strip_removes_contact_values_and_their_labels():
    out = strip_contact_details(RESUME)
    assert not [s for s in SECRETS if s in out]
    lines = out.splitlines()
    assert lines[0] == "PREETHAM VENKATESH"
    assert lines[1] == "Bengaluru"  # "Email:", "Phone:" and the links are gone
    assert "Aadhaar" not in out  # a line left holding only its label is dropped
    assert "Infosys" in out and "careers@acme.in" in out  # shared mailboxes stay


def test_strip_leaves_text_without_contact_details_untouched():
    text = "Led a team of 5 | Python, FastAPI\n\nShipped 3 services in 2024."
    assert strip_contact_details(text) == text


class RecordingEmbeddings:
    model = "text-embedding-3-small"

    def __init__(self):
        self.seen: list[str] = []

    def embed_documents(self, texts):
        self.seen.extend(texts)
        return [[0.1] for _ in texts]

    def embed_query(self, text):
        self.seen.append(text)
        return [0.1]

    async def aembed_query(self, text):
        return self.embed_query(text)

    async def aembed_documents(self, texts):
        return self.embed_documents(texts)


@pytest.mark.asyncio
async def test_embedding_provider_never_receives_contact_details():
    inner = RecordingEmbeddings()
    model = rag_service.ContactFreeEmbeddings(inner)
    model.embed_documents([RESUME, f"{EMAIL}\n{PHONE}"])
    model.embed_query(f"Python engineer {EMAIL}")
    await model.aembed_query(RESUME)
    await model.aembed_documents([RESUME])
    assert not [s for seen in inner.seen for s in SECRETS if s in seen]
    assert all(seen.strip() for seen in inner.seen)  # never an empty input
    assert model.model == "text-embedding-3-small"  # job_matching reads this


def test_ingest_stores_and_embeds_chunks_without_contact_details(monkeypatch):
    inner = RecordingEmbeddings()
    store = MagicMock()
    monkeypatch.setattr(rag_service, "_provider_embedding_model", lambda _: inner)
    monkeypatch.setattr(rag_service, "get_embedding_provider", lambda _: "openai")
    monkeypatch.setattr(rag_service, "_ensure_hnsw_index", lambda: None)
    captured = {}

    def vector_store(user_id, doc_type, embeddings, provider):
        captured["embeddings"] = embeddings
        return store

    monkeypatch.setattr(rag_service, "get_vector_store", vector_store)
    count = rag_service.ingest_document(
        "u1", "resume", RESUME, {"document_id": "d1"}, SimpleNamespace(provider="openai")
    )
    docs = store.add_documents.call_args.args[0]
    assert count == len(docs) >= 1
    stored = "\n".join(d.page_content for d in docs)
    assert not [s for s in SECRETS if s in stored]
    assert "Infosys" in stored
    assert isinstance(captured["embeddings"], rag_service.ContactFreeEmbeddings)


@pytest.fixture
def scrubbing():
    previous = logging.getLogRecordFactory()
    pii_vault.install_pii_log_scrubbing()
    pii_vault.install_pii_log_scrubbing()  # idempotent
    yield
    logging.setLogRecordFactory(previous)


def test_installed_scrubbing_cleans_every_record(scrubbing, caplog):
    log = logging.getLogger("app.some.module")
    with caplog.at_level(logging.INFO):
        log.info("Applied for %s (%s)", EMAIL, PHONE)
        try:
            raise RuntimeError(f"bad Aadhaar {AADHAAR}")
        except RuntimeError:
            log.exception("failed for %s", EMAIL)
    text = caplog.text
    assert not [s for s in SECRETS if s in text]
    assert "[EMAIL]" in text and "[PHONE]" in text and "[AADHAAR]" in text


def test_entry_points_install_log_scrubbing():
    from pathlib import Path

    app = Path(rag_service.__file__).resolve().parents[1]
    for entry in ("main.py", "notification_worker.py", "temporal_worker.py"):
        assert "install_pii_log_scrubbing()" in (app / entry).read_text(encoding="utf-8"), entry
