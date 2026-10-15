"""National Weather Service active alerts for a point (US only; free, no key).

GET https://api.weather.gov/alerts/active?point=lat,lon (GeoJSON; CAP fields). A User-Agent
that identifies the app is required. Points outside the US answer HTTP 400, which Leaf
records for the cell so it never asks again. Event names were checked against the live
/alerts/types list (2026-10-14): "Hard Freeze Warning" and "Excessive Heat Warning" are not
current types; "Extreme Heat Warning" is.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from backend.cache import SharedCache

ALERTS_URL = "https://api.weather.gov/alerts/active"
USER_AGENT = "Leaf garden care (https://leafathon.site)"
CACHE_TTL_S = 600
NOT_COVERED_TTL_S = 7 * 24 * 3600

# NWS event -> what it means for a garden.
EVENTS: dict[str, str] = {
    "Frost Advisory": "frost",
    "Freeze Watch": "freeze",
    "Freeze Warning": "freeze",
    "Heat Advisory": "heat",
    "Extreme Heat Watch": "heat",
    "Extreme Heat Warning": "heat",
    "Wind Advisory": "wind",
    "High Wind Watch": "wind",
    "High Wind Warning": "wind",
    "Flood Watch": "flood",
    "Flood Advisory": "flood",
    "Flood Warning": "flood",
    "Flash Flood Watch": "flood",
    "Flash Flood Warning": "flood",
}


class Alert(BaseModel):
    id: str
    event: str
    kind: str  # frost, freeze, heat, wind, flood
    severity: str = ""
    urgency: str = ""
    headline: str = ""
    onset: str | None = None
    ends: str | None = None
    instruction: str = ""


class Alerts(BaseModel):
    covered: bool  # False outside the US: NWS has no alerts there, Leaf uses the forecast
    alerts: list[Alert] = []
    source: str = "National Weather Service"


def parse(data: dict[str, Any]) -> list[Alert]:
    out = []
    for f in data.get("features") or []:
        p = f.get("properties") or {}
        event = str(p.get("event") or "")
        if event not in EVENTS:
            continue
        out.append(
            Alert(
                id=str(p.get("id") or f.get("id") or ""),
                event=event,
                kind=EVENTS[event],
                severity=str(p.get("severity") or ""),
                urgency=str(p.get("urgency") or ""),
                headline=str(p.get("headline") or "")[:300],
                onset=p.get("onset"),
                ends=p.get("ends") or p.get("expires"),
                instruction=str(p.get("instruction") or "")[:800],
            )
        )
    return out


async def active_alerts(
    cell: str,
    lat: float,
    lon: float,
    cache: SharedCache | None = None,
    http: httpx.AsyncClient | None = None,
) -> Alerts:
    if cache is not None:
        hit = await cache.get_json("nws", cell)
        if hit is not None:
            return Alerts.model_validate(hit)
    params = {"point": f"{lat:.4f},{lon:.4f}"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    try:
        if http is not None:
            r = await http.get(ALERTS_URL, params=params, headers=headers, timeout=15)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(ALERTS_URL, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise AlertsUnavailable("NWS did not answer.") from exc
    if r.status_code == 400 and _out_of_bounds(r):  # outside NWS coverage
        result, ttl = Alerts(covered=False), NOT_COVERED_TTL_S
    elif r.status_code == 200:
        try:
            result, ttl = Alerts(covered=True, alerts=parse(r.json())), CACHE_TTL_S
        except ValueError as exc:
            raise AlertsUnavailable("NWS sent an unreadable reply.") from exc
    else:
        raise AlertsUnavailable(f"NWS HTTP {r.status_code}")
    if cache is not None:
        await cache.put_json("nws", cell, result.model_dump(), ttl)
    return result


class AlertsUnavailable(RuntimeError):
    pass


def _out_of_bounds(r: httpx.Response) -> bool:
    """Only NWS's "point is out of bounds" answer means outside the US; any other 400 is a
    passing error and must not mark the cell for good."""
    return "out of bounds" in r.text.lower()
