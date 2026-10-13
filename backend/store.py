"""Case storage behind a small interface, so the backend can change without touching callers.

MemoryCaseStore keeps cases in this process (fine for one instance; they are lost on restart).
A SQLite or object-storage store implements the same three methods.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Protocol

from backend.case import Case


class CaseStore(Protocol):
    async def get(self, case_id: str) -> Case | None: ...
    async def put(self, case: Case) -> None: ...
    async def delete(self, case_id: str) -> None: ...


class MemoryCaseStore:
    """Bounded and time-limited: idle cases expire, and the oldest go first when full."""

    def __init__(self, ttl_s: float = 2 * 3600, max_cases: int = 200):
        self.ttl_s, self.max_cases = ttl_s, max_cases
        self._cases: OrderedDict[str, Case] = OrderedDict()

    def _expire(self) -> None:
        now = time.time()
        for cid in [c for c, case in self._cases.items() if now - case.updated_at > self.ttl_s]:
            del self._cases[cid]

    async def get(self, case_id: str) -> Case | None:
        self._expire()
        case = self._cases.get(case_id)
        if case is not None:
            self._cases.move_to_end(case_id)
        return case

    async def put(self, case: Case) -> None:
        case.updated_at = time.time()
        self._cases[case.id] = case
        self._cases.move_to_end(case.id)
        self._expire()
        while len(self._cases) > self.max_cases:
            self._cases.popitem(last=False)

    async def delete(self, case_id: str) -> None:
        self._cases.pop(case_id, None)
