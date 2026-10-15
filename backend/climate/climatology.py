"""1991-2020 climatology for a 0.1-degree cell, computed in code from Open-Meteo's archive.

- Hardiness zone, USDA method: the average of the 30 annual extreme minimum temperatures,
  in 10 F zones (zone 1 starts at -60 F) split into 5 F halves, a colder and b warmer.
- Frost dates: a frost is a day with a minimum of 0 C (32 F) or lower, the NCEI freeze
  threshold. For each year, the last frost of spring and the first of autumn; then the dates
  at three risk levels. "10%" means a frost after that date (spring) or before it (autumn) in
  1 of 10 years; "50%" is the median; "90%" means 9 of 10 years.
- Season length: the frost-free days per year (median), with its 10% and 90% values.
- Monthly normals: mean daily maximum and minimum, precipitation, ET0 and frost days.

The archive is reanalysis on an 11 km grid, not a weather station. It smooths local extremes
(cold hollows, cities), so a zone can differ by a half zone from the USDA station map. Results
say so.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backend.climate.openmeteo import OpenMeteo
from backend.climate.sources import cite

YEARS = (1991, 2020)
MODEL = "era5_seamless"  # ERA5-Land temperatures at 0.1 deg with ERA5 for the rest
VARIABLES = [
    "temperature_2m_min",
    "temperature_2m_max",
    "precipitation_sum",
    "et0_fao_evapotranspiration",
]
FROST_C = 0.0  # 32 F
MIN_YEARS = 25  # a year with more than 10 missing days is dropped; need this many


@dataclass
class Daily:
    day: date
    tmin: float | None
    tmax: float | None
    rain: float | None
    et0: float | None


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def hardiness_zone(mean_extreme_min_c: float) -> dict[str, Any]:
    """USDA zone for an average annual extreme minimum: zone 1a is -60 to -55 F."""
    f = c_to_f(mean_extreme_min_c)
    steps = math.floor((f + 60) / 5)  # half zones from -60 F
    steps = max(0, min(steps, 25))  # 1a .. 13b
    number, half = steps // 2 + 1, "ab"[steps % 2]
    low_f = -60 + steps * 5
    return {
        "zone": f"{number}{half}",
        "mean_extreme_min_c": round(mean_extreme_min_c, 1),
        "mean_extreme_min_f": round(f, 1),
        "range_f": [low_f, low_f + 5],
        "range_c": [round(f_to_c(low_f), 1), round(f_to_c(low_f + 5), 1)],
    }


def parse_archive(data: dict[str, list[Any]]) -> list[Daily]:
    out = []
    times = data.get("time") or []
    cols = [data.get(v) or [None] * len(times) for v in VARIABLES]
    for i, t in enumerate(times):
        tmin, tmax, rain, et0 = (col[i] if i < len(col) else None for col in cols)
        out.append(Daily(date.fromisoformat(t), tmin, tmax, rain, et0))
    return out


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation between ranks (NumPy's default), nearest rank next to infinity."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    if not (math.isfinite(sorted_vals[lo]) and math.isfinite(sorted_vals[hi])):
        # A frost-free year is +/-inf; interpolating with it is meaningless: nearest rank.
        return sorted_vals[round(pos)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _season_years(days: list[Daily], southern: bool) -> dict[int, list[Daily]]:
    """Group days into frost years. North: calendar years. South: July to June, so spring
    frosts (Aug-Dec) and autumn frosts (Feb-Jun) fall in one growing season."""
    years: dict[int, list[Daily]] = defaultdict(list)
    for d in days:
        key = d.day.year if not southern else (d.day.year if d.day.month >= 7 else d.day.year - 1)
        years[key].append(d)
    return years


def _offset(d: date, southern: bool) -> int:
    """Days since the start of the frost year (Jan 1 north, Jul 1 south), counted in a
    non-leap reference year so a frost on Apr 20 is the same offset every year (Feb 29
    counts as Feb 28)."""
    day = min(d.day, 28) if d.month == 2 else d.day
    if not southern:
        return (date(2001, d.month, day) - date(2001, 1, 1)).days
    return (date(2001 if d.month >= 7 else 2002, d.month, day) - date(2001, 7, 1)).days


def _date_from_offset(offset: float, southern: bool) -> str:
    """Month-day of a frost-year offset (a non-leap reference year), e.g. "04-28"."""
    base = date(2001, 1, 1) if not southern else date(2001, 7, 1)
    return (base + timedelta(days=round(offset))).strftime("%m-%d")


def frost_dates(days: list[Daily], southern: bool = False) -> dict[str, Any]:
    """Last spring and first autumn frost at 10/50/90% risk, and the frost-free season."""
    springs: list[float] = []  # offset of the last spring frost; -inf when none
    autumns: list[float] = []  # offset of the first autumn frost; +inf when none
    seasons: list[float] = []
    mid = 182  # Jul 2 north / Dec 31 south: the warm middle of the frost year
    for _, ds in sorted(_season_years(days, southern).items()):
        if sum(1 for d in ds if d.tmin is not None) < 355:
            continue  # incomplete year (the first southern half-year, or missing data)
        spring = [
            _offset(d.day, southern)
            for d in ds
            if d.tmin is not None and d.tmin <= FROST_C and _offset(d.day, southern) < mid
        ]
        autumn = [
            _offset(d.day, southern)
            for d in ds
            if d.tmin is not None and d.tmin <= FROST_C and _offset(d.day, southern) >= mid
        ]
        last = max(spring) if spring else -math.inf
        first = min(autumn) if autumn else math.inf
        springs.append(last)
        autumns.append(first)
        # Frost-free days between the two frosts; a year without either is a full 365.
        span = (first if math.isfinite(first) else 365) - (last if math.isfinite(last) else -1)
        seasons.append(max(0, span - 1))
    n = len(springs)
    if n < MIN_YEARS:
        return {"years": n, "enough_data": False}

    def at(values: list[float], q: float) -> str | None:
        v = _percentile(sorted(values), q)
        return _date_from_offset(v, southern) if math.isfinite(v) else None

    frost_years = sum(
        1 for s, a in zip(springs, autumns, strict=True) if math.isfinite(s) or math.isfinite(a)
    )
    seasons_sorted = sorted(seasons)
    return {
        "years": n,
        "enough_data": True,
        "threshold_c": FROST_C,
        "years_with_frost": frost_years,
        # Spring: in 1 of 10 years the last frost is later than "risk_10"; 9 of 10 than "risk_90".
        "last_spring_frost": {
            "risk_90": at(springs, 0.10),
            "risk_50": at(springs, 0.50),
            "risk_10": at(springs, 0.90),
        },
        # Autumn: in 1 of 10 years the first frost is earlier than "risk_10".
        "first_autumn_frost": {
            "risk_10": at(autumns, 0.10),
            "risk_50": at(autumns, 0.50),
            "risk_90": at(autumns, 0.90),
        },
        "season_days": {
            "short_10": round(_percentile(seasons_sorted, 0.10)),
            "median": round(_percentile(seasons_sorted, 0.50)),
            "long_90": round(_percentile(seasons_sorted, 0.90)),
        },
    }


def annual_extreme_minimums(days: list[Daily], southern: bool = False) -> list[float]:
    """The lowest daily minimum of each complete winter year (calendar year north; Jul-Jun
    south, so a southern winter is never split)."""
    out = []
    for _, ds in sorted(_season_years(days, southern).items()):
        mins = [d.tmin for d in ds if d.tmin is not None]
        if len(mins) >= 355:
            out.append(min(mins))
    return out


def monthly_normals(days: list[Daily]) -> list[dict[str, Any]]:
    by_month: dict[int, list[Daily]] = defaultdict(list)
    for d in days:
        by_month[d.day.month].append(d)
    years = len({d.day.year for d in days}) or 1
    out = []
    for m in range(1, 13):
        ds = by_month.get(m, [])
        tmax = [d.tmax for d in ds if d.tmax is not None]
        tmin = [d.tmin for d in ds if d.tmin is not None]
        out.append(
            {
                "month": m,
                "tmax_c": round(statistics.fmean(tmax), 1) if tmax else None,
                "tmin_c": round(statistics.fmean(tmin), 1) if tmin else None,
                "rain_mm": round(sum(d.rain or 0 for d in ds) / years, 1),
                "et0_mm": round(sum(d.et0 or 0 for d in ds) / years, 1),
                "frost_days": round(sum(1 for t in tmin if t <= FROST_C) / years, 1),
            }
        )
    return out


def summarise(days: list[Daily], lat: float) -> dict[str, Any]:
    southern = lat < 0
    minimums = annual_extreme_minimums(days, southern)
    if len(minimums) < MIN_YEARS:
        raise ValueError(f"only {len(minimums)} complete years of data")
    zone = hardiness_zone(statistics.fmean(minimums))
    zone["years"] = len(minimums)
    return {
        "period": f"{YEARS[0]}-{YEARS[1]}",
        "model": MODEL,
        "hemisphere": "south" if southern else "north",
        "hardiness_zone": zone,
        "frost": frost_dates(days, southern),
        "monthly": monthly_normals(days),
        "caveat": (
            "Computed from 30 years of reanalysis on an 11 km grid, not a weather station; "
            "cold hollows and cities can differ by a half zone or more."
        ),
        "sources": cite("usda_phzm", "usda_phzm_method", "ncei_freeze", "openmeteo_et0"),
    }


async def build(client: OpenMeteo, lat: float, lon: float) -> dict[str, Any]:
    """Fetch 1991-2020 for a cell centre and compute its climatology (about 780 units)."""
    raw = await client.archive_daily(
        lat, lon, date(YEARS[0], 1, 1), date(YEARS[1], 12, 31), VARIABLES, MODEL
    )
    return summarise(parse_archive(raw), lat)
