"""Weather risks, degree days and sowing readiness, computed in code from the forecast.

Thresholds and their sources (backend/climate/sources.py):
- Frost possible: a night low of 2.2 C (36 F) or below. NWS issues a Frost Advisory for lows
  of 33-36 F on clear, calm nights (nws_frost). Freeze: 0 C (32 F); hard freeze: -2.2 C
  (28 F) (ncei_freeze).
- Heat: a high of 32.2 C (90 F) or more; with a night above 21.1 C (70 F), tomato fruit set
  suffers (rutgers_heat).
- Wind: sustained 48 km/h (30 mph) or gusts of 72 km/h (45 mph), the NWS Indianapolis Wind
  Advisory criteria; NWS criteria are set locally (nws_wind).
- Degree days: single average with a horizontal cutoff at 30 C (usbr_gdd), base 10 C unless
  the crop has its own (mrcc_gdd).
- Sowing: soil at 6 cm against the crop's minimum and optimum germination range
  (osu_germination, ucanr_germination).
"""

from __future__ import annotations

import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backend.climate.crops import CROPS, GENERAL_GDD_BASE_C, Crop
from backend.climate.forecast import Forecast
from backend.climate.sources import cite


def _c(f: float) -> float:
    return (f - 32) * 5 / 9


MPH_KMH = 1.609344
# The sources state these in Fahrenheit and mph; converted exactly, so 36 F is in the band.
FROST_POSSIBLE_C = _c(36)
FREEZE_C = _c(32)
HARD_FREEZE_C = _c(28)
HEAT_C = _c(90)
WARM_NIGHT_C = _c(70)
WIND_KMH = 30 * MPH_KMH
GUST_KMH = 45 * MPH_KMH
GDD_CAP_C = 30.0


def _nights(fc: Forecast, today: date, nights: int) -> list[dict[str, Any]]:
    """Each coming night (18:00 to 09:00 local): its lowest temperature and when."""
    hours = fc.hour_rows()
    out = []
    for n in range(nights):
        start = datetime.combine(today + timedelta(days=n), datetime.min.time()) + timedelta(
            hours=18
        )
        end = start + timedelta(hours=15)
        temps = [
            (h["temperature_2m"], h["time"])
            for h in hours
            if h["temperature_2m"] is not None and start <= datetime.fromisoformat(h["time"]) < end
        ]
        if temps:
            low, at = min(temps)
            out.append(
                {"night_of": (today + timedelta(days=n)).isoformat(), "low_c": low, "at": at}
            )
    return out


def frost_level(low_c: float) -> str | None:
    if low_c <= HARD_FREEZE_C:
        return "hard_freeze"
    if low_c <= FREEZE_C:
        return "freeze"
    if low_c <= FROST_POSSIBLE_C:
        return "frost_possible"
    return None


def risks(fc: Forecast, now: datetime | None = None, days: int = 3) -> dict[str, Any]:
    """Frost, heat and wind over the next `days` days, each with its threshold's source."""
    today = fc.local_today(now)
    frost = []
    for night in _nights(fc, today, days):
        level = frost_level(night["low_c"])
        if level:
            frost.append({**night, "level": level})
    heat, wind = [], []
    for d in fc.day_rows():
        day = date.fromisoformat(d["date"])
        if not today <= day < today + timedelta(days=days):
            continue
        if d["temperature_2m_max"] is not None and d["temperature_2m_max"] >= HEAT_C:
            heat.append({"date": d["date"], "high_c": d["temperature_2m_max"]})
    for h in fc.hour_rows():
        t = datetime.fromisoformat(h["time"])
        if not today <= t.date() < today + timedelta(days=days):
            continue
        sustained, gust = h["wind_speed_10m"] or 0, h["wind_gusts_10m"] or 0
        if sustained >= WIND_KMH or gust >= GUST_KMH:
            wind.append({"time": h["time"], "wind_kmh": sustained, "gust_kmh": gust})
    warm_nights = [n for n in _nights(fc, today, days) if n["low_c"] >= WARM_NIGHT_C]
    return {
        "frost": frost,
        "heat": heat,
        "warm_nights": [n["night_of"] for n in warm_nights],
        "wind": wind[:6],
        "thresholds": {
            "frost_possible_c": round(FROST_POSSIBLE_C, 1),
            "freeze_c": round(FREEZE_C, 1),
            "hard_freeze_c": round(HARD_FREEZE_C, 1),
            "heat_c": round(HEAT_C, 1),
            "warm_night_c": round(WARM_NIGHT_C, 1),
            "wind_kmh": round(WIND_KMH, 1),
            "gust_kmh": round(GUST_KMH, 1),
        },
        "sources": cite("nws_frost", "ncei_freeze", "rutgers_heat", "nws_wind"),
    }


def gdd_day(tmin: float, tmax: float, base_c: float, cap_c: float = GDD_CAP_C) -> float:
    """USBR AgriMet: Tmax capped at 30 C, Tmin raised to the base, then the average minus the
    base, floored at 0."""
    return max(0.0, (min(tmax, cap_c) + max(tmin, base_c)) / 2 - base_c)


def gdd_total(days: list[tuple[float | None, float | None]], base_c: float) -> float:
    return round(
        sum(gdd_day(lo, hi, base_c) for lo, hi in days if lo is not None and hi is not None), 1
    )


def gdd_recent(fc: Forecast, crop: Crop | None = None, now: datetime | None = None) -> dict:
    """Degree days over the last 7 days and the next 7 (season totals come from the archive)."""
    base = crop.gdd_base_c if crop and crop.gdd_base_c is not None else GENERAL_GDD_BASE_C
    today = fc.local_today(now)
    past, ahead = [], []
    for d in fc.day_rows():
        pair = (d["temperature_2m_min"], d["temperature_2m_max"])
        (past if date.fromisoformat(d["date"]) < today else ahead).append(pair)
    return {
        "base_c": base,
        "last_7_days": gdd_total(past, base),
        "next_7_days": gdd_total(ahead, base),
        "sources": cite("usbr_gdd", "mrcc_gdd"),
    }


def soil_temperature_now(fc: Forecast, now: datetime | None = None) -> float | None:
    """Mean soil temperature at 6 cm over the last 24 hours (local)."""
    ts = (now or datetime.now(UTC)).timestamp() + fc.utc_offset_s
    local_now = datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)  # the cell's wall clock
    vals = [
        h["soil_temperature_6cm"]
        for h in fc.hour_rows()
        if h["soil_temperature_6cm"] is not None
        and local_now - timedelta(hours=24) <= datetime.fromisoformat(h["time"]) <= local_now
    ]
    return round(statistics.fmean(vals), 1) if vals else None


def sowing(soil_c: float | None, crops: list[Crop] | None = None) -> list[dict[str, Any]]:
    """For each crop with germination data: too cold, possible, ideal or too warm to sow."""
    out = []
    for crop in crops if crops is not None else list(CROPS.values()):
        if crop.germ_min_c is None or soil_c is None:
            continue
        lo, hi = crop.germ_opt_c or (crop.germ_min_c, crop.germ_min_c)
        if soil_c < crop.germ_min_c:
            status = "too_cold"
        elif soil_c < lo:
            status = "possible"
        elif soil_c <= hi:
            status = "ideal"
        else:
            status = "too_warm"
        out.append(
            {
                "crop": crop.key,
                "status": status,
                "soil_c": soil_c,
                "min_c": crop.germ_min_c,
                "optimum_c": [lo, hi],
                "sources": cite(*[s for s in crop.sources if "germination" in s]),
            }
        )
    return out
