"""
YouTube Data API v3 service.

search.list costs 100 quota units per call (default: 10,000/day).
Always cache results in Redis with 24-hour TTL.
"""
import hashlib
import json
import logging
from datetime import datetime

import httpx
import redis

from app.core.config import settings

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
CACHE_TTL = 86_400  # 24 hours

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def _cache_key(query: str) -> str:
    return f"yt:videos:{hashlib.sha256(query.encode()).hexdigest()}"


def _build_query(company: str, role: str) -> str:
    generic_companies = {"", "n/a", "unknown", "company"}
    if company and company.lower().strip() not in generic_companies:
        return f"{company} {role} interview questions {datetime.now().year}"
    return f"{role} behavioral interview STAR method questions"


async def search_interview_videos(
    company: str, role: str, max_results: int = 6
) -> list[dict]:
    query = _build_query(company, role)

    try:
        r = _get_redis()
        cached = r.get(_cache_key(query))
        if cached:
            logger.debug("YouTube cache hit for query: %s", query)
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis unavailable for YouTube cache read: %s", exc)

    if not settings.YOUTUBE_API_KEY:
        logger.warning("YOUTUBE_API_KEY not configured — skipping YouTube search")
        return []

    try:
        params = {
            "part": "snippet",
            "type": "video",
            "maxResults": max_results,
            "q": query,
            "key": settings.YOUTUBE_API_KEY,
            "relevanceLanguage": "en",
            "videoEmbeddable": "true",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(YOUTUBE_SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        videos = [
            {
                "video_id": item["id"]["videoId"],
                "title": item["snippet"]["title"],
                "channel": item["snippet"]["channelTitle"],
                "thumbnail": item["snippet"]["thumbnails"]["medium"]["url"],
                "published_at": item["snippet"]["publishedAt"],
                "embed_url": f"https://www.youtube.com/embed/{item['id']['videoId']}",
                "watch_url": f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                "description": item["snippet"]["description"][:200],
            }
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]

        try:
            r = _get_redis()
            r.setex(_cache_key(query), CACHE_TTL, json.dumps(videos))
        except Exception as exc:
            logger.warning("Redis unavailable for YouTube cache write: %s", exc)

        return videos

    except Exception as exc:
        logger.warning("YouTube search failed for query '%s': %s", query, exc)
        return []
