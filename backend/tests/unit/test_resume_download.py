"""GET /resume/download/{id}: which tailored resumes are re-rendered."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient


def _doc(user_id, ats_data):
    from app.models.db import UserDocument

    return UserDocument(
        id=uuid.uuid4(),
        user_id=user_id,
        doc_type="resume_tailored",
        filename="resume.pdf",
        storage_path="u/resume.pdf",
        raw_text="# Jane Doe\n## SKILLS\nPython",
        ats_data=ats_data,
    )


async def _download(monkeypatch, doc, *, render=None, pinned=False, params=None):
    from app.api.v1.deps import get_current_user, get_db
    from app.main import app
    from app.models.db import User

    calls = {"render": [], "stored": 0}
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "app.api.v1.resume._pinned_by_pending_approval", AsyncMock(return_value=pinned)
    )

    def fake_render(text, full_name="", template="modern"):
        calls["render"].append(template)
        if render is not None:
            return render(text, full_name, template)
        return b"%PDF-rendered"

    def fake_stored(path, user_id):
        calls["stored"] += 1
        return b"%PDF-stored"

    monkeypatch.setattr("app.core.clerk_auth.verify_token", lambda token: {"sub": "user-1"})
    monkeypatch.setattr("app.services.pdf_service.generate_resume_pdf", fake_render)
    monkeypatch.setattr("app.services.storage_service.download_file", fake_stored)

    class _Result:
        def scalar_one_or_none(self):
            return doc

    class _FakeDB:
        async def execute(self, *a, **k):
            return _Result()

    async def _fake_db():
        return _FakeDB()

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id=doc.user_id, email="a@b.com", full_name="Jane Doe"
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/resume/download/{doc.id}",
                headers={"Authorization": "Bearer t"},
                params=params,
            )
    finally:
        app.dependency_overrides.clear()
    return resp, calls


@pytest.mark.asyncio
async def test_pinned_pdf_serves_exact_stored_bytes(monkeypatch):
    doc = _doc(uuid.uuid4(), {"template": "classic", "page_target": 1})
    response, calls = await _download(monkeypatch, doc, pinned=True)
    assert response.content == b"%PDF-stored"
    assert calls["render"] == [] and calls["stored"] == 1


@pytest.mark.asyncio
async def test_page_query_coerces_strings_and_rejects_out_of_range(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.services.resume_export.fit_resume",
        lambda *args: SimpleNamespace(pdf=b"%PDF-fitted", page_count=1),
    )
    doc = _doc(uuid.uuid4(), {"template": "classic"})
    for pages in ("1", "2"):
        response, _ = await _download(monkeypatch, doc, params={"pages": pages})
        assert response.status_code == 200 and response.content == b"%PDF-fitted"
    for pages in ("0", "3", "one"):
        response, _ = await _download(monkeypatch, doc, params={"pages": pages})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_rerenders_with_the_recorded_template(monkeypatch):
    doc = _doc(uuid.uuid4(), {"template": "classic"})
    resp, calls = await _download(monkeypatch, doc)
    assert resp.status_code == 200
    assert resp.content == b"%PDF-rendered"
    assert calls["render"] == ["classic"]
    assert calls["stored"] == 0


@pytest.mark.asyncio
async def test_legacy_document_without_template_serves_stored_pdf(monkeypatch):
    # Documents saved before the template was recorded must not be silently
    # re-rendered as "modern".
    doc = _doc(uuid.uuid4(), {"keywords_matched": []})
    resp, calls = await _download(monkeypatch, doc)
    assert resp.status_code == 200
    assert resp.content == b"%PDF-stored"
    assert calls["render"] == []


@pytest.mark.asyncio
async def test_render_failure_falls_back_to_stored_pdf(monkeypatch):
    def boom(*_):
        raise RuntimeError("layout error")

    doc = _doc(uuid.uuid4(), {"template": "modern"})
    resp, calls = await _download(monkeypatch, doc, render=boom)
    assert resp.status_code == 200
    assert resp.content == b"%PDF-stored"
    assert calls["render"] == ["modern"]
