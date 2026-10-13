"""Recent weather and the published disease-pressure indices that apply to the crop.

Weather data by Open-Meteo.com (CC BY 4.0, free non-commercial tier): https://open-meteo.com/
Leaf shows that attribution wherever this data appears. Responses are cached.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from backend import disease_rules as rules

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION = "Weather data by Open-Meteo.com"
ATTRIBUTION_URL = "https://open-meteo.com/"
HISTORY_DAYS = 14
GEOCODE_TTL_S = 24 * 3600
WEATHER_TTL_S = 3600
CACHE_MAX = 500
CAVEAT = (
    "Wet hours are estimated from modelled humidity (hours at 90% or more), not measured on "
    "your leaves; modelled data tends to undercount them. Days are local calendar days."
)

# The geocoder only knows places, so a bare US state name ("Ohio") would match a village of
# that name elsewhere. A state is located at its capital, and the result says so.
US_STATES = {
    "AL": ("Alabama", "Montgomery"),
    "AK": ("Alaska", "Juneau"),
    "AZ": ("Arizona", "Phoenix"),
    "AR": ("Arkansas", "Little Rock"),
    "CA": ("California", "Sacramento"),
    "CO": ("Colorado", "Denver"),
    "CT": ("Connecticut", "Hartford"),
    "DE": ("Delaware", "Dover"),
    "FL": ("Florida", "Tallahassee"),
    "GA": ("Georgia", "Atlanta"),
    "HI": ("Hawaii", "Honolulu"),
    "ID": ("Idaho", "Boise"),
    "IL": ("Illinois", "Springfield"),
    "IN": ("Indiana", "Indianapolis"),
    "IA": ("Iowa", "Des Moines"),
    "KS": ("Kansas", "Topeka"),
    "KY": ("Kentucky", "Frankfort"),
    "LA": ("Louisiana", "Baton Rouge"),
    "ME": ("Maine", "Augusta"),
    "MD": ("Maryland", "Annapolis"),
    "MA": ("Massachusetts", "Boston"),
    "MI": ("Michigan", "Lansing"),
    "MN": ("Minnesota", "Saint Paul"),
    "MS": ("Mississippi", "Jackson"),
    "MO": ("Missouri", "Jefferson City"),
    "MT": ("Montana", "Helena"),
    "NE": ("Nebraska", "Lincoln"),
    "NV": ("Nevada", "Carson City"),
    "NH": ("New Hampshire", "Concord"),
    "NJ": ("New Jersey", "Trenton"),
    "NM": ("New Mexico", "Santa Fe"),
    "NY": ("New York", "Albany"),
    "NC": ("North Carolina", "Raleigh"),
    "ND": ("North Dakota", "Bismarck"),
    "OH": ("Ohio", "Columbus"),
    "OK": ("Oklahoma", "Oklahoma City"),
    "OR": ("Oregon", "Salem"),
    "PA": ("Pennsylvania", "Harrisburg"),
    "RI": ("Rhode Island", "Providence"),
    "SC": ("South Carolina", "Columbia"),
    "SD": ("South Dakota", "Pierre"),
    "TN": ("Tennessee", "Nashville"),
    "TX": ("Texas", "Austin"),
    "UT": ("Utah", "Salt Lake City"),
    "VT": ("Vermont", "Montpelier"),
    "VA": ("Virginia", "Richmond"),
    "WA": ("Washington", "Olympia"),
    "WV": ("West Virginia", "Charleston"),
    "WI": ("Wisconsin", "Madison"),
    "WY": ("Wyoming", "Cheyenne"),
}
_STATE_LOOKUP = {k.lower(): v for k, v in US_STATES.items()} | {
    name.lower(): (name, capital) for name, capital in US_STATES.values()
}


class ConditionsError(Exception):
    """Weather could not be found; the message is safe to show the gardener."""


@dataclass(frozen=True)
class Place:
    label: str
    lat: float
    lon: float
    admin1: str = ""
    country_code: str = ""
    approximate: bool = False  # True when a whole state is located at its capital


class WeatherClient:
    def __init__(self, http: httpx.AsyncClient | None = None, now=None):
        self._http = http
        self._now = now or (lambda: datetime.now(UTC))
        self._geo: dict[str, tuple[float, Place | None]] = {}
        self._wx: dict[tuple[float, float, str], tuple[float, dict[str, Any]]] = {}

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if self._http is not None:
                r = await self._http.get(url, params=params, timeout=10)
            else:
                async with httpx.AsyncClient(timeout=10) as http:
                    r = await http.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ConditionsError("The weather service did not answer. Try again later.") from exc

    async def geocode(self, region: str) -> Place | None:
        key = " ".join(region.lower().split())
        hit = self._geo.get(key)
        if hit and time.time() - hit[0] < GEOCODE_TTL_S:
            return hit[1]
        place = await self._geocode(region.strip())
        if len(self._geo) >= CACHE_MAX:
            self._geo.clear()
        self._geo[key] = (time.time(), place)
        return place

    async def _geocode(self, region: str) -> Place | None:
        state = _STATE_LOOKUP.get(region.lower().rstrip("."))
        if state:
            name, capital = state
            results = await self._search(capital)
            match = next(
                (r for r in results if r.get("country_code") == "US" and r.get("admin1") == name),
                None,
            )
            if match:
                return Place(
                    f"{name} (weather at {capital})",
                    round(match["latitude"], 2),
                    round(match["longitude"], 2),
                    name,
                    "US",
                    approximate=True,
                )
            return None
        city, _, qualifier = region.partition(",")
        results = await self._search(city.strip())
        if qualifier.strip():
            q = qualifier.strip().lower()
            q_state = _STATE_LOOKUP.get(q, (q,))[0].lower()
            results = [
                r
                for r in results
                if q_state in (str(r.get("admin1", "")).lower(), str(r.get("country", "")).lower())
                or q == str(r.get("country_code", "")).lower()
            ] or results
        if not results:
            return None
        best = results[0]
        label = ", ".join(
            x for x in (best.get("name"), best.get("admin1"), best.get("country_code")) if x
        )
        return Place(
            label,
            round(best["latitude"], 2),
            round(best["longitude"], 2),
            best.get("admin1", ""),
            best.get("country_code", ""),
        )

    async def _search(self, name: str) -> list[dict[str, Any]]:
        if not name:
            return []
        data = await self._get(GEOCODE_URL, {"name": name, "count": 10, "language": "en"})
        return data.get("results", []) or []

    async def history(self, lat: float, lon: float) -> tuple[list[rules.Hour], str]:
        """Hourly weather for the last 14 complete local days, plus the time zone name."""
        lat, lon = round(lat, 1), round(lon, 1)  # privacy and a better cache hit rate
        key = (lat, lon, self._now().strftime("%Y-%m-%dT%H"))
        hit = self._wx.get(key)
        if hit and time.time() - hit[0] < WEATHER_TTL_S:
            data = hit[1]
        else:
            data = await self._get(
                FORECAST_URL,
                {
                    "latitude": lat,
                    "longitude": lon,
                    "past_days": HISTORY_DAYS,
                    "forecast_days": 1,
                    "timezone": "auto",
                    "hourly": "temperature_2m,relative_humidity_2m,precipitation",
                },
            )
            if len(self._wx) >= CACHE_MAX:
                self._wx.clear()
            self._wx[key] = (time.time(), data)
        return parse_hours(data, self._now()), str(data.get("timezone", ""))


def parse_hours(data: dict[str, Any], now_utc: datetime) -> list[rules.Hour]:
    """Keep only hours of complete days before today (local), dropping forecast hours and
    any hour with a missing value."""
    hourly = data.get("hourly") or {}
    offset = timedelta(seconds=int(data.get("utc_offset_seconds") or 0))
    today_local = (now_utc + offset).date()
    out = []
    for t, temp, rh, p in zip(
        hourly.get("time", []),
        hourly.get("temperature_2m", []),
        hourly.get("relative_humidity_2m", []),
        hourly.get("precipitation", []),
        strict=False,
    ):
        if temp is None or rh is None:
            continue
        when = datetime.fromisoformat(t)
        if when.date() >= today_local:
            continue
        out.append(rules.Hour(when, float(temp), float(rh), float(p or 0.0)))
    return out


def summarise(hours: list[rules.Hour], crop: str | None, place: Place) -> dict[str, Any]:
    """The general rain and humidity summary for any crop, plus only the published indices
    that apply to this crop."""
    days = rules.group_days(hours)[-HISTORY_DAYS:]
    if not days:
        raise ConditionsError("No complete days of weather were available for this place.")
    wet = [d.wet_hours for d in days]
    summary: dict[str, Any] = {
        "place": place.label,
        "approximate_location": place.approximate,
        "days": len(days),
        "from": days[0].day.isoformat(),
        "to": days[-1].day.isoformat(),
        "rain_mm_total": round(sum(d.rain_mm for d in days), 1),
        "rainy_days": sum(1 for d in days if d.rain_mm >= 1.0),
        "wet_hours_total": sum(wet),
        "days_with_10h_wet": sum(1 for w in wet if w >= 10),
        "temp_c": {
            "min": round(min(d.tmin for d in days), 1),
            "max": round(max(d.tmax for d in days), 1),
            "mean": round(sum(d.tmean for d in days) / len(days), 1),
        },
        "gdd_base10_14d": round(sum(rules.gdd(d) for d in days), 1),
        "daily": [
            {
                "date": d.day.isoformat(),
                "rain_mm": d.rain_mm,
                "wet_h": d.wet_hours,
                "tmin": round(d.tmin, 1),
                "tmax": round(d.tmax, 1),
            }
            for d in days
        ],
        "caveat": CAVEAT,
        "attribution": ATTRIBUTION,
    }
    indices: list[dict[str, Any]] = []
    if crop == "tomato":
        dsv = [rules.tomcast_day(d) for d in days]
        indices.append(
            {
                "name": "TOMCAST disease severity values",
                "applies_to": "tomato early blight, Septoria leaf spot, anthracnose",
                "last_7_days": sum(dsv[-7:]),
                "last_14_days": sum(dsv),
                "daily": dsv,
                "meaning": f"Spray-timing programmes advise a protectant fungicide after about "
                f"{rules.TOMCAST_ADVISORY}-20 accumulated values (UConn standard: "
                f"{rules.TOMCAST_ADVISORY}). A weather-risk index, not a diagnosis.",
                "sources": [
                    "https://ipm.ucanr.edu/DISEASE/DATABASE/dsvtable.html",
                    "https://ipm.cahnr.uconn.edu/?p=1983",
                ],
            }
        )
    elif crop == "potato":
        smith = rules.smith_periods(days)
        hutton = rules.hutton_periods(days)
        first = days[0].hours[0].time
        runs = [r for r in rules.wallin_periods(hours) if r[0] >= first]
        seven_days_ago = days[-7].hours[0].time if len(days) >= 7 else first
        indices += [
            {
                "name": "Hutton criteria periods",
                "applies_to": "potato late blight (UK-calibrated)",
                "count": len(hutton),
                "dates": [d.isoformat() for d in hutton],
                "meaning": "Each is a pair of days warm and humid enough for late blight to "
                "spread, by the UK national warning criteria.",
                "sources": ["https://edepot.wur.nl/448972"],
            },
            {
                "name": "Smith periods",
                "applies_to": "potato late blight (UK-calibrated, stricter than Hutton)",
                "count": len(smith),
                "dates": [d.isoformat() for d in smith],
                "meaning": "The older UK warning criteria; fewer days qualify than for Hutton.",
                "sources": ["https://edepot.wur.nl/448972"],
            },
            {
                "name": "Wallin late blight severity values",
                "applies_to": "potato late blight (Northeast US)",
                "last_7_days": sum(v for end, _, v in runs if end >= seven_days_ago),
                "last_14_days": sum(v for _, _, v in runs),
                "meaning": f"Maine guidance: {rules.WALLIN_FIRST_SPRAY} accumulated values "
                "after emergence call for the first fungicide spray; blight is expected 7-10 "
                "days later. A weather-risk index, not a diagnosis.",
                "sources": [
                    "https://extension.umaine.edu/publications/wp-content/uploads/sites/52/2020/03/2418_updated2020.pdf"
                ],
            },
        ]
    summary["indices"] = indices
    if not indices:
        summary["note"] = (
            f"No published crop-specific disease index applies to "
            f"{crop or 'an unknown crop'}; this is the general rain and humidity summary only."
        )
    return summary
