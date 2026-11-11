"""
llm_proxy_service.py — Output filter to redact API keys from LLM responses.

Prevents prompt injection attacks from extracting API keys by scanning all
LLM output for known key patterns and replacing them with [REDACTED].

Integrates as a LangChain callback handler attached to every LLM call.
"""

import logging
import re
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

logger = logging.getLogger(__name__)

# Patterns that match known API key formats
# Anchored so ordinary words that merely end in "sk-" ("risk-based-testing-...")
# are left alone.
_B = r"(?<![A-Za-z0-9_-])"
_KEY_PATTERNS = [
    re.compile(_B + r"sk-[a-zA-Z0-9_-]{20,}"),  # OpenAI
    re.compile(_B + r"sk-ant-[a-zA-Z0-9_-]{20,}"),  # Anthropic
    re.compile(_B + r"AIza[a-zA-Z0-9_-]{30,}"),  # Google
    re.compile(_B + r"gsk_[a-zA-Z0-9_-]{20,}"),  # Groq
    re.compile(_B + r"nvapi-[a-zA-Z0-9_-]{20,}"),  # NVIDIA NIM
    re.compile(_B + r"hf_[a-zA-Z0-9]{20,}"),  # HuggingFace
    re.compile(_B + r"xai-[a-zA-Z0-9_-]{20,}"),  # xAI
    re.compile(r"Bearer\s+[a-zA-Z0-9_.-]{20,}"),  # Generic bearer tokens
    re.compile(_B + r"(?:sk|rk|pk)_(?:live|test)_[a-zA-Z0-9]{16,}"),  # Stripe / Clerk
    re.compile(_B + r"(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{30,}"),  # GitHub
    re.compile(_B + r"github_pat_[a-zA-Z0-9_]{40,}"),  # GitHub fine-grained
    re.compile(_B + r"xox[abprs]-[a-zA-Z0-9-]{10,}"),  # Slack
    re.compile(_B + r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"),  # AWS access key id
    re.compile(_B + r"eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"),  # JWT
]

REDACTED = "[REDACTED]"


def redact_keys(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Scan text for API key patterns and replace with [REDACTED].

    Args:
        text: Raw LLM output text
        secrets: Exact secret values to remove as well, for keys whose
            provider format no pattern recognises (e.g. unprefixed keys).

    Returns:
        Sanitized text with any detected keys redacted
    """
    if not text:
        return text

    result = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            result = result.replace(secret, REDACTED)
    for pattern in _KEY_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


class KeyRedactionCallback(BaseCallbackHandler):
    """LangChain callback that redacts API keys from LLM output.

    Attach to any LLM call to ensure keys never leak through prompt injection.

    Usage:
        llm = ChatOpenAI(..., callbacks=[KeyRedactionCallback()])
    """

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Redact keys from all generations in the response."""
        for generations in response.generations:
            for gen in generations:
                if gen.text:
                    original = gen.text
                    gen.text = redact_keys(gen.text)
                    if gen.text != original:
                        logger.warning("Redacted potential API key from LLM output")
                if hasattr(gen, "message") and gen.message and gen.message.content:
                    original = gen.message.content
                    gen.message.content = redact_keys(gen.message.content)


def get_redaction_callback() -> KeyRedactionCallback:
    """Get a singleton redaction callback instance."""
    return KeyRedactionCallback()
