"""Running the care planner against the platform: gather a garden's inputs (plants, their
species profiles, waterings, forecast, alerts, degree days), plan, store the tasks
(idempotent), and word the notifications.

Every model step here is optional: when a model fails or the budget is in brownout, a
template says the same thing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.care import Plan, PlantState, crop_for, plan_day
from backend.care.pests import PESTS
from backend.care.sensor import FRESH_S
from backend.climate import Climate
from backend.climate.crops import CROPS, GENERAL_GDD_BASE_C
from backend.climate.nws import Alert, AlertsUnavailable
from backend.climate.openmeteo import WeatherUnavailable
from backend.data.repo import Garden, Repo, now_s
from backend.knowledge import species_key
from backend.knowledge.profile import profile_view
from backend.models import ModelError, complete_json
from backend.prompts import CARE_WORDING_SYSTEM, JOB_TRIAGE_SYSTEM, sanitize
from backend.schemas import AlertTriage, CareWording, ModelCall

log = logging.getLogger("leaf.care")

WATER_LOOKBACK_S = 10 * 86400
INDOOR_LOOKBACK_S = 120 * 86400
URGENCY_ORDER = {"urgent": 0, "soon": 1, "normal": 2}


@dataclass
class CareContext:
    repo: Repo
    climate: Climate
    leaf: Any  # backend.pipeline.Leaf: llm and settings for wording


def local_today(garden: Garden, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(garden.timezone)).date()


async def plant_states(ctx: CareContext, garden: Garden) -> list[PlantState]:
    tz = ZoneInfo(garden.timezone)
    out = []
    readings = await ctx.repo.latest_readings(garden.id, now_s() - FRESH_S)
    for plant in await ctx.repo.list_plants(garden.id):
        key = (
            species_key(plant.species, binomial=True)
            if plant.species
            else species_key(plant.common_name)
        )
        row = await ctx.repo.get_species(key) if key else None
        profile = profile_view(row) if row and row["status"] == "ready" else None
        if plant.setting == "indoor":
            events = await ctx.repo.plant_events(
                garden.id, plant.id, ["watered", "water_feedback"], now_s() - INDOOR_LOOKBACK_S
            )
            watered = [e for e in events if e.kind == "watered"]
            out.append(
                PlantState(
                    plant=plant,
                    crop=crop_for(plant),
                    profile=profile,
                    last_watered=datetime.fromtimestamp(watered[0].created_at, tz).date()
                    if watered
                    else None,
                    feedback=[
                        str(e.payload.get("kind")) for e in events if e.kind == "water_feedback"
                    ],
                    reading=readings.get(plant.id),
                )
            )
        else:
            events = await ctx.repo.plant_events(
                garden.id, plant.id, ["watered"], now_s() - WATER_LOOKBACK_S
            )
            out.append(
                PlantState(
                    plant=plant,
                    crop=crop_for(plant),
                    profile=profile,
                    waterings=[
                        {
                            "date": datetime.fromtimestamp(e.created_at, tz).date().isoformat(),
                            **{
                                k: v for k, v in e.payload.items() if k in ("amount_l", "amount_mm")
                            },
                        }
                        for e in events
                    ],
                    reading=readings.get(plant.id),
                )
            )
    return out


async def plan_garden(
    ctx: CareContext, garden: Garden, now: datetime | None = None, trace_id: str = ""
) -> Plan:
    """Today's plan for a garden; new tasks are stored, existing ones (and their done or
    skipped status) are kept. Safe to call as often as needed."""
    now = now or datetime.now(UTC)
    today = local_today(garden, now)
    states = await plant_states(ctx, garden)
    fc = alerts = season = None
    if garden.cell:
        try:
            fc = await ctx.climate.forecast(garden.cell)
        except WeatherUnavailable as exc:
            log.warning("care: no forecast for %s: %s", garden.cell, exc)
        try:
            alerts = await ctx.climate.alerts(garden.cell)
        except AlertsUnavailable as exc:
            log.warning("care: no NWS alerts for %s: %s", garden.cell, exc)
        crops = {s.crop.key for s in states if s.crop and s.plant.setting == "outdoor"}
        bases = {p.base_c for p in PESTS if crops & set(p.host_crops)}
        if bases:
            try:
                g = await ctx.climate.season_gdd(garden.cell, sorted(bases | {GENERAL_GDD_BASE_C}))
                season = g["by_base_c"]
            except WeatherUnavailable as exc:
                log.warning("care: no degree days for %s: %s", garden.cell, exc)
    plan = plan_day(garden, states, fc, alerts, season, today, now, trace_id)
    if plan.tasks:
        await ctx.repo.add_tasks(plan.tasks)
    return plan


def ordered(tasks: list[Any]) -> list[Any]:
    return sorted(tasks, key=lambda t: (URGENCY_ORDER.get(t.urgency, 3), t.due_at or 0))


def template_message(garden: Garden, tasks: list[Any]) -> tuple[str, str]:
    open_tasks = [t for t in ordered(tasks) if t.status == "open"]
    if not open_tasks:
        return (
            f"{garden.name or 'Your garden'}: nothing to do today",
            "Leaf found no tasks for today.",
        )
    first = open_tasks[0]
    title = (
        f"Urgent: {first.title}"
        if first.urgency == "urgent"
        else f"{len(open_tasks)} garden task{'s' if len(open_tasks) > 1 else ''} today"
    )
    body = "; ".join(t.title for t in open_tasks[:3])
    return title[:60], body[:160]


async def morning_message(
    ctx: CareContext, garden: Garden, tasks: list[Any], trace: list[ModelCall]
) -> tuple[str, str]:
    """Lightning words the summary from the tasks; a template when it cannot."""
    title, body = template_message(garden, tasks)
    open_tasks = [t for t in ordered(tasks) if t.status == "open"]
    if not open_tasks:
        return title, body
    items = [{"title": t.title, "urgency": t.urgency} for t in open_tasks[:8]]
    try:
        out = await complete_json(
            ctx.leaf.llm,
            step="care_wording",
            models=ctx.leaf.settings.models("care_wording"),
            messages=[
                {"role": "system", "content": CARE_WORDING_SYSTEM},
                {"role": "user", "content": f"<tasks>{sanitize(json.dumps(items))}</tasks>"},
            ],
            schema=CareWording,
            trace=trace,
            thinking=False,
            max_tokens=ctx.leaf.settings.budget("care_wording"),
            temperature=0.5,
        )
    except ModelError:  # includes SpendCapReached (brownout): the template stands
        return title, body
    plants = await ctx.repo.list_plants(garden.id)
    names = [n for p in plants for n in (p.nickname, p.common_name, p.species)]
    if out.title.strip() and out.body.strip() and adds_nothing(out, open_tasks, names):
        return out.title.strip()[:60], out.body.strip()[:160]
    return title, body


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def adds_nothing(out: CareWording, tasks: list[Any], plant_names: list[str] = ()) -> bool:
    """Code check on the model's wording: every number and every plant it names (the
    garden's own plants and every crop Leaf knows) must come from the tasks themselves."""
    source = " ".join(f"{t.title} {t.reason}" for t in tasks).lower()
    text = f"{out.title} {out.body}".lower()
    if any(n not in source for n in _NUMBER.findall(text)):
        return False
    names = [n.lower() for n in plant_names if n] + [a for c in CROPS.values() for a in c.names]
    for alias in names:
        if re.search(rf"\b{re.escape(alias)}s?\b", text) and not re.search(
            rf"\b{re.escape(alias)}", source
        ):
            return False
    return True


async def triage_alert(ctx: CareContext, alert: Alert, trace: list[ModelCall]) -> AlertTriage:
    """Nano 3 (background, speed does not matter) turns an NWS alert into one plain line."""
    fallback = AlertTriage(relevant=True, summary=alert.headline[:140])
    payload = {
        "event": alert.event,
        "headline": alert.headline,
        "onset": alert.onset,
        "ends": alert.ends,
        "instruction": alert.instruction[:600],
    }
    try:
        out = await complete_json(
            ctx.leaf.llm,
            step="job_triage",
            models=ctx.leaf.settings.models("job_triage"),
            messages=[
                {"role": "system", "content": JOB_TRIAGE_SYSTEM},
                {"role": "user", "content": f"<alert>{sanitize(json.dumps(payload))}</alert>"},
            ],
            schema=AlertTriage,
            trace=trace,
            thinking=False,
            max_tokens=ctx.leaf.settings.budget("job_triage"),
            temperature=0.2,
        )
    except ModelError:
        return fallback
    out.summary = sanitize(out.summary.strip(), 140) or fallback.summary
    out.act_by = out.act_by if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", out.act_by or "") else ""
    return out
