from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core import model_router


def test_cache_key_hides_identity_and_includes_model_and_system_prompt():
    messages = [SystemMessage(content="policy"), HumanMessage(content="private prompt")]
    prompt = model_router._messages_text(messages)
    key = model_router._cache_key("member@example.com", "job_search", prompt, "model-a")
    assert "member" not in key and "private" not in key
    assert key != model_router._cache_key("other", "job_search", prompt, "model-a")
    assert key != model_router._cache_key("member@example.com", "job_search", prompt, "model-b")
    changed = model_router._messages_text([SystemMessage(content="changed"), messages[1]])
    assert key != model_router._cache_key("member@example.com", "job_search", changed, "model-a")


def test_redacting_calls_never_store_restored_personal_data(monkeypatch):
    cache = MagicMock()
    monkeypatch.setattr(model_router, "_get_llm_cache", lambda: cache)
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content="Jane jane@example.com")
    wrapped = model_router.CachingLLM(llm, "member", "company_research", redacting=True)
    assert wrapped.invoke("prompt").content == "Jane jane@example.com"
    cache.get.assert_not_called()
    cache.setex.assert_not_called()
