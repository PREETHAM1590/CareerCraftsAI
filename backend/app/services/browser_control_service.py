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


async def run_browser_task(
    llm: BaseChatModel,
    task: str,
    user_id: str,
    max_steps: int = 15,
    live_browser: bool = False,
    run_id: str | None = None,
) -> str:
    """Server browser execution is retired; never bypass the paired extension."""
    raise RuntimeError("Use a public job connector or the paired browser extension")


async def apply_to_job(
    llm: BaseChatModel,
    user_id: str,
    job_url: str,
    applicant_info: str,
    submit: bool = False,
    live_browser: bool = False,
    run_id: str | None = None,
) -> str:
    """Autonomously fill any job application form via the browser agent.

    submit=False: fill through to the final review screen and stop (READY_FOR_REVIEW).
    submit=True: fill and click the final Submit/Apply button (SUBMITTED).
    """
    final = (
        "Click the final Submit/Apply button to submit the application, then confirm SUBMITTED."
        if submit
        else "Stop before the final Submit/Apply button and report READY_FOR_REVIEW with what was filled."
    )
    task = (
        f"Go to {job_url}. Click the Apply / Easy Apply / Apply now button. "
        f"Fill every required field of the application form using this applicant profile:\n{applicant_info[:1500]}\n"
        f"Use reasonable, truthful answers; leave optional fields blank if unknown. "
        f"Upload a resume only if a file picker requires it and a file is available, otherwise skip. "
        f"Proceed through multi-step forms. {final} "
        f"If login, CAPTCHA, OTP, payment, account creation, or missing required personal data blocks "
        f"progress, stop and report REQUIRES_MANUAL."
    )
    return await run_browser_task_with_captcha_retry(
        llm, task, user_id, max_steps=25, live_browser=live_browser, run_id=run_id
    )
