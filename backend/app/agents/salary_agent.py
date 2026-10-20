"""
salary_agent.py — Salary Intelligence & Negotiation Assistant.

LangGraph node that:
1. Queries Exa.ai for real-time salary data (keyless web search without a key)
2. Extracts 25th, 50th, 75th percentile figures, in the currency the sources use
3. Classifies user's offer against market percentiles
4. Generates a negotiation script (opening, counter-offer at p75, 2 justifications)
5. Logs run to agent_runs table
6. Returns awaiting_approval for HITL gate on negotiation script

Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.8, 3.10
"""

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from enum import Enum

from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from app.agents._llm_json import call_llm_json
from app.agents.state import AgentState
from app.core.model_router import build_agent_llm
from app.core.sync_db import _get_sync_factory, fetch_model_settings
from app.services import web_research
from app.services.exa_service import ExaService

logger = logging.getLogger(__name__)

AGENT_TYPE = "salary_intelligence"


class NegotiationDraft(BaseModel):
    opening: str
    counter_offer: int = Field(ge=0)
    justifications: list[str] = Field(min_length=2)


NEGOTIATION_SYSTEM_PROMPT = """You are a salary negotiation expert. Given the role, company,
market salary percentiles, and the candidate's offer classification, generate a negotiation script.
Quote amounts in the currency and notation given (for example ₹38 lakh, $184,000).

The script MUST contain exactly these sections:
1. "opening" — A confident opening statement for the negotiation conversation
2. "counter_offer" — A specific counter-offer amount set at the 75th percentile value provided
3. "justifications" — Exactly 2 compelling justification points supporting the counter-offer

Return your response as a JSON object with keys: "opening", "counter_offer", "justifications"
(justifications is a list of 2 strings).
Do NOT include markdown fences or any text outside the JSON."""


# ─────────────────────────────────────────────────────────────────────────────
# Pure function: classify offer against market percentiles
# ─────────────────────────────────────────────────────────────────────────────


class OfferClassification(str, Enum):
    """Classification of an offer relative to market percentiles."""

    BELOW_MARKET = "below_market"
    AT_MARKET = "at_market"
    ABOVE_MARKET = "above_market"


def classify_offer(offer: int, p25: int, p50: int, p75: int) -> OfferClassification:
    """Classify offer against market percentiles.

    - below_market: offer < p25
    - above_market: offer > p75
    - at_market: p25 <= offer <= p75 (inclusive on both bounds)

    Args:
        offer: The candidate's offer amount.
        p25: 25th percentile salary.
        p50: 50th percentile salary (median).
        p75: 75th percentile salary.

    Returns:
        OfferClassification enum value.
    """
    if offer < p25:
        return OfferClassification.BELOW_MARKET
    elif offer > p75:
        return OfferClassification.ABOVE_MARKET
    return OfferClassification.AT_MARKET


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


# Annual figures only; anything outside these bounds is a typo, an hourly
# rate or a different unit, not a salary.
_ANNUAL_BOUNDS = {
    "INR": (100_000, 100_000_000),
    "USD": (15_000, 2_000_000),
    "EUR": (12_000, 1_500_000),
    "GBP": (12_000, 1_500_000),
}
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_INR_PREFIX = r"(?:₹|rs\.?|inr)\s*"
_INR_LAKH = re.compile(
    rf"(?:{_INR_PREFIX})?(\d{{1,3}}(?:\.\d+)?)\s*(?:l\b|lakhs?\b|lacs?\b|lpa\b)", re.IGNORECASE
)
_INR_CRORE = re.compile(
    rf"(?:{_INR_PREFIX})?(\d{{1,2}}(?:\.\d+)?)\s*(?:cr\b|crores?\b)", re.IGNORECASE
)
_INR_PLAIN = re.compile(
    rf"{_INR_PREFIX}(\d{{1,3}}(?:,\d{{2,3}})+|\d{{5,}})(?!\s*(?:l\b|lakh|lac|lpa|cr))",
    re.IGNORECASE,
)
_FOREIGN = re.compile(r"([$€£])\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM]\b)?")
_HOURLY = re.compile(r"^\s*(?:/\s*h(?:ou)?r|per\s+hour|an\s+hour|hourly)", re.IGNORECASE)
_MONTHLY = re.compile(r"^\s*(?:/\s*mo(?:nth)?|per\s+month|a\s+month|monthly)", re.IGNORECASE)


def _salary_figures(text: str) -> list[tuple[str, int]]:
    """Annual salary figures quoted in text, as (ISO currency, amount)."""
    figures: list[tuple[str, int]] = []

    def add(currency: str, amount: float, end: int) -> None:
        tail = text[end : end + 20]
        if _HOURLY.match(tail):
            return
        if _MONTHLY.match(tail):
            amount *= 12
        low, high = _ANNUAL_BOUNDS[currency]
        if low <= amount <= high:
            figures.append((currency, int(round(amount))))

    for match in _INR_LAKH.finditer(text):
        add("INR", float(match.group(1)) * 100_000, match.end())
    for match in _INR_CRORE.finditer(text):
        add("INR", float(match.group(1)) * 10_000_000, match.end())
    for match in _INR_PLAIN.finditer(text):
        add("INR", float(match.group(1).replace(",", "")), match.end())
    for match in _FOREIGN.finditer(text):
        amount = float(match.group(2).replace(",", ""))
        unit = (match.group(3) or "").lower()
        amount *= 1_000 if unit == "k" else 1_000_000 if unit == "m" else 1
        add(_SYMBOLS[match.group(1)], amount, match.end())
    return figures


def _extract_percentiles(salary_results: list[dict]) -> dict | None:
    """p25/p50/p75 of the salary figures quoted across the results.

    Figures are kept in the currency most of them use (never mixed or
    converted). Returns None when fewer than three figures are found —
    reported to the user as data unavailable rather than a guess.
    """
    by_currency: dict[str, list[int]] = {}
    sources: dict[str, set[str]] = {}
    for result in salary_results:
        text = " ".join(str(result.get(key) or "") for key in ("title", "text", "snippet"))
        for currency, amount in _salary_figures(text):
            by_currency.setdefault(currency, []).append(amount)
            if result.get("url"):
                sources.setdefault(currency, set()).add(result["url"])

    if not by_currency:
        return None
    currency = max(by_currency, key=lambda c: len(by_currency[c]))
    salary_numbers = sorted(by_currency[currency])
    if len(salary_numbers) < 3:
        return None

    n = len(salary_numbers)
    return {
        "p25": salary_numbers[max(0, n // 4)],
        "p50": salary_numbers[n // 2],
        "p75": salary_numbers[min(n - 1, (3 * n) // 4)],
        "currency": currency,
        "sample_size": n,
        "sources": sorted(sources.get(currency, set())),
    }


def format_amount(amount: int, currency: str) -> str:
    """Human notation for a salary: ₹38.6 lakh, $184,570, €72,000."""
    if currency == "INR":
        if amount >= 10_000_000:
            return f"₹{amount / 10_000_000:.2f} crore"
        return f"₹{amount / 100_000:.1f} lakh"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{amount:,}"


async def _search_salary_sources(role: str, company: str | None, location: str) -> list[dict]:
    """Salary pages about the role: Exa when configured, else keyless web search.

    Keyless search asks for the company-specific figure first, then the
    market in that location.
    """
    results = await ExaService().search_salary(role, company, location or None)
    if _extract_percentiles(results):
        return results

    queries = []
    if company:
        queries.append(" ".join(filter(None, [role, "salary", company, location])))
    queries.append(" ".join(filter(None, [role, "salary", location])))
    seen = {r.get("url") for r in results}
    for query in queries:
        for hit in await web_research.duckduckgo(query, limit=8):
            if hit["url"] in seen:
                continue
            seen.add(hit["url"])
            results.append({"url": hit["url"], "title": hit["title"], "text": hit["snippet"]})
    return results


def _log_agent_run(
    user_id: str,
    run_id: str,
    status: str,
    input_data: dict,
    output_data: dict | None,
    tokens_used: int | None,
    duration_ms: int,
) -> None:
    """Log run to agent_runs table synchronously (called from thread executor)."""
    from app.core.event_bus import suppress_terminal_events

    if suppress_terminal_events.get():
        return
    from app.models.db import AgentRun

    factory = _get_sync_factory()
    with factory() as db:
        run_uuid = uuid.UUID(run_id)
        agent_run = db.get(AgentRun, run_uuid)
        if agent_run is None:
            agent_run = AgentRun(
                id=run_uuid,
                user_id=uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
                agent_type=AGENT_TYPE,
            )
            db.add(agent_run)
        agent_run.status = status
        agent_run.input = input_data
        agent_run.output = output_data
        agent_run.tokens_used = tokens_used
        agent_run.duration_ms = duration_ms
        agent_run.completed_at = datetime.now(timezone.utc) if status != "running" else None
        db.commit()


def _generate_negotiation_script(
    llm,
    role: str,
    company: str | None,
    p25: int,
    p50: int,
    p75: int,
    classification: str,
    currency: str = "USD",
) -> dict:
    """Generate negotiation script using LLM.

    Returns dict with: opening, counter_offer, justifications.
    """
    company_text = f" at {company}" if company else ""
    prompt_content = (
        f"Role: {role}{company_text}\n"
        f"Market Salary Data (annual, {currency}):\n"
        f"  - 25th percentile: {format_amount(p25, currency)}\n"
        f"  - 50th percentile (median): {format_amount(p50, currency)}\n"
        f"  - 75th percentile: {format_amount(p75, currency)}\n"
        f"Offer classification: {classification}\n"
        f"Counter-offer target: {format_amount(p75, currency)} (75th percentile)\n\n"
        f"Generate the negotiation script."
    )

    script = call_llm_json(
        llm,
        NEGOTIATION_SYSTEM_PROMPT,
        prompt_content,
        NegotiationDraft,
    ).model_dump()
    script["counter_offer"] = p75
    return script


# ─────────────────────────────────────────────────────────────────────────────
# LangGraph Node
# ─────────────────────────────────────────────────────────────────────────────


def salary_report_node(state: AgentState) -> AgentState:
    """LangGraph node for salary intelligence and negotiation script generation.

    Expected context keys:
        - role (str): Job title / role
        - company (str, optional): Company name
        - location (str): Location for salary data
        - offer_amount (int, optional): User's current offer for classification

    Returns:
        - On success with data: status=awaiting_approval, pending_action with report + script
        - On data unavailable: status=completed, result with data_unavailable=True
        - On error: status=failed, error message
    """

    start_time = time.monotonic()
    run_id = state.get("run_id") or str(uuid.uuid4())

    try:
        user_id = state["user_id"]
        context = state["context"]
        role = context.get("role", "")
        company = context.get("company")
        location = context.get("location", "")
        offer_amount = context.get("offer_amount")

        if not role:
            return {
                **state,
                "status": "failed",
                "error": "'role' is required in context.",
            }

        # Get user's model settings for LLM routing (Requirement 3.7)
        model_settings = state.get("model_settings") or fetch_model_settings(user_id)
        if not model_settings:
            return {
                **state,
                "status": "failed",
                "error": "No active model settings configured for user.",
            }

        # Query salary sources (Requirement 3.1)
        from app.core.sync_db import run_coro_sync

        salary_results = run_coro_sync(_search_salary_sources(role, company, location))

        # Extract percentiles from search results (Requirement 3.2)
        percentiles = _extract_percentiles(salary_results)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Handle data_unavailable case — HTTP 206 (Requirement 3.8)
        if percentiles is None:
            result = {
                "data_unavailable": True,
                "role": role,
                "company": company,
                "location": location,
                "data_sources": [r.get("url", "") for r in salary_results[:5]],
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }

            _log_agent_run(
                user_id=user_id,
                run_id=run_id,
                status="completed",
                input_data={"role": role, "company": company, "location": location},
                output_data=result,
                tokens_used=None,
                duration_ms=duration_ms,
            )

            return {
                **state,
                "status": "completed",
                "result": result,
            }

        p25, p50, p75 = percentiles["p25"], percentiles["p50"], percentiles["p75"]
        currency = percentiles["currency"]

        # Classify offer if provided (Requirement 3.3)
        classification = None
        if offer_amount is not None:
            classification = classify_offer(offer_amount, p25, p50, p75).value

        # Build LLM and generate negotiation script (Requirements 3.4, 3.7)
        llm = build_agent_llm(model_settings)
        script = _generate_negotiation_script(
            llm, role, company, p25, p50, p75, classification or "at_market", currency
        )

        duration_ms = int((time.monotonic() - start_time) * 1000)

        report = {
            "p25": p25,
            "p50": p50,
            "p75": p75,
            "offer_amount": offer_amount,
            "classification": classification,
            "currency": currency,
            "sample_size": percentiles["sample_size"],
            "data_sources": percentiles["sources"][:8],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "company": company,
            "location": location,
        }

        # Log run to agent_runs (Requirement 3.6)
        _log_agent_run(
            user_id=user_id,
            run_id=run_id,
            status="awaiting_approval",
            input_data={
                "role": role,
                "company": company,
                "location": location,
                "offer_amount": offer_amount,
            },
            output_data={"report": report, "script": script},
            tokens_used=None,  # Token tracking handled by LangChain callbacks
            duration_ms=duration_ms,
        )

        # Return awaiting_approval for HITL gate (Requirement 3.10)
        return {
            **state,
            "status": "awaiting_approval",
            "pending_action": {
                "type": "salary_report_review",
                "report": report,
                "script": script,
            },
            "result": report,
            "messages": state["messages"]
            + [AIMessage(content=f"Salary report generated for {role} in {location}.")],
        }

    except Exception as exc:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        logger.error("Salary agent failed for user %s: %s", state.get("user_id"), exc)
        return {**state, "status": "failed", "error": "Agent failed"}
