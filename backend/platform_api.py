"""Platform endpoints: gardens (private links), the front door, usage, and the job runner.

A garden is opened with its private key in the `X-Leaf-Key` header (the page reads it from
the link's #k= fragment, which never reaches a server log). A wrong key and a missing garden
both answer 404, so ids cannot be probed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from backend.config import ROUTES
from backend.context import REQUEST
from backend.data.driver import DataError
from backend.data.repo import (
    Event,
    Garden,
    GardenIn,
    NotificationSettings,
    Plant,
    PlantIn,
    Repo,
    Task,
    TaskStatus,
)
from backend.jobs import JobMessage
from backend.router import REFUSALS, front_door
from backend.schemas import FrontDoorDecision, Intent

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
    return await repo.add_plant(garden.id, body)


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


def summarize_usage(raw: dict[str, list[dict[str, Any]]], guard: dict[str, Any]) -> dict[str, Any]:
    by_task: dict[str, dict[str, Any]] = {}
    models: dict[str, int] = {}
    total_cost, total_calls = 0.0, 0
    for r in raw["calls"]:
        t = by_task.setdefault(
            r["task"],
            {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "cost_usd": 0.0, "latency_ms_sum": 0.0, "models": {}},
        )  # fmt: skip
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
