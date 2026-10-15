"""The climate engine: everything is computed in code and shared per 0.1-degree grid cell.

A garden's cell (backend/data/repo.grid_cell) decides which forecast, climatology and alerts
it gets; all of them are fetched for the cell's centre, cached, and reused by every garden
in it. Models never compute weather: they only word what this package decides.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from backend.cache import SharedCache
from backend.climate import climatology as clim
from backend.climate.forecast import Forecast, get_forecast
from backend.climate.nws import Alerts, active_alerts
from backend.climate.openmeteo import ARCHIVE_URL, DAILY_ALLOWANCE, OpenMeteo, units
from backend.climate.risk import gdd_total
from backend.data.driver import DataError
from backend.data.repo import CELL_DEG, Repo

log = logging.getLogger("leaf.climate")

GDD_TTL_S = 24 * 3600


def cell_center(cell: str) -> tuple[float, float]:
    """The centre of a cell id such as "407_-740" (40.7, -74.0)."""
    a, b = cell.split("_")
    return round(int(a) * CELL_DEG, 4), round(int(b) * CELL_DEG, 4)


class Climate:
    def __init__(
        self,
        repo: Repo | None,
        cache: SharedCache | None,
        http: httpx.AsyncClient | None = None,
        allowance: float = DAILY_ALLOWANCE,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.repo = repo
        self.cache = cache
        self.http = http
        self.om = OpenMeteo(repo, http, allowance, sleep, cache=cache)

    async def forecast(self, cell: str, now: datetime | None = None) -> Forecast:
        lat, lon = cell_center(cell)
        return await get_forecast(self.om, self.cache, cell, lat, lon, now)

    async def alerts(self, cell: str) -> Alerts:
        """NWS alerts for the cell; cells known to be outside the US are not asked."""
        known = await self.repo.get_cell(cell) if self.repo else None
        if known is not None and known.get("us") == 0:
            return Alerts(covered=False)
        lat, lon = cell_center(cell)
        result = await active_alerts(cell, lat, lon, self.cache, self.http)
        if self.repo is not None and known is not None and known.get("us") is None:
            try:
                await self.repo.set_cell_us(cell, result.covered)
            except DataError as exc:
                log.warning("could not record NWS coverage for %s: %s", cell, exc)
        return result

    async def climatology(self, cell: str) -> dict[str, Any] | None:
        return await self.repo.get_climatology(cell) if self.repo else None

    async def build_climatology(self, cell: str) -> dict[str, Any]:
        lat, lon = cell_center(cell)
        return await clim.build(self.om, lat, lon)

    async def season_gdd(self, cell: str, bases: list[float]) -> dict[str, Any]:
        """Degree days since the season began (Jan 1 north, Jul 1 south) for each base, from
        the archive up to 8 days ago plus the forecast's recent days. The daily temperatures
        are fetched once per cell and local day (about 20 units) and shared by every base."""
        fc = await self.forecast(cell)
        today = fc.local_today()
        lat, lon = cell_center(cell)
        start = (
            date(today.year, 1, 1)
            if lat >= 0
            else date(today.year if today.month >= 7 else today.year - 1, 7, 1)
        )
        key = f"{cell}:{today.isoformat()}"
        pairs = await self.cache.get_json("gdd_days", key) if self.cache else None
        if pairs is None:
            archive_end = today - timedelta(days=8)
            pairs = []
            if archive_end >= start:
                days = (archive_end - start).days + 1
                data = await self.om.get(
                    ARCHIVE_URL,
                    {
                        "latitude": lat,
                        "longitude": lon,
                        "start_date": start.isoformat(),
                        "end_date": archive_end.isoformat(),
                        "daily": "temperature_2m_min,temperature_2m_max",
                        "timezone": "auto",
                    },
                    units(2, days),
                )
                d = data.get("daily") or {}
                lows, highs = d.get("temperature_2m_min") or [], d.get("temperature_2m_max") or []
                pairs += [list(p) for p in zip(lows, highs, strict=False)]
            for row in fc.day_rows():
                day = date.fromisoformat(row["date"])
                if max(start, archive_end + timedelta(days=1)) <= day < today:
                    pairs.append([row["temperature_2m_min"], row["temperature_2m_max"]])
            if self.cache is not None:
                await self.cache.put_json("gdd_days", key, pairs, GDD_TTL_S)
        return {
            "since": start.isoformat(),
            "through": (today - timedelta(days=1)).isoformat(),
            "days": len(pairs),
            "by_base_c": {f"{b:g}": gdd_total([tuple(p) for p in pairs], b) for b in bases},
        }
