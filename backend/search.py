"""Tavily web search at runtime (this is what qualifies Leaf for Best Use of Tavily)."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from backend.models import gather_limited
from backend.schemas import Source

log = logging.getLogger("leaf.search")

# Hosts match as "the domain itself or any subdomain of it", never as a bare suffix.
TRUSTED_DOMAINS = ("edu", "gov", "ac.uk", "gc.ca", "rhs.org.uk", "extension.org")
CACHE_TTL_S = 24 * 3600
CACHE_MAX = 500


class Searcher(Protocol):
    async def search(self, query: str) -> list[dict[str, Any]]: ...

    async def search_web(
        self,
        query: str,
        *,
        topic: str = "general",
        time_range: str | None = None,
        max_results: int = 5,
    ) -> tuple[list[dict[str, Any]], int]:
        """Results plus Tavily credits charged (0 on a cache hit)."""
        ...


class TavilySearcher:
    """Basic-depth search (1 credit per call). Queries are steered to extension guides.

    Results are cached for 24 hours per query, so repeated demo photos don't spend
    Tavily credits twice.
    """

    def __init__(self, api_key: str):
        from tavily import AsyncTavilyClient

        self._client = AsyncTavilyClient(api_key=api_key)
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Quick mode: steer to extension guides."""
        results, _ = await self.search_web(f"{query.strip()} university extension")
        return results

    async def search_web(
        self,
        query: str,
        *,
        topic: str = "general",
        time_range: str | None = None,
        max_results: int = 5,
    ) -> tuple[list[dict[str, Any]], int]:
        key = f"{topic}|{time_range}|{max_results}|{' '.join(query.lower().split())}"
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1], 0
        kwargs: dict[str, Any] = {
            "search_depth": "basic",
            "topic": topic,
            "max_results": max_results,
        }
        if time_range:
            kwargs["time_range"] = time_range
        resp = await self._client.search(query, **kwargs)
        results = resp.get("results", [])
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[key] = (time.time(), results)
        return results, 1  # basic search: 1 credit (docs.tavily.com/documentation/api-credits)


class NullSearcher:
    """Used when TAVILY_API_KEY is missing; Leaf still works, just without citations."""

    async def search(self, query: str) -> list[dict[str, Any]]:
        return []

    async def search_web(self, query: str, **_: Any) -> tuple[list[dict[str, Any]], int]:
        return [], 0


def _trust(url: str) -> int:
    """1 for university, government and extension-service hosts; 0 otherwise."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return 1 if any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS) else 0


def rank_sources(results: list[dict[str, Any]], limit: int = 6) -> list[Source]:
    """Dedupe by URL, prefer extension/.edu/.gov pages, then Tavily's own score."""
    seen: dict[str, dict[str, Any]] = {}
    for r in results:
        url = r.get("url")
        if url and url not in seen:
            seen[url] = r
    ordered = sorted(
        seen.values(), key=lambda r: (_trust(r["url"]), r.get("score", 0.0)), reverse=True
    )
    return [
        Source(
            id=i + 1,
            title=str(r.get("title") or r["url"])[:200],
            url=r["url"],
            snippet=str(r.get("content") or "")[:600],
        )
        for i, r in enumerate(ordered[:limit])
    ]


async def search_all(searcher: Searcher, queries: list[str]) -> list[Source]:
    batches = await gather_limited([searcher.search(q) for q in queries[:3]], limit=3)
    results: list[dict[str, Any]] = []
    for b in batches:
        if isinstance(b, Exception):
            log.warning("search failed: %s", b)
            continue
        results.extend(b)
    return rank_sources(results)
