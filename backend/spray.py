"""When to spray: the best hours in the next five days, from the forecast and fixed rules.

A treatment that washes off, drifts or burns the leaves is wasted or harmful, so the timing
matters as much as the product. Leaf scores every daylight hour of the Open-Meteo forecast in
code (no model involved) and picks the best window each day.

Hard rules, from common label and extension guidance (the product label always wins):
- daylight only, between 6:00 and 20:00 local time;
- leaves dry: no rain this hour or in the two hours before;
- no rain for at least 6 hours after (most products need a few hours to dry and bind);
- wind at most 16 km/h (10 mph) with gusts at most 24 km/h (15 mph), to limit drift;
- between 7 C (45 F) and the plan's heat limit: 32 C (90 F), or 29 C (85 F) when the plan
  uses oil or sulfur, which can burn leaves in heat;
- if a product harms bees, only early (before 8:00) or in the evening (from 18:00).

Soft preferences lower the score: fewer than 24 dry hours after, temperatures away from
13-24 C, dead calm air (drift can hang in still air), humidity above 85% (slow drying), and
hot middays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

FIRST_HOUR, LAST_HOUR = 6, 19  # a window may start 6:00-19:00 and must end by 20:00
MIN_C, IDEAL_LOW_C, IDEAL_HIGH_C = 7.0, 13.0, 24.0
MAX_WIND_KMH, MAX_GUST_KMH, CALM_KMH, BREEZY_KMH = 16.0, 24.0, 3.0, 10.0
WET_MM, WET_PROB = 0.2, 40  # an hour counts as rainy at 0.2 mm or a 40% chance
DRY_AFTER_H, GOOD_DRY_H = 6, 24
BEE_EARLY, BEE_LATE = 8, 18  # bee-safe: start before 8:00 or from 18:00
DAYS = 5
SOONER_MARGIN = 5  # prefer an earlier window when it scores within this of the best

RULES = [
    "Daylight only (6:00 to 20:00).",
    "Leaves dry: no rain in the hour or the two hours before.",
    "At least 6 dry hours after spraying; 24 or more is best.",
    "Wind 16 km/h (10 mph) or less, gusts 24 km/h (15 mph) or less.",
    "Between 7 C (45 F) and the heat limit for the product.",
    "The product label always wins over these rules.",
]


@dataclass
class FHour:
    time: datetime  # local
    temp_c: float
    rh: float
    precip_mm: float
    prob: float
    wind_kmh: float
    gust_kmh: float

    @property
    def wet(self) -> bool:
        return self.precip_mm >= WET_MM or self.prob >= WET_PROB


@dataclass
class Scored:
    hour: FHour
    ok: bool
    score: float
    blocker: str = ""
    dry_after: int = 0
    notes: list[str] = field(default_factory=list)


class Window(BaseModel):
    start: str  # local ISO time
    end: str
    start_utc: str
    end_utc: str
    score: int
    dry_hours_after: int  # capped at 48
    temp_c: tuple[float, float]
    wind_kmh: float
    gust_kmh: float
    notes: list[str] = Field(default_factory=list)
    best: bool = False


class DayHours(BaseModel):
    date: str
    hours: list[dict[str, Any]]  # {"hour": 6, "score": 0-100 or -1, "why": str}


class SprayForecast(BaseModel):
    place: str = ""
    timezone: str = ""
    windows: list[Window] = Field(default_factory=list)  # best window per day, in time order
    days: list[DayHours] = Field(default_factory=list)
    heat_limit_c: float = 32.0
    bee_safe_hours: bool = False
    rules: list[str] = Field(default_factory=lambda: list(RULES))
    message: str = ""  # shown when there is no good window
    attribution: str = "Weather data by Open-Meteo.com"


def parse_forecast(data: dict[str, Any]) -> tuple[list[FHour], timedelta]:
    h = data.get("hourly") or {}
    offset = timedelta(seconds=int(data.get("utc_offset_seconds") or 0))
    cols = [
        h.get(k, [])
        for k in (
            "time",
            "temperature_2m",
            "relative_humidity_2m",
            "precipitation",
            "precipitation_probability",
            "wind_speed_10m",
            "wind_gusts_10m",
        )
    ]
    out = []
    for t, temp, rh, p, prob, wind, gust in zip(*cols, strict=False):
        if temp is None or wind is None:
            continue
        out.append(
            FHour(
                datetime.fromisoformat(t),
                float(temp),
                float(rh if rh is not None else 60),
                float(p or 0),
                float(prob or 0),
                float(wind),
                float(gust if gust is not None else wind),
            )
        )
    return out, offset


def _dry_after(hours: list[FHour], i: int) -> int:
    n = 0
    for h in hours[i + 1 : i + 1 + 48]:
        if h.wet:
            break
        n += 1
    return n


def score_hour(hours: list[FHour], i: int, heat_limit_c: float, bees: bool) -> Scored:
    h = hours[i]
    hr = h.time.hour
    dry_after = _dry_after(hours, i)
    if not FIRST_HOUR <= hr <= LAST_HOUR:
        return Scored(h, False, 0, "dark")
    if bees and BEE_EARLY <= hr < BEE_LATE:
        return Scored(h, False, 0, "bees foraging")
    if h.wet or any(p.wet for p in hours[max(0, i - 2) : i]):
        return Scored(h, False, 0, "rain or wet leaves")
    if dry_after < DRY_AFTER_H and i + DRY_AFTER_H < len(hours):
        return Scored(h, False, 0, f"rain within {DRY_AFTER_H} h", dry_after)
    if h.wind_kmh > MAX_WIND_KMH or h.gust_kmh > MAX_GUST_KMH:
        return Scored(h, False, 0, "too windy", dry_after)
    if h.temp_c < MIN_C:
        return Scored(h, False, 0, "too cold", dry_after)
    if h.temp_c > heat_limit_c:
        return Scored(h, False, 0, "too hot", dry_after)

    score, notes = 100.0, []
    if dry_after < GOOD_DRY_H:
        score -= 2 * (GOOD_DRY_H - dry_after)
        notes.append(f"rain about {dry_after + 1} h later")
    if h.temp_c < IDEAL_LOW_C:
        score -= 2 * (IDEAL_LOW_C - h.temp_c)
    elif h.temp_c > IDEAL_HIGH_C:
        score -= 2 * (h.temp_c - IDEAL_HIGH_C)
    if h.wind_kmh < CALM_KMH:
        score -= 10
        notes.append("very still air, so spray low and close to the leaves")
    elif h.wind_kmh > BREEZY_KMH:
        score -= 3 * (h.wind_kmh - BREEZY_KMH)
    if h.rh > 85:
        score -= 10
        notes.append("humid, so it will dry slowly")
    if 11 <= hr <= 15 and h.temp_c > IDEAL_HIGH_C:
        score -= 15
    return Scored(h, True, max(1.0, score), "", dry_after, notes)


def plan_spray(
    data: dict[str, Any],
    now_local: datetime | None,
    heat_limit_c: float = 32.0,
    bees: bool = False,
    place: str = "",
) -> SprayForecast:
    hours, offset = parse_forecast(data)
    if now_local is None:
        now_local = (
            datetime.now(UTC).replace(tzinfo=None) + offset
        )  # Open-Meteo times are naive local
    out = SprayForecast(
        place=place,
        timezone=str(data.get("timezone", "")),
        heat_limit_c=heat_limit_c,
        bee_safe_hours=bees,
    )
    scored = [score_hour(hours, i, heat_limit_c, bees) for i in range(len(hours))]
    first_day = now_local.date()
    days = [first_day + timedelta(days=d) for d in range(DAYS)]
    for day in days:
        cells = []
        for s in scored:
            if s.hour.time.date() != day or not FIRST_HOUR <= s.hour.time.hour <= LAST_HOUR:
                continue
            past = s.hour.time < now_local.replace(minute=0, second=0, microsecond=0)
            cells.append(
                {
                    "hour": s.hour.time.hour,
                    "score": -1 if past or not s.ok else round(s.score),
                    "why": "past" if past else s.blocker,
                }
            )
        out.days.append(DayHours(date=day.isoformat(), hours=cells))

        # Best run of consecutive good hours that day (a spray takes under an hour; longer
        # runs give slack). A run is scored by its mean.
        runs: list[list[Scored]] = []
        run: list[Scored] = []
        for s in scored:
            t = s.hour.time
            usable = (
                t.date() == day
                and s.ok
                and t >= now_local.replace(minute=0, second=0, microsecond=0)
            )
            if usable:
                run.append(s)
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        if not runs:
            continue
        best = max(
            runs, key=lambda r: (sum(x.score for x in r) / len(r), -r[0].hour.time.timestamp())
        )
        start, end = best[0].hour.time, best[-1].hour.time + timedelta(hours=1)
        notes = [n for x in best for n in x.notes]
        if bees:
            notes.append("outside bee foraging hours")
        mean = round(sum(x.score for x in best) / len(best))
        dry = best[-1].dry_after
        out.windows.append(
            Window(
                start=start.isoformat(timespec="minutes"),
                end=end.isoformat(timespec="minutes"),
                start_utc=(start - offset).isoformat(timespec="minutes") + "Z",
                end_utc=(end - offset).isoformat(timespec="minutes") + "Z",
                score=mean,
                dry_hours_after=dry,
                temp_c=(min(x.hour.temp_c for x in best), max(x.hour.temp_c for x in best)),
                wind_kmh=max(x.hour.wind_kmh for x in best),
                gust_kmh=max(x.hour.gust_kmh for x in best),
                notes=list(dict.fromkeys(notes)),
            )
        )

    if out.windows:
        top = max(w.score for w in out.windows)
        # Sooner beats slightly better: the problem keeps spreading while you wait.
        pick = next(w for w in out.windows if w.score >= top - SOONER_MARGIN)
        pick.best = True
    else:
        out.message = (
            "No hour in the next five days meets the rules (rain, wind or heat). Hold off, do "
            "the non-spray steps now, and check again in a couple of days."
        )
    return out
