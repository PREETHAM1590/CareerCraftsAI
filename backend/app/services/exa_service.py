"""
Exa.ai search service for salary data, company news, and tech stack research.

Used by Salary Agent and Company Research Agent.
"""
import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
REQUEST_TIMEOUT = 15.0


class ExaService:
    """Client for the Exa.ai search API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.EXA_API_KEY

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _search(self, query: str, num_results: int = 5, **kwargs: Any) -> list[dict]:
        """Execute a search request against Exa.ai and return results."""
        if not self.api_key:
            logger.warning("EXA_API_KEY not configured — skipping Exa search")
            return []

        payload: dict[str, Any] = {
            "query": query,
            "numResults": num_results,
            "useAutoprompt": True,
            **kwargs,
        }

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    EXA_SEARCH_URL,
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            return data.get("results", [])
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Exa search HTTP error for query '%s': %s %s",
                query,
                exc.response.status_code,
                exc.response.text[:200],
            )
            return []
        except Exception as exc:
            logger.warning("Exa search failed for query '%s': %s", query, exc)
            return []

    async def search_salary(
        self, role: str, company: str | None = None, location: str | None = None
    ) -> list[dict]:
        """Search for salary data for a given role, company, and location.

        Returns a list of Exa search results containing salary information.
        Returns empty list on failure or missing API key.
        """
        parts = [role, "salary", "compensation"]
        if company:
            parts.append(company)
        if location:
            parts.append(location)
        query = " ".join(parts)

        return await self._search(query, num_results=10)

    async def search_news(self, company: str) -> list[dict]:
        """Search for recent news about a company.

        Returns a list of Exa search results with news articles.
        Returns empty list on failure or missing API key.
        """
        query = f"{company} company news recent announcements"
        return await self._search(query, num_results=5)

    async def search_tech_stack(self, company: str) -> list[dict]:
        """Search for information about a company's technology stack.

        Returns a list of Exa search results about the company's tech stack.
        Returns empty list on failure or missing API key.
        """
        query = f"{company} engineering tech stack technology tools"
        return await self._search(query, num_results=5)
