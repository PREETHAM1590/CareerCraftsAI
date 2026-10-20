"""Provider work stays outside the privacy fence, including on concurrent Forget."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("forget_phase", [None, "read", "write"])
async def test_harness_prepares_without_lock_and_discards_stale_work(monkeypatch, forget_phase):
    from app.agents import harness as module
    from app.agents.harness import AgentHarness
    from app.services import memory_lifecycle

    state = {"locked": False, "generation": 0}

    @asynccontextmanager
    async def access(user_id, generation=None):
        assert not state["locked"]
        state["locked"] = True
        try:
            yield state["generation"] if generation in (None, state["generation"]) else None
        finally:
            state["locked"] = False

    async def prepare(phase):
        assert not state["locked"], "Provider call must not retain an owner lock"
        if phase == forget_phase:
            state["generation"] += 1
        return object()

    async def read(**kwargs):
        assert state["locked"]
        return {}

    async def save(prepared):
        assert state["locked"]

    agent = AgentHarness("postgresql://unused", "redis://unused")
    agent._ensure_initialized = AsyncMock()
    agent._memory = MagicMock()
    agent._memory.build_agent_context = AsyncMock(side_effect=read)
    agent._memory.save_episode = AsyncMock()
    agent._memory.upsert_learning = AsyncMock()
    agent._extract_and_save_memories = AsyncMock()
    agent._semantic_memory = MagicMock()

    async def prepare_read(*args):
        return await prepare("read")

    async def prepare_write(**kwargs):
        return await prepare("write")

    agent._semantic_memory.prepare_context = AsyncMock(side_effect=prepare_read)
    agent._semantic_memory.prepare_after_run = AsyncMock(side_effect=prepare_write)
    agent._semantic_memory.build_context = AsyncMock(side_effect=read)
    agent._semantic_memory.save_prepared = AsyncMock(side_effect=save)
    monkeypatch.setattr(memory_lifecycle, "memory_access", access)
    monkeypatch.setattr("app.core.sync_db.fetch_model_settings", lambda *a: None)

    async def invoke(request):
        assert not state["locked"]
        return {**request, "status": "completed", "result": {"letter": "draft"}}

    monkeypatch.setattr(module.orchestrator, "ainvoke", invoke)

    result = await agent.run(str(uuid4()), "cover_letter", {}, {})
    assert result["status"] == "completed"
    if forget_phase:
        agent._semantic_memory.save_prepared.assert_not_awaited()
        agent._memory.save_episode.assert_not_awaited()
        agent._extract_and_save_memories.assert_not_awaited()
    else:
        agent._semantic_memory.save_prepared.assert_awaited_once()
        agent._memory.save_episode.assert_awaited_once()
    if forget_phase == "read":
        agent._semantic_memory.build_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_preparation_is_ephemeral_and_persistence_has_no_provider(monkeypatch):
    from app.agents.semantic_memory import SemanticMemoryBridge
    from memory.embedder import MemoryEmbedder

    bridge = SemanticMemoryBridge()
    monkeypatch.setattr(bridge, "_resolve_user_settings", lambda *a: ({"provider": "google"}, None))
    calls = []

    async def embed(self, text):
        assert self.redis is None
        calls.append(text)
        return [0.0] * 1536

    monkeypatch.setattr(MemoryEmbedder, "embed", embed)
    prepared = await bridge.prepare_after_run(
        str(uuid4()), "cover_letter", {}, {"letter": "draft"}, True, "standard", {}
    )
    assert calls

    async def forbid(*args):
        raise AssertionError("Provider called during persistence")

    monkeypatch.setattr(MemoryEmbedder, "embed", forbid)
    # Every summary/memory lookup is now resolved in memory; the manager cannot
    # fall back to network if a text was omitted from preparation.
    provider_hits = []
    saved = []

    class FakeManager:
        redis = MagicMock()

        def set_session(self, *args):
            pass

        async def save_episode(self, episode):
            saved.append(await self.embedder.embed(episode.summary))

        async def save_learning(self, learning):
            pass

        async def save_memory(self, memory):
            saved.append(await self.embedder.embed(memory.content))

        async def close(self):
            pass

    mgr = FakeManager()
    mgr.embedder = prepared.embeddings
    monkeypatch.setattr(bridge, "_manager", lambda embeddings: mgr)
    monkeypatch.setattr(MemoryEmbedder, "embed", lambda *a: provider_hits.append(a))
    await bridge.save_prepared(prepared)
    assert saved and all(v == [0.0] * 1536 for v in saved)
    assert not provider_hits
    with pytest.raises(KeyError):
        await prepared.embeddings.embed("not prepared")
