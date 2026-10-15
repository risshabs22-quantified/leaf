"""Leaf as an MCP server (Streamable HTTP) at /mcp, for Claude, Cursor and other MCP clients.

Both protocol eras are served, from one endpoint, without sessions:
- 2025-03-26 to 2025-11-25: the client sends `initialize`, then `notifications/initialized`,
  then calls tools. Leaf answers with the version the client asked for.
- 2026-07-28 (current; stateless): no handshake. Every request carries its protocol version in
  `params._meta`, and the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers must match
  it. `server/discover` replaces `initialize`.

Discovery (`initialize`, `server/discover`, `tools/list`, `ping`) needs no key; it shows only
what the tools are. Every `tools/call` needs a Leaf API key (`Authorization: Bearer lf_...`),
whose scopes and limits apply as for the REST API. Replies are plain JSON (no event stream),
GET and DELETE are refused with 405, and a request with a foreign `Origin` is refused with 403.

Tools: diagnose, identify, care_plan, season_plan, weather_risk.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from backend.connect import tools
from backend.connect.auth import authenticate

log = logging.getLogger("leaf.mcp")

router = APIRouter(tags=["MCP"])

MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO = {"name": "leaf", "title": "Leaf plant doctor", "version": "1.0.0"}
INSTRUCTIONS = (
    "Leaf diagnoses plant problems from photos and notes, plans a garden's day from its weather "
    "and plants, and writes a season plan checked against frost dates and cited sources. "
    "Every answer names its sources; Leaf gives guidance, not certainty. diagnose and identify "
    "use model credits and take up to a minute."
)
MAX_B64 = 14 * 1024 * 1024  # characters; about 10 MB of image
SMALL_BODY = 64 * 1024  # without a valid key, a message may be this big (no images)
LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}

# JSON-RPC error codes: standard ones, plus the ones the 2026-07-28 spec defines.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
HEADER_MISMATCH, UNSUPPORTED_VERSION, UNAUTHORIZED, RATE_LIMITED = -32020, -32022, -32001, -32000


# ---------- tool definitions ----------


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        out["required"] = required
    return out


IMAGE = {
    "type": "string",
    "description": "A JPEG or PNG photo, base64 encoded (up to about 10 MB).",
}
PLANT = {"type": "string", "description": "A plant id from the garden (optional)."}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "diagnose",
        "title": "Diagnose a plant problem",
        "description": (
            "Diagnose a plant problem from a photo and/or a description. Returns the verdict "
            "(confident, likely, or send to a lab), the most likely causes with confidence, a "
            "care plan checked in code against the garden's organic and pet choices, and the "
            "web sources it rests on. With plant_id the report joins that plant: its steps become "
            "today's tasks and the season plan is updated. Uses model credits; takes up to a "
            "minute."
        ),
        "inputSchema": _schema(
            {
                "notes": {"type": "string", "description": "What you see on the plant."},
                "image_base64": IMAGE,
                "region": {"type": "string", "description": "A town, for local weather."},
                "plant_id": PLANT,
            }
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "identify",
        "title": "Identify a plant from a photo",
        "description": (
            "Name a plant from a photo: candidate species with the visible features that support "
            "each one and a check against cited sources. Uses model credits."
        ),
        "inputSchema": _schema(
            {"image_base64": IMAGE, "notes": {"type": "string"}}, required=["image_base64"]
        ),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "care_plan",
        "title": "Today's care plan",
        "description": (
            "The garden's care plan for today: urgent frost, heat, wind or flood warnings, "
            "tasks (watering, protection, feeding, pest checks) each with its reason and source, "
            "and each plant's water status."
        ),
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "season_plan",
        "title": "The season plan",
        "description": (
            "The garden's week-by-week plan for the next year (sowing, planting out, feeding, "
            "pruning, protection, harvest), checked against local frost dates and cited facts. "
            "With start=true it begins writing a new one (about two minutes, uses model credits; "
            "call again without start to read it)."
        ),
        "inputSchema": _schema({"start": {"type": "boolean", "default": False}}),
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "weather_risk",
        "title": "Frost, heat and wind risk",
        "description": (
            "Frost, heat and wind risk for a place from the forecast, National Weather Service "
            "alerts (US) and the next five days. Without lat and lon it uses the garden's "
            "place. Weather data by Open-Meteo.com."
        ),
        "inputSchema": _schema(
            {
                "lat": {"type": "number", "minimum": -90, "maximum": 90},
                "lon": {"type": "number", "minimum": -180, "maximum": 180},
            }
        ),
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]


class DiagnoseArgs(BaseModel):
    notes: str = Field("", max_length=2000)
    image_base64: str = Field("", max_length=MAX_B64)
    region: str = Field("", max_length=100)
    plant_id: str | None = Field(None, max_length=40)


class IdentifyArgs(BaseModel):
    image_base64: str = Field(..., min_length=1, max_length=MAX_B64)
    notes: str = Field("", max_length=500)


class SeasonArgs(BaseModel):
    start: bool = False


class WeatherArgs(BaseModel):
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


def decode_image(text: str) -> bytes | None:
    if not text:
        return None
    if text.startswith("data:"):
        text = text.partition(",")[2]
    text = "".join(text.split())  # some clients wrap base64 in line breaks
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "image_base64 is not valid base64.") from exc


async def run_tool(request: Request, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Authenticate for the tool (scope and limits as in the REST API) and run it."""
    if name == "diagnose":
        a = DiagnoseArgs.model_validate(args)
        caller = await authenticate(request, "tools", model=True)
        data = decode_image(a.image_base64)
        return await tools.diagnose_tool(request, caller, a.notes, data, a.region, a.plant_id)
    if name == "identify":
        a2 = IdentifyArgs.model_validate(args)
        caller = await authenticate(request, "tools", model=True)
        data = decode_image(a2.image_base64)
        assert data is not None
        return await tools.identify_tool(request, caller, data, a2.notes)
    if name == "care_plan":
        return await tools.today_tool(request, await authenticate(request, "read"))
    if name == "season_plan":
        a3 = SeasonArgs.model_validate(args)
        if a3.start:
            caller = await authenticate(request, "tools", model=True)
            return await tools.season_start_tool(request, caller)
        return await tools.season_tool(request, await authenticate(request, "read"))
    a4 = WeatherArgs.model_validate(args)
    return await tools.weather_risk_tool(
        request, await authenticate(request, "tools"), a4.lat, a4.lon
    )


# ---------- the endpoint ----------


def rpc_error(
    rid: Any,
    code: int,
    message: str,
    status: int = 200,
    headers: dict[str, str] | None = None,
    data: Any = None,
) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "error": err}, status_code=status, headers=headers
    )


def origin_ok(request: Request) -> bool:
    """A browser page on another site must not drive Leaf with a visitor's key (DNS rebinding).
    Requests without an Origin (every non-browser MCP client) pass."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    ours: set[str] = set()
    public = request.app.state.leaf.settings.public_url
    if public:
        ours.add(urlparse(public).netloc.lower())
    # Local development: loopback origins on a loopback host. The Host header is not trusted
    # otherwise, because under DNS rebinding it matches the attacker's own Origin.
    if (parsed.hostname or "") in {h.strip("[]") for h in LOOPBACK}:
        host = request.headers.get("host", "").lower().rsplit(":", 1)[0].strip("[]")
        if host in {h.strip("[]") for h in LOOPBACK}:
            return True
    return parsed.netloc.lower() in ours


@router.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
async def mcp_no_stream(request: Request) -> Response:
    """Leaf offers no event stream and keeps no sessions."""
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request) -> Response:
    if not origin_ok(request):
        return rpc_error(None, INVALID_REQUEST, "Origin not allowed.", status=403)
    big = int(request.headers.get("content-length") or 0) > SMALL_BODY
    if big:  # images are for callers with a valid key; others get no large reads or parsing
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        key = (
            await request.app.state.data.repo.api_key_by_secret(token.strip())
            if scheme.lower() == "bearer"
            else None
        )
        if key is None:
            return rpc_error(None, INVALID_REQUEST, "That request is too large.", status=413)
    try:
        body = json.loads(await request.body())
    except ValueError:
        return rpc_error(None, PARSE_ERROR, "Parse error: the body is not JSON.", status=400)
    if not isinstance(body, dict):
        return rpc_error(
            None, INVALID_REQUEST, "Send one JSON-RPC message per request.", status=400
        )
    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    if not isinstance(method, str):
        return Response(status_code=202)  # a response from the client, or malformed: ignore
    if "id" not in body:  # a notification (e.g. notifications/initialized)
        return Response(status_code=202)

    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(META_VERSION)
    header_version = request.headers.get("mcp-protocol-version", "")
    if version is None and header_version == MODERN and method != "initialize":
        return rpc_error(rid, INVALID_PARAMS, f"Missing params._meta['{META_VERSION}']", status=400)
    if version is None and header_version and header_version not in SUPPORTED:
        return rpc_error(
            rid,
            UNSUPPORTED_VERSION,
            "Unsupported protocol version",
            status=400,
            data={"supported": list(SUPPORTED), "requested": header_version},
        )
    modern = method != "initialize" and version is not None
    if modern:
        if version not in SUPPORTED:
            return rpc_error(
                rid,
                UNSUPPORTED_VERSION,
                "Unsupported protocol version",
                status=400,
                data={"supported": list(SUPPORTED), "requested": version},
            )
        header_method = request.headers.get("mcp-method", "")
        header_name = request.headers.get("mcp-name", "")
        if version == MODERN:
            problems = []
            if header_version != version:
                problems.append(f"MCP-Protocol-Version header '{header_version}' != '{version}'")
            if header_method != method:
                problems.append(f"Mcp-Method header '{header_method}' != '{method}'")
            if method == "tools/call" and header_name != params.get("name"):
                problems.append(f"Mcp-Name header '{header_name}' != '{params.get('name')}'")
            if problems:
                return rpc_error(
                    rid, HEADER_MISMATCH, "Header mismatch: " + "; ".join(problems), status=400
                )
    current = version == MODERN
    bad = 400 if modern else 200

    def done(result: dict[str, Any], cacheable: bool = False) -> JSONResponse:
        if current:
            result = {"resultType": "complete", **result}
            if cacheable:
                result |= {"ttlMs": 300_000, "cacheScope": "public"}
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})

    if method == "initialize":
        asked = params.get("protocolVersion")
        chosen = asked if asked in SUPPORTED and asked != MODERN else LEGACY[0]
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": chosen,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                },
            }
        )
    if method == "server/discover":
        return done(
            {
                "supportedVersions": list(SUPPORTED),
                "capabilities": {"tools": {}},
                "instructions": INSTRUCTIONS,
                "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
            },
            cacheable=True,
        )
    if method == "ping":
        return done({})
    if method == "tools/list":
        return done({"tools": TOOLS}, cacheable=True)
    if method != "tools/call":
        return rpc_error(rid, METHOD_NOT_FOUND, f"Method not found: {method}", status=bad)

    name = params.get("name")
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    if name not in {t["name"] for t in TOOLS}:
        return rpc_error(rid, INVALID_PARAMS, f"Unknown tool: {name}", status=bad)
    try:
        result = await run_tool(request, name, args)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return rpc_error(
            rid,
            INVALID_PARAMS,
            f"Invalid arguments for tool {name}: {where}: {first['msg']}",
            status=bad,
        )
    except HTTPException as exc:
        if exc.status_code in (401, 403, 429):  # the key's problem: an HTTP error, not a result
            return rpc_error(
                rid,
                UNAUTHORIZED if exc.status_code != 429 else RATE_LIMITED,
                str(exc.detail),
                status=exc.status_code,
                headers=exc.headers,
            )
        return done({"content": [{"type": "text", "text": str(exc.detail)}], "isError": True})
    except Exception:  # noqa: BLE001
        log.exception("mcp tool %s failed", name)
        return done(
            {
                "content": [{"type": "text", "text": "Leaf could not complete that. Try again."}],
                "isError": True,
            }
        )
    text = json.dumps(result, ensure_ascii=False, default=str)
    return done(
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": json.loads(text),
            "isError": False,
        }
    )
