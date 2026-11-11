"""Browser compatibility helpers. Server task execution is retired.

Application filling and submission use the paired extension with mandatory
review. Retired entry points fail closed.
"""

import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from app.core.config import settings

logger = logging.getLogger(__name__)

# Persistent browser data directory — cookies survive restarts
BROWSER_DATA_DIR = (
    Path(settings.BASE_DIR if hasattr(settings, "BASE_DIR") else ".") / ".browser_data"
)

# Site challenges stop automation; only the user may resolve them.
# Strings that look like a CAPTCHA / WAF / anti-bot block page.
_CAPTCHA_MARKERS: tuple[str, ...] = (
    "captcha",
    "unusual traffic",
    "are you a human",
    "verify you are",
    "access denied",
    "rate limit",
    "too many requests",
    "bot detection",
    "please complete the security check",
    "are you a robot",
    "checking your browser",
    "cloudflare",
    "perimeterx",
    "datadome",
    "incapsula",
    "distil",
    "akamai",
    "just a moment",
)


class CaptchaBlocked(RuntimeError):
    """A site challenge requires manual intervention; automation stops."""


def _looks_like_captcha(text: str) -> bool:
    return any(marker in (text or "").lower() for marker in _CAPTCHA_MARKERS)


async def run_browser_task_with_captcha_retry(
    llm: BaseChatModel,
    task: str,
    user_id: str,
    max_steps: int = 15,
    live_browser: bool = False,
    run_id: str | None = None,
) -> str:
    """Compatibility entry point: one attempt, stop on challenge, never evade it."""
    result = await run_browser_task(
        llm, task, user_id, max_steps=max_steps, live_browser=live_browser, run_id=run_id
    )
    if _looks_like_captcha(result):
        raise CaptchaBlocked("Site challenge requires manual intervention")
    return result


class BrowserAutomationRetired(RuntimeError):
    """Server-side browser automation was replaced by the paired extension."""


async def run_browser_task(
    llm: BaseChatModel,
    task: str,
    user_id: str,
    max_steps: int = 15,
    live_browser: bool = False,
    run_id: str | None = None,
) -> str:
    """Server browser execution is retired; never bypass the paired extension."""
    raise BrowserAutomationRetired("Use a public job connector or the paired browser extension")
