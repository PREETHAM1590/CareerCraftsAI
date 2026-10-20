"""
semantic_memory.py — pgvector memory bridge for AgentHarness.

Keeps the lightweight strategy memory in app.agents.memory, while adding the
full user_memories / agent_episodes / agent_learnings memory loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import redis

from app.core.config import settings
from app.core.security import decrypt_api_key
from memory.embedder import MemoryEmbedder
from memory.extractor import MemoryExtractor
from memory.manager import MemoryManager as PgMemoryManager
from memory.models import Episode, Learning, Memory

logger = logging.getLogger(__name__)


@dataclass
class PreparedEmbeddings:
    """In-memory vectors only: fenced DB operations can never call a provider."""

    vectors: dict[str, list[float]]

    async def embed(self, text: str) -> list[float]:
        return self.vectors[text]


@dataclass
class PreparedRunMemory:
    episode: Episode
    learning: Learning
    memories: list[Memory]
    embeddings: PreparedEmbeddings


class SemanticMemoryBridge:
    """Connects LangGraph harness runs to pgvector-backed memory."""

    def __init__(self) -> None:
        self._extractor = MemoryExtractor()

    def _resolve_user_settings(
        self,
        user_id: str,
        user_settings: dict[str, Any],
    ) -> tuple[dict[str, Any], Any | None]:
        """Return decrypted provider settings plus optional ORM model row."""
        if user_settings.get("provider"):
            return dict(user_settings), None

        try:
            from app.core.sync_db import fetch_model_settings

            model_settings = fetch_model_settings(user_id)
            if not model_settings:
                return {}, None
            return {
                "provider": model_settings.provider,
                "model_name": model_settings.model_name,
                "api_key": (
                    decrypt_api_key(model_settings.api_key_enc, settings.APP_SECRET_KEY)
                    if model_settings.api_key_enc
                    else ""
                ),
                "ollama_url": model_settings.ollama_url,
            }, model_settings
        except Exception as exc:
            logger.debug("Semantic memory model settings unavailable: %s", exc)
            return {}, None

    def _manager(self, embeddings: PreparedEmbeddings) -> PgMemoryManager:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        db_url = settings.DATABASE_URL.replace("+asyncpg", "")
        return PgMemoryManager(db_url=db_url, redis_client=redis_client, embedder=embeddings)

    async def _prepare_embeddings(self, resolved: dict, texts: list[str]) -> PreparedEmbeddings:
        # Never read or publish Redis entries here: Forget may run while a
        # provider is pending. All preparation is ephemeral until fenced writes.
        embedder = MemoryEmbedder(resolved, redis_client=None)
        vectors = {}
        for text in dict.fromkeys(texts):
            vectors[text] = await embedder.embed(text)
        return PreparedEmbeddings(vectors)

    async def prepare_context(
        self, user_id: str, task_text: str, user_settings: dict[str, Any]
    ) -> PreparedEmbeddings:
        """Provider work before acquiring the owner fence; bounded in total."""
        async with asyncio.timeout(30):
            resolved, _ = await asyncio.to_thread(
                self._resolve_user_settings, user_id, user_settings
            )
            return await self._prepare_embeddings(resolved, [task_text] if task_text else [])

    async def build_context(
        self,
        user_id: str,
        agent_type: str,
        task_text: str,
        embeddings: PreparedEmbeddings,
    ) -> dict[str, Any]:
        """Build pgvector AgentContext, suitable for injection into state.context."""
        mgr = self._manager(embeddings)
        try:
            ctx = await mgr.build_agent_context(UUID(str(user_id)), agent_type, task=task_text)
            data = ctx.model_dump(mode="json")
            try:
                await mgr.log_access(
                    UUID(str(user_id)),
                    query=task_text[:1000],
                    results=data.get("preferences", [])[:10],
                    agent_type=agent_type,
                )
            except Exception as exc:
                logger.debug("Semantic memory access log failed: %s", exc)
            return data
        except Exception as exc:
            logger.warning("Semantic memory context failed: %s", exc)
            return {}
        finally:
            await mgr.close()
            mgr.redis.close()

    async def prepare_after_run(
        self,
        user_id: str,
        agent_type: str,
        context: dict[str, Any],
        output: dict[str, Any],
        success: bool,
        strategy: str,
        user_settings: dict[str, Any],
    ) -> PreparedRunMemory:
        """Extract/encode without a DB transaction, owner lock or persistent cache."""
        async with asyncio.timeout(30):
            resolved, model_settings = await asyncio.to_thread(
                self._resolve_user_settings, user_id, user_settings
            )
            uid = UUID(str(user_id))
            output_summary = _summarise(output, 1500)
            episode = Episode(
                user_id=uid,
                agent_type=agent_type,
                summary=output_summary or _summarise(context, 1000),
                input=context,
                output=output,
                outcome="success" if success else "failure",
            )
            memories = _heuristic_memories(uid, agent_type, output)
            if success and output_summary and model_settings is not None:
                try:
                    from app.core.model_router import build_agent_llm

                    llm = build_agent_llm(model_settings)
                    extracted = await asyncio.wait_for(
                        self._extractor.extract_from_agent_output(
                            user_id=uid,
                            agent_type=agent_type,
                            output_text=output_summary,
                            llm=llm,
                        ),
                        timeout=20,
                    )
                    memories.extend(extracted[:8])
                except Exception as exc:
                    logger.debug("Semantic memory extraction skipped: %s", exc)
            embeddings = await self._prepare_embeddings(
                resolved, [episode.summary] + [memory.content for memory in memories]
            )
            return PreparedRunMemory(
                episode=episode,
                learning=Learning(
                    agent_type=agent_type, learning=strategy, success_rate=1.0 if success else 0.0
                ),
                memories=memories,
                embeddings=embeddings,
            )

    async def save_prepared(self, prepared: PreparedRunMemory) -> None:
        """Only local persistence; caller must hold the matching generation fence."""
        mgr = self._manager(prepared.embeddings)
        try:
            episode = prepared.episode
            mgr.set_session(
                episode.user_id, f"last_{episode.agent_type}_summary", episode.summary[:1000]
            )
            await mgr.save_episode(episode)
            await mgr.save_learning(prepared.learning)
            for memory in prepared.memories:
                await mgr.save_memory(memory)
        finally:
            await mgr.close()
            mgr.redis.close()


def _heuristic_memories(user_id: UUID, agent_type: str, output: dict[str, Any]) -> list[Memory]:
    memories: list[Memory] = []
    action_type = str(output.get("type") or agent_type)
    if action_type:
        memories.append(
            Memory(
                user_id=user_id,
                memory_type="skill",
                content=f"{agent_type} completed action type {action_type}",
                source_agent=agent_type,
                confidence=0.8,
            )
        )

    if agent_type == "job_search":
        matches = output.get("matches") or output.get("result", {}).get("matches") or []
        if isinstance(matches, list):
            for item in matches[:5]:
                if not isinstance(item, dict):
                    continue
                company = item.get("company")
                role = item.get("title") or item.get("role")
                score = item.get("match_score")
                if company and role:
                    memories.append(
                        Memory(
                            user_id=user_id,
                            memory_type="outcome",
                            content=f"Found job match: {role} at {company}"
                            + (f" with score {score}" if score is not None else ""),
                            source_agent=agent_type,
                            confidence=0.9,
                        )
                    )

    if agent_type == "linkedin":
        headline = output.get("headline")
        about = output.get("about")
        if headline:
            memories.append(
                Memory(
                    user_id=user_id,
                    memory_type="style",
                    content=f"Preferred LinkedIn headline draft: {str(headline)[:300]}",
                    source_agent=agent_type,
                    confidence=0.85,
                )
            )
        if about:
            memories.append(
                Memory(
                    user_id=user_id,
                    memory_type="fact",
                    content=f"LinkedIn about draft emphasized: {str(about)[:500]}",
                    source_agent=agent_type,
                    confidence=0.75,
                )
            )

    if agent_type == "resume" and output.get("resume_markdown"):
        memories.append(
            Memory(
                user_id=user_id,
                memory_type="skill",
                content="Resume agent produced tailored resume text for target job",
                source_agent=agent_type,
                confidence=0.8,
            )
        )

    if agent_type == "email" and (output.get("subject") or output.get("body")):
        memories.append(
            Memory(
                user_id=user_id,
                memory_type="style",
                content=f"Email draft style: subject={str(output.get('subject') or '')[:160]}",
                source_agent=agent_type,
                confidence=0.8,
            )
        )

    return memories[:8]


def _summarise(data: Any, max_len: int = 1000) -> str:
    if data is None:
        return ""
    try:
        text = json.dumps(data, default=str)
    except Exception:
        text = str(data)
    return text[:max_len]
