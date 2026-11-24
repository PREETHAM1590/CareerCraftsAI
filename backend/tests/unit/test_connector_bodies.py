"""Bounded best-effort description fill for BambooHR and Rippling boards."""

import asyncio
from types import SimpleNamespace

from app.services import job_connectors as jc


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else ""

    def raise_for_status(self):
        return None

    def json(self):
        import json as _json

        return _json.loads(self.text)


def test_bamboohr_bodies_fill(monkeypatch):
    calls = []

    async def fake_get(url, **kw):
        calls.append(url)
        return FakeResponse('[{"id": "7", "description": "<p>Ship things</p>"}]')

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    rows = [{"id": "7", "description": ""}, {"id": "8", "description": "already"}]
    asyncio.run(jc._fill_bamboohr_bodies("acme", rows))
    assert rows[0]["description"] == "Ship things"
    assert rows[1]["description"] == "already"


def test_bamboohr_bodies_silent_on_error(monkeypatch):
    async def fake_get(url, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    rows = [{"id": "7", "description": ""}]
    asyncio.run(jc._fill_bamboohr_bodies("acme", rows))
    assert rows[0]["description"] == ""


def test_rippling_bodies_bounded_and_extracted(monkeypatch):
    urls = []

    async def fake_get(url, **kw):
        urls.append(url)
        return FakeResponse('<div class="job-description">Build <b>platforms</b></div>')

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    rows = [
        {"id": str(i), "url": f"https://ats.rippling.com/j/{i}", "description": ""}
        for i in range(jc._BODIES_MAX + 10)
    ]
    asyncio.run(jc._fill_rippling_bodies("acme", rows))
    assert len(urls) == 25
    assert rows[0]["description"] == "Build platforms"
    assert rows[30]["description"] == ""
