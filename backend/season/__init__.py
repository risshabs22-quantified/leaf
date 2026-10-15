"""Leaf Season Plan: a garden's next twelve months, week by week, decided by rules first.

Ultra writes the plan one month at a time (each month its own schema-checked call). Code then
checks every item against the rules below, and Super verifies what is left against the same
rules and the cited facts before anything is shown. Super can only remove items, never add.
A replan rewrites only the weeks a change touches and goes through the same checks.

Code rules, applied to every item after every model step:
- Shape: the item names one of the garden's plants (or the whole garden), a known kind and a
  week of the month being written; anything else is dropped. Sources are only the numbered
  ones Leaf supplied; an item with none says it is general practice.
- Frost dates: an outdoor sowing or transplant before the earliest date for the plant's frost
  class is removed. CSU Extension (csu_planting): hardy "as early as 2-4 weeks before the date
  of the average last spring frost" (Leaf uses 4), semi-hardy "0-2 weeks before" (2), tender
  "around the date", very tender "typically 2+ weeks after" (2). A tender or very tender one
  after the average first autumn frost is removed too. A plant with no known class is
  treated as tender. Between the average date and the 1-in-10 late date, a caution is added.
- Temperature limits: an outdoor sowing or transplant in a month whose normal daytime high is
  below the class's minimum (CSU: hardy and semi-hardy 40 F, tender 55 F, very tender 60 F)
  is removed. A verified minimum temperature in the plant's profile, above the month's
  normal night low, adds a caution. Sowing outdoors adds the crop's minimum soil temperature.
- Organic and pet choices: products are found and judged by backend/actives.py, as in every
  diagnosis care plan; an item that breaks a choice is removed with the reason shown.
- Toxicity: in a garden with pets or children, pruning and harvest items for a plant whose
  profile says it is toxic carry a precaution with the source.

An item that names a planting with words ("plant out", "set out") but another kind is
treated as a planting for these rules, so a relabelled step cannot slip past them.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from backend.actives import OIL_HEAT_LIMIT_C, PET_HIGH, find_actives, garden_prompt
from backend.climate.climatology import c_to_f
from backend.climate.crops import FROST_CLASS, Crop, find_crop
from backend.climate.risk import FROST_POSSIBLE_C, HEAT_C
from backend.climate.sources import SOURCES
from backend.data.repo import Garden, Plant
from backend.prompts import sanitize

PLAN_WEEKS = 52
KINDS = (
    "sow_indoors",
    "sow",
    "transplant",
    "feed",
    "prune",
    "protect",
    "harvest",
    "pest_check",
    "other",
)
STARTS = ("sow", "transplant")
PLANTING_KINDS = ("sow_indoors", "sow", "transplant")  # these come only from the outline
ROUTINE_PER_MONTH = {"pest_check": 1, "feed": 2, "prune": 2}  # per plant: no weekly repeats
MAX_OUTLINE_PER_PLANT = 6
MAX_ITEMS_PER_MONTH = 30
MAX_ITEMS_PER_WEEK = 10
ACTION_CHARS, WHY_CHARS = 160, 400
FACT_FIELDS = (
    "frost_tolerance",
    "temp_min_c",
    "temp_max_c",
    "feeding",
    "timing",
    "hardiness_zones",
    "light",
    "soil",
    "pests",
    "diseases",
)
FACTS_PER_FIELD = 2
INDOOR_SOWING_WEEKS = 12  # a month this close before the last frost may need indoor sowing
DORMANT_TMAX_C = 5.0  # Leaf heuristic: a month this cold outdoors needs no model call

CLASS_ORDER = ("hardy", "half_hardy", "tender", "very_tender")  # least to most tender
CLASS_LABEL = {
    "hardy": "hardy",
    "half_hardy": "semi-hardy",
    "tender": "tender",
    "very_tender": "very tender",
}
# Days from the average last spring frost to the earliest outdoor sowing or transplant, and
# the lowest normal daytime high (C) for it, per class (CSU Extension, csu_planting).
EARLIEST_DAYS = {"hardy": -28, "half_hardy": -14, "tender": 0, "very_tender": 14}
MIN_DAY_HIGH_C = {
    "hardy": round((40 - 32) * 5 / 9, 1),
    "half_hardy": round((40 - 32) * 5 / 9, 1),
    "tender": round((55 - 32) * 5 / 9, 1),
    "very_tender": round((60 - 32) * 5 / 9, 1),
}
_PLANTING_WORDS = re.compile(
    r"\b(plant(ing)?\s+(it\s+|them\s+)?(out|outside|outdoors)|set\s+(it\s+|them\s+)?out|"
    r"transplant\w*|direct[- ]sow\w*|"
    r"sow\w*\s+(\w+\s+){0,3}(outside|outdoors|in the (bed|ground|garden)))\b",
    re.IGNORECASE,
)


def source(key: str) -> dict[str, str]:
    return {"title": SOURCES[key]["title"], "url": SOURCES[key]["url"]}


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def fmt_day(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def fmt_c(c: float) -> str:
    return f"{c:g} °C ({round(c_to_f(c))} °F)"


# ---------- inputs ----------


@dataclass
class PlanPlant:
    ref: str  # "p1": the id the models see
    plant: Plant
    name: str
    crop: Crop | None
    frost_class: str
    frost_source: dict[str, str]  # empty when assumed
    frost_n: int = 0  # the source's number in prompts (0 when assumed)
    temp_min_c: float | None = None
    temp_min_source: dict[str, str] = field(default_factory=dict)
    toxic: str = "unknown"  # the worse of pets and children: toxic, mildly_toxic, non_toxic
    toxic_source: dict[str, str] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)  # {field, value, quote, n}

    @property
    def outdoor(self) -> bool:
        return self.plant.setting == "outdoor"

    @property
    def assumed(self) -> bool:
        return not self.frost_source


@dataclass
class PlanInputs:
    garden: Garden
    southern: bool
    frost: dict[str, Any]  # climatology's frost block
    monthly: list[dict[str, Any]]  # 12 monthly normals
    zone: dict[str, Any]
    plants: dict[str, PlanPlant]
    sources: list[dict[str, str]]  # numbered from 1 in prompts

    def by_id(self, plant_id: str | None) -> PlanPlant | None:
        """The plant a stored item belongs to (items keep plant_id; refs are per call)."""
        return next((p for p in self.plants.values() if p.plant.id == plant_id), None)

    def ref_of(self, item: dict[str, Any]) -> str:
        pp = self.by_id(item.get("plant_id"))
        return pp.ref if pp else "garden"

    def normal(self, month: int) -> dict[str, Any]:
        return next((m for m in self.monthly if m.get("month") == month), {})

    def last_frost(self, day: date, key: str = "risk_50") -> date | None:
        return frost_day((self.frost.get("last_spring_frost") or {}).get(key), day, self.southern)

    def first_frost(self, day: date, key: str = "risk_50") -> date | None:
        return frost_day((self.frost.get("first_autumn_frost") or {}).get(key), day, self.southern)

    def earliest(self, pp: PlanPlant, day: date) -> date | None:
        last = self.last_frost(day)
        return last + timedelta(days=EARLIEST_DAYS[pp.frost_class]) if last else None


def frost_day(mmdd: str | None, day: date, southern: bool) -> date | None:
    """A climatology month-day ("05-01") in the frost year that contains `day`: the calendar
    year in the north, July to June in the south (so a southern winter is not split)."""
    if not mmdd:
        return None
    m, d = int(mmdd[:2]), int(mmdd[3:5])
    if southern:
        start = day.year if day.month >= 7 else day.year - 1
        year = start if m >= 7 else start + 1
    else:
        year = day.year
    try:
        return date(year, m, d)
    except ValueError:  # Feb 29 in a common year
        return date(year, m, 28)


class _Numbered:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, src: dict[str, Any]) -> int:
        entry = {"title": str(src.get("title", ""))[:200], "url": str(src.get("url", ""))[:500]}
        if not entry["title"]:
            return 0
        if entry not in self.items:
            self.items.append(entry)
        return self.items.index(entry) + 1


def _worse(a: str, b: str) -> str:
    return max(a, b, key=CLASS_ORDER.index)


def build_inputs(garden: Garden, states: list[Any], climatology: dict[str, Any]) -> PlanInputs:
    """Everything the writer, the verifier and the code rules share, from the garden's plants
    (care.PlantState, with their species profiles) and its cell's climatology summary."""
    nums = _Numbered()
    nums.add(source("ncei_freeze"))
    nums.add(source("csu_planting"))
    plants: dict[str, PlanPlant] = {}
    for i, st in enumerate(states, 1):
        plant: Plant = st.plant
        crop = st.crop or find_crop(plant.species, plant.common_name)
        profile = st.profile or {}
        facts = profile.get("facts", {})
        cls, cls_src = "", {}
        if crop and crop.key in FROST_CLASS:
            cls, cls_src = FROST_CLASS[crop.key], source("csu_planting")
        for f in facts.get("frost_tolerance", []):
            src = f.get("source") or {}
            if not (src.get("url") or src.get("title")):
                continue  # an unsourced fact never changes a rule
            if f.get("value") in CLASS_ORDER and (not cls or _worse(f["value"], cls) != cls):
                cls, cls_src = f["value"], src
        if not cls:
            cls = "tender"  # no source: the cautious answer
        mins = [
            (float(f["value"]), f["source"])
            for f in facts.get("temp_min_c", [])
            if isinstance(f.get("value"), (int, float))
            and (f.get("source") or {}).get("url")  # an unsourced fact adds no caution
        ]
        tmin, tmin_src = max(mins, key=lambda x: x[0]) if mins else (None, {})
        tox = profile.get("toxicity") or {}
        rank = {"toxic": 2, "mildly_toxic": 1, "non_toxic": 0}
        worst = max(
            (tox.get("toxic_to_pets", "unknown"), tox.get("toxic_to_children", "unknown")),
            key=lambda v: rank.get(v, -1),
        )
        tox_src = next(
            (
                f.get("source") or {}
                for fld in ("toxic_to_pets", "toxic_to_children")
                for f in facts.get(fld, [])
                if f.get("value") == worst
            ),
            {},
        )
        pp = PlanPlant(
            ref=f"p{i}",
            plant=plant,
            name=st.name,
            crop=crop,
            frost_class=cls,
            frost_source=cls_src,
            frost_n=nums.add(cls_src) if cls_src else 0,
            temp_min_c=tmin,
            temp_min_source=tmin_src,
            toxic=worst if worst in rank else "unknown",
            toxic_source=tox_src,
        )
        for fld in FACT_FIELDS:
            for f in facts.get(fld, [])[:FACTS_PER_FIELD]:
                n = nums.add(f.get("source") or {})
                pp.facts.append(
                    {
                        "field": fld,
                        "value": sanitize(f.get("value"), 200),
                        "quote": sanitize(f.get("quote"), 240),
                        "n": n,
                    }
                )
        if crop:
            for key in crop.sources:
                if "germination" in key:
                    nums.add(source(key))
        plants[pp.ref] = pp
    return PlanInputs(
        garden=garden,
        southern=climatology.get("hemisphere") == "south",
        frost=climatology.get("frost") or {},
        monthly=climatology.get("monthly") or [],
        zone=climatology.get("hardiness_zone") or {},
        plants=plants,
        sources=nums.items,
    )


# ---------- the season's months ----------


@dataclass
class Month:
    year: int
    month: int
    weeks: list[str]  # Mondays (YYYY-MM-DD) that fall in this month
    active: bool = True

    @property
    def label(self) -> str:
        return date(self.year, self.month, 1).strftime("%B %Y")


def season_months(inputs: PlanInputs, today: date) -> list[Month]:
    """The next 52 weeks from this week's Monday, grouped by the month each Monday falls in.
    A month needs a model call only if something can happen in it: a normal daytime high of
    5 C or more, an indoor plant, or the 12 weeks before the last spring frost (indoor sowing)."""
    start = monday(today)
    months: dict[tuple[int, int], Month] = {}
    for i in range(PLAN_WEEKS):
        w = start + timedelta(weeks=i)
        months.setdefault((w.year, w.month), Month(w.year, w.month, [])).weeks.append(w.isoformat())
    indoor = any(not p.outdoor for p in inputs.plants.values())
    for m in months.values():
        first = date(m.year, m.month, 1)
        last_day = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        tmax = inputs.normal(m.month).get("tmax_c")
        frost = inputs.last_frost(first)
        sowing = bool(
            frost and frost - timedelta(weeks=INDOOR_SOWING_WEEKS) <= last_day and first <= frost
        )
        m.active = indoor or sowing or tmax is None or tmax >= DORMANT_TMAX_C
    return list(months.values())


# ---------- prompts' shared blocks (all written in code) ----------


def rules_text(inputs: PlanInputs, today: date) -> str:
    lines = []
    last = inputs.last_frost(today)  # only its month and day are shown
    fr = inputs.frost
    if fr.get("enough_data") and last:
        late = inputs.last_frost(last, "risk_10")
        first = inputs.first_frost(last)
        lines.append(
            f"Average last spring frost: {fmt_day(last)}"
            + (f" (1 year in 10 it is after {fmt_day(late)})" if late else "")
            + (f". Average first autumn frost: {fmt_day(first)}" if first else "")
            + ". Source [1]."
        )
    elif fr.get("enough_data"):
        lines.append("This place has no frost in most years. Source [1].")
    else:
        lines.append("Frost dates are unknown here; plant tender plants out only in warm weather.")
    if inputs.zone.get("zone"):
        lines.append(
            f"USDA hardiness zone {inputs.zone['zone']} (estimated from 30 years of data)."
        )
    for pp in inputs.plants.values():
        where = "outdoors" if pp.outdoor else "indoors"
        text = f"{pp.ref} {pp.name} ({where}): {CLASS_LABEL[pp.frost_class]}"
        text += " (no source; treated as tender)" if pp.assumed else f" (source [{pp.frost_n}])"
        if pp.outdoor and last:
            earliest = last + timedelta(days=EARLIEST_DAYS[pp.frost_class])
            text += (
                f"; never sow or plant out before {fmt_day(earliest)} (hardening off for a few"
                " hours a day in the weeks before is fine), nor when the month's normal daytime"
                f" high is below {fmt_c(MIN_DAY_HIGH_C[pp.frost_class])}"
            )
            if pp.frost_class in ("tender", "very_tender") and inputs.first_frost(last):
                text += f", nor after {fmt_day(inputs.first_frost(last))}"
        if pp.temp_min_c is not None:
            text += f"; minimum temperature {fmt_c(pp.temp_min_c)}"
        lines.append(text + ".")
    prefs = inputs.garden.prefs
    lines.append(f"Gardener's choices: {garden_prompt(prefs)}.")
    if prefs.pets:
        lines.append(
            "Pets or children use this garden: for a toxic plant, pruning and harvest need gloves"
            " and the clippings binned."
        )
    return "\n".join(lines)


def plants_block(inputs: PlanInputs) -> list[dict[str, Any]]:
    return [
        {
            "id": pp.ref,
            "name": sanitize(pp.name, 80),
            "setting": pp.plant.setting,
            "stage": sanitize(pp.plant.stage, 40),
            "planted_on": pp.plant.planted_on.isoformat() if pp.plant.planted_on else "",
            "crop": pp.crop.key if pp.crop else "",
        }
        for pp in inputs.plants.values()
    ]


def facts_block(inputs: PlanInputs) -> list[dict[str, Any]]:
    return [{"plant": pp.ref, **f} for pp in inputs.plants.values() for f in pp.facts]


def sources_block(inputs: PlanInputs) -> list[dict[str, Any]]:
    return [{"n": i, "title": s["title"]} for i, s in enumerate(inputs.sources, 1)]


def month_block(inputs: PlanInputs, m: Month) -> dict[str, Any]:
    n = inputs.normal(m.month)
    return {
        "month": m.label,
        "weeks": m.weeks,
        "normals_1991_2020": {
            k: n.get(k) for k in ("tmax_c", "tmin_c", "rain_mm", "frost_days") if k in n
        },
    }


# ---------- the code rules ----------


def _clean(text: Any, limit: int) -> str:
    return sanitize(" ".join(str(text or "").split()), limit)


def item_id(week: str, plant_id: str, kind: str, action: str) -> str:
    raw = f"{week}|{plant_id}|{kind}|{action.lower()}".encode()
    return hashlib.sha256(raw).hexdigest()[:12]


_HARDEN = re.compile(r"\bharden", re.IGNORECASE)


def is_planting(item: dict[str, Any]) -> bool:
    """A sowing or planting out, by kind or by wording (hardening off is not one)."""
    if item["kind"] in STARTS:
        return True
    if item["kind"] == "sow_indoors" or _HARDEN.search(item["action"]):
        return False
    return bool(_PLANTING_WORDS.search(item["action"]))


def _snap_week(raw: Any, allowed: set[str]) -> str:
    try:
        w = monday(date.fromisoformat(str(raw)[:10])).isoformat()
    except ValueError:
        return ""
    return w if w in allowed else ""


def check_items(
    raw: list[Any], inputs: PlanInputs, weeks: list[str], by: str, limit: bool = True
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Shape, frost, temperature, product and toxicity rules for model-written items (each a
    SeasonItemOut or a dict of its fields). Returns (kept, removed); kept items are complete,
    with their sources resolved and every caution attached."""
    allowed = set(weeks)
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    per_week: dict[str, int] = {}
    routine: dict[tuple[str, str], int] = {}
    prefs = inputs.garden.prefs
    for r in raw:
        r = r if isinstance(r, dict) else r.model_dump()
        week = _snap_week(r.get("week"), allowed)
        ref = str(r.get("plant") or "").strip()
        pp = inputs.plants.get(ref)
        action = _clean(r.get("action"), ACTION_CHARS)
        if not week or not action or (pp is None and ref.lower() not in ("garden", "")):
            continue  # not a week of this month, empty, or a plant not in the garden
        kind = str(r.get("kind") or "").strip().lower()
        item: dict[str, Any] = {
            "week": week,
            "ref": pp.ref if pp else "garden",
            "plant_id": pp.plant.id if pp else None,
            "plant": pp.name if pp else "Whole garden",
            "kind": kind if kind in KINDS else "other",
            "action": action,
            "why": _clean(r.get("why"), WHY_CHARS),
            "sources": [],
            "cautions": [],
            "by": by,
        }
        ids = r.get("source_ids") or []
        for n in ids if isinstance(ids, list) else []:
            if isinstance(n, int) and 1 <= n <= len(inputs.sources):
                if inputs.sources[n - 1] not in item["sources"]:
                    item["sources"].append(inputs.sources[n - 1])
        item["id"] = item_id(week, item["plant_id"] or "garden", item["kind"], action)
        reason = _rules(item, pp, inputs, date.fromisoformat(week))
        if reason is None:
            reason = _products(item, inputs, date.fromisoformat(week))
        if reason is not None:
            removed.append(
                {k: item[k] for k in ("week", "plant", "action")} | {"reason": reason, "by": "code"}
            )
            continue
        if pp and prefs.pets and pp.toxic in ("toxic", "mildly_toxic"):
            if item["kind"] in ("prune", "harvest"):
                item["cautions"].append(
                    {
                        "text": f"{pp.name} is {pp.toxic.replace('_', ' ')} to pets or children:"
                        " wear gloves, keep them away while you work and bin the clippings.",
                        "source": pp.toxic_source,
                    }
                )
        if limit and (
            per_week.get(week, 0) >= MAX_ITEMS_PER_WEEK or len(kept) >= MAX_ITEMS_PER_MONTH
        ):
            continue
        if any(k["id"] == item["id"] for k in kept):
            continue
        cap = ROUTINE_PER_MONTH.get(item["kind"]) if limit else None
        slot = (item["ref"], item["kind"])
        if cap is not None and routine.get(slot, 0) >= cap:
            continue  # the same routine job for the same plant: not every week
        routine[slot] = routine.get(slot, 0) + 1
        per_week[week] = per_week.get(week, 0) + 1
        kept.append(item)
    return kept, removed


def _rules(
    item: dict[str, Any], pp: PlanPlant | None, inputs: PlanInputs, week: date
) -> str | None:
    """Frost and temperature rules for an outdoor sowing or transplant. Returns why it must
    go, or None (adding cautions to the item)."""
    if pp is None or not pp.outdoor or not is_planting(item):
        return None
    cls = CLASS_LABEL[pp.frost_class]
    why_cls = " (Leaf has no source on its frost tolerance, so treats it as tender)" * pp.assumed
    earliest = inputs.earliest(pp, week)
    if earliest and week < earliest:  # the week must start on or after the earliest date
        return (
            f"Too early: {pp.name} is {cls}{why_cls}; it goes outdoors from {fmt_day(earliest)}"
            " here (CSU Extension, from the average last spring frost)."
        )
    first = inputs.first_frost(week)
    if pp.frost_class in ("tender", "very_tender") and first and week >= first:
        return (
            f"Too late: {pp.name} is {cls}{why_cls} and the average first autumn frost here is"
            f" {fmt_day(first)}."
        )
    tmax = inputs.normal(week.month).get("tmax_c")
    need = MIN_DAY_HIGH_C[pp.frost_class]
    if tmax is not None and tmax < need:
        return (
            f"Too cold: daytime highs here average {fmt_c(tmax)} in {week.strftime('%B')};"
            f" {cls} plants need {fmt_c(need)} or more (CSU Extension)."
        )
    late = inputs.last_frost(week, "risk_10")
    if pp.frost_class != "hardy" and late and week <= late:
        item["cautions"].append(
            {
                "text": f"In 1 year in 10 a frost comes after {fmt_day(late)} here: keep a cover"
                " ready.",
                "source": source("ncei_freeze"),
            }
        )
    tmin = inputs.normal(week.month).get("tmin_c")
    if pp.temp_min_c is not None and tmin is not None and tmin < pp.temp_min_c:
        item["cautions"].append(
            {
                "text": f"Nights here average {fmt_c(tmin)} in {week.strftime('%B')}, below"
                f" {pp.name}'s minimum of {fmt_c(pp.temp_min_c)}: wait for a warm spell or"
                " protect it at night.",
                "source": pp.temp_min_source,
            }
        )
    crop = pp.crop
    if item["kind"] == "sow" and crop and crop.germ_min_c is not None:
        germ = next((k for k in crop.sources if "germination" in k), "")
        item["cautions"].append(
            {
                "text": f"Sow once the soil is at least {fmt_c(crop.germ_min_c)} in the morning,"
                " 10 cm deep.",
                "source": source(germ) if germ else {},
            }
        )
    return None


def _products(item: dict[str, Any], inputs: PlanInputs, week: date) -> str | None:
    """The organic and pet choices for any product the item names (backend/actives.py)."""
    found = find_actives(f"{item['action']} {item['why']}")
    if not found:
        return None
    prefs = inputs.garden.prefs
    bad = [a.name for a in found if not a.organic]
    if prefs.organic and bad:
        return f"Uses {', '.join(bad)}, which is not allowed in organic growing."
    risky = [a.name for a in found if a.pet == PET_HIGH]
    if prefs.pets and risky:
        return f"{', '.join(risky)} is a serious risk to pets."
    for a in found:
        if a.pet_note and (prefs.pets or a.pet == PET_HIGH):
            item["cautions"].append({"text": a.pet_note, "source": {}})
    if any(a.bees for a in found):
        item["cautions"].append(
            {
                "text": "Harms bees: spray in the evening when bees are not foraging, never on"
                " open flowers.",
                "source": {},
            }
        )
    tmax = inputs.normal(week.month).get("tmax_c")
    if any(a.oil or a.sulfur for a in found) and tmax is not None and tmax >= OIL_HEAT_LIMIT_C:
        item["cautions"].append(
            {
                "text": f"Oils and sulfur can burn leaves above {fmt_c(OIL_HEAT_LIMIT_C)}; highs"
                f" here average {fmt_c(tmax)} in {week.strftime('%B')}. Spray on a cool morning.",
                "source": {},
            }
        )
    item["cautions"].append({"text": "Follow the product label: it is the law.", "source": {}})
    return None


# ---------- the outline: each plant's sowing and planting-out weeks ----------


def _snap_outline(raw: list[Any], inputs: PlanInputs, weeks: list[str]) -> list[dict[str, Any]]:
    """A planting dated in the week that holds a plant's earliest outdoor date (weeks start
    on Monday, the date falls mid-week) moves to the first week that starts on or after it,
    so the plan keeps the planting instead of losing it. Nothing earlier is moved."""
    out = []
    for r in raw:
        r = dict(r if isinstance(r, dict) else r.model_dump())
        pp = inputs.plants.get(str(r.get("plant") or "").strip())
        try:
            week = monday(date.fromisoformat(str(r.get("week"))[:10]))
        except ValueError:
            out.append(r)
            continue
        kind = str(r.get("kind") or "").strip().lower()
        earliest = inputs.earliest(pp, week) if pp and pp.outdoor and kind in STARTS else None
        if earliest and week < earliest <= week + timedelta(days=6):
            later = (week + timedelta(weeks=1)).isoformat()
            if later in weeks:
                r["week"] = later
        out.append(r)
    return out


def outline_items(
    raw: list[Any], inputs: PlanInputs, weeks: list[str], by: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The outline's milestones, through the same code rules; only plantings are kept, at
    most a few per plant. Every month of the plan takes its plantings from here, so the
    months (written separately) cannot contradict each other."""
    kept, removed = check_items(_snap_outline(raw, inputs, weeks), inputs, weeks, by, limit=False)
    per_plant: dict[str, int] = {}
    out = []
    for it in kept:
        if it["kind"] not in PLANTING_KINDS:
            continue
        n = per_plant.get(it["ref"], 0)
        if n >= MAX_OUTLINE_PER_PLANT:
            continue
        per_plant[it["ref"]] = n + 1
        out.append(it)
    return out, removed


def outline_block(outline: list[dict[str, Any]], inputs: PlanInputs) -> list[dict[str, Any]]:
    return [
        {"week": it["week"], "plant": inputs.ref_of(it), "kind": it["kind"], "action": it["action"]}
        for it in outline
        if inputs.by_id(it.get("plant_id")) is not None
    ]


def merge_month(
    written: list[dict[str, Any]], outline: list[dict[str, Any]], weeks: list[str]
) -> list[dict[str, Any]]:
    """A month's items: the writer's, minus its sowings and plantings (those come only from
    the outline), plus the outline's in this month's weeks. Aftercare that mentions planting
    ("harden off before planting out") stays; it still went through the frost rules."""
    allowed = set(weeks)
    own = [it for it in written if it["kind"] not in PLANTING_KINDS]
    return [it for it in outline if it["week"] in allowed] + own


# ---------- verification ----------


def verify_items_block(items: list[dict[str, Any]], inputs: PlanInputs) -> list[dict[str, Any]]:
    return [
        {
            "id": it["id"],
            "week": it["week"],
            "plant": inputs.ref_of(it),
            "kind": it["kind"],
            "action": it["action"],
            "why": it["why"],
            "cites": [s["title"][:80] for s in it["sources"]],
        }
        for it in items
    ]


def apply_issues(
    items: list[dict[str, Any]], issues: list[Any], by: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Remove the items Super flagged (it can only remove). Unknown ids are ignored."""
    flagged = {}
    for i in issues:
        i = i if isinstance(i, dict) else i.model_dump()
        flagged.setdefault(str(i.get("id", "")), _clean(i.get("problem"), 240))
    kept = [it for it in items if it["id"] not in flagged]
    removed = [
        {k: it[k] for k in ("week", "plant", "action")}
        | {"reason": flagged[it["id"]] or "Flagged by the verifier.", "by": by}
        for it in items
        if it["id"] in flagged
    ]
    return kept, removed


# ---------- replanning ----------


def diff_weeks(
    old: dict[str, list[dict[str, Any]]], new: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """What a replan changed, week by week, computed in code (not taken from the model)."""

    def keyset(items: list[dict[str, Any]]) -> dict[tuple[str, str, str], str]:
        return {
            (
                it.get("plant_id") or "",
                it["kind"],
                it["action"].lower(),
            ): f"{it['action']} ({it['plant']})"
            for it in items
        }

    out = []
    for week in sorted(set(old) | set(new)):
        a, b = keyset(old.get(week, [])), keyset(new.get(week, []))
        added = [b[k] for k in b if k not in a]
        dropped = [a[k] for k in a if k not in b]
        if added or dropped:
            out.append({"week": week, "added": added, "removed": dropped})
    return out


def weather_triggers(
    inputs: PlanInputs,
    weeks: dict[str, list[dict[str, Any]]],
    days: list[dict[str, Any]],
    today: date,
) -> list[dict[str, Any]]:
    """A forecast frost or heat wave in a week whose plan sows or plants out: the weeks to
    replan (that week and the next), decided in code. Frost: a low of 2.2 C (36 F) or less
    for plants that are not hardy; heat: a high of 32.2 C (90 F) or more for any planting."""
    out = []
    checks = (
        ("frost", "temperature_2m_min", lambda v: v <= FROST_POSSIBLE_C, "low", "nws_frost"),
        ("heat", "temperature_2m_max", lambda v: v >= HEAT_C, "high", "rutgers_heat"),
    )
    for kind, col, risky, word, src_key in checks:
        hits = [
            d
            for d in days
            if d.get(col) is not None and d["date"] >= today.isoformat() and risky(d[col])
        ]
        if not hits:
            continue
        affected: set[str] = set()
        for d in hits:
            wk = monday(date.fromisoformat(d["date"])).isoformat()
            for it in weeks.get(wk, []):
                pp = inputs.by_id(it.get("plant_id"))
                if pp and pp.outdoor and is_planting(it):
                    if kind == "heat" or pp.frost_class != "hardy":
                        affected.add(wk)
        if not affected:
            continue
        worst = (
            min(hits, key=lambda d: d[col]) if kind == "frost" else max(hits, key=lambda d: d[col])
        )
        nxt = {(date.fromisoformat(w) + timedelta(weeks=1)).isoformat() for w in affected}
        first_week = min(affected)
        on = date.fromisoformat(worst["date"])
        out.append(
            {
                "kind": kind,
                # Keyed by the planting week it threatens, so one event replans once.
                "key": f"{kind}:{first_week}",
                "weeks": sorted((affected | nxt) & set(weeks)),
                "text": f"Forecast: a {word} of {fmt_c(round(worst[col], 1))} on"
                f" {on.strftime('%a')} {fmt_day(on)}, in a week with planting planned.",
                "source": source(src_key),
            }
        )
    return out


# ---------- calendar ----------


def calendar_events(weeks: list[Any], garden_name: str) -> list[dict[str, Any]]:
    """One all-day event per item on the Monday of its week."""
    out = []
    for w in weeks:
        for it in w.items:
            details = [it.get("why", "")]
            details += [c["text"] for c in it.get("cautions", [])]
            details += [f"Source: {s['title']} {s['url']}".strip() for s in it.get("sources", [])]
            if not it.get("sources"):
                details.append("General practice; no source cited.")
            out.append(
                {
                    "uid": f"{it['id']}-{w.week}",
                    "day": date.fromisoformat(w.week),
                    "title": f"{it['action']} ({it['plant']})"[:120],
                    "details": "\n".join(d for d in details if d)[:1500],
                }
            )
    return out
