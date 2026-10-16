"""Platform endpoints: gardens (private links), the front door, usage, and the job runner.

A garden is opened with its private key in the `X-Leaf-Key` header (the page reads it from
the link's #k= fragment, which never reaches a server log). A wrong key and a missing garden
both answer 404, so ids cannot be probed.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from backend.calendar_file import ics_calendar
from backend.care.sensor import FRESH_S, is_fresh, sensor_advice
from backend.care.service import CareContext, local_today, ordered, plan_garden
from backend.climate import Climate, cell_center
from backend.climate.crops import GENERAL_GDD_BASE_C, Crop, find_crop
from backend.climate.forecast import Forecast
from backend.climate.nws import AlertsUnavailable
from backend.climate.openmeteo import (
    ATTRIBUTION,
    ATTRIBUTION_URL,
    DAILY_ALLOWANCE,
    WeatherUnavailable,
)
from backend.climate.risk import gdd_recent, risks, soil_temperature_now, sowing
from backend.climate.sources import cite
from backend.climate.water import Placement, WaterAdvice, indoor_advice, outdoor_advice
from backend.conditions import ConditionsError, Place, parse_hours
from backend.conditions import summarise as summarise_conditions
from backend.config import ROUTES
from backend.context import REQUEST
from backend.data.driver import DataError
from backend.data.repo import (
    Event,
    EventIn,
    Garden,
    GardenIn,
    NotificationSettings,
    Plant,
    PlantIn,
    Repo,
    Task,
    TaskStatus,
    grid_cell,
    new_id,
    now_s,
)
from backend.jobs import RADAR_CREDITS_PER_DAY, JobMessage, species_job
from backend.knowledge import budget as knowledge_budget
from backend.knowledge import species_key
from backend.knowledge.identify import identify
from backend.knowledge.profile import profile_view
from backend.notify import notify
from backend.router import REFUSALS, front_door
from backend.schemas import FrontDoorDecision, Intent, ModelCall
from backend.season import calendar_events, monday
from backend.season.service import PlanUnavailable, link_diagnosis, start_plan
from backend.spray import plan_spray
from backend.vision import prepare_image

log = logging.getLogger("leaf.platform")

router = APIRouter()

MAX_PLANTS = 200
MAX_TASK_DAYS = 31


def _repo(request: Request) -> Repo:
    return request.app.state.data.repo


def _ip(request: Request) -> str:
    from backend.app import client_ip  # the app module owns proxy-depth handling

    return client_ip(request, request.app.state.leaf.settings.trusted_proxy_hops)


async def _open(request: Request, garden_id: str, key: str | None) -> Garden:
    request.app.state.garden_use_limiter.check(_ip(request))
    try:
        garden = await _repo(request).open_garden(garden_id, key or "")
    except DataError as exc:
        log.error("garden lookup failed: %s", exc)
        raise HTTPException(503, "Leaf's garden storage is unavailable. Please try again.") from exc
    if garden is None:
        raise HTTPException(404, "No garden at this link. Check that you copied all of it.")
    ctx = REQUEST.get()
    if ctx is not None:
        ctx.actor = f"g:{garden.id}"
    return garden


Key = Annotated[str | None, Header(alias="X-Leaf-Key")]


# ---------- gardens ----------


class GardenCreated(BaseModel):
    garden: Garden
    key: str  # shown once; the private link is /g/<id>#k=<key>
    link: str


class GardenView(BaseModel):
    garden: Garden
    plants: list[Plant]


@router.post("/api/gardens", response_model=GardenCreated)
async def create_garden(request: Request, body: GardenIn) -> GardenCreated:
    request.app.state.garden_limiter.check(_ip(request))
    try:
        garden, key = await _repo(request).create_garden(body)
    except DataError as exc:
        log.error("garden create failed: %s", exc)
        raise HTTPException(503, "Leaf's garden storage is unavailable. Please try again.") from exc
    ctx = REQUEST.get()
    if ctx is not None:
        ctx.actor = f"g:{garden.id}"
    job = JobMessage(
        kind="garden_ready", key=f"garden_ready:{garden.id}", payload={"garden_id": garden.id}
    )
    try:
        await request.app.state.queue.send([job])
    except DataError as exc:  # the garden works without it; housekeeping-style jobs catch up
        log.warning("could not queue garden_ready for %s: %s", garden.id, exc)
    base = request.app.state.leaf.settings.public_url or str(request.base_url).rstrip("/")
    return GardenCreated(garden=garden, key=key, link=f"{base}/g/{garden.id}#k={key}")


@router.get("/api/gardens/{garden_id}", response_model=GardenView)
async def get_garden(request: Request, garden_id: str, key: Key = None) -> GardenView:
    garden = await _open(request, garden_id, key)
    return GardenView(garden=garden, plants=await _repo(request).list_plants(garden.id))


@router.delete("/api/gardens/{garden_id}")
async def delete_garden(request: Request, garden_id: str, key: Key = None) -> dict:
    garden = await _open(request, garden_id, key)
    await _repo(request).delete_garden(garden.id)
    return {"deleted": True}


@router.post("/api/gardens/{garden_id}/plants", response_model=Plant)
async def add_plant(request: Request, garden_id: str, body: PlantIn, key: Key = None) -> Plant:
    garden = await _open(request, garden_id, key)
    repo = _repo(request)
    # Archived plants count too, so archive-then-add cannot grow a garden without limit.
    if len(await repo.list_plants(garden.id, include_archived=True)) >= MAX_PLANTS:
        raise HTTPException(400, f"A garden can hold up to {MAX_PLANTS} plants.")
    plant = await repo.add_plant(garden.id, body)
    if body.species or body.common_name:  # its profile is built once and shared
        await queue_species(request, body.species, [body.common_name] if body.common_name else [])
    return plant


@router.delete("/api/gardens/{garden_id}/plants/{plant_id}")
async def archive_plant(request: Request, garden_id: str, plant_id: str, key: Key = None) -> dict:
    garden = await _open(request, garden_id, key)
    if not await _repo(request).archive_plant(garden.id, plant_id):
        raise HTTPException(404, "No such plant in this garden.")
    return {"archived": True}


@router.get("/api/gardens/{garden_id}/tasks", response_model=list[Task])
async def list_tasks(
    request: Request,
    garden_id: str,
    key: Key = None,
    day_from: Annotated[str | None, Query(alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    day_to: Annotated[str | None, Query(alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    status: TaskStatus | None = None,
) -> list[Task]:
    garden = await _open(request, garden_id, key)
    today = datetime.now(UTC).date()
    try:
        start = datetime.fromisoformat(day_from).date() if day_from else today
        end = datetime.fromisoformat(day_to).date() if day_to else start + timedelta(days=7)
    except ValueError as exc:
        raise HTTPException(422, "Dates must be real dates (YYYY-MM-DD).") from exc
    if end < start or (end - start).days > MAX_TASK_DAYS:
        raise HTTPException(422, f"Ask for at most {MAX_TASK_DAYS} days of tasks at a time.")
    return await _repo(request).list_tasks(garden.id, start.isoformat(), end.isoformat(), status)


class TaskUpdate(BaseModel):
    status: Literal["done", "skipped", "open"]


@router.post("/api/gardens/{garden_id}/tasks/{task_id}")
async def update_task(
    request: Request, garden_id: str, task_id: str, body: TaskUpdate, key: Key = None
) -> dict:
    garden = await _open(request, garden_id, key)
    ctx = REQUEST.get()
    trace_id = ctx.trace_id if ctx else ""
    if not await _repo(request).set_task_status(garden.id, task_id, body.status, trace_id):
        raise HTTPException(404, "No such task in this garden.")
    return {"status": body.status}


@router.get("/api/gardens/{garden_id}/events", response_model=list[Event])
async def list_events(
    request: Request, garden_id: str, key: Key = None, limit: int = Query(50, ge=1, le=500)
) -> list[Event]:
    garden = await _open(request, garden_id, key)
    return await _repo(request).list_events(garden.id, limit)


@router.get("/api/gardens/{garden_id}/notifications", response_model=NotificationSettings)
async def get_notifications(
    request: Request, garden_id: str, key: Key = None
) -> NotificationSettings:
    garden = await _open(request, garden_id, key)
    return await _repo(request).get_notifications(garden.id)


@router.put("/api/gardens/{garden_id}/notifications", response_model=NotificationSettings)
async def put_notifications(
    request: Request, garden_id: str, body: NotificationSettings, key: Key = None
) -> NotificationSettings:
    garden = await _open(request, garden_id, key)
    await _repo(request).put_notifications(garden.id, body)
    return body


# ---------- the front door ----------


class RouteRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    intent: Intent | None = None  # set by a UI button that already knows; skips the model


class RouteResponse(FrontDoorDecision):
    refusal: str = ""  # the fixed reply to show instead of an answer when safe is false


@router.post("/api/route", response_model=RouteResponse)
async def route(request: Request, body: RouteRequest) -> RouteResponse:
    request.app.state.route_limiter.check(_ip(request))
    leaf = request.app.state.leaf
    decision = await front_door(
        leaf.llm, leaf.settings, body.text, request.app.state.cache, body.intent
    )
    return RouteResponse(**decision.model_dump(), refusal=refusal_for(decision))


def refusal_for(decision: FrontDoorDecision) -> str:
    return "" if decision.safe else REFUSALS.get(decision.risk, REFUSALS["harmful"])


# ---------- usage (public, aggregate only) ----------


@router.get("/api/ops/usage")
async def usage(request: Request, days: int = Query(1, ge=1, le=7)) -> dict[str, Any]:
    """Cost, tokens, latency, model mix, cache hit rate and cost per active user. No personal
    data: counts and sums only."""
    request.app.state.ops_limiter.check(_ip(request))
    meter = request.app.state.meter
    await meter.flush()
    today = datetime.now(UTC).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    try:
        raw = await _repo(request).usage(start, today.isoformat())
    except DataError as exc:
        log.error("usage query failed: %s", exc)
        raise HTTPException(503, "Usage data is unavailable right now.") from exc
    return summarize_usage(raw, meter.snapshot())


@router.get("/api/ops/platform")
async def ops_platform(request: Request) -> dict[str, Any]:
    """What the platform is doing: jobs in the last 24 hours, how much data it holds, and
    today's use of the free services. Counts only; no personal data and no secrets."""
    request.app.state.ops_limiter.check(_ip(request))
    day = datetime.now(UTC).date().isoformat()
    try:
        raw = await _repo(request).platform_stats(now_s() - 86400, day)
    except DataError as exc:
        raise HTTPException(503, "Platform data is unavailable right now.") from exc
    jobs: dict[str, dict[str, int]] = {}
    for r in raw["jobs"]:
        jobs.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
    return {
        "day": day,
        "jobs_24h": jobs,
        "data": raw["data"],
        "free_services": {
            r["service"]: {"units": round(float(r["units"]), 1), "calls": int(r["calls"])}
            for r in raw["free"]
        },
        "allowances": {
            "open-meteo": DAILY_ALLOWANCE,
            knowledge_budget.SERVICE: knowledge_budget.KNOWLEDGE_CREDITS_PER_DAY,
            "tavily:radar": RADAR_CREDITS_PER_DAY,
        },
    }


def summarize_usage(raw: dict[str, list[dict[str, Any]]], guard: dict[str, Any]) -> dict[str, Any]:
    by_task: dict[str, dict[str, Any]] = {}
    models: dict[str, int] = {}
    total_cost, total_calls = 0.0, 0
    for r in raw["calls"]:
        t = by_task.setdefault(
            r["task"],
            {
                "calls": 0,
                "failed": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms_sum": 0.0,
                "models": {},
            },
        )
        calls = int(r["calls"])
        t["calls"] += calls
        t["failed"] += int(r["failed"] or 0)
        t["prompt_tokens"] += int(r["prompt_tokens"] or 0)
        t["completion_tokens"] += int(r["completion_tokens"] or 0)
        t["cost_usd"] += float(r["cost_usd"] or 0)
        t["latency_ms_sum"] += float(r["avg_latency_ms"] or 0) * calls
        t["models"][r["model"]] = t["models"].get(r["model"], 0) + calls
        models[r["model"]] = models.get(r["model"], 0) + calls
        total_cost += float(r["cost_usd"] or 0)
        total_calls += calls
    for t in by_task.values():
        t["avg_latency_ms"] = round(t.pop("latency_ms_sum") / t["calls"]) if t["calls"] else 0
        t["cost_usd"] = round(t["cost_usd"], 6)
    hits = sum(int(c["hits"]) for c in raw["cache"])
    misses = sum(int(c["misses"]) for c in raw["cache"])
    caches: dict[str, dict[str, int]] = {}
    for c in raw["cache"]:
        ns = caches.setdefault(c["namespace"], {"hits": 0, "misses": 0})
        ns["hits"] += int(c["hits"])
        ns["misses"] += int(c["misses"])
    active_days = [int(a["active"]) for a in raw["active"]]
    active = sum(active_days)  # user-days
    return {
        "calls": total_calls,
        "cost_usd": round(total_cost, 6),
        "active_user_days": active,
        "cost_per_active_user_day": round(total_cost / active, 6) if active else None,
        "target_per_active_user_day": 0.01,
        "cache_hit_rate": round(hits / (hits + misses), 3) if hits + misses else None,
        "caches": caches,
        "models": models,
        "tasks": dict(sorted(by_task.items())),
        "routes": {
            k: {"chain": list(v.chain), "max_tokens": v.max_tokens, "optional": v.optional}
            for k, v in ROUTES.items()
        },
        "guard": guard,
    }


# ---------- jobs (reachable only from the Worker's queue consumer) ----------


@router.post("/internal/jobs/run", include_in_schema=False)
async def run_job(
    request: Request, internal: Annotated[str | None, Header(alias="X-Leaf-Internal")] = None
) -> JSONResponse:
    # The Worker strips X-Leaf-Internal from every public request and refuses /internal/
    # paths, so only its queue consumer can set it.
    if internal != "1":
        raise HTTPException(404, "Not found")
    try:
        msg = JobMessage.model_validate(await request.json())
    except (ValueError, ValidationError):
        return JSONResponse({"status": "bad_message"}, status_code=422)
    try:
        status, body = await request.app.state.runner.run(msg)
    except DataError as exc:
        log.error("job %s: storage error: %s", msg.key, exc)
        return JSONResponse({"status": "storage_error"}, status_code=503)
    return JSONResponse(body, status_code=status)


# ---------- climate (M2) ----------


def _climate(request: Request) -> Climate:
    return request.app.state.climate


def _day_list(fc: Forecast) -> list[dict[str, Any]]:
    today = fc.local_today().isoformat()
    return [
        {
            "date": d["date"],
            "high_c": d["temperature_2m_max"],
            "low_c": d["temperature_2m_min"],
            "rain_mm": d["precipitation_sum"],
            "rain_chance": d["precipitation_probability_max"],
            "et0_mm": d["et0_fao_evapotranspiration"],
            "gust_kmh": d["wind_gusts_10m_max"],
            "sunrise": d["sunrise"],
            "sunset": d["sunset"],
        }
        for d in fc.day_rows()
        if d["date"] >= today
    ]


def _next_hours(fc: Forecast, hours: int = 48) -> list[dict[str, Any]]:
    now = datetime.fromtimestamp(datetime.now(UTC).timestamp() + fc.utc_offset_s, UTC)
    start = now.replace(tzinfo=None, minute=0, second=0, microsecond=0).isoformat(
        timespec="minutes"
    )
    return [h for h in fc.hour_rows() if h["time"] >= start][:hours]


def _raw(fc: Forecast) -> dict[str, Any]:
    """The forecast in Open-Meteo's own shape, for the existing spray and disease tools."""
    return {"hourly": fc.hourly, "utc_offset_seconds": fc.utc_offset_s, "timezone": fc.timezone}


async def garden_extras(
    request: Request, cell: str, fc: Forecast, crops: list[Crop], garden: Garden
) -> dict[str, Any]:
    """Spray windows, the published disease-risk rules and season degree days for a garden."""
    extras: dict[str, Any] = {}
    spray = plan_spray(_raw(fc), None, bees=False, place="")
    extras["spray_window"] = spray.model_dump(mode="json")
    hours = parse_hours(_raw(fc), datetime.now(UTC))
    lat, lon = cell_center(cell)
    place = Place(label=f"grid cell {cell}", lat=lat, lon=lon)
    disease = {}
    for crop in crops:
        if crop.key in ("tomato", "potato") and hours:
            try:
                disease[crop.key] = summarise_conditions(hours, crop.key, place)
            except ConditionsError:
                pass
    extras["disease_risk"] = disease
    bases = sorted({c.gdd_base_c for c in crops if c.gdd_base_c is not None} | {GENERAL_GDD_BASE_C})
    try:
        extras["season_degree_days"] = {
            **await _climate(request).season_gdd(cell, bases),
            "sources": cite("usbr_gdd", "mrcc_gdd"),
        }
    except WeatherUnavailable as exc:
        log.warning("season degree days unavailable for %s: %s", cell, exc)
        extras["season_degree_days"] = None
    return extras


async def climate_view(
    request: Request, cell: str, crops: list[Crop] | None, garden: Garden | None = None
) -> dict[str, Any]:
    """Everything the climate engine knows about a cell. Never starts a climatology build."""
    climate = _climate(request)
    try:
        fc = await climate.forecast(cell)
    except WeatherUnavailable as exc:
        log.warning("forecast unavailable for %s: %s", cell, exc)
        raise HTTPException(503, "The weather service is unavailable. Try again later.") from exc
    try:
        alerts: dict[str, Any] = (await climate.alerts(cell)).model_dump()
    except AlertsUnavailable as exc:
        log.warning("NWS unavailable for %s: %s", cell, exc)
        alerts = {"covered": None, "alerts": [], "note": "NWS did not answer."}
    clim = await climate.climatology(cell)
    soil_c = soil_temperature_now(fc)
    lat, lon = cell_center(cell)
    return {
        "cell": cell,
        "cell_centre": {"lat": lat, "lon": lon},
        "timezone": fc.timezone,
        "climatology": (
            {"status": clim["status"], **clim["data"]}
            if clim and clim["status"] == "ready"
            else {"status": clim["status"] if clim else "not_requested"}
        ),
        "days": _day_list(fc),
        "risks": risks(fc),
        "alerts": alerts,
        "soil_temperature_6cm_c": soil_c,
        "sowing": sowing(soil_c, crops),
        "degree_days": [gdd_recent(fc, c) for c in (crops or [None])],
        "next_48_hours": _next_hours(fc),
        "attribution": {"text": ATTRIBUTION, "url": ATTRIBUTION_URL},
        **(await garden_extras(request, cell, fc, crops or [], garden) if garden else {}),
    }


@router.get("/api/gardens/{garden_id}/climate")
async def garden_climate(request: Request, garden_id: str, key: Key = None) -> dict[str, Any]:
    garden = await _open(request, garden_id, key)
    if not garden.cell:
        raise HTTPException(400, "This garden has no location yet.")
    plants = await _repo(request).list_plants(garden.id)
    crops = [c for p in plants if (c := find_crop(p.species, p.common_name))]
    unique = list({c.key: c for c in crops}.values()) or None  # none: show every crop
    return await climate_view(request, garden.cell, unique, garden)


@router.get("/api/climate")
async def public_climate(
    request: Request,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lon: Annotated[float, Query(ge=-180, le=180)],
) -> dict[str, Any]:
    """The climate engine for any point (the future API and MCP `weather_risk` tool use it)."""
    request.app.state.climate_limiter.check(_ip(request))
    return await climate_view(request, grid_cell(lat, lon), None)


class PlantLog(BaseModel):
    kind: Literal["watered", "too_wet", "too_dry", "just_right"]
    amount_l: float | None = Field(None, gt=0, le=1000)
    amount_mm: float | None = Field(None, gt=0, le=200)


@router.post("/api/gardens/{garden_id}/plants/{plant_id}/log")
async def log_plant(
    request: Request, garden_id: str, plant_id: str, body: PlantLog, key: Key = None
) -> dict:
    garden = await _open(request, garden_id, key)
    repo = _repo(request)
    if await repo.get_plant(garden.id, plant_id) is None:
        raise HTTPException(404, "No such plant in this garden.")
    kind = "watered" if body.kind == "watered" else "water_feedback"
    payload = body.model_dump(exclude_none=True)
    ctx = REQUEST.get()
    await repo.log_event(
        EventIn(
            garden_id=garden.id,
            plant_id=plant_id,
            kind=kind,
            payload=payload,
            trace_id=ctx.trace_id if ctx else "",
        )
    )
    return {"logged": body.kind}


WATER_LOOKBACK_S = 10 * 86400


@router.get("/api/gardens/{garden_id}/plants/{plant_id}/water", response_model=WaterAdvice)
async def plant_water(
    request: Request, garden_id: str, plant_id: str, key: Key = None
) -> WaterAdvice:
    garden = await _open(request, garden_id, key)
    repo = _repo(request)
    plant = await repo.get_plant(garden.id, plant_id)
    if plant is None:
        raise HTTPException(404, "No such plant in this garden.")
    reading = (await repo.latest_readings(garden.id, now_s() - FRESH_S)).get(plant.id)
    if is_fresh(reading, now_s()):  # the gardener's sensor beats any estimate
        return sensor_advice(reading)
    placement = Placement.from_container(plant.container)
    southern = (garden.lat or 0) < 0
    if plant.setting == "indoor":
        events = await repo.plant_events(
            garden.id, plant.id, ["watered", "water_feedback"], now_s() - 120 * 86400
        )
        tz = ZoneInfo(garden.timezone)
        watered = [e for e in events if e.kind == "watered"]
        last = datetime.fromtimestamp(watered[0].created_at, tz).date() if watered else None
        feedback = [str(e.payload.get("kind")) for e in events if e.kind == "water_feedback"]
        return indoor_advice(placement, datetime.now(tz).date(), southern, last, feedback)
    if not garden.cell:
        raise HTTPException(400, "Set the garden's location to plan watering outdoors.")
    try:
        fc = await _climate(request).forecast(garden.cell)
    except WeatherUnavailable as exc:
        raise HTTPException(503, "The weather service is unavailable. Try again later.") from exc
    events = await repo.plant_events(garden.id, plant.id, ["watered"], now_s() - WATER_LOOKBACK_S)
    waterings = [
        {
            "date": datetime.fromtimestamp(e.created_at + fc.utc_offset_s, UTC).date().isoformat(),
            **{k: v for k, v in e.payload.items() if k in ("amount_l", "amount_mm")},
        }
        for e in events
    ]
    crop = find_crop(plant.species, plant.common_name)
    return outdoor_advice(crop, plant.stage, placement, fc, waterings)


# ---------- knowledge graph and identify (M3) ----------


SPECIES_REGISTRATIONS_PER_DAY = 50  # new species rows a UTC day (each may later cost credits)


async def queue_species(
    request: Request, scientific_name: str, common_names: list[str]
) -> str | None:
    """Register a species and queue its profile (built once, shared). Only confirmed
    identifications and garden plants call this; anonymous lookups never do. Returns the key,
    or None when today's limit on new species is reached."""
    repo = _repo(request)
    key = (
        species_key(scientific_name, binomial=True)
        if scientific_name
        else species_key(common_names[0] if common_names else "")
    )
    if not key:
        return None
    if await repo.get_species(key) is None:
        now = now_s()
        if await repo.species_registered_since(now - now % 86400) >= SPECIES_REGISTRATIONS_PER_DAY:
            log.warning("species registration limit reached; %s not queued", key)
            return None
    if await repo.register_species(key, scientific_name, common_names):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M")
        await request.app.state.queue.send([species_job(key, stamp)])
    return key


@router.get("/api/species/{name}")
async def species(request: Request, name: str) -> dict[str, Any]:
    """A species profile with every fact's source. Read-only: profiles are built when a
    gardener confirms an identification or adds the plant to a garden."""
    request.app.state.species_limiter.check(_ip(request))
    if len(name) > 120:
        raise HTTPException(422, "That name is too long.")
    repo = _repo(request)
    for key in dict.fromkeys([species_key(name), species_key(name, binomial=True)]):
        row = await repo.get_species(key) if key else None
        if row is not None:
            return (
                profile_view(row)
                if row["status"] == "ready"
                else {"key": key, "status": row["status"]}
            )
    raise HTTPException(404, "Leaf has no profile for this plant yet.")


@router.post("/api/identify")
async def identify_plant(
    request: Request,
    image: Annotated[UploadFile, File()],
    notes: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    """Vision proposes, Super checks against sources; the gardener confirms with one tap."""
    from backend.app import MAX_UPLOAD_BYTES

    request.app.state.identify_limiter.check(_ip(request))
    data = await image.read(MAX_UPLOAD_BYTES + 1)
    return await identify_photo(request, data, notes)


async def identify_photo(request: Request, data: bytes, notes: str) -> dict[str, Any]:
    """Identify a plant from photo bytes (the web page, the API and MCP share this).
    The caller has already applied its own rate limit."""
    from backend.app import MAX_UPLOAD_BYTES, run_limited

    leaf = request.app.state.leaf
    if not leaf.vision_model:
        raise HTTPException(503, "Photo identification is unavailable right now.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That photo is too large (10 MB at most).")
    try:
        view_b64, _ = await asyncio.to_thread(prepare_image, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    trace: list[ModelCall] = []
    repo = _repo(request)
    use_search = await knowledge_budget.credits_left(repo) >= 1

    async def spent(credits: int) -> None:
        await knowledge_budget.record(repo, credits)

    result = await run_limited(
        request,
        lambda: identify(
            leaf.llm,
            leaf.settings,
            leaf.searcher,
            leaf.vision_model,
            view_b64,
            notes[:500],
            trace,
            use_search=use_search,
            on_credits=spent,
        ),
    )
    ident_id = new_id()
    ctx = REQUEST.get()
    await _repo(request).save_identification(
        ident_id, result["candidates"], result["features"], ctx.trace_id if ctx else ""
    )
    return {"id": ident_id, **result, "trace": [c.model_dump() for c in trace]}


class Confirm(BaseModel):
    scientific_name: str = Field(..., min_length=2, max_length=120)
    common_name: str = Field("", max_length=80)
    garden_id: str | None = Field(None, max_length=40)


@router.post("/api/identify/{ident_id}/confirm")
async def confirm_identification(
    request: Request, ident_id: str, body: Confirm, key: Key = None
) -> dict[str, Any]:
    """The one tap: the gardener's choice (one of the candidates, or their own name) becomes
    the plant's species, its profile is queued, and with a garden link it joins the garden."""
    repo = _repo(request)
    ident = await repo.get_identification(ident_id)
    if ident is None:
        raise HTTPException(404, "That identification has expired. Take the photo again.")
    garden = await _open(request, body.garden_id, key) if body.garden_id else None
    known = {c["key"]: c for c in ident["candidates"]}
    candidate = known.get(species_key(body.scientific_name, binomial=True))
    if candidate is not None:  # the model's canonical names, not whatever was sent
        scientific, common = candidate["scientific_name"], candidate["common_name"]
    else:  # typed by the gardener: kept as their name for it, not as a scientific name
        scientific, common = "", " ".join(body.scientific_name.split())[:120]
    chosen = species_key(scientific, binomial=True) if scientific else species_key(common)
    if not await repo.choose_identification(ident_id, chosen):
        raise HTTPException(409, "This identification was already confirmed.")
    skey = await queue_species(request, scientific, [common] if common else [])
    plant = None
    if garden is not None:
        plant = await repo.add_plant(garden.id, PlantIn(species=scientific, common_name=common))
        await repo.log_event(
            EventIn(
                garden_id=garden.id,
                plant_id=plant.id,
                kind="identified",
                ref=ident_id,
                payload={"species": skey or chosen, "from_candidates": candidate is not None},
            )
        )
    row = await repo.get_species(skey) if skey else None
    return {
        "species": skey or chosen,
        "profile_status": row["status"] if row else "not_queued",
        "from_candidates": candidate is not None,
        "plant": plant.model_dump() if plant else None,
    }


# ---------- Leaf Care (M4): Today, push, place search ----------


def _care_ctx(request: Request) -> CareContext:
    return CareContext(_repo(request), _climate(request), request.app.state.leaf)


@router.get("/api/gardens/{garden_id}/today")
async def today(request: Request, garden_id: str, key: Key = None) -> dict[str, Any]:
    """The garden's day: urgent alerts first, then every task with its reason, due time and
    source, and each plant's water status. Plans on demand (idempotent), so a new garden or a
    late change in the forecast shows at once."""
    return await today_view(request, await _open(request, garden_id, key))


async def today_view(request: Request, garden: Garden) -> dict[str, Any]:
    """Today for a garden (shared by the web page, the API and MCP)."""
    ctx = REQUEST.get()
    plan = await plan_garden(_care_ctx(request), garden, trace_id=ctx.trace_id if ctx else "")
    day = local_today(garden).isoformat()
    tasks = ordered(await _repo(request).list_tasks(garden.id, day, day))
    alerts: list[dict[str, Any]] = []
    if garden.cell:
        try:
            nws = await _climate(request).alerts(garden.cell)
            alerts = [a.model_dump() for a in nws.alerts]
        except AlertsUnavailable:
            pass
    season = await _repo(request).get_season_plan(garden.id)
    week: list[dict[str, Any]] = []
    if season is not None:
        this = monday(local_today(garden)).isoformat()
        week = [
            it
            for w in await _repo(request).list_plan_weeks(season.id, this, this)
            for it in w.items
        ]
    return {
        "garden": garden.model_dump(),
        "date": day,
        "season_week": week,
        "urgent": plan.urgent,
        "alerts": alerts,
        "tasks": [t.model_dump() for t in tasks],
        "water": plan.water,
        "attribution": {"text": ATTRIBUTION, "url": ATTRIBUTION_URL},
    }


# Push services the major browsers use. Leaf's server POSTs to a subscription's endpoint, so
# any other host is refused (otherwise a forged endpoint could aim requests anywhere).
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "push.services.mozilla.com",
    "web.push.apple.com",
    "notify.windows.com",
)


def push_host_ok(endpoint: str) -> bool:
    from urllib.parse import urlparse

    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


class PushKeys(BaseModel):
    p256dh: str = Field(..., min_length=20, max_length=200)
    auth: str = Field(..., min_length=8, max_length=100)


class PushSubscription(BaseModel):
    endpoint: str = Field(..., max_length=1000)
    keys: PushKeys


@router.get("/api/push/key")
async def push_key(request: Request) -> dict[str, Any]:
    s = request.app.state.leaf.settings
    enabled = bool(s.vapid_public_key and getattr(request.app.state, "pusher", None))
    return {"public_key": s.vapid_public_key, "enabled": enabled}


@router.post("/api/gardens/{garden_id}/push")
async def push_subscribe(
    request: Request, garden_id: str, body: PushSubscription, key: Key = None
) -> dict[str, Any]:
    garden = await _open(request, garden_id, key)
    if not push_host_ok(body.endpoint):
        raise HTTPException(422, "That is not a browser push service.")
    repo = _repo(request)
    await repo.add_push(garden.id, body.endpoint, body.keys.p256dh, body.keys.auth)
    settings = await repo.get_notifications(garden.id)
    if "push" not in settings.channels:
        settings.channels = [*settings.channels, "push"]
        await repo.put_notifications(garden.id, settings)
    return {"subscribed": True, "devices": len(await repo.list_push(garden.id))}


class PushEndpoint(BaseModel):
    endpoint: str = Field(..., max_length=1000)


@router.post("/api/gardens/{garden_id}/push/remove")
async def push_unsubscribe(
    request: Request, garden_id: str, body: PushEndpoint, key: Key = None
) -> dict[str, Any]:
    garden = await _open(request, garden_id, key)
    removed = await _repo(request).delete_push(garden.id, body.endpoint)
    return {"removed": removed}


@router.post("/api/gardens/{garden_id}/push/test")
async def push_test(request: Request, garden_id: str, key: Key = None) -> dict[str, Any]:
    """Send a test notification to this garden's devices (at most one a minute)."""
    garden = await _open(request, garden_id, key)
    minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
    status = await notify(
        _repo(request),
        request.app.state.queue,
        getattr(request.app.state, "pusher", None),
        garden,
        kind="test",
        title="Leaf notifications are on",
        body="This is how Leaf will tell you about frost, heat and today's garden tasks.",
        dedupe_key=f"test:{garden.id}:{minute}",
        urgent=True,
        respect_quiet=False,
    )
    return {"status": status}


@router.get("/api/gardens/{garden_id}/notifications/history")
async def notification_history(request: Request, garden_id: str, key: Key = None) -> list[dict]:
    garden = await _open(request, garden_id, key)
    return await _repo(request).list_notifications(garden.id)


@router.get("/api/geocode")
async def geocode(request: Request, q: Annotated[str, Query(min_length=2, max_length=80)]) -> dict:
    """A place name to coordinates for setting a garden's location (Open-Meteo geocoding)."""
    request.app.state.tool_limiter.check(_ip(request))
    weather = request.app.state.weather
    try:
        place = await weather.geocode(q)
    except ConditionsError as exc:
        raise HTTPException(502, "The place search is unavailable right now.") from exc
    if place is None:
        raise HTTPException(404, "Leaf could not find that place.")
    return {"label": place.label, "lat": place.lat, "lon": place.lon}


class Location(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


@router.post("/api/gardens/{garden_id}/location")
async def set_location(
    request: Request, garden_id: str, body: Location, key: Key = None
) -> dict[str, Any]:
    """Add or move the garden's location; its weather cell and climatology follow."""
    garden = await _open(request, garden_id, key)
    updated = await _repo(request).set_location(garden.id, body.lat, body.lon)
    job = JobMessage(
        kind="garden_ready",
        key=f"garden_ready:{garden.id}:{updated.cell}",
        payload={"garden_id": garden.id},
    )
    try:
        await request.app.state.queue.send([job])
    except DataError as exc:
        log.warning("could not queue garden_ready for %s: %s", garden.id, exc)
    return {"garden": updated.model_dump()}


# ---------- Season Plan (M5) ----------

SEASON_WRITES_PER_DAY = 3  # per garden; each plan costs about $0.21 in model calls
WRITING_STALE_S = 15 * 60  # a plan still 'writing' after this may be started again


async def season_view(request: Request, garden: Garden) -> dict[str, Any]:
    repo = _repo(request)
    plan = await repo.get_season_plan(garden.id)
    latest = await repo.latest_season_plan(garden.id)
    view: dict[str, Any] = {
        "garden": {"id": garden.id, "name": garden.name, "has_place": bool(garden.cell)},
        "this_week": monday(local_today(garden)).isoformat(),
        "plan": None,
        "pending": None,
        "weeks": [],
        "changes": [],
    }
    if latest is not None and latest.status != "ready":  # a write in progress, or failed
        view["pending"] = {"status": latest.status, "note": latest.note, "at": latest.created_at}
    if plan is None:
        return view
    view["plan"] = plan.model_dump(exclude={"trace_id"})
    view["plan"]["basis"].pop("outline", None)  # its items are already in the weeks
    view["weeks"] = [w.model_dump() for w in await repo.list_plan_weeks(plan.id)]
    view["changes"] = [
        {"at": e.created_at, **{k: e.payload.get(k) for k in ("trigger", "summary", "changes")}}
        for e in await repo.list_events(garden.id, 200)
        if e.kind == "replan" and e.payload.get("changes")
    ][:10]
    return view


@router.get("/api/gardens/{garden_id}/season")
async def season(request: Request, garden_id: str, key: Key = None) -> dict[str, Any]:
    """The garden's season plan: the current verified plan, every week with its items
    (reason, sources, cautions), what the checks removed and why, the latest replans, and a
    rewrite in progress if there is one. Nothing unverified is ever returned."""
    garden = await _open(request, garden_id, key)
    return await season_view(request, garden)


@router.post("/api/gardens/{garden_id}/season", status_code=202)
async def season_write(request: Request, garden_id: str, key: Key = None) -> dict[str, Any]:
    """Write (or rewrite) the season plan in the background: an outline, then a month at a
    time, then the code rules and Super. The current plan stays until the new one is ready.
    Poll GET .../season until "pending" is gone or failed."""
    garden = await _open(request, garden_id, key)
    request.app.state.tool_limiter.check(_ip(request))
    return await start_season(request, garden)


async def start_season(request: Request, garden: Garden) -> dict[str, Any]:
    """Start (or rewrite) a garden's season plan; shared by the web page, the API and MCP."""
    repo = _repo(request)
    latest = await repo.latest_season_plan(garden.id)
    busy = latest is not None and latest.status in ("writing", "unverified")
    if busy and now_s() - latest.updated_at < WRITING_STALE_S:
        return await season_view(request, garden)
    if await repo.count_events(garden.id, "season_plan", now_s() - 86400) >= SEASON_WRITES_PER_DAY:
        raise HTTPException(429, "Leaf writes a garden's plan at most 3 times a day. Try tomorrow.")
    day_cap = request.app.state.leaf.settings.season_daily_cap
    if await repo.count_events_all("season_plan", now_s() - 86400) >= day_cap:
        raise HTTPException(
            429,
            f"Leaf writes at most {day_cap} season plans a day across all gardens, to keep "
            "the day's budget for diagnoses. Please try again tomorrow.",
        )
    ctx = REQUEST.get()
    try:
        await start_plan(
            _care_ctx(request), request.app.state.queue, garden, ctx.trace_id if ctx else ""
        )
    except PlanUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    return await season_view(request, garden)


@router.get("/api/gardens/{garden_id}/season.ics")
async def season_calendar(request: Request, garden_id: str, key: Key = None) -> Response:
    """The verified plan as a calendar file: one all-day event per item, on its week's Monday."""
    garden = await _open(request, garden_id, key)
    repo = _repo(request)
    plan = await repo.get_season_plan(garden.id)
    if plan is None:
        raise HTTPException(404, "This garden has no finished season plan yet.")
    weeks = await repo.list_plan_weeks(plan.id)
    name = f"Leaf: {garden.name or 'my garden'}"
    settings = request.app.state.leaf.settings
    text = ics_calendar(
        calendar_events(weeks, name), name, settings.public_url or str(request.base_url), "/season"
    )
    return Response(
        text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leaf-season-plan.ics"'},
    )


class DiagnosisLink(BaseModel):
    case_id: str = Field(..., min_length=4, max_length=64)
    owner_token: str = Field(..., min_length=8, max_length=64)


@router.post("/api/gardens/{garden_id}/plants/{plant_id}/diagnosis")
async def link_case(
    request: Request, garden_id: str, plant_id: str, body: DiagnosisLink, key: Key = None
) -> dict[str, Any]:
    """Leaf Doctor joins the garden: the report's treatment steps (re-checked with this
    garden's choices) become today's tasks, a check-in is scheduled, and the season plan's
    next weeks are replanned. Needs the garden's key and the report's owner token."""
    garden = await _open(request, garden_id, key)
    repo = _repo(request)
    plant = await repo.get_plant(garden.id, plant_id)
    if plant is None or plant.status != "active":
        raise HTTPException(404, "That plant is not in this garden.")
    try:
        entry = await request.app.state.journal.get(body.case_id)
    except Exception as exc:  # noqa: BLE001 - storage down or misconfigured
        log.exception("could not read the journal")
        raise HTTPException(
            503, "Saved reports are unavailable right now. Try again later."
        ) from exc
    if entry is None or not secrets.compare_digest(body.owner_token, entry.owner_token):
        raise HTTPException(404, "No report at that link, or it is not yours.")
    ctx = REQUEST.get()
    try:
        return await link_diagnosis(
            _care_ctx(request),
            request.app.state.queue,
            garden,
            plant,
            entry.id,
            entry.report,
            ctx.trace_id if ctx else "",
        )
    except PlanUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
