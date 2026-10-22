"""Proxycurl service for LinkedIn profile data retrieval."""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

PROXYCURL_BASE_URL = "https://nubela.co/proxycurl/api"


class ProxycurlService:
    """Service for finding contacts at a company via the Proxycurl API."""

    def __init__(self) -> None:
        self.api_key = settings.PROXYCURL_API_KEY
        self.base_url = PROXYCURL_BASE_URL

    async def find_contacts(
        self, company: str, role_context: str | None = None
    ) -> list[dict]:
        """Find up to 10 contacts at a company relevant to the role context.

        Args:
            company: The target company name.
            role_context: Optional role/department context to refine results.

        Returns:
            A list of up to 10 dicts with keys: name, title, linkedin_url.
            Returns an empty list on any error.
        """
        if not self.api_key:
            logger.warning("PROXYCURL_API_KEY not configured, returning empty contacts")
            return []

        params: dict = {
            "company_name": company,
            "page_size": 10,
        }
        if role_context:
            params["keyword_filter"] = role_context

        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.base_url}/search/person",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()

            data = response.json()
            profiles = data.get("results", [])

            contacts = []
            for profile in profiles[:10]:
                contacts.append(
                    {
                        "name": profile.get("name", "Unknown"),
                        "title": profile.get("title", ""),
                        "linkedin_url": profile.get("linkedin_profile_url", ""),
                    }
                )

            return contacts

        except httpx.HTTPStatusError as e:
            logger.error(
                "Proxycurl API returned HTTP %s for company=%s: %s",
                e.response.status_code,
                company,
                e.response.text[:200],
            )
            return []
        except httpx.RequestError as e:
            logger.error(
                "Proxycurl request failed for company=%s: %s", company, str(e)
            )
            return []
        except Exception as e:
            logger.error(
                "Unexpected error in ProxycurlService for company=%s: %s",
                company,
                str(e),
            )
            return []
