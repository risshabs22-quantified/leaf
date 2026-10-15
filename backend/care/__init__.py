"""Leaf Care: each morning, the day's tasks for one garden, decided in code.

Every task has a reason, a due time (local, stored as Unix time) and a source: a cited
reference (FAO-56, NWS, a species fact's page) or a rule labelled as Leaf's heuristic.
Models never add tasks; Lightning only words the morning summary (care_wording).

Tasks:
- water / skip per plant: FAO-56 balance outdoors, the indoor model indoors (climate.water)
- protect_frost, protect_heat, protect_wind, flood: forecast risks and NWS alerts
- feed: plants whose profile has a verified feeding fact, every 4 weeks in the growing season
- harvest_check: plants at the fruiting or harvest stage, every 3 days
- prune_check: outdoor plants, once a month
- scout_<pest>: degree days since the season began reach the pest's published threshold
- photo_checkin: once a week per plant, and the morning after a frost night
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from backend.care.pests import PESTS, Pest
from backend.care.sensor import is_fresh, sensor_advice
from backend.climate.crops import Crop, find_crop
from backend.climate.forecast import Forecast
from backend.climate.nws import Alerts
from backend.climate.risk import risks
from backend.climate.sources import SOURCES
from backend.climate.water import Placement, indoor_advice, outdoor_advice
from backend.data.repo import Garden, Plant, TaskIn

HEURISTIC = {"title": SOURCES["HEURISTIC"]["title"], "url": "", "heuristic": True}
FEED_EVERY_DAYS = 28
HARVEST_EVERY_DAYS = 3
PRUNE_EVERY_DAYS = 30
PHOTO_EVERY_DAYS = 7
SCOUT_WINDOW_F = 300  # degree days (F) after the threshold during which scouting is suggested
GROWING_MONTHS_NORTH = range(4, 10)  # April-September (heuristic)
GROWING_MONTHS_SOUTH = (10, 11, 12, 1, 2, 3)


def src(key: str) -> dict[str, Any]:
    return {"title": SOURCES[key]["title"], "url": SOURCES[key]["url"]}


@dataclass
class PlantState:
    """What the planner knows about one plant this morning."""

    plant: Plant
    crop: Crop | None = None
    profile: dict[str, Any] | None = None  # knowledge.profile.profile_view, if ready
    waterings: list[dict[str, Any]] = field(default_factory=list)  # outdoor balance input
    last_watered: date | None = None  # indoor model
    feedback: list[str] = field(default_factory=list)
    reading: Any = None  # the plant's newest soil-moisture reading (backend/care/sensor.py)

    @property
    def name(self) -> str:
        p = self.plant
        return p.nickname or p.common_name or p.species or "plant"


@dataclass
class Plan:
    tasks: list[TaskIn]
    water: list[dict[str, Any]]  # per plant: action, reason (shown even when no task)
    urgent: list[dict[str, Any]]  # alerts for the top of the Today screen


def _at(day: date, hh: int, mm: int, tz: ZoneInfo) -> int:
    return int(datetime.combine(day, time(hh, mm), tzinfo=tz).timestamp())


def _sunset(fc: Forecast, day: date, tz: ZoneInfo) -> int | None:
    for d in fc.day_rows():
        if d["date"] == day.isoformat() and d.get("sunset"):
            return int(datetime.fromisoformat(d["sunset"]).replace(tzinfo=tz).timestamp())
    return None


def _cycle(plant: Plant, today: date, every: int, offset: int, tz: ZoneInfo) -> bool:
    """Every `every` days counted from when the plant was added, in the garden's time zone."""
    start = datetime.fromtimestamp(plant.created_at, tz).date()
    return (today - start).days >= offset and (today - start).days % every == offset % every


def _alert_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _starts_by(a: Any, horizon: int) -> bool:
    """An alert matters for today's tasks when it starts before `horizon` (or has no time)."""
    onset = _alert_time(a.onset)
    return onset is None or onset.timestamp() <= horizon


def _alert_when(a: Any, tz: ZoneInfo) -> str:
    onset, ends = _alert_time(a.onset), _alert_time(a.ends)
    fmt = "%a %H:%M"
    if onset and ends:
        return f" from {onset.astimezone(tz):{fmt}} to {ends.astimezone(tz):{fmt}}"
    if onset:
        return f" from {onset.astimezone(tz):{fmt}}"
    return ""


def _alert_source(a: Any) -> dict[str, Any]:
    return {"title": f"NWS {a.event}: {a.headline}"[:200], "url": "https://www.weather.gov/"}


def _profile_fact(profile: dict[str, Any] | None, field_name: str) -> dict[str, Any] | None:
    items = (profile or {}).get("facts", {}).get(field_name) or []
    return items[0] if items else None


def _hardy(state: PlantState) -> bool:
    fact = _profile_fact(state.profile, "frost_tolerance")
    return bool(fact and fact["value"] == "hardy")


def plan_day(
    garden: Garden,
    states: list[PlantState],
    fc: Forecast | None,
    alerts: Alerts | None,
    season_gdd: dict[str, float] | None,
    today: date,
    now: datetime,
    trace_id: str = "",
) -> Plan:
    tz = ZoneInfo(garden.timezone)
    day = today.isoformat()
    tasks: list[TaskIn] = []
    water: list[dict[str, Any]] = []
    urgent: list[dict[str, Any]] = []

    def task(
        kind: str,
        title: str,
        reason: str,
        due: int,
        source: dict[str, Any],
        plant: Plant | None = None,
        level: str = "normal",
    ) -> None:
        tasks.append(
            TaskIn(
                garden_id=garden.id,
                plant_id=plant.id if plant else None,
                day=day,
                kind=kind,
                title=title[:120],
                reason=reason[:600],
                source=source,
                due_at=due,
                urgency=level,
                created_by="rule",
                trace_id=trace_id,
            )
        )

    outdoor = [s for s in states if s.plant.setting == "outdoor"]
    southern = (garden.lat or 0) < 0

    # 1. Water or skip, every plant.
    for s in states:
        placement = Placement.from_container(s.plant.container)
        if is_fresh(s.reading, now.timestamp()):  # the gardener's sensor beats any estimate
            advice = sensor_advice(s.reading)
        elif s.plant.setting == "indoor":
            advice = indoor_advice(placement, today, southern, s.last_watered, s.feedback)
        elif fc is not None:
            advice = outdoor_advice(s.crop, s.plant.stage, placement, fc, s.waterings, now)
        else:
            continue
        water.append(
            {
                "plant_id": s.plant.id,
                "name": s.name,
                "action": advice.action,
                "reason": advice.reason,
                "next_check": advice.next_check,
                "model": advice.model,
                "label": s.reading.label if advice.model == "sensor" else "",
            }
        )
        if advice.action in ("water", "check"):
            amount = (
                f" about {advice.amount_l:g} L"
                if advice.amount_l
                else f" about {advice.amount_mm:g} mm ({advice.amount_mm:g} L per m²)"
                if advice.amount_mm
                else ""
            )
            verb = "Water" if advice.action == "water" else "Check the soil of"
            test = (
                " (TEST reading)" if advice.model == "sensor" and s.reading.label == "test" else ""
            )
            source = (
                {**advice.sources[0], "heuristic": True}  # the dry mark is Leaf's own default
                if advice.model == "sensor"
                else HEURISTIC
                if advice.model == "indoor"
                else {**src("fao56_ch8"), "note": "FAO-56 water balance"}
            )
            task(
                "water",
                f"{verb} {s.name}{amount if advice.action == 'water' else ''}{test}",
                advice.reason,
                _at(today, 8, 0, tz),
                source,
                s.plant,
            )

    # 2. Protection from the forecast and NWS alerts (outdoor plants only). Alerts work even
    #    when the forecast is unavailable; an alert counts for today only when it starts by
    #    tomorrow morning, and its text uses the alert's own times.
    if outdoor:
        r = risks(fc, now) if fc is not None else {"frost": [], "heat": [], "wind": []}
        sunset = (_sunset(fc, today, tz) if fc is not None else None) or _at(today, 18, 0, tz)
        horizon = _at(today + timedelta(days=1), 10, 0, tz)
        active = [a for a in (alerts.alerts if alerts else []) if _starts_by(a, horizon)]
        tonight = [n for n in r["frost"] if n["night_of"] == day]
        frost_alerts = [a for a in active if a.kind in ("frost", "freeze")]
        if tonight or frost_alerts:
            level = tonight[0]["level"] if tonight else "freeze"
            hard = level == "hard_freeze" or any(a.event == "Freeze Warning" for a in frost_alerts)
            tender = [s for s in outdoor if not _hardy(s) or hard]
            if tender:
                names = ", ".join(s.name for s in tender[:6])
                if tonight:
                    lead = (
                        f"Tonight's low is forecast at {tonight[0]['low_c']:.0f} \u00b0C "
                        f"({level.replace('_', ' ')}). "
                    )
                else:
                    a = frost_alerts[0]
                    lead = f"NWS {a.event}{_alert_when(a, tz)}. "
                why = (
                    lead + f"Cover {names} before dark, water dry soil in the afternoon "
                    "(moist soil holds heat), and bring pots in."
                )
                source = _alert_source(frost_alerts[0]) if frost_alerts else src("nws_frost")
                task(
                    "protect_frost",
                    "Protect plants from frost tonight",
                    why,
                    sunset - 3600,
                    source,
                    level="urgent" if level != "frost_possible" else "soon",
                )
                urgent.append(
                    {"kind": "frost", "title": "Frost tonight", "text": why, "source": source}
                )
        heat_today = [h for h in r["heat"] if h["date"] == day]
        heat_alerts = [a for a in active if a.kind == "heat"]
        if heat_today or heat_alerts:
            lead = (
                f"Today's high is forecast at {heat_today[0]['high_c']:.0f} \u00b0C. "
                if heat_today
                else f"NWS {heat_alerts[0].event}{_alert_when(heat_alerts[0], tz)}. "
            )
            why = lead + (
                "Water early in the morning, shade young plants in the afternoon "
                "and skip feeding today; tomatoes set less fruit above about 32 \u00b0C."
            )
            source = src("rutgers_heat")
            task(
                "protect_heat",
                "Protect plants from the heat",
                why,
                _at(today, 9, 0, tz),
                source,
                level="soon",
            )
            urgent.append({"kind": "heat", "title": "Hot day", "text": why, "source": source})
        wind_today = [w for w in r["wind"] if w["time"].startswith(day)]
        wind_alerts = [a for a in active if a.kind == "wind"]
        if wind_today or wind_alerts:
            gust = max((w["gust_kmh"] for w in wind_today), default=0)
            lead = (
                f"Gusts up to {gust:.0f} km/h are forecast today. "
                if gust
                else f"NWS {wind_alerts[0].event}{_alert_when(wind_alerts[0], tz)}. "
            )
            task(
                "protect_wind",
                "Secure plants against wind",
                lead + "Stake tall plants, secure covers and move pots to shelter.",
                _at(today, 12, 0, tz),
                _alert_source(wind_alerts[0]) if wind_alerts else src("nws_wind"),
                level="soon",
            )
        for a in [a for a in active if a.kind == "flood"]:
            why = (
                f"NWS {a.event}{_alert_when(a, tz)}: {a.headline} Move pots off low ground "
                "and do not water today."
            )
            task(
                "flood",
                "Flood alert: protect pots and beds",
                why,
                _at(today, 8, 0, tz),
                _alert_source(a),
                level="urgent",
            )
            urgent.append(
                {"kind": "flood", "title": a.event, "text": why, "source": _alert_source(a)}
            )
            break
        # The morning after a frost night: look for damage.
        if any(n["night_of"] == (today - timedelta(days=1)).isoformat() for n in r["frost"]):
            task(
                "photo_checkin",
                "Check plants for frost damage",
                "Last night was frosty. Look for blackened or limp leaves and take a photo "
                "of any damage so Leaf can compare.",
                _at(today, 10, 0, tz),
                HEURISTIC,
            )

    # 3. Feeding, harvest, pruning and photo check-ins on their cycles.
    growing = today.month in (GROWING_MONTHS_SOUTH if southern else GROWING_MONTHS_NORTH)
    for s in states:
        feeding = _profile_fact(s.profile, "feeding")
        if feeding and growing and _cycle(s.plant, today, FEED_EVERY_DAYS, 14, tz):
            task(
                "feed",
                f"Feed {s.name}",
                f"From {feeding['source']['title']}: {feeding['value']}. Leaf reminds every "
                f"{FEED_EVERY_DAYS} days in the growing season (its own interval).",
                _at(today, 9, 0, tz),
                {**feeding["source"], "interval": "heuristic"},
                s.plant,
            )
        if (s.plant.stage or "").lower() in ("fruiting", "harvest", "ripening") and _cycle(
            s.plant, today, HARVEST_EVERY_DAYS, 0, tz
        ):
            task(
                "harvest_check",
                f"Check {s.name} for ripe produce",
                "It is fruiting: picking often keeps it producing. Leaf checks every "
                f"{HARVEST_EVERY_DAYS} days (its own interval).",
                _at(today, 10, 0, tz),
                HEURISTIC,
                s.plant,
            )
        if (
            s.plant.setting == "outdoor"
            and growing
            and _cycle(s.plant, today, PRUNE_EVERY_DAYS, 10, tz)
        ):
            task(
                "prune_check",
                f"Look over {s.name} for dead or diseased growth",
                "Removing dead, damaged or diseased growth limits disease. Leaf suggests a "
                "monthly look (its own interval).",
                _at(today, 11, 0, tz),
                HEURISTIC,
                s.plant,
            )
        if _cycle(s.plant, today, PHOTO_EVERY_DAYS, 3, tz):
            task(
                "photo_checkin",
                f"Take a check-in photo of {s.name}",
                "A weekly photo lets Leaf spot changes early (its own interval).",
                _at(today, 12, 0, tz),
                HEURISTIC,
                s.plant,
            )

    # 4. Pest scouting from degree days (US extension thresholds, counted from January 1:
    #    northern hemisphere only).
    if season_gdd is not None and not southern:
        for pest in PESTS:
            hosts = [s for s in outdoor if s.crop and s.crop.key in pest.host_crops]
            if not hosts:
                continue
            total_c = season_gdd.get(f"{pest.base_c:g}")
            if total_c is None:
                continue
            total_f = total_c * 9 / 5
            if pest.gdd_f <= total_f <= pest.gdd_f + SCOUT_WINDOW_F:
                names = ", ".join(sorted({s.name for s in hosts}))
                task(
                    f"scout_{pest.key}",
                    f"Scout {names} for {pest.name}",
                    f"Degree days since January 1 have reached {total_f:.0f} (base "
                    f"{pest.base_f:g} °F; the source counts from {pest.start}); "
                    f"{pest.event} at about {pest.gdd_f:g}. "
                    f"{pest.look_for}",
                    _at(today, 9, 30, tz),
                    {"title": pest.source_title, "url": pest.source_url},
                    level="soon",
                )

    return Plan(tasks=tasks, water=water, urgent=urgent)


def crop_for(plant: Plant) -> Crop | None:
    """Scientific name first (exact species), then the common name."""
    return find_crop(plant.species) if plant.species else find_crop(plant.common_name)


__all__ = ["Pest", "Plan", "PlantState", "crop_for", "plan_day"]
