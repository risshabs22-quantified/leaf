"""The daily Tavily credit cap: sized from the real monthly balance, and Leaf still answers
(without fresh sources, and says so) once it is used up."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from backend.search import (
    FALLBACK_DAILY_CAP,
    PAUSED_NOTICE,
    CreditBudget,
    SearchPaused,
    search_paused,
    size_daily_cap,
)
from tests.conftest import FakeSearcher, make_leaf
from tests.test_agent import RESP, collect, finish, search, turn


def test_the_daily_cap_spreads_the_rest_of_the_month_evenly():
    # 1,000-credit plan, 290 used, a 100-credit reserve, 19 days left in October
    assert size_daily_cap(1000, 290, date(2026, 10, 13), 100) == 32
    assert size_daily_cap(1000, 0, date(2026, 11, 1), 100) == 30
    assert size_daily_cap(1000, 990, date(2026, 10, 31), 100) == 0  # never negative


async def test_the_budget_pauses_search_for_the_rest_of_the_day_then_resets():
    today = [date(2026, 10, 13)]

    async def balance():
        return 1000, 290

    budget = CreditBudget(balance, today=lambda: today[0])
    for _ in range(32):
        await budget.take()
    assert budget.paused
    with pytest.raises(SearchPaused):
        await budget.take()
    today[0] = date(2026, 10, 14)  # a new day: re-sized from the balance, counter reset
    assert not budget.paused
    await budget.take()
    assert budget.used == 1 and budget.cap == 33


async def test_a_ceiling_and_an_unreadable_balance_both_keep_the_cap_small():
    async def balance():
        return 100_000, 0

    capped = CreditBudget(balance, max_daily=5, today=lambda: date(2026, 10, 13))
    await capped.take()
    assert capped.cap == 5

    async def broken():
        raise RuntimeError("usage endpoint down")

    fallback = CreditBudget(broken, today=lambda: date(2026, 10, 13))
    await fallback.take()
    assert fallback.cap == FALLBACK_DAILY_CAP


class BudgetedSearcher(FakeSearcher):
    """FakeSearcher that spends from a CreditBudget, like TavilySearcher."""

    def __init__(self, budget: CreditBudget):
        super().__init__()
        self.budget = budget

    async def search(self, query: str) -> list[dict[str, Any]]:
        await self.budget.take()
        return await super().search(query)

    async def search_web(self, query: str, **kw: Any):
        await self.budget.take()
        return await super().search_web(query, **kw)

    async def extract(self, url: str, query: str):
        await self.budget.take()
        return await super().extract(url, query)


def spent_budget() -> CreditBudget:
    return CreditBudget(None, max_daily=0, today=lambda: date(2026, 10, 13))


async def test_quick_still_answers_without_sources_and_says_so(settings):
    budget = spent_budget()
    budget.cap = 0
    budget.day = date(2026, 10, 13)
    leaf, _, _ = make_leaf(settings, RESP)
    leaf.searcher = BudgetedSearcher(budget)
    res = await leaf.diagnose(image_b64="abc")
    assert search_paused(leaf.searcher)
    assert res.diagnosis.candidates and res.verdict and not res.sources
    assert res.notice == PAUSED_NOTICE
    assert all(not step.source_ids for step in res.plan.steps)


async def test_quick_with_credits_left_has_no_notice(settings):
    async def balance():
        return 1000, 0

    leaf, _, _ = make_leaf(settings, RESP)
    leaf.searcher = BudgetedSearcher(CreditBudget(balance, today=lambda: date(2026, 10, 13)))
    res = await leaf.diagnose(image_b64="abc")
    assert res.sources and res.notice == ""


async def test_deep_still_answers_when_search_is_paused_and_says_so(settings):
    budget = spent_budget()
    budget.cap, budget.day = 0, date(2026, 10, 13)
    from backend.deep import DeepLeaf
    from backend.store import MemoryCaseStore

    script = [turn(search()), turn(finish(("Early blight", 0.8, [])))]
    leaf, _, _ = make_leaf(settings, RESP, None, script)
    leaf.searcher = BudgetedSearcher(budget)  # before the investigator takes its reference
    deep = DeepLeaf(leaf, MemoryCaseStore())
    res, _ = await collect(deep)
    assert res.status == "done" and res.differential and res.notice == PAUSED_NOTICE
    skipped = [s for s in res.steps if s.tool == "search_sources"]
    assert skipped and "paused" in skipped[0].skipped
