"""Bounded best-effort description fill for BambooHR and Rippling boards."""

import asyncio

import pytest

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
        return FakeResponse(
            '{"result": {"jobOpening": {"description": "<p>Ship <b>things</b></p>"}}}'
        )

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    rows = [{"id": "7", "description": ""}, {"id": "8", "description": "already"}]
    asyncio.run(jc._fill_bamboohr_bodies("acme", rows))
    assert calls == ["https://acme.bamboohr.com/careers/7/detail"]
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
        return FakeResponse(
            '<div class="job-description"><div>Build <b>platforms</b></div>'
            "<section><div>Own on-call</div><p>Mentor engineers</p></section>"
            "<p>Write docs</p></div><div>Footer</div>"
        )

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    rows = [
        {"id": str(i), "url": f"https://ats.rippling.com/j/{i}", "description": ""}
        for i in range(jc._BODIES_MAX + 10)
    ]
    asyncio.run(jc._fill_rippling_bodies("acme", rows))
    assert len(urls) == 25
    assert rows[0]["description"] == "Build platforms Own on-call Mentor engineers Write docs"
    assert rows[30]["description"] == ""


async def test_rippling_page_keeps_rows_when_body_budget_expires(monkeypatch):
    async def fake_get(url, **kw):
        if url.endswith("/jobs"):
            return FakeResponse(
                '[{"uuid":"1","name":"First","url":"https://ats.rippling.com/j/1"},'
                '{"uuid":"2","name":"Second","url":"https://ats.rippling.com/j/2"}]'
            )
        if url.endswith("/1"):
            return FakeResponse('<div id="description">First body</div>')
        await asyncio.Future()

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    monkeypatch.setattr(jc, "_RIPPLING_BODIES_TIMEOUT_S", 0.05)
    source = jc.Source(id="rippling:acme", family="rippling", tenant="acme")
    page = await asyncio.wait_for(jc.fetch_page(source), 2)

    assert [(job["title"], job["description"]) for job in page.jobs] == [
        ("First", "First body"),
        ("Second", ""),
    ]


async def test_rippling_body_budget_preserves_external_cancellation(monkeypatch):
    started = asyncio.Event()

    async def fake_get(url, **kw):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(jc, "_polite_get", fake_get)
    task = asyncio.create_task(
        jc._fill_rippling_bodies("acme", [{"url": "https://ats.rippling.com/j/1"}])
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_bamboohr_body_fill_stops_at_the_budget_and_keeps_partial_rows(monkeypatch):
    async def slow_get(url, **kw):
        if "/careers/0/" in url:
            return FakeResponse('{"result": {"jobOpening": {"description": "First"}}}')
        await asyncio.sleep(5)
        return FakeResponse('{"result": {"jobOpening": {"description": "Late"}}}')

    monkeypatch.setattr(jc, "_polite_get", slow_get)
    monkeypatch.setattr(jc, "_BODIES_BUDGET_S", 0.2)
    rows = [{"id": str(i), "description": ""} for i in range(3)]
    asyncio.run(jc._fill_bodies_within_budget(jc._fill_bamboohr_bodies("acme", rows)))
    assert rows[0]["description"] == "First"
    assert rows[1]["description"] == ""


def test_plain_strips_entity_escaped_bodies():
    raw = (
        "&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;"
        "R&amp;amp;D&amp;nbsp;team&lt;/p&gt;&lt;/div&gt;"
    )
    assert jc.plain(raw) == "R&D\xa0team"
    assert jc.plain("<p>Use List&lt;String&gt;</p>") == "Use List<String>"
    assert jc.plain("Under &lt;5 years") == "Under <5 years"


def test_descriptions_keep_paragraphs_and_lists():
    body = "<h2>Role</h2><p>Build <b>APIs</b></p><ul><li>Python</li><li>SQL</li></ul>"
    assert jc.clean_description(body) == "Role\nBuild APIs\n- Python\n- SQL"
    assert "\n" not in jc.plain(body)


def test_clean_description_fixes_saved_html_and_keeps_text():
    assert jc.clean_description('<div class="x"><p>At Affirm</p></div><p>Pay &amp; go <stro') == (
        "At Affirm\nPay & go"
    )
    escaped = "&lt;p&gt;Hello&lt;/p&gt; the rest stays &lt;stro"
    assert jc.clean_description(escaped) == "Hello\nthe rest stays"
    text = "Line one\n\n  - C++ where a < b and c > d, List<String>"
    assert jc.clean_description(text) == text
    assert jc.clean_description(None) == ""
