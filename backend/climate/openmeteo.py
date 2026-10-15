"""Open-Meteo client with the free tier's weighted-call budget enforced in code.

Weather data by Open-Meteo.com (CC BY 4.0, free for non-commercial use): https://open-meteo.com/
Limits (open-meteo.com/en/pricing, checked 2026-10-14): 600 calls/min, 5,000/h, 10,000/day.
A call with more than 10 variables or more than 14 days counts as several:
units = max(1, variables / 10) x max(1, days / 14).

Leaf keeps a daily allowance below the limit and records every call's units in D1, so the
allowance holds across restarts. Climatology (30 years of daily data, about 780 units) is
fetched in 5-year chunks spaced out to stay under the per-minute limit.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import httpx

from backend.data.driver import DataError
from backend.data.repo import Repo

if TYPE_CHECKING:
    from backend.cache import SharedCache

log = logging.getLogger("leaf.climate")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ATTRIBUTION = "Weather data by Open-Meteo.com"
ATTRIBUTION_URL = "https://open-meteo.com/"
SERVICE = "open-meteo"
DAILY_ALLOWANCE = 8000.0  # of 10,000 a day: room for retries and other callers
HOURLY_ALLOWANCE = 4000.0  # of 5,000 an hour
SERVICE_HOUR = "open-meteo:hour"  # api_usage rows keyed by UTC hour ("2026-10-15T09")
ARCHIVE_CACHE_TTL_S = 3 * 24 * 3600  # chunks kept so a retried build resumes
CHUNK_YEARS = 5
CHUNK_SPACING_S = 20.0  # 5-year chunks of 4 variables are ~130 units: ~390 a minute at most
ARCHIVE_TIMEOUT_S = 90.0  # a 5-year chunk took over 30 s once (2026-10-15)
CHUNK_ATTEMPTS = 2  # retry a chunk once, so one slow reply does not refetch all 30 years


class WeatherUnavailable(RuntimeError):
    """Open-Meteo did not answer, or Leaf's daily allowance for it is spent."""


def _hour_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H")


def units(n_variables: int, n_days: int) -> float:
    """Open-Meteo's weighted cost of one call (fractional, as its pricing page describes)."""
    return max(1.0, n_variables / 10) * max(1.0, n_days / 14)


class OpenMeteo:
    def __init__(
        self,
        repo: Repo | None = None,
        http: httpx.AsyncClient | None = None,
        allowance: float = DAILY_ALLOWANCE,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        hourly_allowance: float = HOURLY_ALLOWANCE,
        cache: SharedCache | None = None,
    ):
        self.repo = repo
        self.http = http
        self.allowance = allowance
        self.hourly_allowance = hourly_allowance
        self.sleep = sleep
        self.cache = cache
        self._spent_local = 0.0  # used when there is no repository (tests, scripts)
        self._hour_local: tuple[str, float] = ("", 0.0)

    async def spent_today(self) -> float:
        if self.repo is None:
            return self._spent_local
        try:
            return await self.repo.api_usage_on(SERVICE)
        except DataError:
            return self._spent_local

    async def spent_this_hour(self) -> float:
        hour = _hour_key()
        if self.repo is not None:
            try:
                return await self.repo.api_usage_on(SERVICE_HOUR, hour)
            except DataError:
                pass
        return self._hour_local[1] if self._hour_local[0] == hour else 0.0

    async def can_spend(self, cost: float) -> str | None:
        """None when `cost` fits today's and this hour's allowance, else which one is full."""
        if await self.spent_today() + cost > self.allowance:
            return "day"
        if await self.spent_this_hour() + cost > self.hourly_allowance:
            return "hour"
        return None

    async def _charge(self, cost: float) -> None:
        hour = _hour_key()
        self._spent_local += cost
        prev = self._hour_local[1] if self._hour_local[0] == hour else 0.0
        self._hour_local = (hour, prev + cost)
        if self.repo is not None:
            try:
                await self.repo.add_api_usage(SERVICE, cost)
                await self.repo.add_api_usage(SERVICE_HOUR, cost, day=hour)
            except DataError as exc:  # the call already happened; keep going
                log.warning("could not record Open-Meteo usage: %s", exc)

    async def get(
        self, url: str, params: dict[str, Any], cost: float, timeout: float = 30.0
    ) -> dict[str, Any]:
        full = await self.can_spend(cost)
        if full:
            raise WeatherUnavailable(f"Leaf's weather allowance for this {full} is used up.")
        try:
            if self.http is not None:
                r = await self.http.get(url, params=params, timeout=timeout)
            else:
                async with httpx.AsyncClient(timeout=timeout) as http:
                    r = await http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise WeatherUnavailable("The weather service did not answer.") from exc
        await self._charge(cost)  # charged even on an error reply: it may still count
        if r.status_code != 200:
            reason = ""
            try:
                reason = str(r.json().get("reason", ""))[:200]
            except ValueError:
                pass
            raise WeatherUnavailable(f"Open-Meteo HTTP {r.status_code} {reason}".strip())
        try:
            return r.json()
        except ValueError as exc:
            raise WeatherUnavailable("Open-Meteo sent an unreadable reply.") from exc

    async def archive_daily(
        self, lat: float, lon: float, start: date, end: date, variables: list[str], model: str
    ) -> dict[str, list[Any]]:
        """Daily archive data in local days, fetched in chunks and joined."""
        out: dict[str, list[Any]] = {"time": [], **{v: [] for v in variables}}
        year = start.year
        first = True
        while year <= end.year:
            chunk_start = max(start, date(year, 1, 1))
            chunk_end = min(end, date(year + CHUNK_YEARS - 1, 12, 31))
            if not first:
                await self.sleep(CHUNK_SPACING_S)
            first = False
            days = (chunk_end - chunk_start).days + 1
            params = {
                "latitude": lat,
                "longitude": lon,
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "daily": ",".join(variables),
                "models": model,
                "timezone": "auto",
            }
            key = "|".join(str(params[k]) for k in sorted(params))
            data = await self.cache.get_json("archive", key) if self.cache else None
            for attempt in range(CHUNK_ATTEMPTS if data is None else 0):
                try:
                    data = await self.get(
                        ARCHIVE_URL, params, units(len(variables), days), ARCHIVE_TIMEOUT_S
                    )
                    if self.cache is not None:
                        await self.cache.put_json("archive", key, data, ARCHIVE_CACHE_TTL_S)
                    break
                except WeatherUnavailable as exc:
                    if attempt + 1 == CHUNK_ATTEMPTS or "allowance" in str(exc):
                        raise
                    log.warning("archive chunk %s failed (%s); retrying once", chunk_start, exc)
                    await self.sleep(CHUNK_SPACING_S)
            daily = data.get("daily") or {}
            times = list(daily.get("time") or [])
            out["time"] += times
            for key in variables:  # pad a missing or short column so dates stay aligned
                col = list(daily.get(key) or [])[: len(times)]
                out[key] += col + [None] * (len(times) - len(col))
            year += CHUNK_YEARS
        return out


def units_for(params: dict[str, Any]) -> float:
    """The weighted cost of a forecast or archive request from its own parameters."""
    variables = sum(
        len([v for v in str(params.get(k, "")).split(",") if v]) for k in ("hourly", "daily")
    )
    if params.get("start_date") and params.get("end_date"):
        days = (
            date.fromisoformat(str(params["end_date"]))
            - date.fromisoformat(str(params["start_date"]))
        ).days + 1
    else:
        days = int(params.get("forecast_days", 7)) + int(params.get("past_days", 0))
    return units(max(1, variables), days)


def climatology_cost(variables: int = 4, years: int = 30) -> float:
    """Units for a full 1991-2020 build, chunked as archive_daily does it."""
    chunks = math.ceil(years / CHUNK_YEARS)
    return sum(units(variables, 365.25 * CHUNK_YEARS) for _ in range(chunks))
