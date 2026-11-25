"""Typed, fast decisions for the browser extension ("System One" models).

A request is {state, questions}; each question is one of
  choice — pick one key of `criteria` ({key: description})
  score  — place the state on an ordered rubric (`criteria`: [levels…])
  noul   — probability that a statement is true
and every answer carries calibrated probabilities, so callers can act only
above a confidence threshold and hand the rest to the user.

Providers (DECISION_ENGINE_PROVIDER):
  jev  — TypeSafe's hosted Jev, POST {TYPESAFE_BASE_URL}/v1/systemone
  laya — a self-hosted Laya server exposing the same endpoint (deploy/laya)
  none — the heuristics below (token overlap), no network call
"auto" picks jev when TYPESAFE_API_KEY is set, else laya when LAYA_URL is
set, else none. A provider error falls back to the heuristics, so a model
outage slows the extension down instead of stopping it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_STATE_CHARS = 12_000
MAX_QUESTIONS = 16
MAX_CHOICE_OPTIONS = 255

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "and",
    "or",
    "in",
    "for",
    "is",
    "are",
    "your",
    "you",
    "do",
    "please",
}


def provider() -> str:
    configured = settings.DECISION_ENGINE_PROVIDER
    if configured != "auto":
        return configured
    if settings.TYPESAFE_API_KEY:
        return "jev"
    if settings.LAYA_URL:
        return "laya"
    return "none"


def validate_questions(questions: dict) -> None:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty object")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"at most {MAX_QUESTIONS} questions per call")
    for name, q in questions.items():
        kind = (q or {}).get("type")
        if kind not in {"choice", "score", "noul"}:
            raise ValueError(f"question {name!r}: type must be choice, score or noul")
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            raise ValueError(f"question {name!r}: instructions are required")
        criteria = q.get("criteria")
        if kind == "choice" and (
            not isinstance(criteria, dict) or not 2 <= len(criteria) <= MAX_CHOICE_OPTIONS
        ):
            raise ValueError(f"question {name!r}: choice needs 2-{MAX_CHOICE_OPTIONS} criteria")
        if kind == "score" and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 10):
            raise ValueError(f"question {name!r}: score needs 2-10 levels")


async def decide(user_id: str | None, state: Any, questions: dict) -> dict:
    """Answer `questions` about `state`. Returns
    {"provider": str, "answers": {name: {...}}} in the Jev answer shape.

    ``user_id`` loads the member's known PII so the outbound form state is
    redacted exactly like a gateway chat call."""
    validate_questions(questions)
    chosen = provider()
    if chosen in {"jev", "laya"}:
        try:
            answers = await _remote(user_id, chosen, state, questions)
            return {"provider": chosen, "answers": answers}
        except Exception as exc:
            logger.warning(
                "Decision engine %s failed, using heuristics: %s", chosen, type(exc).__name__
            )
    return {
        "provider": "heuristic",
        "answers": {n: _heuristic(state, q) for n, q in questions.items()},
    }


async def _remote(user_id: str | None, chosen: str, state: Any, questions: dict) -> dict:
    if chosen == "jev":
        url = settings.TYPESAFE_BASE_URL.rstrip("/") + "/v1/systemone"
        headers = {"Authorization": f"Bearer {settings.TYPESAFE_API_KEY}"}
        body = {"model": settings.TYPESAFE_MODEL, "state": state, "questions": questions}
    else:
        url = settings.LAYA_URL.rstrip("/") + "/v1/systemone"
        headers = (
            {"Authorization": f"Bearer {settings.LAYA_API_KEY}"} if settings.LAYA_API_KEY else {}
        )
        body = {"state": state, "questions": questions}
    body["state"], body["questions"] = await _redact_payload(
        user_id, body["state"], body["questions"]
    )
    async with httpx.AsyncClient(timeout=settings.DECISION_ENGINE_TIMEOUT_S) as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        answers = response.json().get("answers")
    if not isinstance(answers, dict):
        raise ValueError("decision engine returned no answers")
    return answers


# One form plan matches many select/radio fields in a row; reuse the member's
# privacy context briefly instead of reloading the profile for each one.
_PRIVACY_TTL_S = 60.0
_PRIVACY_MAX = 256
_privacy_cache: dict[str, tuple[float, Any, Any]] = {}


async def _privacy_context(user_id: str | None):
    from app.services import pii_profile

    key = str(user_id)
    now = time.monotonic()
    hit = _privacy_cache.get(key)
    if hit and hit[0] > now:
        return hit[1], hit[2]
    policy, known = await pii_profile.load_privacy_context(user_id)
    if len(_privacy_cache) >= _PRIVACY_MAX:
        _privacy_cache.clear()
    _privacy_cache[key] = (now + _PRIVACY_TTL_S, policy, known)
    return policy, known


async def _redact_payload(user_id: str | None, state: Any, questions: dict) -> tuple[Any, dict]:
    """Swap the member's personal values for placeholders before the form
    state leaves for the decision provider — the same privacy boundary every
    chat-model path goes through. Mirrors the gateway's _privacy_vault: a
    loading failure stops the call in strict mode, and a member's disabled
    privacy policy turns redaction off."""
    from app.core.config import settings as s
    from app.core.pii_vault import PiiVault

    if not s.PII_REDACTION_ENABLED:
        return state, questions
    try:
        policy, known = await _privacy_context(user_id)
        if not policy.enabled:
            return state, questions
        vault = PiiVault(known, detect_patterns=policy.detect_patterns)
    except Exception as exc:
        logger.warning("decision_engine_pii_unavailable error_type=%s", type(exc).__name__)
        if s.PII_REDACTION_STRICT:
            raise
        return state, questions
    redacted_state, redacted_questions = vault.redact_obj(state), vault.redact_obj(questions)
    if vault.summary():
        logger.info("decision_engine_pii_redacted counts=%s", vault.summary())
    return redacted_state, redacted_questions


# ── Heuristic fallback ──────────────────────────────────────────────


def _tokens(value: Any) -> set[str]:
    return {t for t in _WORD.findall(str(value).lower()) if t not in _STOP}


def _similarity(a: Any, b: Any) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb)
    # "LinkedIn" vs "LinkedIn Jobs", "No" vs "No, I do not": every word of
    # one side appears in the other — strong evidence, but short of exact.
    contained = 0.85 if overlap == min(len(ta), len(tb)) else 0.0
    return max(overlap / len(ta | tb), contained)


def _heuristic(state: Any, question: dict) -> dict:
    kind = question["type"]
    if kind == "choice":
        criteria: dict = question["criteria"]
        target = state.get("value") if isinstance(state, dict) and "value" in state else state
        scores = {
            key: max(
                _similarity(target, key),
                _similarity(target, desc),
                (
                    1.0
                    if str(target).strip().lower() in {str(key).lower(), str(desc).lower()}
                    else 0.0
                ),
            )
            for key, desc in criteria.items()
        }
        total = sum(scores.values())
        if total == 0:
            probs = {k: 1 / len(criteria) for k in criteria}
        else:
            probs = {k: v / total for k, v in scores.items()}
        best = max(probs, key=probs.get)
        # Heuristic probabilities are not calibrated: report the raw overlap
        # of the winner as confidence so weak matches stay below threshold.
        return {
            "type": "choice",
            "choice": best,
            "probabilities": probs,
            "confidence": scores[best],
        }
    if kind == "noul":
        return {"type": "noul", "noul": 0.5}
    levels = question["criteria"]
    return {
        "type": "score",
        "score": (len(levels) - 1) / 2,
        "confidence": 0.0,
        "probabilities": {str(i): 1 / len(levels) for i in range(len(levels))},
    }


async def match_option(
    value: Any,
    options: list[str],
    label: str = "",
    *,
    local_only: bool = False,
    user_id: str | None = None,
) -> tuple[str | None, float]:
    """Pick the option of a select/radio group that means `value`.
    Returns (option, confidence); option is None below the threshold.

    ``local_only`` keeps the stored answer on this server (heuristics only):
    for identity and sensitive questions, whose answer must not reach an
    external decision service."""
    if not options or value in (None, ""):
        return None, 0.0
    for option in options:  # exact match needs no model
        if str(option).strip().lower() == str(value).strip().lower():
            return option, 1.0
    criteria = {f"o{i}": option for i, option in enumerate(options[:MAX_CHOICE_OPTIONS])}
    state = {"question": label, "value": str(value)}
    question = {
        "type": "choice",
        "instructions": (
            "Which option of the form question `question` has the same meaning as `value`?"
        ),
        "criteria": criteria,
    }
    if local_only:
        result = {"answers": {"option": _heuristic(state, question)}}
    else:
        result = await decide(str(user_id) if user_id else None, state, {"option": question})
    answer = result["answers"].get("option") or {}
    key, confidence = answer.get("choice"), float(answer.get("confidence") or 0.0)
    if key not in criteria or confidence < settings.DECISION_ENGINE_MIN_CONFIDENCE:
        return None, confidence
    return criteria[key], confidence
