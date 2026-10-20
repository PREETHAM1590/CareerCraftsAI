"""Shared job-search cache: hits, single flight, versioning, and Redis failure."""

import asyncio
import json

import fakeredis
import pytest

from app.core.config import settings
from app.services import search_cache


@pytest.fixture
def redis(monkeypatch):
    server = fakeredis.FakeServer()
    client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: client)
    monkeypatch.setattr(search_cache, "_POLL", 0.01)
    return client


def _compute(calls, value="v", payload="p"):
    async def compute():
        calls.append(1)
        return value, payload

    return compute


@pytest.mark.asyncio
async def test_miss_then_hit_and_counts(redis):
    calls = []
    first = await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=1)
    second = await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=1)
    assert first == ("v", None)
    assert second == (None, "p")
    assert len(calls) == 1
    assert 0 < await redis.ttl("k") <= 60
    keys = await redis.keys(f"{search_cache.PREFIX}:stats:*")
    assert {k.rsplit(":", 1)[1] for k in keys} == {"hit", "miss"}


@pytest.mark.asyncio
async def test_none_payload_is_not_cached(redis):
    calls = []
    for _ in range(2):
        assert await search_cache.get_or_compute(
            "live", "k", 60, _compute(calls, payload=None), wait_s=1
        ) == ("v", None)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_oversized_payload_is_not_cached(redis, monkeypatch):
    monkeypatch.setattr(search_cache, "MAX_PAYLOAD_BYTES", 3)
    await search_cache.get_or_compute("live", "k", 60, _compute([], payload="toolong"), wait_s=1)
    assert await redis.get("k") is None


@pytest.mark.asyncio
async def test_exception_caches_nothing_and_releases_lock(redis):
    async def boom():
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await search_cache.get_or_compute("live", "k", 60, boom, wait_s=1)
    assert await redis.get("k") is None
    assert await redis.get("k:lock") is None


@pytest.mark.asyncio
async def test_concurrent_callers_compute_once(redis):
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.1)
        return "v", "p"

    results = await asyncio.gather(
        *(search_cache.get_or_compute("live", "k", 60, slow, wait_s=2) for _ in range(5))
    )
    assert len(calls) == 1
    assert results.count(("v", None)) == 1
    assert results.count((None, "p")) == 4


@pytest.mark.asyncio
async def test_waiter_computes_itself_when_owner_caches_nothing(redis):
    calls = []

    async def empty():
        calls.append(1)
        await asyncio.sleep(0.05)
        return [], None

    await asyncio.gather(
        *(search_cache.get_or_compute("live", "k", 60, empty, wait_s=2) for _ in range(2))
    )
    assert len(calls) == 2  # the waiter did not hang for wait_s


@pytest.mark.asyncio
async def test_disabled_or_down_redis_computes_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", False)
    assert await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=5) == (
        "v",
        None,
    )

    class Down:
        async def get(self, *a, **k):
            raise ConnectionError

        async def set(self, *a, **k):
            raise ConnectionError

    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", True)
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: Down())
    assert await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=5) == (
        "v",
        None,
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_catalog_version_bump(redis):
    assert await search_cache.catalog_version() == "0"
    await search_cache.bump_catalog_version()
    assert await search_cache.catalog_version() == "1"


def test_query_normalisation_ignores_case_and_word_order():
    a = {"titles": ["Python Developer"], "locations": ["Bangalore", "Remote"]}
    b = {"titles": ["developer python"], "locations": ["remote ", "BANGALORE"]}
    assert search_cache.terms_of(a) == search_cache.terms_of(b)
    assert search_cache.places_of(a) == search_cache.places_of(b)
    assert search_cache.query_hash({"x": 1, "y": [1]}) == search_cache.query_hash(
        {"y": [1], "x": 1}
    )


# ── wiring ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_hit_rereads_cached_ids_without_scanning(redis, monkeypatch):
    from app.services import job_catalog

    job = {
        "job_id": "j1",
        "title": "Python Developer",
        "company": "Acme",
        "location": "Remote",
        "remote": "remote",
        "url": "https://x.io/1",
        "posted_at": None,
        "occurrences": [],
    }
    scans, reads = [], []

    async def scan(query, public, live_ids, relevant_of):
        scans.append(1)
        return [job], []

    async def by_id(ids, sources):
        reads.append(ids)
        return [job]

    monkeypatch.setattr(job_catalog, "_search_catalog_db", scan)
    monkeypatch.setattr(job_catalog, "_catalog_rows_by_id", by_id)
    q = {"titles": ["python developer"], "location": "Remote"}
    first = await job_catalog.search_catalog(q, live_platforms=["jobspy"])
    second = await job_catalog.search_catalog(
        {"titles": ["Developer Python"], "location": "remote"}, live_platforms=["jobspy"]
    )
    assert scans == [1] and reads == [["j1"]]
    assert first[0][0]["job_id"] == second[0][0]["job_id"] == "j1"

    await search_cache.bump_catalog_version()  # a catalog refresh invalidates
    await job_catalog.search_catalog(q, live_platforms=["jobspy"])
    assert scans == [1, 1]


@pytest.mark.asyncio
async def test_catalog_result_with_source_warnings_is_not_cached(redis, monkeypatch):
    from app.services import job_catalog

    scans = []

    async def scan(query, public, live_ids, relevant_of):
        scans.append(1)
        return [], ["greenhouse:acme: TimeoutError"]

    monkeypatch.setattr(job_catalog, "_search_catalog_db", scan)
    for _ in range(2):
        await job_catalog.search_catalog({"titles": ["x"]}, live_platforms=["jobspy"])
    assert scans == [1, 1]


@pytest.mark.asyncio
async def test_live_hit_skips_adapter_and_write_through(redis, monkeypatch):
    from app.services import job_search_service as svc

    calls, writes = [], []

    def adapter(q, location, n):
        calls.append(q)
        return [
            {
                "title": "Python Developer",
                "company": "Acme",
                "url": "https://acme.io/jobs/1",
                "location": "Remote",
            }
        ]

    async def write_through(group, name):
        writes.append(name)

    async def no_catalog(*a, **k):
        return [], []

    monkeypatch.setitem(svc._ADAPTERS, "fake", adapter)
    monkeypatch.setattr("app.services.job_catalog.search_catalog", no_catalog)
    monkeypatch.setattr("app.services.job_catalog.write_through", write_through)
    q = {"titles": ["python developer"], "locations": ["Remote"]}
    first, _ = await svc.search_all_platforms(q, platforms=["fake"])
    second, _ = await svc.search_all_platforms(q, platforms=["fake"])
    assert len(calls) == 1 and writes == ["fake"]
    assert [j["url"] for j in first] == [j["url"] for j in second]
    assert json.loads(await redis.get(next(iter(await redis.keys("*:live:fake:*")))))
