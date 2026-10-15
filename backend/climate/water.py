"""When and how much to water, computed in code.

Outdoor plants: the FAO-56 root-zone water balance (Chapter 8), run day by day over the last
7 days (the forecast model's analysis of the recent past) and the next 7 of forecast:
    Dr,i = Dr,i-1 - (P - RO)i - Ii + ETc,i + DPi,   0 <= Dr <= TAW          (Eq. 85, 86)
    ETc = Kc x ET0;  TAW = 1000 (thetaFC - thetaWP) Zr;  RAW = p TAW        (Eq. 82, 83)
    p = p(Table 22) + 0.04 (5 - ETc), limited to 0.1 <= p <= 0.8          (Table 22 note)
Rain below 0.2 ET0 is ignored (FAO-56: it evaporates). Water and rain arrive early in the
day, so they refill the root zone first (anything beyond field capacity drains: DP), then
the day's ETc is used. This reproduces FAO-56 Example 38's published table (day 1: 40 mm on a
23 mm deficit drains 17 mm and ends the day 5 mm short); Eq. 88 read literally would take
ETc first and end that day at zero. Runoff and capillary rise are taken as zero
(garden beds; water table assumed deeper than 1 m). Water when Dr reaches RAW; the amount
refills the root zone (net irrigation <= Dr).

Indoor plants never use outdoor ET0. Nobody publishes numbers for houseplant watering
intervals, so the indoor model is an openly labelled heuristic (pot size, light, season)
that learns from the gardener's "too wet / too dry" feedback.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.climate.crops import Crop
from backend.climate.forecast import Forecast
from backend.climate.sources import cite

# FAO-56 Table 19: (thetaFC low, high, thetaWP low, high) in m3/m3; Leaf uses the midpoints.
SOILS: dict[str, tuple[float, float, float, float]] = {
    "sand": (0.07, 0.17, 0.02, 0.07),
    "loamy_sand": (0.11, 0.19, 0.03, 0.10),
    "sandy_loam": (0.18, 0.28, 0.06, 0.16),
    "loam": (0.20, 0.30, 0.07, 0.17),
    "silt_loam": (0.22, 0.36, 0.09, 0.21),
    "silt": (0.28, 0.36, 0.12, 0.22),
    "silty_clay_loam": (0.30, 0.37, 0.17, 0.24),
    "silty_clay": (0.30, 0.42, 0.17, 0.29),
    "clay": (0.32, 0.40, 0.20, 0.24),
}
DEFAULT_SOIL = "loam"

# Labelled heuristics (no published source; shown to the gardener as assumptions).
START_FRACTION_OF_RAW = 0.5  # depletion assumed 7 days ago when Leaf knows nothing newer
LIKELY_RAIN_PROBABILITY = 70  # forecast rain counts only at this probability or above
POT_AVAILABLE_FRACTION = 0.25  # litres of plant-available water per litre of potting mix
POT_CANOPY_FACTOR = 2.0  # canopy area as a multiple of the pot's top, unless given
GENERIC = {"kc": (0.6, 1.0, 0.85), "zr": (0.4, 0.6), "p": 0.40}

Stage = Literal["initial", "development", "mid", "late"]
STAGE_WORDS: dict[str, Stage] = {
    "seed": "initial",
    "seedling": "initial",
    "initial": "initial",
    "transplant": "initial",
    "young": "development",
    "vegetative": "development",
    "development": "development",
    "growing": "development",
    "flowering": "mid",
    "fruiting": "mid",
    "mid": "mid",
    "mature": "mid",
    "harvest": "late",
    "late": "late",
    "ripening": "late",
    "senescence": "late",
}


def soil_water(soil: str) -> tuple[float, float]:
    fc_lo, fc_hi, wp_lo, wp_hi = SOILS.get(soil, SOILS[DEFAULT_SOIL])
    return (fc_lo + fc_hi) / 2, (wp_lo + wp_hi) / 2


def taw_mm(theta_fc: float, theta_wp: float, zr_m: float) -> float:
    """FAO-56 Eq. 82."""
    return 1000 * (theta_fc - theta_wp) * zr_m


def p_adjusted(p_table: float, etc_mm: float) -> float:
    """FAO-56 Table 22 note: p + 0.04 (5 - ETc), limited to 0.1-0.8."""
    return min(0.8, max(0.1, p_table + 0.04 * (5 - etc_mm)))


def stage_of(text: str | None) -> Stage:
    return STAGE_WORDS.get((text or "").strip().lower(), "mid")


def kc_for(crop: Crop | None, stage: Stage) -> float:
    """FAO-56 Kc for a stage. Kc changes linearly across the development stage (ini to mid)
    and the late stage (mid to end), so those two take the midpoint."""
    ini, mid, end = crop.kc if crop and crop.kc else GENERIC["kc"]
    return {
        "initial": ini,
        "development": round((ini + mid) / 2, 3),
        "mid": mid,
        "late": round((mid + end) / 2, 3),
    }[stage]


def effective_rain(rain_mm: float, et0_mm: float) -> float:
    """FAO-56: daily rain below about 0.2 ET0 evaporates and is ignored."""
    return rain_mm if rain_mm >= 0.2 * et0_mm else 0.0


@dataclass
class Step:
    depletion_start: float
    water_in: float
    deep_percolation: float
    etc: float
    depletion_end: float


def step(depletion: float, water_in: float, etc: float, taw: float) -> Step:
    """One day of FAO-56 Eq. 85-88, with water arriving early in the day (Example 38)."""
    after = depletion - water_in
    dp = max(0.0, -after)
    end = min(taw, max(0.0, after) + etc)
    return Step(depletion, water_in, dp, etc, end)


class Placement(BaseModel):
    """Where a plant grows, from the plant's `container` details."""

    kind: Literal["ground", "raised_bed", "pot"] = "ground"
    soil: str = DEFAULT_SOIL
    bed_depth_m: float | None = Field(None, gt=0.05, le=2)
    pot_litres: float | None = Field(None, gt=0.1, le=2000)
    canopy_m2: float | None = Field(None, gt=0, le=100)
    light: Literal["low", "medium", "bright"] = "medium"
    need: Literal["low", "medium", "high"] = "medium"

    @classmethod
    def from_container(cls, container: dict[str, Any]) -> Placement:
        data = dict(container or {})
        if "pot_l" in data and "pot_litres" not in data:
            data["pot_litres"] = data.pop("pot_l")
        if data.get("pot_litres") and "kind" not in data:
            data["kind"] = "pot"
        try:
            return cls.model_validate({k: v for k, v in data.items() if k in cls.model_fields})
        except ValueError:
            return cls()


class WaterAdvice(BaseModel):
    model: Literal["fao56", "indoor"]
    action: Literal["water", "skip", "check"]
    amount_mm: float | None = None  # depth over the root zone (1 mm = 1 L per m2)
    amount_l: float | None = None  # pots: litres for this pot
    next_check: str | None = None  # date
    reason: str
    numbers: dict[str, Any] = {}
    assumptions: list[str] = []
    history: list[dict[str, Any]] = []
    sources: list[dict[str, str]] = []


def _pot_geometry(litres: float) -> float:
    """Top area (m2) of a pot as tall as it is wide: V = 2 pi r^3."""
    r = (litres / 1000 / (2 * math.pi)) ** (1 / 3)
    return math.pi * r * r


def _water_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_day.setdefault(e["date"], []).append(e)
    return by_day


def outdoor_advice(
    crop: Crop | None,
    stage_text: str | None,
    placement: Placement,
    fc: Forecast,
    waterings: list[dict[str, Any]],
    now: datetime | None = None,
) -> WaterAdvice:
    """FAO-56 balance for one outdoor plant. `waterings`: [{"date", "amount_mm"?, "amount_l"?}]
    from the gardener's log; an amount-less watering refills the root zone."""
    stage = stage_of(stage_text)
    kc = kc_for(crop, stage)
    p_table = crop.p if crop and crop.p is not None else GENERIC["p"]
    zr_lo = (crop.zr if crop and crop.zr else GENERIC["zr"])[0]
    assumptions: list[str] = []
    if crop is None or crop.kc is None:
        assumptions.append(
            "No FAO-56 crop coefficients for this plant yet; Leaf uses Kc 0.6/1.0/0.85, "
            "rooting depth 0.4 m and p 0.40 (assumed; most FAO-56 vegetables at mid-season "
            "are 0.95-1.15)."
        )
    if not stage_text:
        assumptions.append("Growth stage not set; Leaf assumes mid-season (highest water use).")

    pot = placement.kind == "pot" and placement.pot_litres
    if pot:
        litres = float(placement.pot_litres)
        top = _pot_geometry(litres)
        canopy = placement.canopy_m2 or top * POT_CANOPY_FACTOR
        taw = litres * POT_AVAILABLE_FRACTION  # litres
        unit = "L"
        assumptions.append(
            f"Pot: {POT_AVAILABLE_FRACTION:.0%} of the pot's volume is taken as water the plant "
            "can use (heuristic; UC ANR asks mixes to hold at least 40% water by volume)."
        )
        if not placement.canopy_m2:
            assumptions.append(
                f"Leaves assumed to cover {POT_CANOPY_FACTOR:g}x the pot's top (heuristic); set "
                "the plant's spread for a better estimate."
            )
    else:
        theta_fc, theta_wp = soil_water(placement.soil)
        zr = zr_lo
        if placement.kind == "raised_bed" and placement.bed_depth_m:
            zr = min(zr, placement.bed_depth_m)
        taw = taw_mm(theta_fc, theta_wp, zr)
        unit = "mm"
        if placement.soil not in SOILS:
            assumptions.append("Soil type unknown; Leaf assumes loam.")

    def volume(mm: float, area: float) -> float:
        return mm * area  # mm over m2 = litres

    def logged_amount(w: dict[str, Any]) -> float | None:
        """A logged watering in this balance's unit; None means "watered, amount unknown":
        refill. Litres on open ground need the plant's spread to become mm."""
        if pot:
            if w.get("amount_l"):
                return float(w["amount_l"])
            if w.get("amount_mm"):
                return float(w["amount_mm"]) * top
            return None
        if w.get("amount_mm"):
            return float(w["amount_mm"])
        if w.get("amount_l"):
            if placement.canopy_m2:
                return float(w["amount_l"]) / placement.canopy_m2
            note = (
                "A watering logged in litres was not counted: set the plant's spread (square "
                "metres) or log millimetres (1 mm = 1 L per square metre)."
            )
            if note not in assumptions:
                assumptions.append(note)
            return 0.0
        return None

    today = fc.local_today(now)
    rows = fc.day_rows()
    by_day = _water_events(waterings)
    history: list[dict[str, Any]] = []
    depletion: float | None = None
    for d in rows:
        day = date.fromisoformat(d["date"])
        if day >= today:
            break
        et0 = d["et0_fao_evapotranspiration"] or 0.0
        rain = effective_rain(d["precipitation_sum"] or 0.0, et0)
        etc = kc * et0
        p = p_adjusted(p_table, etc)
        if depletion is None:
            depletion = START_FRACTION_OF_RAW * p * taw
            assumptions.append(
                "Leaf does not know how wet the soil was a week ago and starts halfway to the "
                "watering point (heuristic); log waterings to sharpen this."
            )
        irrigation = 0.0
        refill = False
        for w in by_day.get(d["date"], []):
            amount = logged_amount(w)
            if amount is None:
                refill = True
            else:
                irrigation += amount
        water_in = (volume(rain, top) if pot else rain) + irrigation
        use = volume(etc, canopy) if pot else etc
        if refill:
            water_in = max(water_in, depletion)
        s = step(depletion, water_in, use, taw)
        history.append(
            {
                "date": d["date"],
                "et0": round(et0, 2),
                "rain": round(rain, 1),
                "watered": "refill" if refill else round(irrigation, 1),
                "depletion": round(s.depletion_end, 1),
            }
        )
        depletion = s.depletion_end
    if depletion is None:
        return WaterAdvice(
            model="fao56",
            action="check",
            reason="No recent weather for this place yet.",
            sources=cite("fao56_ch8"),
        )

    def day_numbers(d: dict[str, Any]) -> tuple[float, float, float, float]:
        et0 = d["et0_fao_evapotranspiration"] or 0.0
        likely = (d["precipitation_probability_max"] or 0) >= LIKELY_RAIN_PROBABILITY
        rain = effective_rain(d["precipitation_sum"] or 0.0, et0) if likely else 0.0
        etc = kc * et0
        return et0, rain, etc, p_adjusted(p_table, etc) * taw

    for w in by_day.get(today.isoformat(), []):  # watered earlier today
        amount = logged_amount(w)
        depletion = 0.0 if amount is None else max(0.0, depletion - amount)
        history.append(
            {
                "date": today.isoformat(),
                "watered": amount or "refill",
                "depletion": round(depletion, 1),
            }
        )
    future = [d for d in rows if date.fromisoformat(d["date"]) >= today]
    if not future:
        return WaterAdvice(model="fao56", action="check", reason="No forecast for today yet.")
    et0_t, rain_t, etc_t, raw_t = day_numbers(future[0])
    rain_in = volume(rain_t, top) if pot else rain_t
    use_t = volume(etc_t, canopy) if pot else etc_t
    raw_now = raw_t
    numbers = {
        "kc": kc,
        "stage": stage,
        "et0_today_mm": round(et0_t, 2),
        "etc_today": round(use_t, 2),
        "depletion": round(depletion, 1),
        "raw": round(raw_now, 1),
        "taw": round(taw, 1),
        "unit": unit,
        "rain_today_mm": round(rain_t, 1),
    }
    sources = cite("fao56_t12", "fao56_t19", "fao56_ch8", "openmeteo_et0", "HEURISTIC")
    if pot:
        sources += cite("ucanr_mix")
    assumptions.append(
        f"Forecast rain counts only when its chance is {LIKELY_RAIN_PROBABILITY}% or more "
        "(heuristic)."
    )
    if depletion + use_t >= raw_now:
        amount = round(depletion, 1)
        if rain_in > 0 and depletion + use_t - rain_in < raw_now:
            return WaterAdvice(
                model="fao56",
                action="skip",
                next_check=(today + timedelta(days=1)).isoformat(),
                reason=f"Due for water, but {rain_t:.0f} mm of rain is likely today, enough to "
                "cover it: skip and check tomorrow.",
                numbers=numbers,
                assumptions=assumptions,
                history=history,
                sources=sources,
            )
        return WaterAdvice(
            model="fao56",
            action="water",
            amount_mm=None if pot else amount,
            amount_l=amount if pot else None,
            reason=(
                f"The root zone is {depletion:.0f} {unit} short of full and today's use is "
                f"{use_t:.1f} {unit}; it reaches the {raw_now:.0f} {unit} point where this "
                f"plant starts to struggle. Give about {amount:g} {unit}"
                + ("." if pot else f" ({amount:g} L per square metre).")
            ),
            numbers=numbers,
            assumptions=assumptions,
            history=history,
            sources=sources,
        )
    # Not yet: walk the forecast to the day the root zone reaches the watering point.
    level = depletion
    due = None
    for d in future:
        _, rain, etc, raw = day_numbers(d)
        rain_v = volume(rain, top) if pot else rain
        use = volume(etc, canopy) if pot else etc
        if level + use - rain_v >= raw:
            due = d["date"]
            break
        level = step(level, rain_v, use, taw).depletion_end
    return WaterAdvice(
        model="fao56",
        action="skip",
        next_check=due or (today + timedelta(days=len(future))).isoformat(),
        reason=(
            f"No water needed today: the root zone is {depletion:.0f} {unit} short of full and "
            f"this plant copes until {raw_now:.0f} {unit}."
            + (
                f" Leaf expects it to need water on {due}."
                if due
                else " The forecast shows no need this week."
            )
        ),
        numbers=numbers,
        assumptions=assumptions,
        history=history,
        sources=sources,
    )


# ---------- indoor (never outdoor ET0) ----------

BASE_INTERVAL_DAYS = {"low": 14.0, "medium": 7.0, "high": 4.0}
LIGHT_FACTOR = {"low": 1.4, "medium": 1.0, "bright": 0.75}
WINTER_FACTOR = 1.5
FEEDBACK_STEP = {"too_wet": 1.2, "too_dry": 0.85}
DRAIN_FRACTION = 0.2  # roughly a fifth of the pot's volume, enough to see water drain


def winter(day: date, southern: bool) -> bool:
    months = (6, 7, 8) if southern else (12, 1, 2)
    return day.month in months


def indoor_advice(
    placement: Placement,
    today: date,
    southern: bool,
    last_watered: date | None,
    feedback: list[str],
) -> WaterAdvice:
    """Heuristic interval for a houseplant, tuned by feedback ("too_wet", "too_dry")."""
    litres = placement.pot_litres or 3.0
    interval = BASE_INTERVAL_DAYS[placement.need]
    interval *= (litres / 3.0) ** (1 / 3)  # bigger pots dry more slowly
    interval *= LIGHT_FACTOR[placement.light]
    is_winter = winter(today, southern)
    if is_winter:
        interval *= WINTER_FACTOR
    for f in feedback[:5]:  # the newest five answers
        interval *= FEEDBACK_STEP.get(f, 1.0)
    interval = min(42.0, max(2.0, interval))
    days = round(interval)
    amount = round(litres * DRAIN_FRACTION, 1)
    numbers = {
        "interval_days": days,
        "pot_litres": litres,
        "light": placement.light,
        "need": placement.need,
        "winter": is_winter,
        "feedback_used": feedback[:5],
    }
    assumptions = [
        "Indoor plants are not watered by outdoor weather. These intervals are Leaf's own "
        "heuristic (no published numbers exist): bigger pots and dim light dry slower, and "
        "winter growth is slower. Your 'too wet / too dry' answers adjust it.",
        "Always feel the top 2-3 cm of soil first: water only if it is dry.",
    ]
    if last_watered is None:
        return WaterAdvice(
            model="indoor",
            action="check",
            next_check=today.isoformat(),
            reason=f"Check the soil today; if the top is dry, give about {amount:g} L, until "
            "water runs from the drainage hole. Log it so Leaf can plan the next one.",
            numbers=numbers,
            assumptions=assumptions,
            sources=cite("HEURISTIC"),
        )
    due = last_watered + timedelta(days=days)
    if today >= due:
        return WaterAdvice(
            model="indoor",
            action="water",
            amount_l=amount,
            next_check=today.isoformat(),
            reason=f"{(today - last_watered).days} days since the last watering (Leaf's interval "
            f"for this plant is {days}). If the top of the soil is dry, give about {amount:g} L.",
            numbers=numbers,
            assumptions=assumptions,
            sources=cite("HEURISTIC"),
        )
    return WaterAdvice(
        model="indoor",
        action="skip",
        next_check=due.isoformat(),
        reason=f"Watered {(today - last_watered).days} days ago; next check on {due.isoformat()}.",
        numbers=numbers,
        assumptions=assumptions,
        sources=cite("HEURISTIC"),
    )
