"""The meter and the spend guard.

Every model call passes through MeteredLLM (wrapped around the Token Factory client in
app.py), which refuses calls once the day's budget is spent and records each call's task,
model, tokens, cost and latency against the request's trace id. Records are flushed to D1 in
one batch, in the background; metering never fails a request.

Modes, per UTC day (docs/LEAF_PLATFORM.md, "Cost engineering"):
- normal: everything runs.
- brownout (spend >= brownout x cap): optional tasks (config.ROUTES[...].optional) are refused,
  so their callers use rules or templates; core tasks still run.
- rules_only (spend >= cap): every model call is refused before it is sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections import defaultdict
from typing import Any, Literal

from backend.config import ROUTES
from backend.context import REQUEST, TASK, current_trace_id
from backend.data.driver import DataError
from backend.data.repo import CallRow, Repo, utc_day
from backend.models import LLM, CallResult, ModelError, SpendCapReached

log = logging.getLogger("leaf.meter")

Mode = Literal["normal", "brownout", "rules_only"]
MAX_PENDING = 2000  # rows kept for a retry while D1 is unreachable; older ones are dropped


class Meter:
    def __init__(self, cap_usd: float, brownout: float = 0.8, repo: Repo | None = None, clock=None):
        self.cap_usd = cap_usd
        self.brownout = brownout
        self.repo = repo
        self.clock = clock or time.time
        self.day = utc_day(self.clock())
        self.spent = 0.0
        self.calls_today = 0
        self.pending: list[CallRow] = []
        self.cache: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        self.actors_pending: set[tuple[str, str]] = set()
        self._actors_seen: set[str] = set()
        self._salt: tuple[str, str] | None = None
        self._flush_lock = asyncio.Lock()
        # Until today's spend has been read back from D1, assume most of the budget may be
        # gone: optional tasks pause, so a restart during an outage cannot double the cap.
        self.unrestored = repo is not None

    def _roll(self) -> None:
        today = utc_day(self.clock())
        if today != self.day:
            self.day, self.spent, self.calls_today = today, 0.0, 0
            self._actors_seen.clear()

    # ---------- the guard ----------

    def mode(self) -> Mode:
        self._roll()
        if self.cap_usd <= 0 or self.spent >= self.cap_usd:
            return "rules_only"
        if self.spent >= self.cap_usd * self.brownout or self.unrestored:
            return "brownout"
        return "normal"

    def allows(self, task: str) -> bool:
        mode = self.mode()
        if mode == "rules_only":
            return False
        route = ROUTES.get(task)
        return not (mode == "brownout" and route is not None and route.optional)

    def check(self, task: str) -> None:
        if not self.allows(task):
            raise SpendCapReached(f"{task}: daily model budget ({self.mode()})")

    # ---------- recording ----------

    def record(
        self,
        task: str,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cost_usd: float | None,
        latency_ms: int,
        ok: bool = True,
    ) -> None:
        self._roll()
        cost = float(cost_usd or 0.0)
        self.spent += cost
        self.calls_today += 1
        ctx = REQUEST.get()
        if ctx is not None:
            ctx.model_calls += 1
            # Counted here, not when the response starts: streamed cases call models later.
            self.active(ctx.visitor)
        self.pending.append(
            CallRow(
                day=self.day,
                trace_id=current_trace_id(),
                task=task,
                model=model,
                prompt_tokens=prompt_tokens or 0,
                completion_tokens=completion_tokens or 0,
                cost_usd=round(cost, 6),
                latency_ms=latency_ms,
                ok=ok,
                created_at=int(self.clock()),
            )
        )
        if len(self.pending) > MAX_PENDING:
            del self.pending[: len(self.pending) - MAX_PENDING]

    def cache_event(self, namespace: str, hit: bool) -> None:
        self._roll()
        self.cache[(self.day, namespace)][0 if hit else 1] += 1

    def active(self, actor: str) -> None:
        """Count `actor` (a garden or a hashed visitor) as active today, once."""
        self._roll()
        if actor and actor not in self._actors_seen:
            self._actors_seen.add(actor)
            self.actors_pending.add((self.day, actor))

    async def visitor(self, ip: str) -> str:
        """A visitor id that cannot be linked across days: sha256(day salt + ip)."""
        self._roll()
        if self.repo is None:
            return ""
        if self._salt is None or self._salt[0] != self.day:
            self._salt = (self.day, await self.repo.day_salt(self.day))
        return "v:" + hashlib.sha256((self._salt[1] + ip).encode()).hexdigest()[:16]

    # ---------- persistence ----------

    async def restore(self) -> None:
        """After a restart, today's spend comes back from D1, so the cap still holds."""
        if self.repo is None:
            return
        self._roll()
        try:
            self.spent = max(self.spent, await self.repo.spend_on(self.day))
        except DataError as exc:
            log.warning("could not restore today's spend (brownout until it is): %s", exc)
            return
        self.unrestored = False

    async def flush(self) -> None:
        if self.unrestored:
            await self.restore()
        if self.repo is None or not (self.pending or self.cache or self.actors_pending):
            return
        async with self._flush_lock:
            calls, self.pending = self.pending, []
            cache = {k: (v[0], v[1]) for k, v in self.cache.items()}
            self.cache.clear()
            actors, self.actors_pending = self.actors_pending, set()
            try:
                await self.repo.record_usage(calls, cache, actors)
            except DataError as exc:
                log.warning("meter flush failed, keeping %d rows: %s", len(calls), exc)
                self.pending = (calls + self.pending)[-MAX_PENDING:]
                for k, (hits, misses) in cache.items():
                    self.cache[k][0] += hits
                    self.cache[k][1] += misses
                self.actors_pending |= actors

    def snapshot(self) -> dict[str, Any]:
        mode = self.mode()
        return {
            "day": self.day,
            "mode": mode,
            "spent_usd": round(self.spent, 4),
            "cap_usd": self.cap_usd,
            "brownout_at_usd": round(self.cap_usd * self.brownout, 4),
            "calls": self.calls_today,
        }


class MeteredLLM:
    """Wraps any LLM: the spend guard runs before each call and the meter after it."""

    def __init__(self, inner: LLM, meter: Meter):
        self.inner = inner
        self.meter = meter

    def __getattr__(self, name: str) -> Any:  # prices, cost(), load_prices() and so on
        return getattr(self.inner, name)

    def _done(self, task: str, res: CallResult) -> CallResult:
        self.meter.record(
            task, res.model, res.prompt_tokens, res.completion_tokens, res.cost_usd, res.latency_ms
        )
        return res

    def _failed(self, task: str, model: str, exc: ModelError) -> None:
        self.meter.record(task, model, 0, 0, exc.cost_usd, 0, ok=False)

    def _abandoned(self, task: str, model: str) -> None:
        """A call cancelled by a deadline or a client that left may still be billed; count
        the route's typical cost so the cap never under-counts."""
        route = ROUTES.get(task)
        cost_fn = getattr(self.inner, "cost", None)
        est = 0.0
        if route is not None and callable(cost_fn):
            est = cost_fn(model, route.est_in, route.est_out)
        self.meter.record(task, model, 0, 0, est if math.isfinite(est) else 0.0, 0, ok=False)

    async def _call(self, method: str, model: str, messages: list[dict[str, Any]], **kwargs):
        task = TASK.get()
        self.meter.check(task)
        try:
            res = await getattr(self.inner, method)(model=model, messages=messages, **kwargs)
        except ModelError as exc:
            self._failed(task, model, exc)
            raise
        except (asyncio.CancelledError, TimeoutError):
            self._abandoned(task, model)
            raise
        return self._done(task, res)

    async def complete(self, *, model: str, messages: list[dict[str, Any]], **kwargs: Any):
        return await self._call("complete", model, messages, **kwargs)

    async def complete_tools(self, *, model: str, messages: list[dict[str, Any]], **kwargs: Any):
        return await self._call("complete_tools", model, messages, **kwargs)

    async def list_models(self) -> list[dict[str, Any]]:
        return await self.inner.list_models()
