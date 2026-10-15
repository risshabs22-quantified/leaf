"""The Leaf REST API, version 1. OpenAPI: /api/v1/openapi.json; a readable reference is on
/connect. Every call needs `Authorization: Bearer lf_...` (a key from the garden's Connect page).

Scopes: `read` (garden, Today, season plan, readings), `readings` (send soil moisture, for
Home Assistant), `tools` (diagnose, identify, weather risk, start a season plan). Calls that can
spend model credits (diagnose, identify, season plan) have a smaller daily budget per key.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

from backend.care.sensor import sensor_advice
from backend.care.service import CareContext, local_today, plan_garden
from backend.connect import tools
from backend.connect.auth import authenticate
from backend.data.repo import Garden, Plant, Reading

log = logging.getLogger("leaf.connect")

router = APIRouter(prefix="/api/v1", tags=["Leaf API v1"])

MAX_UPLOAD = 10 * 1024 * 1024
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing, invalid or revoked API key."},
    403: {"description": "The key does not have the scope this call needs."},
    429: {"description": "The key is over a limit; see the Retry-After header."},
}


class GardenInfo(BaseModel):
    garden: Garden
    plants: list[Plant]


@router.get("/garden", response_model=GardenInfo, responses=ERRORS, summary="The key's garden")
async def garden(request: Request) -> GardenInfo:
    """The garden this key acts for, with its active plants. Scope: `read`."""
    caller = await authenticate(request, "read")
    plants = await request.app.state.data.repo.list_plants(caller.garden.id)
    return GardenInfo(garden=caller.garden, plants=plants)


@router.get("/today", responses=ERRORS, summary="The garden's day")
async def today(request: Request) -> dict[str, Any]:
    """Urgent alerts, today's tasks (each with its reason and source), and each plant's water
    status. Planned on demand and safe to call often. Scope: `read`."""
    return await tools.today_tool(request, await authenticate(request, "read"))


@router.get("/season", responses=ERRORS, summary="The season plan")
async def season(request: Request) -> dict[str, Any]:
    """The current verified plan, week by week, with each item's reason, sources and cautions,
    what the checks removed, and the latest replans. `pending` shows a write in progress.
    Scope: `read`."""
    return await tools.season_tool(request, await authenticate(request, "read"))


@router.post("/season", status_code=202, responses=ERRORS, summary="Write the season plan")
async def season_write(request: Request) -> dict[str, Any]:
    """Start writing (or rewriting) the season plan in the background (about two minutes;
    poll `GET /season`). A garden may start three a day. Scope: `tools`; uses model credits."""
    caller = await authenticate(request, "tools", model=True)
    return await tools.season_start_tool(request, caller)


class ReadingIn(BaseModel):
    plant_id: str | None = Field(None, max_length=40, description="The plant's id.")
    plant: str | None = Field(
        None,
        max_length=120,
        description="Or the plant's name or nickname (it must match one plant exactly).",
    )
    value: float = Field(..., ge=0, le=100, description="Soil moisture, percent.")
    unit: Literal["%"] = "%"
    dry_below: float | None = Field(
        None,
        gt=0,
        lt=100,
        description="Water below this percent. Leaf's default is 30; sensors differ.",
    )
    test: bool = Field(
        False, description="Send true for a test reading. Leaf labels it TEST wherever it uses it."
    )
    source: str = Field("", max_length=80, description="Free text, e.g. the sensor's entity id.")


@router.post("/readings", responses=ERRORS, summary="Send a soil moisture reading")
async def post_reading(request: Request, body: ReadingIn) -> dict[str, Any]:
    """The Home Assistant webhook. A fresh reading (12 hours) replaces the weather-based water
    estimate for its plant: below the dry mark Leaf asks you to water, at or above it Leaf says
    no water is needed, and today's water task is rewritten at once. Scope: `readings`."""
    caller = await authenticate(request, "readings")
    return await record_reading(request, caller.garden, body, caller.key.id if caller.key else None)


async def record_reading(
    request: Request, garden: Garden, body: ReadingIn, key_id: str | None
) -> dict[str, Any]:
    """Store a reading, then rewrite today's water task from it (the web page's test button
    and the Home Assistant webhook share this)."""
    repo = request.app.state.data.repo
    plant = _pick_plant(await repo.list_plants(garden.id), body.plant_id, body.plant)
    await repo.add_reading(
        garden.id,
        plant.id,
        body.value,
        dry_below=body.dry_below,
        label="test" if body.test else "sensor",
        source=body.source,
        key_id=key_id,
    )
    latest = (await repo.latest_readings(garden.id, 0))[plant.id]
    advice = sensor_advice(latest)
    # Today's open water task was written before this reading: write it again from the sensor.
    await repo.drop_open_tasks(garden.id, plant.id, "water", local_today(garden).isoformat())
    ctx = CareContext(repo, request.app.state.climate, request.app.state.leaf)
    await plan_garden(ctx, garden)
    return {
        "stored": True,
        "plant_id": plant.id,
        "label": latest.label,
        "water": {"action": advice.action, "reason": advice.reason},
    }


def _pick_plant(plants: list[Plant], plant_id: str | None, name: str | None) -> Plant:
    if plant_id:
        found = next((p for p in plants if p.id == plant_id), None)
        if found is None:
            raise HTTPException(404, "That plant is not in this garden.")
        return found
    if not name:
        raise HTTPException(422, "Give plant_id, or the plant's name.")
    wanted = " ".join(name.lower().split())
    matches = [
        p
        for p in plants
        if wanted in (" ".join(x.lower().split()) for x in (p.nickname, p.common_name, p.species))
    ]
    if len(matches) != 1:
        raise HTTPException(
            404 if not matches else 409,
            "No plant has that name."
            if not matches
            else "More than one plant has that name; use plant_id.",
        )
    return matches[0]


@router.get("/readings", responses=ERRORS, summary="Recent soil moisture readings")
async def list_readings(
    request: Request,
    plant_id: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Reading]:
    """Readings sent in the last 14 days, newest first. Scope: `read`."""
    caller = await authenticate(request, "read")
    return await request.app.state.data.repo.list_readings(caller.garden.id, plant_id, limit)


async def _photo(image: UploadFile | None) -> bytes | None:
    return await image.read(MAX_UPLOAD + 1) if image is not None else None


@router.post("/diagnose", responses=ERRORS, summary="Diagnose a plant problem")
async def diagnose(
    request: Request,
    image: Annotated[UploadFile | None, File(description="A photo of the problem.")] = None,
    notes: Annotated[str, Form(max_length=2000, description="What you see.")] = "",
    region: Annotated[str, Form(max_length=100, description="A town, for local weather.")] = "",
    plant_id: Annotated[
        str | None,
        Form(max_length=40, description="Add the report to this plant: tasks and a replan."),
    ] = None,
) -> dict[str, Any]:
    """A photo and/or notes through Leaf's diagnosis (vision, Nemotron Ultra, Tavily sources,
    Nemotron Lightning plan), with the garden's organic and pet choices applied in code. Returns
    the verdict, hypotheses, plan, sources, a report link and the owner token for check-ins.
    Scope: `tools`; uses model credits."""
    caller = await authenticate(request, "tools", model=True)
    return await tools.diagnose_tool(
        request,
        caller,
        notes,
        await _photo(image),
        region,
        plant_id,
        include_token=True,
    )


@router.post("/identify", responses=ERRORS, summary="Identify a plant from a photo")
async def identify(
    request: Request,
    image: Annotated[UploadFile, File(description="A photo of the plant.")],
    notes: Annotated[str, Form(max_length=500)] = "",
) -> dict[str, Any]:
    """Candidates with the evidence for each; confirm one in the app. Scope: `tools`; uses
    model credits."""
    caller = await authenticate(request, "tools", model=True)
    data = await _photo(image)
    assert data is not None
    return await tools.identify_tool(request, caller, data, notes)


@router.get("/weather-risk", responses=ERRORS, summary="Frost, heat and wind risk")
async def weather_risk(
    request: Request,
    lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> dict[str, Any]:
    """Risk from the forecast and, in the US, NWS alerts, with the next five days. Without a
    point it uses the key's garden. Weather data by Open-Meteo.com (CC BY 4.0). Scope: `tools`."""
    caller = await authenticate(request, "tools")
    return await tools.weather_risk_tool(request, caller, lat, lon)


@router.get("/openapi.json", include_in_schema=False)
async def openapi_v1(request: Request) -> dict[str, Any]:
    """The OpenAPI document for this API only."""
    schema = get_openapi(
        title="Leaf API",
        version="1.0.0",
        summary="Leaf's plant doctor, care planner and season plan for your garden.",
        description=(
            "Authenticate with `Authorization: Bearer lf_...`. Make a key on your garden's "
            "Connect page (/connect). Keys have scopes (`read`, `readings`, `tools`) and "
            "limits per hour, per day and for calls that use Leaf's models. Every answer comes "
            "from code rules and cited sources; Leaf gives guidance, not certainty."
        ),
        routes=list(router.routes),  # this router only (its paths already carry the prefix)
    )
    schema["servers"] = [{"url": tools.base_url(request)}]
    schema.setdefault("components", {})["securitySchemes"] = {
        "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "lf_<key>"}
    }
    schema["security"] = [{"bearerAuth": []}]
    return schema
