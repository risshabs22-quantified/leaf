"""The garden's own controls for Connect (garden key, from the Connect page): API keys and a
test soil-moisture reading."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.connect.api_v1 import ReadingIn, record_reading
from backend.connect.auth import MAX_KEYS_PER_GARDEN, SCOPES
from backend.connect.tools import base_url
from backend.data.repo import ApiKey, Reading
from backend.platform_api import Key, _open

router = APIRouter(tags=["Connect"])

LIMIT_MAX = {"per_hour": 600, "per_day": 5000, "model_per_day": 50}


class KeyIn(BaseModel):
    label: str = Field("", max_length=60)
    scopes: list[str] = Field(default_factory=lambda: ["read"], min_length=1, max_length=3)
    per_hour: int = Field(120, ge=1, le=LIMIT_MAX["per_hour"])
    per_day: int = Field(1000, ge=1, le=LIMIT_MAX["per_day"])
    model_per_day: int = Field(20, ge=0, le=LIMIT_MAX["model_per_day"])


class KeyCreated(ApiKey):
    key: str  # the secret: shown once, never stored


@router.get("/api/connect/status")
async def status(request: Request) -> dict[str, Any]:
    """What this Leaf offers (no secrets)."""
    base = base_url(request)
    return {
        "api": {"base": f"{base}/api/v1", "openapi": f"{base}/api/v1/openapi.json"},
        "mcp": {"url": f"{base}/mcp"},
        "scopes": list(SCOPES),
        "limits": {
            "max_keys": MAX_KEYS_PER_GARDEN,
            **{f"max_{k}": v for k, v in LIMIT_MAX.items()},
        },
    }


@router.post("/api/gardens/{garden_id}/api-keys", response_model=KeyCreated)
async def create_key(request: Request, garden_id: str, body: KeyIn, key: Key = None) -> KeyCreated:
    """Make an API key for this garden. The secret is in the answer once; keep it."""
    garden = await _open(request, garden_id, key)
    unknown = [s for s in body.scopes if s not in SCOPES]
    if unknown:
        raise HTTPException(422, f"Unknown scope: {unknown[0]}. Scopes are {', '.join(SCOPES)}.")
    repo = request.app.state.data.repo
    if len(await repo.list_api_keys(garden.id)) >= MAX_KEYS_PER_GARDEN:
        raise HTTPException(
            409, f"A garden can have {MAX_KEYS_PER_GARDEN} API keys. Revoke one first."
        )
    scopes = sorted(set(body.scopes), key=SCOPES.index)
    given = body.model_fields_set
    # A Home Assistant key posts on every state change, so it starts with room for that.
    only_readings = scopes == ["readings"]
    per_hour = body.per_hour if "per_hour" in given or not only_readings else 600
    per_day = body.per_day if "per_day" in given or not only_readings else 5000
    made, secret = await repo.create_api_key(
        garden.id, body.label, scopes, per_hour, per_day, body.model_per_day
    )
    return KeyCreated(**made.model_dump(), key=secret)


@router.get("/api/gardens/{garden_id}/api-keys", response_model=list[ApiKey])
async def list_keys(request: Request, garden_id: str, key: Key = None) -> list[ApiKey]:
    garden = await _open(request, garden_id, key)
    return await request.app.state.data.repo.list_api_keys(garden.id)


@router.delete("/api/gardens/{garden_id}/api-keys/{key_id}")
async def revoke_key(
    request: Request, garden_id: str, key_id: str, key: Key = None
) -> dict[str, bool]:
    garden = await _open(request, garden_id, key)
    if not await request.app.state.data.repo.revoke_api_key(garden.id, key_id):
        raise HTTPException(404, "No such key.")
    return {"revoked": True}


@router.post("/api/gardens/{garden_id}/readings")
async def send_reading(
    request: Request, garden_id: str, body: ReadingIn, key: Key = None
) -> dict[str, Any]:
    """Send a TEST reading with the garden key (the Connect page's test button; always stored
    as a test, whatever the body says). Home Assistant uses `POST /api/v1/readings` with a
    `readings` API key instead."""
    garden = await _open(request, garden_id, key)
    # A value typed on a web page is never a sensor reading: it is always stored as a test.
    return await record_reading(request, garden, body.model_copy(update={"test": True}), None)


@router.get("/api/gardens/{garden_id}/readings", response_model=list[Reading])
async def readings(request: Request, garden_id: str, key: Key = None) -> list[Reading]:
    garden = await _open(request, garden_id, key)
    return await request.app.state.data.repo.list_readings(garden.id)
