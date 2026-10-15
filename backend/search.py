"""Tavily web search at runtime (this is what qualifies Leaf for Best Use of Tavily)."""

from __future__ import annotations

import calendar
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

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
        include_domains: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Results plus Tavily credits charged (0 on a cache hit). `include_domains` limits
        the search to those hosts ("example.edu", "*.example.edu")."""
        ...

    async def extract(self, url: str, query: str) -> tuple[list[str], int]:
        """Passages of one page most relevant to `query`, plus credits charged."""
        ...

    async def extract_many(self, urls: list[str], query: str) -> tuple[dict[str, list[str]], int]:
        """Passages for up to five pages in one call (one credit per five pages)."""
        ...

    async def extract_pages(self, urls: list[str]) -> tuple[dict[str, str], int]:
        """The text of up to five whole pages in one call (one credit per five pages)."""
        ...


class SearchPaused(RuntimeError):
    """Today's Tavily credit allowance is used up. Callers answer without fresh sources."""


# Kept back each month so diagnoses keep working however the daily shares are spent.
# Development has stopped, so production runs with a small reserve (LEAF_SEARCH_RESERVE).
DEV_RESERVE_CREDITS = 100
FALLBACK_DAILY_CAP = 30  # when the balance cannot be read


def size_daily_cap(plan_limit: int, plan_usage: int, today: date, reserve: int) -> int:
    """Spread what is left of this month's credits evenly over the days left in it, so a
    scraper can never use up the month in a day."""
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    return max(0, (plan_limit - plan_usage - reserve) // days_left)


async def tavily_balance(api_key: str) -> tuple[int, int]:
    """(plan limit, credits used this month) from Tavily's usage endpoint."""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.tavily.com/usage", headers={"Authorization": f"Bearer {api_key}"}
        )
        r.raise_for_status()
        acct = r.json()["account"]
        return int(acct["plan_limit"]), int(acct["plan_usage"])


class CreditBudget:
    """A daily allowance of Tavily credits, re-sized each day from the real monthly balance.

    `balance` returns (plan limit, used); `max_daily` (LEAF_TAVILY_DAILY_CAP) is an optional
    ceiling. If the balance cannot be read, the allowance falls back to a small fixed number.
    """

    def __init__(
        self,
        balance: Callable[[], Awaitable[tuple[int, int]]] | None,
        *,
        max_daily: int = 0,
        reserve: int = DEV_RESERVE_CREDITS,
        today: Callable[[], date] = date.today,
    ):
        self._balance, self.max_daily, self.reserve, self._today = (
            balance,
            max_daily,
            reserve,
            today,
        )
        self.day: date | None = None
        self.cap = 0
        self.used = 0
        # Optional shared store (the platform KV). A container restart then picks up the
        # day's allowance where it was instead of sizing a fresh one from what is left.
        self.store: Any = None

    async def _day_start_usage(self, today: date, used_now: int) -> int:
        """Credits already used when today began: the first reading of the day, kept."""
        if self.store is None:
            return used_now
        key = f"tavily-day-start:{today.isoformat()}"
        try:
            saved = await self.store.get(key)
            if saved is not None:
                return min(int(saved), used_now)
            await self.store.put(key, str(used_now), 2 * 86400)
        except Exception as exc:  # noqa: BLE001 - the allowance still works without the store
            log.warning("Tavily day-start store unavailable (%s)", exc)
        return used_now

    async def _refresh(self) -> None:
        today = self._today()
        if self.day == today:
            return
        cap, used_today = self.max_daily or FALLBACK_DAILY_CAP, 0
        if self._balance is not None:
            try:
                limit, used = await self._balance()
                start = await self._day_start_usage(today, used)
                cap = size_daily_cap(limit, start, today, self.reserve)
                used_today = max(0, used - start)
                if self.max_daily:
                    cap = min(cap, self.max_daily)
            except Exception as exc:  # noqa: BLE001 - never block Leaf on the usage endpoint
                log.warning("Tavily balance unavailable (%s); daily cap %d", exc, cap)
        self.day, self.cap, self.used = today, cap, used_today
        log.info("Tavily daily credit cap for %s: %d", today, cap)

    async def remaining(self) -> int:
        """Credits left of today's allowance (for background work that must leave a share
        for diagnoses)."""
        await self._refresh()
        return max(0, self.cap - self.used)

    async def take(self, credits: int = 1) -> None:
        await self._refresh()
        if self.used + credits > self.cap:
            raise SearchPaused("Today's web-search allowance is used up.")
        self.used += credits

    @property
    def paused(self) -> bool:
        return self.day == self._today() and self.used >= self.cap


class TavilySearcher:
    """Basic-depth search (1 credit per call). Queries are steered to extension guides.

    Results are cached for 24 hours per query, so repeated demo photos don't spend
    Tavily credits twice.
    """

    def __init__(self, api_key: str, budget: CreditBudget | None = None):
        from tavily import AsyncTavilyClient

        self._client = AsyncTavilyClient(api_key=api_key)
        self.budget = budget
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._extract_cache: dict[str, tuple[float, list[str]]] = {}

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
        include_domains: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        domains = sorted(include_domains or [])
        key = f"{topic}|{time_range}|{max_results}|{domains}|{' '.join(query.lower().split())}"
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
        if domains:
            kwargs["include_domains"] = domains
        if self.budget:
            await self.budget.take(1)
        resp = await self._client.search(query, **kwargs)
        results = resp.get("results", [])
        if len(self._cache) >= CACHE_MAX:
            self._cache.clear()
        self._cache[key] = (time.time(), results)
        return results, 1  # basic search: 1 credit (docs.tavily.com/documentation/api-credits)

    async def extract(self, url: str, query: str) -> tuple[list[str], int]:
        key = f"extract|{url}|{' '.join(query.lower().split())}"
        hit = self._extract_cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1], 0
        # Basic depth with a relevance query returns the page's best ~800-character chunks;
        # tested Oct 2026, advanced depth gave the same chunks for twice the credits.
        if self.budget:
            await self.budget.take(1)
        resp = await self._client.extract(
            urls=[url], query=query, chunks_per_source=3, extract_depth="basic"
        )
        results = resp.get("results") or []
        if not results:
            return [], 0  # failed extractions are not charged
        raw = str(results[0].get("raw_content") or "")
        passages = [c.strip()[:900] for c in raw.split("[...]") if c.strip()][:3]
        if len(self._extract_cache) >= CACHE_MAX:
            self._extract_cache.clear()
        self._extract_cache[key] = (time.time(), passages)
        return passages, 1  # basic extract: 1 credit per 5 successful URLs

    async def extract_many(self, urls: list[str], query: str) -> tuple[dict[str, list[str]], int]:
        """Up to five pages per credit, the same 3 relevant chunks per page as `extract`."""
        urls = list(dict.fromkeys(urls))[:5]
        if not urls:
            return {}, 0
        if self.budget:
            await self.budget.take(1)
        resp = await self._client.extract(
            urls=urls, query=query, chunks_per_source=3, extract_depth="basic"
        )
        out: dict[str, list[str]] = {}
        for r in resp.get("results") or []:
            raw = str(r.get("raw_content") or "")
            passages = [c.strip()[:900] for c in raw.split("[...]") if c.strip()][:3]
            if r.get("url") and passages:
                out[str(r["url"])] = passages
        return out, (1 if out else 0)  # failed extractions are not charged

    async def extract_pages(self, urls: list[str]) -> tuple[dict[str, str], int]:
        """Whole-page text for up to five pages per credit (basic extraction, no relevance
        query, so nothing is cut out of the page)."""
        urls = list(dict.fromkeys(urls))[:5]
        if not urls:
            return {}, 0
        cached: dict[str, str] = {}
        for u in urls:
            hit = self._extract_cache.get(f"page|{u}")
            if hit and time.time() - hit[0] < CACHE_TTL_S:
                cached[u] = hit[1][0]
        todo = [u for u in urls if u not in cached]
        if not todo:
            return cached, 0
        if self.budget:
            await self.budget.take(1)
        resp = await self._client.extract(urls=todo, extract_depth="basic")
        out = {
            str(r["url"]): str(r.get("raw_content") or "")[:PAGE_CHARS]
            for r in resp.get("results") or []
            if r.get("url") and r.get("raw_content")
        }
        if len(self._extract_cache) >= CACHE_MAX:
            self._extract_cache.clear()
        for u, text in out.items():
            self._extract_cache[f"page|{u}"] = (time.time(), [text])
        return {**cached, **out}, (1 if out else 0)


def search_paused(searcher: Searcher) -> bool:
    """Has today's Tavily allowance run out? Then answers carry no fresh sources and say so."""
    budget = getattr(searcher, "budget", None)
    return bool(budget and budget.paused)


PAUSED_NOTICE = (
    "Leaf has used today's web-search allowance, so this answer is not backed by fresh "
    "sources. The care plan sticks to general good practice; check with your local extension "
    "office before using any product."
)


PAGE_CHARS = 20000  # the most of one page Leaf keeps


class NullSearcher:
    """Used when TAVILY_API_KEY is missing; Leaf still works, just without citations."""

    async def search(self, query: str) -> list[dict[str, Any]]:
        return []

    async def search_web(self, query: str, **_: Any) -> tuple[list[dict[str, Any]], int]:
        return [], 0

    async def extract(self, url: str, query: str) -> tuple[list[str], int]:
        return [], 0

    async def extract_many(self, urls: list[str], query: str) -> tuple[dict[str, list[str]], int]:
        return {}, 0

    async def extract_pages(self, urls: list[str]) -> tuple[dict[str, str], int]:
        return {}, 0


def _trust(url: str) -> int:
    """1 for university, government and extension-service hosts; 0 otherwise."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return 1 if any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS) else 0


def rank_sources(results: list[dict[str, Any]], limit: int = 6) -> list[Source]:
    """Dedupe by URL, prefer extension/.edu/.gov pages, then Tavily's own score."""
    seen: dict[str, dict[str, Any]] = {}
    for r in results:
        url = r.get("url")
        if url and url.lower().startswith(("http://", "https://")) and url not in seen:
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
