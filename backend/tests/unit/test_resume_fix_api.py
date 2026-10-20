"""GET /resume/tailored/{id} and POST /resume/tailored/{id}/fix."""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from reportlab.platypus.doctemplate import LayoutError

SPARSE_RESUME = """# Jane Doe
jane@example.com
## SUMMARY
AI engineer building LLM tools.
## EXPERIENCE
### Prompt Engineer Intern | Agentic Universe (Qultured Media Pvt.
- Built prompt evaluation pipelines in Python
## SKILLS
**Languages:** Python, SQL
"""


@pytest.mark.asyncio
async def test_optimize_uses_durable_dispatch_and_preserves_response(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.api.v1.deps import get_current_user, get_db

    user = SimpleNamespace(id=uuid.uuid4(), full_name="Jane")
    db = object()
    app, _ = _build_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    queue = AsyncMock(return_value=str(uuid.uuid4()))
    wait = AsyncMock(
        return_value=SimpleNamespace(status="awaiting_approval", output={"ats_score": 85})
    )
    monkeypatch.setattr("app.api.v1.run_utils.queue_agent_run", queue)
    monkeypatch.setattr("app.api.v1.run_utils.wait_for_agent_result", wait)
    monkeypatch.setattr("app.api.v1.resume._contact_suggestions", AsyncMock(return_value={}))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/v1/resume/optimize", json={"jd_text": "Python Engineer"})
    assert response.status_code == 200
    assert response.json()["ats_score"] == 85
    queue.assert_awaited_once_with(
        db,
        user,
        "resume_optimize",
        {"jd_text": "Python Engineer", "template": "modern", "page_target": 2},
    )
    wait.assert_awaited_once_with(db, user, queue.return_value)


@pytest.mark.asyncio
async def test_manual_edit_without_jd_recomputes_and_survives_reload(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)
    edited = SPARSE_RESUME + "\n## EDUCATION\nBSc Computer Science, Pune University, 2020\n"
    response = await h.fix({"resume_markdown": edited, "remember": False})
    assert response.status_code == 200
    saved = response.json()
    assert saved["ats_score"] != 70
    assert saved["estimate"]["mode"] == "general"
    assert saved["content_version"] == saved["estimate"]["content_version"]
    reloaded = (await h.get()).json()
    assert reloaded["resume_markdown"] == saved["resume_markdown"]
    assert reloaded["estimate"] == saved["estimate"]


@pytest.mark.asyncio
async def test_outdated_full_text_edit_fails_before_storage(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)
    response = await h.fix(
        {"resume_markdown": SPARSE_RESUME + "New text", "expected_version": "0" * 64}
    )
    assert response.status_code == 409
    assert doc.raw_text == SPARSE_RESUME
    assert h.uploaded == [] and h.saved_facts == []


FULL_EMPLOYER = "Agentic Universe (Qultured Media Pvt. Ltd.)"
FIXED_HEADING = f"### Prompt Engineer Intern | {FULL_EMPLOYER} | Remote | Jun 2025 - Present"

FULL_FIX = {
    "contact": {"phone": "+91 98765 43210", "location": "Pune, India"},
    "experience": [
        {
            "index": 0,
            "employer": FULL_EMPLOYER,
            "location": "Remote",
            "start": "Jun 2025",
            "end": "Present",
        }
    ],
    "education": [
        {
            "degree": "B.Tech Computer Science",
            "institution": "Savitribai Phule Pune University",
            "start": "Aug 2021",
            "end": "May 2025",
        }
    ],
}

DATES_WARNING = "Employment dates were not provided for the internship."
EDUCATION_WARNING = "No education section was found in the source resume."
AZURE_WARNING = "The JD asks for Azure experience, which the resume does not show."


def _doc(user_id, *, raw_text=SPARSE_RESUME, ats_data=None):
    from app.models.db import UserDocument

    return UserDocument(
        id=uuid.uuid4(),
        user_id=user_id,
        doc_type="resume_tailored",
        filename="resume.pdf",
        storage_path="u/resume.pdf",
        raw_text=raw_text,
        ats_score=70,
        ats_data={"template": "modern", "summary": "Tailored.", **(ats_data or {})},
    )


def _build_app() -> tuple[FastAPI, bool]:
    """Exercise the resume router without importing unrelated application agents."""
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    from app.api.v1.resume import router
    from app.core.rate_limit import limiter

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(router, prefix="/api/v1")
    return app, False


class _Harness:
    def __init__(self, monkeypatch, doc, *, user_id=None, facts_row=None):
        self.doc = doc
        self.user_id = user_id or (doc.user_id if doc is not None else uuid.uuid4())
        self.facts_row = facts_row
        self.uploaded: list[tuple] = []
        self.deleted: list[tuple] = []
        self.saved_facts: list[tuple] = []
        self.rendered: list[str] = []
        self.ats_calls: list[tuple] = []
        self.added: list = []
        # Ordered log of storage and session calls, to check commit ordering.
        self.calls: list = []
        self.flushes = 0
        self.commit_error: Exception | None = None
        # Models whose pending-approval lookup reports a pin. What those
        # queries match is checked structurally and against Postgres.
        self.pins: set[str] = set()
        # Whether each document lookup locked the row (SELECT ... FOR UPDATE).
        self.doc_locks: list[bool] = []
        self.deletion_scheduled_for = None
        self.render_error: Exception | None = None
        self.ats_error: Exception | None = None

        from app.core.rate_limit import limiter
        from app.services import pdf_service

        limiter.reset()
        real_render = pdf_service.generate_resume_pdf

        def render(text, full_name="", template="modern"):
            if self.render_error is not None:
                raise self.render_error
            self.rendered.append(template)
            return real_render(text, full_name=full_name, template=template)

        def upload_file(user_id, filename, data, content_type):
            assert data.startswith(b"%PDF")
            self.uploaded.append((user_id, filename, content_type))
            self.calls.append(("upload", "user-id/new.pdf"))
            return "user-id/new.pdf"

        def delete_file(path, user_id):
            self.deleted.append((path, user_id))
            self.calls.append(("delete", path))

        async def load_facts_row(db, user_id, *, for_update=False):
            return self.facts_row

        async def load_profile(db, user_id):
            return None

        async def save_facts(db, user_id, *, contact, experience, education):
            # The real merge (pure); the locking read/write is covered by
            # test_resume_facts and the Postgres integration tests.
            from app.services.resume_facts import merge_facts

            stored = dict(self.facts_row.answer or {}) if self.facts_row else {}
            facts = merge_facts(stored, contact=contact, experience=experience, education=education)
            self.saved_facts.append((user_id, facts))
            self.calls.append("save_facts")
            return facts

        class _Ats:
            composite_score = 91
            missing_keywords = ["Azure"]

        def compute_ats_score(resume_text, jd_text):
            if self.ats_error is not None:
                raise self.ats_error
            self.ats_calls.append((resume_text, jd_text))
            return _Ats()

        monkeypatch.setattr("app.services.pdf_service.generate_resume_pdf", render)
        monkeypatch.setattr("app.services.storage_service.upload_file", upload_file)
        monkeypatch.setattr("app.services.storage_service.delete_file", delete_file)
        monkeypatch.setattr("app.services.resume_facts.load_facts_row", load_facts_row)
        monkeypatch.setattr("app.services.resume_facts.load_profile", load_profile)
        monkeypatch.setattr("app.services.resume_facts.save_facts", save_facts)
        monkeypatch.setattr("app.services.ats_service.compute_ats_score", compute_ats_score)

        self.app, is_main = _build_app()
        if is_main:
            monkeypatch.setattr("app.core.clerk_auth.verify_token", lambda token: {"sub": "user-1"})

    def _db(self):
        harness = self
        from app.models.db import UserDocument

        class _Result:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        class _FakeDB:
            async def execute(self, stmt, *a, **k):
                from sqlalchemy.dialects import postgresql

                entity = stmt.column_descriptions[0].get("entity")
                compiled = stmt.compile(dialect=postgresql.dialect())
                params = compiled.params
                if entity.__name__ == "User":
                    from app.models.db import User

                    assert str(compiled).endswith("FOR UPDATE")
                    assert compiled.execution_options.get("populate_existing")
                    harness.calls.append("owner_lock")
                    return _Result(
                        User(
                            id=harness.user_id,
                            deletion_scheduled_for=harness.deletion_scheduled_for,
                        )
                    )
                if entity is not UserDocument:
                    return _Result(uuid.uuid4() if entity.__name__ in harness.pins else None)
                harness.doc_locks.append(str(compiled).endswith("FOR UPDATE"))
                # Honour the query's id/user filters so ownership is exercised.
                values = set(params.values())
                doc = harness.doc
                if doc is None or doc.id not in values or doc.user_id not in values:
                    return _Result(None)
                return _Result(doc)

            def add(self, obj):
                harness.added.append(obj)

            async def flush(self):
                harness.flushes += 1
                harness.calls.append("flush")

            async def commit(self):
                harness.calls.append("commit")
                if harness.commit_error is not None:
                    raise harness.commit_error

            async def rollback(self):
                harness.calls.append("rollback")

        return _FakeDB()

    async def request(self, method, document_id=None, json=None, raise_app_exceptions=True):
        from app.api.v1.deps import get_current_user, get_db
        from app.models.db import User

        db = self._db()

        async def _fake_db():
            return db

        self.app.dependency_overrides[get_db] = _fake_db
        self.app.dependency_overrides[get_current_user] = lambda: User(
            id=self.user_id, email="jane@example.com", full_name="Jane Doe"
        )
        doc_id = document_id or str(self.doc.id)
        path = f"/api/v1/resume/tailored/{doc_id}"
        if method == "POST":
            path += "/fix"
        try:
            transport = ASGITransport(app=self.app, raise_app_exceptions=raise_app_exceptions)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(
                    method, path, json=json, headers={"Authorization": "Bearer t"}
                )
        finally:
            self.app.dependency_overrides.clear()

    async def fix(self, body, document_id=None, raise_app_exceptions=True):
        return await self.request(
            "POST", document_id, json=body, raise_app_exceptions=raise_app_exceptions
        )

    async def get(self, document_id=None):
        return await self.request("GET", document_id)


# ── GET ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_review_with_issues_for_sparse_doc(monkeypatch):
    doc = _doc(uuid.uuid4(), ats_data={"keywords_matched": ["Python"]})
    h = _Harness(monkeypatch, doc)

    resp = await h.get()

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == str(doc.id)
    assert body["template"] == "modern"
    assert body["resume_markdown"] == SPARSE_RESUME
    assert body["summary"] == "Tailored."
    assert body["ats_score"] == body["estimate"]["composite_score"]
    assert body["keywords_matched"] == []
    codes = {i["code"] for i in body["review"]["issues"]}
    assert codes == {
        "missing_phone",
        "truncated_employer",
        "missing_dates",
        "missing_education",
    }
    assert body["review"]["contact"]["email"] == "jane@example.com"
    assert body["review"]["has_education_section"] is False
    [entry] = body["review"]["experience"]
    assert entry["index"] == 0
    assert entry["role"] == "Prompt Engineer Intern"
    assert set(entry["issues"]) == {"truncated_employer", "missing_dates"}
    # Pre-fill falls back to the login email when no profile/facts exist.
    assert body["contact_suggestions"]["email"] == "jane@example.com"
    # Reading must not write anything.
    assert h.uploaded == [] and h.deleted == [] and h.rendered == []


# ── POST fix ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_resolves_all_issues_and_remembers_facts(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({**FULL_FIX, "remember": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review"]["issues"] == []
    assert FIXED_HEADING in body["resume_markdown"]
    assert "jane@example.com | +91 98765 43210 | Pune, India" in body["resume_markdown"]
    assert "## EDUCATION" in body["resume_markdown"]
    assert body["review"]["has_education_section"] is True
    assert body["template"] == "modern"

    # Document updated in place with the new text and re-rendered PDF.
    assert doc.raw_text == body["resume_markdown"]
    assert doc.storage_path == "user-id/new.pdf"
    assert h.rendered == ["modern"]
    assert h.uploaded == [(str(doc.user_id), "resume.pdf", "application/pdf")]
    assert ("u/resume.pdf", str(doc.user_id)) in h.deleted
    assert h.flushes >= 1

    # Facts saved for future tailoring runs.
    [(saved_user, facts)] = h.saved_facts
    assert saved_user == doc.user_id
    assert facts["contact"]["phone"] == "+91 98765 43210"
    [exp] = facts["experience"]
    assert exp["employer"] == FULL_EMPLOYER
    assert exp["employer_match"] == "Agentic Universe (Qultured Media Pvt."
    assert (exp["start"], exp["end"]) == ("Jun 2025", "Present")
    # Only what the user typed counts as a fact; the role is a match key.
    assert exp["submitted"] == ["employer", "location", "start", "end"]
    assert exp["role"] == exp["match_role"] == "Prompt Engineer Intern"
    [edu] = facts["education"]
    assert edu["institution"] == "Savitribai Phule Pune University"
    assert body["display_name"] == "Jane Doe"
    assert body["document_id"] == str(doc.id)
    assert h.added == []


@pytest.mark.asyncio
async def test_fix_with_remember_false_does_not_save_facts(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({**FULL_FIX, "remember": False})

    assert resp.status_code == 200, resp.text
    assert resp.json()["review"]["issues"] == []
    assert h.saved_facts == []
    assert doc.storage_path == "user-id/new.pdf"
    assert ("u/resume.pdf", str(doc.user_id)) in h.deleted


@pytest.mark.asyncio
async def test_template_only_fix_switches_template(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"template": "classic"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template"] == "classic"
    assert doc.ats_data["template"] == "classic"
    assert h.rendered == ["classic"]
    # Text is untouched apart from normalisation; the gaps are still reported.
    assert body["resume_markdown"].strip() == SPARSE_RESUME.strip()
    assert {i["code"] for i in body["review"]["issues"]} >= {"missing_dates"}
    assert doc.storage_path == "user-id/new.pdf"


@pytest.mark.asyncio
async def test_fix_rescores_when_jd_is_recorded(monkeypatch):
    doc = _doc(uuid.uuid4(), ats_data={"jd_text": "Python, Azure", "keywords_missing": []})
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"template": "technical"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["ats_score"] == 91
    assert resp.json()["keywords_missing"] == ["azure"]
    assert h.ats_calls and h.ats_calls[0][1] == "Python, Azure"


@pytest.mark.asyncio
async def test_bad_experience_index_is_422(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"experience": [{"index": 5, "start": "Jun 2025"}]})

    assert resp.status_code == 422
    assert "index 5" in resp.json()["detail"]
    assert doc.raw_text == SPARSE_RESUME
    assert doc.storage_path == "u/resume.pdf"
    assert h.uploaded == [] and h.deleted == [] and h.saved_facts == []


@pytest.mark.asyncio
async def test_blank_manual_edit_is_422(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"resume_markdown": "   \n  "})

    assert resp.status_code == 422
    assert doc.raw_text == SPARSE_RESUME
    assert h.uploaded == []


@pytest.mark.asyncio
async def test_other_users_document_is_404(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc, user_id=uuid.uuid4())

    get_resp = await h.get()
    fix_resp = await h.fix({"template": "classic"})

    assert get_resp.status_code == 404
    assert fix_resp.status_code == 404
    assert doc.ats_data["template"] == "modern"
    assert h.uploaded == [] and h.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("document_id", [None, "not-a-uuid"])
async def test_missing_document_is_404(monkeypatch, document_id):
    h = _Harness(monkeypatch, None)

    target = document_id or str(uuid.uuid4())
    assert (await h.get(target)).status_code == 404
    assert (await h.fix({"template": "classic"}, target)).status_code == 404
    assert h.uploaded == []


@pytest.mark.asyncio
async def test_resolved_warnings_are_filtered_and_skill_gaps_kept(monkeypatch):
    doc = _doc(
        uuid.uuid4(),
        ats_data={"warnings": [DATES_WARNING, EDUCATION_WARNING, AZURE_WARNING]},
    )
    h = _Harness(monkeypatch, doc)

    # Fixing only the dates drops the dates warning; education is still open.
    resp = await h.fix({"experience": [{"index": 0, "start": "Jun 2025", "end": "Present"}]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == [EDUCATION_WARNING, AZURE_WARNING]

    # Adding education resolves the rest; the genuine skill gap stays.
    resp = await h.fix({"education": FULL_FIX["education"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == [AZURE_WARNING]
    # Filtering happens when reading: the stored list is the model's original,
    # so a warning comes back if its gap is reintroduced by a later edit.
    assert doc.ats_data["warnings"] == [DATES_WARNING, EDUCATION_WARNING, AZURE_WARNING]
    assert (await h.get()).json()["warnings"] == [AZURE_WARNING]

    resp = await h.fix({"experience": [{"index": 0, "start": "", "end": ""}]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["warnings"] == [DATES_WARNING, AZURE_WARNING]


@pytest.mark.asyncio
async def test_get_filters_warnings_without_rewriting_them(monkeypatch):
    warnings = [DATES_WARNING, EDUCATION_WARNING, AZURE_WARNING]
    fixed = SPARSE_RESUME.replace(
        "### Prompt Engineer Intern | Agentic Universe (Qultured Media Pvt.",
        FIXED_HEADING,
    )
    doc = _doc(uuid.uuid4(), raw_text=fixed, ats_data={"warnings": list(warnings)})
    h = _Harness(monkeypatch, doc)

    body = (await h.get()).json()

    assert body["warnings"] == [EDUCATION_WARNING, AZURE_WARNING]
    assert body["display_name"] == "Jane Doe"
    assert doc.ats_data["warnings"] == warnings


# ── Commit ordering and cleanup ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_old_pdf_is_deleted_only_after_commit(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({**FULL_FIX, "remember": True})

    assert resp.status_code == 200, resp.text
    calls = h.calls
    assert calls.index(("upload", "user-id/new.pdf")) < calls.index("save_facts")
    assert calls.index("save_facts") < calls.index("commit")
    assert calls.index("commit") < calls.index(("delete", "u/resume.pdf"))


@pytest.mark.asyncio
async def test_commit_failure_removes_new_pdf_and_keeps_old(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)
    h.commit_error = RuntimeError("connection lost during commit")

    resp = await h.fix({"template": "classic"}, raise_app_exceptions=False)

    assert resp.status_code == 500
    assert h.deleted == [("user-id/new.pdf", str(doc.user_id))]
    assert ("delete", "u/resume.pdf") not in h.calls


@pytest.mark.asyncio
async def test_ats_failure_leaves_no_uploaded_pdf(monkeypatch):
    doc = _doc(uuid.uuid4(), ats_data={"jd_text": "Python, Azure"})
    h = _Harness(monkeypatch, doc)
    h.ats_error = RuntimeError("scorer crashed")

    resp = await h.fix({"template": "classic"}, raise_app_exceptions=False)

    assert resp.status_code == 500
    assert h.uploaded == [] and h.deleted == []
    assert doc.storage_path == "u/resume.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_error",
    [
        lambda: LayoutError(
            "Flowable <Paragraph at 0x1 frags=1>(<para>very long</para>) too large"
        ),
        lambda: ValueError("<font name='x'> unsupported"),
        lambda: KeyError("style"),
    ],
)
async def test_render_failure_is_generic_422(monkeypatch, make_error):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)
    h.render_error = make_error()

    resp = await h.fix({"template": "classic"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "The resume could not be rendered as a PDF. Shorten very long lines and try again."
    )
    assert h.uploaded == [] and h.deleted == []
    assert doc.raw_text == SPARSE_RESUME


# ── Saved facts are only what the user typed ─────────────────────────────────


@pytest.mark.asyncio
async def test_saved_experience_fact_has_only_submitted_keys(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"experience": [{"index": 0, "start": "Jun 2025", "end": "Present"}]})

    assert resp.status_code == 200, resp.text
    [(_, facts)] = h.saved_facts
    [exp] = facts["experience"]
    assert exp["submitted"] == ["start", "end"]
    assert (exp["start"], exp["end"]) == ("Jun 2025", "Present")
    # The model's employer/location are not saved as facts.
    assert "employer" not in exp and "location" not in exp
    assert exp["employer_match"] == "Agentic Universe (Qultured Media Pvt."


@pytest.mark.asyncio
async def test_fact_match_keys_come_from_the_manual_edit(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)
    edited = SPARSE_RESUME.replace(
        "### Prompt Engineer Intern | Agentic Universe (Qultured Media Pvt.",
        "### Data Engineer | Beta Corp",
    )

    resp = await h.fix(
        {
            "resume_markdown": edited,
            "experience": [{"index": 0, "location": "Pune"}],
        }
    )

    assert resp.status_code == 200, resp.text
    [(_, facts)] = h.saved_facts
    [exp] = facts["experience"]
    assert (exp["role"], exp["employer_match"]) == ("Data Engineer", "Beta Corp")
    assert exp["submitted"] == ["location"]


# ── Pending approvals pin the PDF ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["ApplicationAttempt", "AgentRun"])
async def test_fix_of_pinned_resume_saves_a_new_version(monkeypatch, model):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)
    h.pins.add(model)

    resp = await h.fix({**FULL_FIX, "template": "classic"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    [new_doc] = h.added
    assert body["document_id"] == str(new_doc.id) != str(doc.id)
    assert new_doc.user_id == doc.user_id
    assert (new_doc.doc_type, new_doc.filename) == ("resume_tailored", "resume.pdf")
    assert new_doc.storage_path == "user-id/new.pdf"
    assert new_doc.raw_text == body["resume_markdown"]
    assert new_doc.ats_data["template"] == "classic"
    assert new_doc.ats_data["summary"] == "Tailored."
    # The approved row and its file are untouched.
    assert doc.raw_text == SPARSE_RESUME
    assert doc.storage_path == "u/resume.pdf"
    assert doc.ats_data["template"] == "modern"
    assert h.deleted == []
    assert "commit" in h.calls


@pytest.mark.asyncio
async def test_unpinned_resume_is_edited_in_place(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"template": "classic"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["document_id"] == str(doc.id)
    assert h.added == []
    assert doc.storage_path == "user-id/new.pdf"
    assert ("u/resume.pdf", str(doc.user_id)) in h.deleted


@pytest.mark.asyncio
async def test_fix_locks_the_document_row_and_get_does_not(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    assert (await h.fix({"template": "classic"})).status_code == 200
    assert (await h.get()).status_code == 200

    assert h.doc_locks == [True, False]


def _conditions(stmt) -> list[tuple[str, str, object]]:
    """(table.column, operator, bound value) for every column-vs-parameter
    comparison in the statement's WHERE clause."""
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.elements import BinaryExpression, BindParameter, ColumnClause

    found = []
    for el in visitors.iterate(stmt.whereclause):
        if (
            isinstance(el, BinaryExpression)
            and isinstance(el.left, ColumnClause)
            and isinstance(el.right, BindParameter)
        ):
            op = getattr(el.operator, "opstring", None) or el.operator.__name__
            found.append((f"{el.left.table.name}.{el.left.name}", op, el.right.effective_value))
    return found


def test_pin_attempt_query_is_owner_scoped_and_needs_an_open_run():
    from sqlalchemy.dialects import postgresql

    from app.api.v1 import resume as resume_api

    doc = _doc(uuid.uuid4())
    stmt = resume_api._pinning_attempt_query(doc)
    conditions = _conditions(stmt)

    assert ("application_attempts.user_id", "eq", doc.user_id) in conditions
    assert ("job_applications.user_id", "eq", doc.user_id) in conditions
    assert ("agent_runs.user_id", "eq", doc.user_id) in conditions
    assert ("job_applications.resume_id", "eq", doc.id) in conditions
    [states] = [v for c, op, v in conditions if c == "application_attempts.state"]
    assert set(states) == {"preparing", "awaiting_approval", "submitting"}
    [statuses] = [v for c, op, v in conditions if c == "agent_runs.status"]
    assert set(statuses) == {"queued", "running", "awaiting_approval"}
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "JOIN agent_runs ON agent_runs.id = application_attempts.run_id" in sql


def test_pin_run_query_is_owner_scoped_and_matches_the_document_in_the_checkpoint():
    from app.api.v1 import resume as resume_api

    doc = _doc(uuid.uuid4())
    conditions = _conditions(resume_api._pinning_run_query(doc))

    assert ("agent_runs.user_id", "eq", doc.user_id) in conditions
    assert ("agent_runs.status", "eq", "awaiting_approval") in conditions
    json_conditions = [(op, v) for c, op, v in conditions if c == "agent_runs.output"]
    doc_id = str(doc.id)
    assert sorted(json_conditions, key=repr) == sorted(
        [
            ("?", "resume_sha256"),
            ("@>", {"pdf_document_id": doc_id}),
            ("@>", {"actions_pending": [{"pdf_document_id": doc_id}]}),
        ],
        key=repr,
    )


def test_pin_lookup_runs_the_attempt_query_then_the_run_query():
    from app.api.v1 import resume as resume_api

    doc = _doc(uuid.uuid4())
    seen = []

    class _DB:
        def __init__(self, answers):
            self.answers = list(answers)

        async def execute(self, stmt):
            seen.append(stmt.column_descriptions[0]["entity"].__name__)
            value = self.answers.pop(0)
            return type("R", (), {"scalar_one_or_none": lambda self: value})()

    assert asyncio.run(resume_api._pinned_by_pending_approval(_DB([None, None]), doc)) is False
    assert seen == ["ApplicationAttempt", "AgentRun"]
    seen.clear()
    assert asyncio.run(resume_api._pinned_by_pending_approval(_DB([uuid.uuid4()]), doc)) is True
    assert seen == ["ApplicationAttempt"]
    seen.clear()
    assert asyncio.run(resume_api._pinned_by_pending_approval(_DB([None, uuid.uuid4()]), doc))
    assert seen == ["ApplicationAttempt", "AgentRun"]


# ── Limits ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_due_account_erasure_refuses_fix_before_document_or_storage(monkeypatch):
    from datetime import UTC, datetime, timedelta

    h = _Harness(monkeypatch, _doc(uuid.uuid4()))
    h.deletion_scheduled_for = datetime.now(UTC) - timedelta(seconds=1)
    response = await h.fix({"template": "classic"})
    assert response.status_code == 403
    assert h.doc_locks == [] and h.uploaded == []


@pytest.mark.asyncio
async def test_fix_locks_owner_before_storage_and_document(monkeypatch):
    h = _Harness(monkeypatch, _doc(uuid.uuid4()))
    response = await h.fix({"template": "classic", "remember": False})
    assert response.status_code == 200
    assert h.calls[0] == "owner_lock"
    assert h.calls.index("owner_lock") < h.calls.index(("upload", "user-id/new.pdf"))


@pytest.mark.asyncio
async def test_more_than_ten_education_entries_is_422(monkeypatch):
    doc = _doc(uuid.uuid4())
    h = _Harness(monkeypatch, doc)

    resp = await h.fix({"education": [{"degree": f"Degree {i}"} for i in range(11)]})

    assert resp.status_code == 422
    assert "10" in str(resp.json()["detail"])
    assert h.uploaded == []
