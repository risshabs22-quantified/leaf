"""Running the season plan against the platform: write it (Ultra, a month per call), check it
(code rules), verify it (Super), store it week by week, replan the weeks a change touches,
and wire Leaf Doctor in (a diagnosis adds tasks and replans; check-ins close the loop).

Model steps fail safe: a month Ultra cannot write is listed as unwritten, a plan Super cannot
verify is kept hidden ('unverified') and the job retries only the verification, and a replan
that cannot be verified leaves the verified plan as it was.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError

from backend.actives import apply_garden
from backend.care.service import CareContext, local_today, plant_states
from backend.climate.forecast import Forecast
from backend.climate.openmeteo import WeatherUnavailable
from backend.data.repo import EventIn, Garden, Plant, SeasonPlan, TaskIn
from backend.models import ModelError, SpendCapReached, complete_json, gather_limited
from backend.prompts import (
    PLAN_VERIFY_SYSTEM,
    REPLAN_SYSTEM,
    SEASON_OUTLINE_SYSTEM,
    SEASON_PLAN_SYSTEM,
    sanitize,
)
from backend.schemas import CarePlan, ModelCall, PlanVerdict, Replan, SeasonMonth, SeasonOutline
from backend.season import (
    PLAN_WEEKS,
    Month,
    PlanInputs,
    apply_issues,
    build_inputs,
    check_items,
    diff_weeks,
    facts_block,
    merge_month,
    monday,
    month_block,
    outline_block,
    outline_items,
    plants_block,
    rules_text,
    season_months,
    sources_block,
    verify_items_block,
    weather_triggers,
)

log = logging.getLogger("leaf.season")

MONTH_CONCURRENCY = 7  # 13 months at most: two rounds of Ultra calls
VERIFY_CHUNK = 25  # small enough that Super's reasoning leaves room for the answer
VERIFY_CONCURRENCY = 4
FAILED_NOTE = "Leaf could not write the plan just now: the models were unavailable. Try again."
MAX_REPLAN_WEEKS = 4
MAX_REMOVED_KEPT = 100
DOCTOR_TASKS = 5
CHECKIN_AGAIN_DAYS = 7


class PlanUnavailable(Exception):
    """The garden cannot have a plan yet; the message says why, for the gardener."""


class VerifyUnavailable(Exception):
    """Super (and its fallback) could not verify; nothing unverified is shown."""


async def gather_inputs(ctx: CareContext, garden: Garden) -> PlanInputs:
    if not garden.cell:
        raise PlanUnavailable(
            "Add your garden's place on Today first: the plan is built on its frost dates."
        )
    row = await ctx.repo.get_climatology(garden.cell)
    if not row or row.get("status") != "ready" or not row.get("data"):
        raise PlanUnavailable(
            "Leaf is still building 30 years of climate data for your area (it can take up to"
            " a day for a new place). Try again later."
        )
    states = await plant_states(ctx, garden)
    if not states:
        raise PlanUnavailable("Add your plants on Today first: the plan is written for them.")
    return build_inputs(garden, states, row["data"])


def _context(inputs: PlanInputs, today: date) -> str:
    return (
        f"<rules>{sanitize(rules_text(inputs, today))}</rules>\n"
        f"<plants>{sanitize(json.dumps(plants_block(inputs)))}</plants>\n"
        f"<facts>{sanitize(json.dumps(facts_block(inputs)))}</facts>\n"
        f"<sources>{sanitize(json.dumps(sources_block(inputs)))}</sources>"
    )


async def write_outline(
    ctx: CareContext,
    inputs: PlanInputs,
    months: list[Month],
    today: date,
    trace: list[ModelCall],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Ultra fixes every plant's sowing and planting-out weeks for the whole season, in one
    call; the code rules check them. Returns (milestones, removed, model)."""
    weeks = [w for m in months for w in m.weeks]
    season = {
        "weeks": weeks,
        "months": [
            month_block(inputs, m)["normals_1991_2020"] | {"month": m.label} for m in months
        ],
    }
    own: list[ModelCall] = []
    try:
        out = await complete_json(
            ctx.leaf.llm,
            step="season_outline",
            models=ctx.leaf.settings.models("season_outline"),
            messages=[
                {"role": "system", "content": SEASON_OUTLINE_SYSTEM},
                {
                    "role": "user",
                    "content": _context(inputs, today)
                    + f"\n<season>{sanitize(json.dumps(season))}</season>",
                },
            ],
            schema=SeasonOutline,
            trace=own,
            thinking=True,
            max_tokens=ctx.leaf.settings.budget("season_outline"),
        )
    finally:
        trace.extend(own)
    model = own[-1].model if own else ""
    kept, removed = outline_items(out.milestones, inputs, weeks, model)
    return kept, removed, model


async def write_month(
    ctx: CareContext,
    inputs: PlanInputs,
    m: Month,
    outline: list[dict[str, Any]],
    today: date,
    trace: list[ModelCall],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Ultra writes one month around the outline; the code rules check it, and its plantings
    are the outline's. Returns (items, removed, model)."""
    own: list[ModelCall] = []
    try:
        out = await complete_json(
            ctx.leaf.llm,
            step="season_plan",
            models=ctx.leaf.settings.models("season_plan"),
            messages=[
                {"role": "system", "content": SEASON_PLAN_SYSTEM},
                {
                    "role": "user",
                    "content": _context(inputs, today)
                    + f"\n<outline>{sanitize(json.dumps(outline_block(outline, inputs)))}</outline>"
                    + f"\n<month>{sanitize(json.dumps(month_block(inputs, m)))}</month>",
                },
            ],
            schema=SeasonMonth,
            trace=own,
            thinking=True,
            max_tokens=ctx.leaf.settings.budget("season_plan"),
        )
    finally:
        trace.extend(own)
    model = own[-1].model if own else ""
    kept, removed = check_items(out.items, inputs, m.weeks, model)
    return merge_month(kept, outline, m.weeks), removed, model


async def verify(
    ctx: CareContext,
    inputs: PlanInputs,
    items: list[dict[str, Any]],
    today: date,
    trace: list[ModelCall],
    known: dict[str, Any] | None = None,
    on_chunk: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Super checks the items against the rules and facts and can only remove them. Raises
    VerifyUnavailable when any chunk cannot be verified. `known` holds verdicts of chunks
    already checked (by a key of their item ids) and `on_chunk` saves each new one, so a
    retry after a timeout checks only what is left."""
    if not items:
        return [], [], ""
    known = known or {}
    chunks = [items[i : i + VERIFY_CHUNK] for i in range(0, len(items), VERIFY_CHUNK)]
    traces: list[list[ModelCall]] = [[] for _ in chunks]

    def key(chunk: list[dict[str, Any]]) -> str:
        return hashlib.sha256("|".join(sorted(it["id"] for it in chunk)).encode()).hexdigest()[:16]

    async def one(chunk: list[dict[str, Any]], own: list[ModelCall]) -> dict[str, Any]:
        k = key(chunk)
        if k in known:
            return known[k]
        block = verify_items_block(chunk, inputs)
        out = await complete_json(
            ctx.leaf.llm,
            step="plan_verify",
            models=ctx.leaf.settings.models("plan_verify"),
            messages=[
                {"role": "system", "content": PLAN_VERIFY_SYSTEM},
                {
                    "role": "user",
                    "content": _context(inputs, today)
                    + f"\n<items>{sanitize(json.dumps(block))}</items>",
                },
            ],
            schema=PlanVerdict,
            trace=own,
            thinking=True,
            max_tokens=ctx.leaf.settings.budget("plan_verify"),
            temperature=0.3,
        )
        verdict = {"issues": [i.model_dump() for i in out.issues], "by": own[-1].model}
        if on_chunk is not None:
            await on_chunk(k, verdict)
        return verdict

    results = await gather_limited(
        [one(c, t) for c, t in zip(chunks, traces, strict=True)], VERIFY_CONCURRENCY
    )
    for t in traces:
        trace.extend(t)
    issues = []
    for r in results:
        if isinstance(r, BaseException):
            raise VerifyUnavailable(str(r)[:200])
        issues += r["issues"]
    by = ", ".join(sorted({r["by"] for r in results if r.get("by")}))
    kept, removed = apply_issues(items, issues, f"verifier ({by})" if by else "verifier")
    return kept, removed, by


def _alive(item: dict[str, Any], inputs: PlanInputs) -> bool:
    """An item for the whole garden, or for a plant that is still in it."""
    return not item.get("plant_id") or inputs.by_id(item["plant_id"]) is not None


async def _still_current(ctx: CareContext, plan: SeasonPlan) -> bool:
    """False once a newer write replaced this one, so it stops spending on model calls."""
    now = await ctx.repo.get_plan(plan.id)
    return now is not None and now.status in ("writing", "unverified")


async def start_plan(
    ctx: CareContext, queue: Any, garden: Garden, trace_id: str = ""
) -> SeasonPlan:
    """Check the garden can have a plan, then start a new write and queue its job. The
    current verified plan stays visible until this one is ready."""
    from backend.jobs import JobMessage  # jobs imports this module

    inputs = await gather_inputs(ctx, garden)
    today = local_today(garden)
    first = monday(today)
    last = first + timedelta(weeks=PLAN_WEEKS - 1)
    basis = {
        "frost": inputs.frost,
        "zone": inputs.zone.get("zone", ""),
        "hemisphere": "south" if inputs.southern else "north",
        "plants": [
            {"name": p.name, "class": p.frost_class, "assumed": p.assumed}
            for p in inputs.plants.values()
        ],
        "made_on": today.isoformat(),
    }
    plan = await ctx.repo.create_season_plan(
        garden.id, first.isoformat(), last.isoformat(), basis, trace_id
    )
    await ctx.repo.log_event(EventIn(garden_id=garden.id, kind="season_plan", ref=plan.id))
    await queue.send(
        [
            JobMessage(
                kind="season_plan",
                key=f"season_plan:{plan.id}",
                payload={"garden_id": garden.id, "plan_id": plan.id},
            )
        ]
    )
    return plan


async def write_plan(
    ctx: CareContext, garden: Garden, plan: SeasonPlan, today: date | None = None
) -> dict[str, Any]:
    """The season_plan job: outline, then every month around it, each saved as it is
    written, then Super verifies, then the plan becomes current. A retry (after a timeout or
    a verifier outage) reuses the outline and the saved months and writes only what is
    missing."""
    trace: list[ModelCall] = []
    today = today or date.fromisoformat(plan.first_week)
    try:
        inputs = await gather_inputs(ctx, garden)
    except PlanUnavailable as exc:
        await ctx.repo.set_season_status(plan, "failed", str(exc))
        return {"status": "failed", "why": str(exc)}
    months = season_months(inputs, today)
    basis = dict(plan.basis)
    if "outline" not in basis:
        try:
            outline, removed, by = await write_outline(ctx, inputs, months, today, trace)
        except ModelError as exc:
            log.warning("season plan %s: no outline: %s", garden.id, exc)
            await ctx.repo.set_season_status(plan, "failed", FAILED_NOTE)
            return {"status": "failed", "why": "no outline", "calls": len(trace)}
        basis |= {"outline": outline, "outline_removed": removed, "outline_by": by}
        await ctx.repo.set_plan_basis(plan, basis)
    outline = [it for it in basis["outline"] if _alive(it, inputs)]
    if not await _still_current(ctx, plan):
        return {"status": "superseded", "calls": len(trace)}
    saved = {w.week for w in await ctx.repo.list_plan_weeks(plan.id)}
    todo = [m for m in months if not all(w in saved for w in m.weeks)]
    quiet = {  # dormant months: no model call, only the outline's plantings (usually none)
        w: ([it for it in outline if it["week"] == w], [])
        for m in todo
        if not m.active
        for w in m.weeks
    }
    await ctx.repo.put_plan_weeks(plan, quiet)

    async def month(m: Month) -> str:
        items, removed, by = await write_month(ctx, inputs, m, outline, today, trace)
        await ctx.repo.put_plan_weeks(
            plan,
            {
                w: ([i for i in items if i["week"] == w], [r for r in removed if r["week"] == w])
                for w in m.weeks
            },
        )
        return by

    active = [m for m in todo if m.active]
    results = await gather_limited([month(m) for m in active], MONTH_CONCURRENCY)
    unwritten = list(basis.get("unwritten", []))
    writers = set(basis.get("written_by", [])) | ({basis["outline_by"]} - {""})
    for m, r in zip(active, results, strict=True):
        if isinstance(r, BaseException):
            log.warning("season plan %s: %s not written: %s", garden.id, m.label, r)
            unwritten.append(m.label)
            # The outline's plantings still stand for that month.
            await ctx.repo.put_plan_weeks(
                plan, {w: ([i for i in outline if i["week"] == w], []) for w in m.weeks}
            )
        else:
            writers.add(r)
    if active and len(unwritten) >= len([m for m in months if m.active]):
        await ctx.repo.set_season_status(plan, "failed", FAILED_NOTE)
        return {"status": "failed", "why": "no month written", "calls": len(trace)}
    basis |= {"unwritten": unwritten, "written_by": sorted(w for w in writers if w)}
    await ctx.repo.set_plan_basis(plan, basis)
    if not await _still_current(ctx, plan):
        return {"status": "superseded", "calls": len(trace)}
    # From here only verification is left: a retry skips straight to it.
    await ctx.repo.set_season_status(plan, "unverified", "Being checked.")

    rows = await ctx.repo.list_plan_weeks(plan.id)
    items = [it for w in rows for it in w.items if _alive(it, inputs)]  # plants still here
    verdicts = dict(basis.get("verdicts", {}))

    async def save_verdict(k: str, verdict: dict[str, Any]) -> None:
        verdicts[k] = verdict
        await ctx.repo.set_plan_basis(plan, basis | {"verdicts": verdicts})

    try:
        kept, flagged, verifier = await verify(
            ctx, inputs, items, today, trace, known=verdicts, on_chunk=save_verdict
        )
    except VerifyUnavailable as exc:
        await ctx.repo.set_season_status(plan, "unverified", "Waiting for verification.")
        raise RuntimeError(f"season plan not verified yet: {exc}") from exc
    removed_by_code = [r for w in rows for r in w.removed] + basis.get("outline_removed", [])
    checks = {
        "removed": (removed_by_code + flagged)[:MAX_REMOVED_KEPT],
        "dormant": [m.label for m in months if not m.active],
        "unwritten": unwritten,
        "written_by": basis["written_by"],
        "verified_by": verifier,
        "items": len(kept),
    }
    by_week: dict[str, list[dict[str, Any]]] = {w.week: [] for w in rows}
    for it in kept:
        by_week.setdefault(it["week"], []).append(it)
    if not await ctx.repo.finish_season_plan(plan, by_week, checks):
        return {"status": "superseded"}
    return {
        "status": "ready",
        "items": len(kept),
        "removed": len(checks["removed"]),
        "unwritten": unwritten,
        "calls": len(trace),
        "cost_usd": round(sum(c.cost_usd or 0 for c in trace), 4),
    }


async def _replan_call(
    ctx: CareContext,
    change: str,
    inputs: PlanInputs,
    today: date,
    current: dict[str, list[dict[str, Any]]],
    trace: list[ModelCall],
) -> Replan:
    return await complete_json(
        ctx.leaf.llm,
        step="replan",
        models=ctx.leaf.settings.models("replan"),
        messages=[
            {"role": "system", "content": REPLAN_SYSTEM},
            {
                "role": "user",
                "content": f"<change>{change}</change>\n"
                + _context(inputs, today)
                + f"\n<weeks>{sanitize(json.dumps(current))}</weeks>",
            },
        ],
        schema=Replan,
        trace=trace,
        thinking=True,
        max_tokens=ctx.leaf.settings.budget("replan"),
    )


async def replan(
    ctx: CareContext, garden: Garden, trigger: dict[str, Any], context: str = ""
) -> dict[str, Any]:
    """Rewrite only the weeks a change touches (Ultra), check and verify the result, store it
    as the plan's next revision, and log what changed (diffed in code) and why. `context` is
    extra, code-checked detail for the replanner, such as a diagnosis's care plan."""
    plan = await ctx.repo.get_season_plan(garden.id)
    if plan is None:
        return {"skipped": "no verified plan"}
    rows = {w.week: w.items for w in await ctx.repo.list_plan_weeks(plan.id)}
    target = [w for w in trigger.get("weeks", []) if w in rows][:MAX_REPLAN_WEEKS]
    if not target:
        return {"skipped": "no plan weeks to change"}
    try:
        inputs = await gather_inputs(ctx, garden)
    except PlanUnavailable as exc:
        return {"skipped": str(exc)}
    today = local_today(garden)
    trace: list[ModelCall] = []
    # Items of plants no longer in the garden are left out (and so drop from the plan).
    old = {
        w: [it for it in rows[w] if not it.get("plant_id") or inputs.by_id(it["plant_id"])]
        for w in target
    }
    current = {
        w: [
            {
                "plant": inputs.ref_of(it),
                "kind": it["kind"],
                "action": it["action"],
                "why": it["why"],
            }
            for it in old[w]
        ]
        for w in target
    }
    change = sanitize(trigger.get("text", ""), 400) + (
        f"\n{sanitize(context, 2000)}" if context else ""
    )
    try:
        out = await _replan_call(ctx, change, inputs, today, current, trace)
    except SpendCapReached:
        # Replans are optional: in brownout the change waits for a later trigger.
        return {"skipped": "brownout: model spend is near today's cap"}
    model = trace[-1].model if trace else ""
    new: dict[str, list[dict[str, Any]]] = {}
    removed: list[dict[str, Any]] = []
    for rw in out.weeks:
        week = rw.week[:10]
        if week not in target or week in new:
            continue
        kept, rem = check_items(
            [{**it.model_dump(), "week": week} for it in rw.items], inputs, [week], model
        )
        new[week], removed = kept, removed + rem
    for w in target:  # a week the model did not return stays as it was
        new.setdefault(w, old[w])
    old_ids = {it["id"] for w in target for it in old[w]}
    fresh = [it for w in target for it in new[w] if it["id"] not in old_ids]
    try:
        verified, flagged_items, _ = await verify(ctx, inputs, fresh, today, trace)
    except VerifyUnavailable as exc:
        raise RuntimeError(f"replan not verified: {exc}") from exc
    fresh_ids = {it["id"] for it in fresh}
    kept_ids = {it["id"] for it in verified}
    new = {
        w: [it for it in its if it["id"] not in fresh_ids or it["id"] in kept_ids]
        for w, its in new.items()
    }
    changes = diff_weeks(old, new)
    summary = sanitize(out.summary.strip(), 300)
    payload = {
        "trigger": {k: trigger.get(k) for k in ("kind", "key", "text", "source")},
        "weeks": target,
        "changes": changes,
        "summary": summary,
        "removed": (removed + flagged_items)[:20],
        "by": model,
    }
    if changes:
        checks = dict(plan.checks)
        checks["removed"] = (checks.get("removed", []) + removed + flagged_items)[:MAX_REMOVED_KEPT]
        why = f"{trigger.get('text', '')} {summary}".strip()[:600]
        if not await ctx.repo.replan_weeks(plan, new, changed=why, checks=checks):
            raise RuntimeError("the plan changed while replanning; retrying")
    await ctx.repo.log_event(
        EventIn(
            garden_id=garden.id,
            kind="replan",
            ref=str(trigger.get("key", ""))[:80],
            payload=payload,
        )
    )
    return {
        "changed": bool(changes),
        **payload,
        "cost_usd": round(sum(c.cost_usd or 0 for c in trace), 4),
    }


def replan_job(garden: Garden, trigger: dict[str, Any], context: str = "") -> Any:
    from backend.jobs import JobMessage

    return JobMessage(
        kind="replan",
        key=f"replan:{garden.id}:{trigger['key']}"[:200],
        payload={"garden_id": garden.id, "trigger": trigger, "context": context[:2000]},
    )


async def weather_replans(
    ctx: CareContext, garden: Garden, fc: Forecast | None = None
) -> list[dict[str, Any]]:
    """The replans a garden's forecast calls for (frost or heat in a week with planting)."""
    plan = await ctx.repo.get_season_plan(garden.id)
    if plan is None or plan.status != "ready" or not garden.cell:
        return []
    try:
        fc = fc or await ctx.climate.forecast(garden.cell)
        inputs = await gather_inputs(ctx, garden)
    except (WeatherUnavailable, PlanUnavailable):
        return []
    today = local_today(garden)
    weeks = {  # the forecast's weeks, and the week after each (where a planting may move)
        w.week: w.items
        for w in await ctx.repo.list_plan_weeks(
            plan.id, monday(today).isoformat(), (today + timedelta(days=23)).isoformat()
        )
    }
    return weather_triggers(inputs, weeks, fc.day_rows(), today)


# ---------- Leaf Doctor wiring ----------


def _case_tag(case_id: str) -> str:
    return case_id[:12]


def _plan_from(report: dict[str, Any], garden: Garden) -> CarePlan:
    """The report's care plan, re-checked in code with this garden's own choices (the
    diagnosis may have been made with other settings)."""
    try:
        plan = CarePlan.model_validate(report.get("plan") or {"summary": ""})
    except ValidationError as exc:
        raise PlanUnavailable("This report has no care plan Leaf can add to a garden.") from exc
    apply_garden(plan, garden.prefs)
    return plan


def diagnosis_name(report: dict[str, Any]) -> str:
    """The leading diagnosis: Deep's differential first, then the quick candidates."""
    first = (report.get("differential") or [None])[0] or (
        (report.get("diagnosis") or {}).get("candidates") or [None]
    )[0]
    return str(first.get("name", "")) if isinstance(first, dict) else ""


async def link_diagnosis(
    ctx: CareContext,
    queue: Any,
    garden: Garden,
    plant: Plant,
    case_id: str,
    report: dict[str, Any],
    trace_id: str = "",
) -> dict[str, Any]:
    """A diagnosis joins the garden: today's treatment steps become tasks, a photo check-in is
    due when the plan says, and the season plan's next weeks are replanned for it."""
    from backend.care import PlantState

    name = PlantState(plant=plant).name
    care = _plan_from(report, garden)
    today = local_today(garden)
    tag = _case_tag(case_id)
    path = f"/c/{case_id}"
    diag = sanitize(diagnosis_name(report), 120) or "a problem"
    src = {"title": f"Leaf Doctor report: {diag}", "url": path}
    tasks = [
        TaskIn(
            garden_id=garden.id,
            plant_id=plant.id,
            day=today.isoformat(),
            kind=f"treat:{tag}:{i}",
            title=sanitize(step.action, 120),
            reason=sanitize(step.why, 300),
            source=src,
            urgency="soon",
            created_by="doctor",
            trace_id=trace_id,
        )
        for i, step in enumerate(care.steps[:DOCTOR_TASKS])
    ]
    due = today + timedelta(days=care.check_in_days)
    tasks.append(
        TaskIn(
            garden_id=garden.id,
            plant_id=plant.id,
            day=due.isoformat(),
            kind=f"checkin:{tag}",
            title=f"Check-in photo: {name}",
            reason=f"See whether the treatment for {diag} is working: add a photo to the report.",
            source=src,
            created_by="doctor",
            trace_id=trace_id,
        )
    )
    added = await ctx.repo.add_tasks(tasks)
    linked = any(
        e.garden_id == garden.id and e.plant_id == plant.id
        for e in await ctx.repo.events_by_ref("diagnosis", case_id)
    )
    if not linked:
        await ctx.repo.log_event(
            EventIn(
                garden_id=garden.id,
                plant_id=plant.id,
                kind="diagnosis",
                ref=case_id,
                payload={
                    "diagnosis": diag,
                    "verdict": report.get("verdict", ""),
                    "steps": len(tasks) - 1,
                },
                trace_id=trace_id,
            )
        )
    queued = await _queue_replan(
        ctx,
        queue,
        garden,
        "diagnosis",
        f"diagnosis:{tag}:{plant.id}",
        f"Leaf Doctor diagnosed {diag} on {name} ({today.isoformat()}).",
        care,
        today,
    )
    return {
        "tasks_added": added,
        "checkin_on": due.isoformat(),
        "replan": queued,
        "removed": care.removed,
    }


async def _queue_replan(
    ctx: CareContext,
    queue: Any,
    garden: Garden,
    kind: str,
    key: str,
    text: str,
    care: CarePlan | None,
    today: date,
) -> bool:
    if await ctx.repo.get_season_plan(garden.id) is None:
        return False
    weeks = [(monday(today) + timedelta(weeks=i)).isoformat() for i in range(MAX_REPLAN_WEEKS)]
    context = ""
    if care is not None:
        steps = [{"action": s.action, "why": s.why} for s in care.steps[:8]]
        context = json.dumps({"care_plan": care.summary, "steps": steps, "removed": care.removed})
    trigger = {"kind": kind, "key": key, "weeks": weeks, "text": text, "source": {}}
    await queue.send([replan_job(garden, trigger, context)])
    return True


async def checkin_loop(
    ctx: CareContext, queue: Any, case_id: str, report: dict[str, Any], record: Any, n: int
) -> list[dict[str, Any]]:
    """A check-in on a report that is linked to gardens: close its check-in task, log what
    was seen, and keep the loop going (another check-in; spreading adds the new steps as
    tasks and replans)."""
    out = []
    seen: set[tuple[str, str | None]] = set()
    for ev in await ctx.repo.events_by_ref("diagnosis", case_id):
        if (ev.garden_id, ev.plant_id) in seen:
            continue
        seen.add((ev.garden_id, ev.plant_id))
        garden = await ctx.repo.get_garden(ev.garden_id)
        plant = await ctx.repo.get_plant(ev.garden_id, ev.plant_id or "")
        if garden is None or plant is None:
            continue
        today = local_today(garden)
        tag = _case_tag(case_id)
        name = plant.nickname or plant.common_name or plant.species or "the plant"
        span = (today - timedelta(days=60)).isoformat(), (today + timedelta(days=60)).isoformat()
        for t in await ctx.repo.list_tasks(garden.id, *span, status="open"):
            if t.kind == f"checkin:{tag}" and t.plant_id == plant.id:
                await ctx.repo.set_task_status(garden.id, t.id, "done")
        status = getattr(record, "status", "unclear")
        await ctx.repo.log_event(
            EventIn(
                garden_id=garden.id,
                plant_id=plant.id,
                kind="checkin",
                ref=case_id,
                payload={"status": status, "change": sanitize(getattr(record, "change", ""), 300)},
            )
        )
        result: dict[str, Any] = {"garden_id": garden.id, "status": status, "replan": False}
        diag = sanitize(diagnosis_name(report), 120) or "the problem"
        src = {"title": f"Leaf Doctor report: {diag}", "url": f"/c/{case_id}"}
        tasks = []
        if status == "spreading":
            care = CarePlan(summary="", steps=list(getattr(record, "steps", []) or []))
            apply_garden(care, garden.prefs)
            tasks += [
                TaskIn(
                    garden_id=garden.id,
                    plant_id=plant.id,
                    day=today.isoformat(),
                    kind=f"treat:{tag}:c{n}:{i}",
                    title=sanitize(s.action, 120),
                    reason=sanitize(s.why, 300),
                    source=src,
                    urgency="soon",
                    created_by="doctor",
                )
                for i, s in enumerate(care.steps[:DOCTOR_TASKS])
            ]
            result["replan"] = await _queue_replan(
                ctx,
                queue,
                garden,
                "checkin",
                f"checkin:{tag}:{plant.id}:{n}",
                f"Check-in {today.isoformat()}: {diag} on {name} is spreading.",
                care,
                today,
            )
        if status != "recovering":
            tasks.append(
                TaskIn(
                    garden_id=garden.id,
                    plant_id=plant.id,
                    day=(today + timedelta(days=CHECKIN_AGAIN_DAYS)).isoformat(),
                    kind=f"checkin:{tag}",
                    title="Check-in photo",
                    reason=f"Another photo shows whether {diag} is under control.",
                    source=src,
                    created_by="doctor",
                )
            )
        result["tasks_added"] = await ctx.repo.add_tasks(tasks)
        out.append(result)
    return out
