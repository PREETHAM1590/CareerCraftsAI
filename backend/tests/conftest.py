import os
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from langchain_core.messages import AIMessage
except ImportError:
    AIMessage = None

# Bootstrap env vars before any `from app...` import so Settings() succeeds.
# Individual tests still monkeypatch via test_config.py.
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-32-chars-minimum!!")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")


if AIMessage is not None:

    class _MutableFakeLLM:
        """Mutable LLM stand-in: supports ``mock_llm.responses = [...]`` after construction."""

        def __init__(self, responses: list[str] | None = None):
            self.responses: list[str] = responses or ["mocked LLM response"]
            self._call_count = 0

        def invoke(self, messages) -> AIMessage:
            if self._call_count < len(self.responses):
                content = self.responses[self._call_count]
            else:
                content = self.responses[-1] if self.responses else ""
            self._call_count += 1
            return AIMessage(content=content)

    @pytest.fixture
    def mock_llm():
        return _MutableFakeLLM()


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = "00000000-0000-0000-0000-000000000001"
    user.email = "test@example.com"
    user.deletion_scheduled_for = None
    user.clerk_user_id = "00000000-0000-0000-0000-000000000abc"
    return user


@pytest.fixture(autouse=True)
def _clear_verified_token_cache():
    """Tests reuse token strings with different fake payloads; never share them."""
    from app.core import clerk_auth

    clerk_auth._token_cache.clear()
    yield
    clerk_auth._token_cache.clear()


@pytest.fixture(autouse=True)
def _search_cache_off(monkeypatch):
    """Search tests run uncached unless they opt in (see test_search_cache.py)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEARCH_CACHE_ENABLED", False)
