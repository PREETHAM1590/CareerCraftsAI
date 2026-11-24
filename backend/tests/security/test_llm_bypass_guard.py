"""No chat-model call may bypass the LLM gateway (and its PII redaction).

Provider chat clients may only be constructed in the gateway and the model
router; agents and services get a key-free gateway client. A new direct
construction site, provider SDK import or provider endpoint in app code fails
here. If one is genuinely needed, route it through llm_gateway instead of
adding it to the allowlists below.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# Where provider chat clients are constructed. model_router._make_llm builds
# them for the gateway's proxy and for _build_llm (the Settings key test).
CHAT_CLIENT_SITES = {"core/model_router.py", "core/llm_gateway.py"}
# Callers of the direct (non-gateway) builders.
DIRECT_BUILDER_CALLERS = {
    "_make_llm": {"core/model_router.py", "core/llm_gateway.py"},
    # Settings "test this key" sends a fixed prompt with no member data.
    "_build_llm": {"core/model_router.py", "api/v1/users.py"},
}
PROVIDER_PACKAGES = (
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
    "langchain_google_vertexai",
    "langchain_ollama",
    "langchain_groq",
    "langchain_mistralai",
    "langchain_nvidia_ai_endpoints",
    "langchain_deepseek",
    "langchain_xai",
    "langchain_community.chat_models",
    "langchain.chat_models",
)
# Raw provider SDKs: nothing in app/ should talk to a provider without LangChain.
RAW_SDKS = ("openai", "anthropic", "google.generativeai", "google.genai", "groq", "mistralai")
PROVIDER_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.groq.com",
    "integrate.api.nvidia.com",
    "openrouter.ai",
    "api.deepseek.com",
    "api.x.ai",
    "api.mistral.ai",
)
# Known model calls outside the gateway, each a documented gap
# (docs/PRIVACY_PII.md). Do not add to this list.
KNOWN_EXTERNAL_MODEL_ENDPOINTS = {
    # Decision engine ("/v1/systemone"): form-option matching on TypeSafe Jev
    # or a self-hosted Laya, only when configured.
    "services/decision_engine.py",
}


def _sources():
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        yield rel, path.read_text(encoding="utf-8"), ast.parse(path.read_text(encoding="utf-8"))


def test_chat_clients_are_only_built_in_the_gateway_and_router():
    offenders = []
    for rel, _, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(PROVIDER_PACKAGES):
                    names = [a.name for a in node.names]
                    chat = [n for n in names if n.startswith("Chat") or n == "init_chat_model"]
                    if chat and rel not in CHAT_CLIENT_SITES:
                        offenders.append(f"{rel}: imports {chat} from {node.module}")
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for module in modules:
                    if any(module == sdk or module.startswith(sdk + ".") for sdk in RAW_SDKS):
                        offenders.append(f"{rel}: imports provider SDK {module}")
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "init_chat_model" and rel not in CHAT_CLIENT_SITES:
                    offenders.append(f"{rel}: init_chat_model()")
    assert offenders == []


def test_direct_builders_are_only_used_where_allowed():
    offenders = []
    for rel, _, tree in _sources():
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.name
            if name in DIRECT_BUILDER_CALLERS and rel not in DIRECT_BUILDER_CALLERS[name]:
                offenders.append(f"{rel}: uses {name}")
    assert offenders == []


def test_no_provider_endpoints_outside_the_router():
    offenders = []
    for rel, text, _ in _sources():
        if rel in CHAT_CLIENT_SITES or rel == "core/config.py":
            continue
        for host in PROVIDER_HOSTS:
            if host in text:
                for line in text.splitlines():
                    # Careers pages of AI companies are job sources, not model calls.
                    if host in line and "/careers" not in line and "/jobs" not in line:
                        offenders.append(f"{rel}: {host}")
        if "chat/completions" in text or "/v1/systemone" in text:
            if rel not in KNOWN_EXTERNAL_MODEL_ENDPOINTS:
                offenders.append(f"{rel}: model endpoint")
    assert offenders == []


def test_get_llm_goes_through_the_gateway():
    source = (APP / "core" / "model_router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    get_llm = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_llm"
    )
    called = {
        (c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", ""))
        for c in ast.walk(get_llm)
        if isinstance(c, ast.Call)
    }
    assert "gateway_chat_model" in called
    assert not called & {"_make_llm", "decrypt_api_key"}
