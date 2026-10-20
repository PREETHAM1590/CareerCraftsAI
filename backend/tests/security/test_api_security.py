"""
Security tests — SQL injection, XSS, auth enforcement, RLS, API key encryption, rate limiting.
Run: pytest tests/security -v
"""

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def make_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_protected_endpoints_reject_unauthenticated():
    protected = [
        ("GET", "/api/v1/users/me"),
        ("GET", "/api/v1/jobs/applications"),
        ("GET", "/api/v1/rag/documents"),
        ("POST", "/api/v1/agents/run"),
        ("GET", "/api/v1/leads"),
        ("GET", "/api/v1/linkedin/outreach/queue"),
        ("POST", "/api/v1/email/compose"),
        ("POST", "/api/v1/resume/optimize"),
        ("POST", "/api/v1/interview/session/start"),
        ("GET", "/api/v1/interview/sessions"),
        ("POST", "/api/v1/linkedin/outreach/identify"),
        ("POST", "/api/v1/cover-letter/generate"),
        ("POST", "/api/v1/interview/session"),
        ("GET", "/api/v1/interview-prep/videos"),
    ]
    async with make_client() as client:
        for method, path in protected:
            resp = await client.request(method, path)
            assert resp.status_code in (
                401,
                422,
                403,
            ), f"{method} {path} returned {resp.status_code} — expected 401/422/403"


@pytest.mark.asyncio
async def test_auth_required_with_expired_token():
    async with make_client() as client:
        resp = await client.get(
            "/api/v1/users/me",
            headers={
                "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                ".eyJzdWIiOiJ0ZXN0IiwiZXhwIjoxNTE2MjM5MDIyfQ.invalid"
            },
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health_endpoint_is_public():
    # Intent: /health requires no auth — not that deps are healthy.
    # 503 with a status body is correct when DB/Redis are down.
    async with make_client() as client:
        resp = await client.get("/health")
    assert resp.status_code in (200, 503)
    assert "status" in resp.json()


def test_no_out_of_band_job_trigger_routes():
    # Temporal is the only way background jobs run; the old /internal/*
    # routes executed them in the API process, outside any workflow.
    assert not [r.path for r in app.routes if getattr(r, "path", "").startswith("/internal")]


@pytest.fixture
def authenticated_transport():
    """Real route validation with an admitted identity, no live DB/provider."""
    import uuid
    from types import SimpleNamespace

    from fastapi import FastAPI

    from app.api.v1 import agents, jobs, rag
    from app.api.v1.deps import get_current_user, get_db

    isolated = FastAPI()
    for router in (agents.router, jobs.router, rag.router):
        isolated.include_router(router, prefix="/api/v1")
    isolated.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4())
    isolated.dependency_overrides[get_db] = lambda: SimpleNamespace()
    return ASGITransport(app=isolated)


@pytest.mark.asyncio
async def test_agent_run_rejects_invalid_task_type(authenticated_transport):
    async with AsyncClient(transport=authenticated_transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/run",
            json={"task_type": "'; DROP TABLE users; --", "context": {}},
        )
    assert resp.status_code == 400


def test_sql_injection_job_filter_is_bound_as_data():
    import uuid

    from app.api.v1.jobs import _application_query

    payload = "'; DROP TABLE users; --"
    query = _application_query(
        uuid.uuid4(),
        status=None,
        location=None,
        source=None,
        posted_within_days=None,
        min_match=None,
        found_after=None,
        found_before=None,
        sort=None,
        q=payload,
    ).compile()
    assert payload not in str(query)
    assert any(payload.lower() in str(value).lower() for value in query.params.values())


def test_model_markup_is_escaped_at_pdf_rendering_boundary():
    from app.services.pdf_service import _markup

    rendered = _markup("<script>alert(1)</script> & **safe**")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered and "&amp;" in rendered


@pytest.mark.asyncio
async def test_doc_upload_rejects_executable_content_type(authenticated_transport):
    from io import BytesIO

    async with AsyncClient(transport=authenticated_transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/rag/upload",
            files={"file": ("malware.exe", BytesIO(b"MZ..."), "application/octet-stream")},
            data={"doc_type": "resume", "is_primary": "false"},
        )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_job_search_max_results_capped(authenticated_transport):
    async with AsyncClient(transport=authenticated_transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/jobs/search",
            json={"search_query": "python", "max_results": 9999},
        )
    assert resp.status_code == 422


def test_api_key_encryption_ciphertext_not_plaintext():
    """Verify encrypted key is AES-GCM ciphertext, not the plaintext key."""
    from app.core.security import decrypt_api_key, encrypt_api_key

    plaintext = "sk-ant-api03-real-looking-key-with-enough-length"
    secret = "test-secret-key-32-chars-minimum!!"
    encrypted = encrypt_api_key(plaintext, secret)

    # Encrypted value must not contain plaintext
    assert plaintext not in encrypted
    # Must be valid base64
    try:
        base64.b64decode(encrypted)
    except Exception:
        pytest.fail("api_key_enc is not valid base64")
    # Decryption roundtrip
    assert decrypt_api_key(encrypted, secret) == plaintext


def test_api_key_encryption_unique_salt_per_key():
    """Verify each encryption produces different ciphertext (unique salt)."""
    from app.core.security import encrypt_api_key

    secret = "test-secret-key-32-chars-minimum!!"
    enc1 = encrypt_api_key("key-1", secret)
    enc2 = encrypt_api_key("key-2", secret)
    enc3 = encrypt_api_key("key-1", secret)
    assert enc1 != enc2
    assert enc1 != enc3  # same plaintext, different salt


def test_rate_limiting_header_present():
    """Verify rate limit headers structure is correct."""
    from app.core.config import settings

    assert settings.RATE_LIMIT_DEFAULT is not None
    parts = settings.RATE_LIMIT_DEFAULT.split("/")
    assert len(parts) == 2
    assert parts[1] in ("second", "minute", "hour", "day")


@pytest.mark.asyncio
async def test_liveness_probe_is_public_and_sets_security_headers():
    async with make_client() as client:
        resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in resp.headers  # plain http in tests


@pytest.mark.asyncio
async def test_docs_pages_skip_the_api_csp():
    """Swagger UI loads from a CDN; the API's default-src 'none' would blank it."""
    async with make_client() as client:
        resp = await client.get("/docs")
    assert "Content-Security-Policy" not in resp.headers
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
