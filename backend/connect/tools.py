"""What Leaf can do for a caller, once: the REST API, MCP and Telegram all call these.

Each function takes the request (for app state), the `Caller` (garden, limits identity) and
plain arguments, and returns plain JSON-able data. Model-spending tools also apply the shared
per-identity limiter, so a key, a Telegram chat and a web visitor are limited the same way.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from backend.care.service import CareContext
from backend.connect.auth import Caller
from backend.data.repo import grid_cell
from backend.platform_api import (
    climate_view,
    identify_photo,
    season_view,
    start_season,
    today_view,
)
from backend.schemas import DiagnoseResponse, Garden
from backend.season.service import PlanUnavailable, link_diagnosis

MAX_STEPS = 8
GARDEN_MODEL_CALLS_PER_DAY = 30  # across every key and Telegram chat of one garden


def base_url(request: Request) -> str:
    s = request.app.state.leaf.settings
    return (s.public_url or str(request.base_url)).rstrip("/")


def diagnosis_summary(
    report: DiagnoseResponse, request: Request, *, include_token: bool = False
) -> dict[str, Any]:
    """A diagnosis for a program: the leading hypotheses, the checked plan, its sources, and a
    link to the full report. The owner token (which allows check-ins) goes only to the caller
    who owns the report over REST, never into a model's context."""
    plan = report.plan
    out = {
        "case_id": report.case_id,
        "report_url": f"{base_url(request)}/c/{report.case_id}" if report.case_id else "",
        "verdict": report.verdict,
        "verdict_reason": report.verdict_reason,
        "notice": report.notice,
        "diagnosis": [
            {"name": c.name, "category": c.category, "confidence": round(c.confidence, 2)}
            for c in report.diagnosis.candidates[:3]
        ],
        "summary": report.diagnosis.reasoning_summary,
        "plan": {
            "summary": plan.summary,
            "steps": [
                {
                    "action": st.action,
                    "why": st.why,
                    "organic": st.organic,
                    "pet_risk": st.pet_risk,
                    "precautions": st.precautions,
                }
                for st in plan.steps[:MAX_STEPS]
            ],
            "watch_for": plan.watch_for,
            "cautions": plan.cautions,
            "removed_for_your_choices": plan.removed,
            "escalate": plan.escalate,
            "escalate_reason": plan.escalate_reason,
            "check_in_days": plan.check_in_days,
        },
        "sources": [{"title": s.title, "url": s.url} for s in report.sources],
    }
    if include_token:
        out["owner_token"] = report.owner_token
    return out


async def spend_model_call(request: Request, garden: Garden | Any) -> None:
    """One call that may spend model credits, against the garden's own daily budget: five
    keys and a few chats of one garden still share these, so a single garden cannot use up the
    day's diagnoses for everyone (the shared limiter and the spend cap sit above this)."""
    repo = request.app.state.data.repo
    service = f"model_calls:{garden.id}"
    if await repo.api_usage_on(service) >= GARDEN_MODEL_CALLS_PER_DAY:
        raise HTTPException(
            429,
            f"This garden has used its {GARDEN_MODEL_CALLS_PER_DAY} model calls for today, "
            "across all its keys and chats. Try again tomorrow.",
        )
    await repo.add_api_usage(service, 1)


async def diagnose_tool(
    request: Request,
    caller: Caller,
    notes: str,
    data: bytes | None = None,
    region: str = "",
    plant_id: str | None = None,
    *,
    include_token: bool = False,
) -> dict[str, Any]:
    """A Quick diagnosis for the caller's garden (its organic and pet choices apply). With a
    `plant_id` the report joins that plant: its steps become tasks and the season plan is
    replanned."""
    from backend.app import diagnose_photo  # app imports this package

    request.app.state.limiter.check(caller.identity)
    await spend_model_call(request, caller.garden)
    from backend.radar.prior import activate

    await activate(request.app, cell=caller.garden.cell)  # Leaf Radar's capped prior
    prefs = Garden(organic=caller.garden.prefs.organic, pets=caller.garden.prefs.pets)
    plant = None
    if plant_id:
        plant = await request.app.state.data.repo.get_plant(caller.garden.id, plant_id)
        if plant is None or plant.status != "active":
            raise HTTPException(404, "That plant is not in this garden.")
    report = await diagnose_photo(request, data, notes, region, "", prefs)
    out = diagnosis_summary(report, request, include_token=include_token)
    if plant is not None and report.case_id:
        ctx = CareContext(
            request.app.state.data.repo, request.app.state.climate, request.app.state.leaf
        )
        try:
            out["garden_link"] = await link_diagnosis(
                ctx,
                request.app.state.queue,
                caller.garden,
                plant,
                report.case_id,
                report.model_dump(mode="json"),
            )
        except PlanUnavailable as exc:
            out["garden_link"] = {"error": str(exc)}
    return out


async def identify_tool(
    request: Request, caller: Caller, data: bytes, notes: str = ""
) -> dict[str, Any]:
    """Name a plant from a photo: candidates with the evidence for each."""
    request.app.state.identify_limiter.check(caller.identity)
    await spend_model_call(request, caller.garden)
    out = await identify_photo(request, data, notes)
    return {k: v for k, v in out.items() if k != "trace"}


async def weather_risk_tool(
    request: Request, caller: Caller, lat: float | None = None, lon: float | None = None
) -> dict[str, Any]:
    """Frost, heat and wind risk from the forecast, NWS alerts (US) and the next days, for a
    point or, with none given, the caller's garden."""
    if lat is None or lon is None:
        if not caller.garden.cell:
            raise HTTPException(400, "Give a latitude and longitude, or set the garden's place.")
        cell = caller.garden.cell
    else:
        cell = grid_cell(lat, lon)
    request.app.state.climate_limiter.check(caller.identity)
    view = await climate_view(request, cell, None)
    return {
        "cell": view["cell"],
        "timezone": view["timezone"],
        "risks": view["risks"],
        "alerts": view["alerts"],
        "soil_temperature_6cm_c": view["soil_temperature_6cm_c"],
        "days": view["days"][:5],
        "attribution": view["attribution"],
    }


async def today_tool(request: Request, caller: Caller) -> dict[str, Any]:
    """The garden's day: urgent alerts, tasks with reasons and sources, water status."""
    v = await today_view(request, caller.garden)
    return {
        "date": v["date"],
        "urgent": v["urgent"],
        "alerts": v["alerts"],
        "tasks": [
            {
                "id": t["id"],
                "plant_id": t["plant_id"],
                "kind": t["kind"],
                "title": t["title"],
                "reason": t["reason"],
                "urgency": t["urgency"],
                "status": t["status"],
                "due_at": t["due_at"],
                "source": t["source"],
            }
            for t in v["tasks"]
        ],
        "water": v["water"],
        "season_this_week": v["season_week"],
        "attribution": v["attribution"],
    }


async def season_tool(request: Request, caller: Caller) -> dict[str, Any]:
    """The verified season plan (or the state of one being written)."""
    return await season_view(request, caller.garden)


async def season_start_tool(request: Request, caller: Caller) -> dict[str, Any]:
    """Start writing (or rewriting) the season plan; the model work happens in the background."""
    request.app.state.tool_limiter.check(caller.identity)
    await spend_model_call(request, caller.garden)
    return await start_season(request, caller.garden)
