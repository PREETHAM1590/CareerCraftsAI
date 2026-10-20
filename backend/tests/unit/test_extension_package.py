import io
import zipfile

import pytest


@pytest.mark.asyncio
async def test_download_route_returns_the_extension_zip(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.api.v1.deps import get_current_user
    from app.main import app
    from app.models.db import User

    monkeypatch.setattr("app.core.clerk_auth.verify_token", lambda token: {"sub": "user-1"})
    app.dependency_overrides[get_current_user] = lambda: User(email="a@b.com")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/api/v1/extension/download", headers={"Authorization": "Bearer t"}
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="careercraft-extension-v' in resp.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert "careercraft-extension/manifest.json" in names
    assert "careercraft-extension/src/content/bridge.js" in names
    assert not any("/test/" in name for name in names)
