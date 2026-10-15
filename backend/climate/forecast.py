"""Hourly forecast for a 0.1-degree cell: fetched once an hour, shared by every garden in it.

One call covers the last 7 days (for the water balance) and the next 7: 19 variables over
14 days, about 1.9 Open-Meteo units. Times are local to the cell (timezone=auto).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from backend.cache import SharedCache
from backend.climate.openmeteo import FORECAST_URL, OpenMeteo, units

HOURLY = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_gusts_10m",
    "soil_temperature_6cm",
    "soil_moisture_3_to_9cm",
    "soil_moisture_9_to_27cm",
    "et0_fao_evapotranspiration",
    "sunshine_duration",
]
DAILY = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "precipitation_probability_max",
    "et0_fao_evapotranspiration",
    "wind_gusts_10m_max",
    "sunrise",
    "sunset",
]
PAST_DAYS = 7
FORECAST_DAYS = 7
CACHE_TTL_S = 2 * 3600  # the key changes every hour; the TTL only bounds storage


class Forecast(BaseModel):
    """Open-Meteo's arrays, kept as they come (compact to cache), plus where and when."""

    cell: str
    lat: float
    lon: float
    timezone: str
    utc_offset_s: int
    fetched_at: int
    hourly: dict[str, list[Any]]
    daily: dict[str, list[Any]]

    def local_today(self, now: datetime | None = None) -> date:
        now = now or datetime.now(UTC)
        return datetime.fromtimestamp(now.timestamp() + self.utc_offset_s, UTC).date()

    def day_rows(self) -> list[dict[str, Any]]:
        times = self.daily.get("time") or []
        return [
            {"date": t, **{k: (self.daily.get(k) or [None] * len(times))[i] for k in DAILY}}
            for i, t in enumerate(times)
        ]

    def hour_rows(self) -> list[dict[str, Any]]:
        times = self.hourly.get("time") or []
        return [
            {"time": t, **{k: (self.hourly.get(k) or [None] * len(times))[i] for k in HOURLY}}
            for i, t in enumerate(times)
        ]


def cost() -> float:
    return units(len(HOURLY) + len(DAILY), PAST_DAYS + FORECAST_DAYS)


async def get_forecast(
    client: OpenMeteo,
    cache: SharedCache | None,
    cell: str,
    lat: float,
    lon: float,
    now: datetime | None = None,
) -> Forecast:
    now = now or datetime.now(UTC)
    key = f"{cell}:{now.strftime('%Y%m%d%H')}"
    if cache is not None:
        hit = await cache.get_json("forecast", key)
        if hit is not None:
            return Forecast.model_validate(hit)
    data = await client.get(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": ",".join(HOURLY),
            "daily": ",".join(DAILY),
            "past_days": PAST_DAYS,
            "forecast_days": FORECAST_DAYS,
            "timezone": "auto",
        },
        cost(),
    )
    fc = Forecast(
        cell=cell,
        lat=lat,
        lon=lon,
        timezone=str(data.get("timezone") or "UTC"),
        utc_offset_s=int(data.get("utc_offset_seconds") or 0),
        fetched_at=int(now.timestamp()),
        hourly=data.get("hourly") or {},
        daily=data.get("daily") or {},
    )
    if cache is not None:
        await cache.put_json("forecast", key, fc.model_dump(), CACHE_TTL_S)
    return fc
