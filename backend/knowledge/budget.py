"""The knowledge features' own daily share of Tavily credits.

Tavily's monthly plan is small and diagnosis (the main product) must not be starved, so
species profiles and photo identification together spend at most KNOWLEDGE_CREDITS_PER_DAY
a UTC day, counted in D1 (api_usage) so the limit holds across restarts. The searcher's
monthly CreditBudget still applies on top.
"""

from __future__ import annotations

from backend.data.driver import DataError
from backend.data.repo import Repo

SERVICE = "tavily:knowledge"
KNOWLEDGE_CREDITS_PER_DAY = 12
PROFILE_CREDITS = 3  # care search, batched extract, toxicity search


async def credits_left(repo: Repo) -> float:
    try:
        return KNOWLEDGE_CREDITS_PER_DAY - await repo.api_usage_on(SERVICE)
    except DataError:
        return 0.0  # unknown: spend nothing


async def record(repo: Repo, credits: int) -> None:
    if credits:
        try:
            await repo.add_api_usage(SERVICE, float(credits))
        except DataError:
            pass  # the credits were spent; the next check reads what D1 has
