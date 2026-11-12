"""Search cache: cancellation, expiry of every key, a hung counter, key composition."""

import asyncio

import fakeredis
import pytest

from app.core.config import settings
from app.services import search_cache


@pytest.fixture
def redis(monkeypatch):
    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
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
async def test_cancelled_owner_releases_its_lock(redis):
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(search_cache.get_or_compute("live", "k", 60, slow, wait_s=1))
    await started.wait()
    assert await redis.exists("k:lock")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await redis.exists("k:lock")


@pytest.mark.asyncio
async def test_every_key_written_expires(redis):
    calls = []
    await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=1)
    await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=1)
    await search_cache.bump_catalog_version()
    keys = [key async for key in redis.scan_iter("*")]
    assert any(":stats:" in key for key in keys)
    for key in keys:
        if key != f"{search_cache.PREFIX}:catalog_version":  # the version must outlive entries
            assert await redis.ttl(key) > 0, key


@pytest.mark.asyncio
async def test_hit_stays_fast_when_the_counter_write_hangs(redis, monkeypatch):
    calls = []
    await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=1)
    real = redis.pipeline

    def hanging_pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)

        async def never():
            await asyncio.sleep(30)

        pipe.execute = never
        return pipe

    monkeypatch.setattr(redis, "pipeline", hanging_pipeline)
    loop = asyncio.get_running_loop()
    start = loop.time()
    _, payload = await search_cache.get_or_compute("live", "k", 60, _compute(calls), wait_s=1)
    assert payload == "p" and len(calls) == 1
    assert loop.time() - start < search_cache._OP_TIMEOUT + 0.3


def test_multi_location_keys_ignore_order_but_not_membership():
    base = {"titles": ["data analyst"]}
    both = search_cache.places_of({**base, "locations": ["Pune", "Mumbai"]})
    assert both == search_cache.places_of({**base, "locations": [" mumbai", "PUNE"]})
    assert both != search_cache.places_of({**base, "locations": ["Pune"]})
    assert search_cache.places_of({"location": "Pune"}) == search_cache.places_of(
        {"locations": ["pune"]}
    )
    assert search_cache.places_of({}) == []


def test_role_alternatives_are_part_of_the_key():
    one = search_cache.terms_of({"titles": ["backend engineer"]})
    two = search_cache.terms_of({"titles": ["backend engineer", "data engineer"]})
    assert one != two
    assert one == search_cache.terms_of({"search_query": "Engineer  Backend"})
