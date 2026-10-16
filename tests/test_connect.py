"""M6 Connect: API keys and limits, the REST API, Home Assistant readings and MCP."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import RateLimiter, app
from backend.care import PlantState, plan_day
from backend.care.sensor import DEFAULT_DRY_BELOW, FRESH_S, is_fresh, sensor_advice
from backend.climate.nws import Alerts
from backend.config import Settings
from backend.data.repo import Reading
from backend.journal import MemoryJournal
from tests.conftest import make_leaf
from tests.test_agent import RESP
from tests.test_care import NOON, TODAY, garden, plant
from tests.test_climate import fake_forecast


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (40, 120, 40)).save(buf, format="PNG")
    return buf.getvalue()


class FakeClimate:
    repo = None

    async def forecast(self, cell, now=None):
        return fake_forecast(datetime.now(UTC).date())

    async def alerts(self, cell):
        return Alerts(covered=True, alerts=[])

    async def season_gdd(self, cell, bases):
        return {"by_base_c": {}}

    async def climatology(self, cell):
        return None


class Q:
    def __init__(self):
        self.sent = []

    async def send(self, jobs, delay_s=0):
        self.sent += list(jobs)


@contextlib.contextmanager
def api(settings, responses=None):
    s = Settings(
        nebius_api_key="test",
        tavily_api_key="test",
        vision_model="vendor/fake-vl",
        deep_blend_first=0.0,
        public_url="https://leaf.test",
    )
    leaf, llm, _ = make_leaf(s, {**RESP, **(responses or {})})
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        app.state.climate = FakeClimate()
        app.state.queue = Q()
        app.state.llm_for_tests = llm
        yield client
    app.state.leaf = app.state.deep = app.state.journal = app.state.climate = None


def new_garden(client, place=True, plants=("tomato",)):
    body = {"name": "Plot", "timezone": "UTC"} | ({"lat": 44.9, "lon": -93.2} if place else {})
    made = client.post("/api/gardens", json=body).json()
    gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
    ids = [
        client.post(f"/api/gardens/{gid}/plants", headers=h, json={"common_name": p}).json()["id"]
        for p in plants
    ]
    return gid, h, ids


def make_key(client, gid, h, **body):
    r = client.post(f"/api/gardens/{gid}/api-keys", headers=h, json=body or {"label": "test"})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(key):
    return {"Authorization": f"Bearer {key['key']}"}


def db(client, fn, *args):
    return client.portal.call(fn, *args)


# ---------- API keys and limits ----------


def test_keys_are_shown_once_stored_hashed_scoped_and_revocable(settings):
    with api(settings) as client:
        gid, h, _ = new_garden(client)
        made = make_key(client, gid, h, label="phone", scopes=["read", "tools"])
        assert made["key"].startswith("lf_") and made["prefix"] == made["key"][:9]
        assert made["scopes"] == ["read", "tools"] and made["per_hour"] == 120
        listed = client.get(f"/api/gardens/{gid}/api-keys", headers=h).json()
        assert [k["id"] for k in listed] == [made["id"]] and "key" not in listed[0]
        assert made["key"] not in json.dumps(listed)
        stored = db(client, app.state.data.db.batch, [("SELECT key_hash FROM api_keys", ())])
        assert made["key"] not in json.dumps(stored[0].rows)  # only its hash is kept
        assert client.get("/api/v1/garden").status_code == 401
        no_key = client.get("/api/v1/garden")
        assert no_key.headers["www-authenticate"].startswith("Bearer")
        assert (
            client.get("/api/v1/garden", headers={"Authorization": "Bearer lf_nope"}).status_code
            == 401
        )
        assert client.get("/api/v1/garden", headers=bearer(made)).json()["garden"]["id"] == gid
        # A key acts for its garden only, within its scopes.
        only_readings = make_key(client, gid, h, label="ha", scopes=["readings"])
        assert client.get("/api/v1/today", headers=bearer(only_readings)).status_code == 403
        assert (
            client.delete(f"/api/gardens/{gid}/api-keys/{made['id']}", headers=h).status_code == 200
        )
        assert client.get("/api/v1/garden", headers=bearer(made)).status_code == 401
        # The garden key and another garden's key cannot manage these keys.
        gid2, h2, _ = new_garden(client)
        assert client.get(f"/api/gardens/{gid}/api-keys", headers=h2).status_code == 404
        assert client.get(f"/api/gardens/{gid}/api-keys").status_code == 404


def test_key_creation_rules(settings):
    with api(settings) as client:
        gid, h, _ = new_garden(client)
        url = f"/api/gardens/{gid}/api-keys"
        assert client.post(url, headers=h, json={"scopes": ["admin"]}).status_code == 422
        assert client.post(url, headers=h, json={"per_hour": 100000}).status_code == 422
        assert client.post(url, headers=h, json={"scopes": []}).status_code == 422
        for i in range(5):
            make_key(client, gid, h, label=str(i))
        assert client.post(url, headers=h, json={"label": "six"}).status_code == 409


def test_per_key_limits_hour_and_model_budget(settings):
    with api(settings) as client:
        gid, h, _ = new_garden(client)
        slow = make_key(client, gid, h, label="slow", scopes=["read"], per_hour=2)
        codes = [client.get("/api/v1/garden", headers=bearer(slow)).status_code for _ in range(4)]
        assert codes == [200, 200, 429, 429]
        limited = client.get("/api/v1/garden", headers=bearer(slow))
        assert 1 <= int(limited.headers["retry-after"]) <= 3600
        # The model budget is its own: one model call a day, then 429, while reads still work.
        tools_key = make_key(client, gid, h, label="m", scopes=["read", "tools"], model_per_day=1)
        first = client.post("/api/v1/diagnose", headers=bearer(tools_key), data={"notes": "spots"})
        second = client.post("/api/v1/diagnose", headers=bearer(tools_key), data={"notes": "spots"})
        assert first.status_code == 200 and second.status_code == 429
        assert "use Leaf's models" in second.json()["detail"]
        assert client.get("/api/v1/garden", headers=bearer(tools_key)).status_code == 200


def test_wrong_keys_are_throttled_by_address(settings):
    with api(settings) as client:
        app.state.garden_use_limiter = RateLimiter(per_ip_per_hour=3, daily_cap=1000)
        codes = [
            client.get(
                "/api/v1/garden", headers={"Authorization": f"Bearer lf_guess{i}"}
            ).status_code
            for i in range(5)
        ]
        assert codes == [401, 401, 401, 429, 429]


# ---------- the REST API ----------


def test_rest_reads_openapi_and_diagnose(settings):
    with api(settings) as client:
        gid, h, (pid,) = new_garden(client)
        key = make_key(client, gid, h, label="all", scopes=["read", "tools"])
        b = bearer(key)
        today = client.get("/api/v1/today", headers=b).json()
        assert today["tasks"] and today["water"] and "attribution" in today
        assert client.get("/api/v1/season", headers=b).json()["plan"] is None
        wr = client.get("/api/v1/weather-risk", headers=b)
        assert wr.status_code == 200 and {"risks", "alerts", "days"} <= set(wr.json())
        assert client.get("/api/v1/weather-risk?lat=95&lon=0", headers=b).status_code == 422
        # Diagnose: the garden's choices apply, the owner can check in, the report is linked.
        r = client.post(
            "/api/v1/diagnose", headers=b, data={"notes": "brown spots", "plant_id": pid}
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["case_id"] and d["owner_token"] and d["report_url"].endswith(f"/c/{d['case_id']}")
        assert d["plan"]["steps"] and d["sources"] and d["diagnosis"][0]["confidence"] <= 1
        assert d["garden_link"]["tasks_added"] >= 1
        assert client.post("/api/v1/diagnose", headers=b, data={}).status_code == 400
        bad = client.post("/api/v1/diagnose", headers=b, data={"notes": "x", "plant_id": "nope"})
        assert bad.status_code == 404
        assert (
            client.post(
                "/api/v1/identify", headers=b, files={"image": ("x.png", b"junk")}
            ).status_code
            == 400
        )
        # The OpenAPI document covers this API only, with a bearer scheme.
        spec = client.get("/api/v1/openapi.json").json()
        assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
        assert all(p.startswith("/api/v1/") for p in spec["paths"])
        assert {"/api/v1/today", "/api/v1/readings", "/api/v1/diagnose"} <= set(spec["paths"])
        assert client.get("/connect").status_code == 200


# ---------- Home Assistant readings ----------


def post_reading(client, key, **body):
    return client.post("/api/v1/readings", headers=bearer(key), json=body)


def water_state(client, gid, h):
    d = client.get(f"/api/gardens/{gid}/today", headers=h).json()
    open_water = [t for t in d["tasks"] if t["kind"] == "water" and t["status"] == "open"]
    return d["water"][0], open_water


def test_a_sensor_reading_replaces_the_weather_estimate_and_is_labelled(settings):
    with api(settings) as client:
        gid, h, (pid, _basil) = new_garden(client, plants=("tomato", "basil"))
        key = make_key(client, gid, h, label="ha", scopes=["readings", "read"])
        first, open_water = water_state(client, gid, h)
        assert first["action"] == "water" and len(open_water) == 2  # the weather estimate
        r = post_reading(client, key, plant_id=pid, value=55, source="sensor.tomato_moisture")
        assert r.status_code == 200 and r.json()["water"]["action"] == "skip"
        assert r.json()["label"] == "sensor"
        d = client.get(f"/api/gardens/{gid}/today", headers=h).json()
        tomato = next(w for w in d["water"] if w["plant_id"] == pid)
        assert tomato["model"] == "sensor" and tomato["action"] == "skip"
        assert "Sensor reading" in tomato["reason"] and "55" in tomato["reason"]
        assert [t["plant_id"] for t in d["tasks"] if t["kind"] == "water"] == [
            p["plant_id"] for p in d["water"] if p["plant_id"] != pid
        ]  # the tomato's open water task is gone; the basil still follows the weather
        # Dry soil, sent as a test: water, and Leaf says it is a TEST wherever it uses it.
        t = post_reading(client, key, plant="tomato", value=12, test=True)
        assert t.status_code == 200 and t.json()["label"] == "test"
        d = client.get(f"/api/gardens/{gid}/today", headers=h).json()
        task = next(t for t in d["tasks"] if t["kind"] == "water" and t["plant_id"] == pid)
        assert "TEST reading" in task["reason"] and "TEST" in task["source"]["title"]
        assert task["source"]["heuristic"] is True
        assert (
            client.get(f"/api/gardens/{gid}/plants/{pid}/water", headers=h).json()["model"]
            == "sensor"
        )
        # A custom dry mark; a done task is never rewritten.
        post_reading(client, key, plant_id=pid, value=45, dry_below=50)
        assert water_state(client, gid, h)[0]["action"] == "water"
        listed = client.get("/api/v1/readings", headers=bearer(key)).json()
        assert [x["value"] for x in listed][:3] == [45, 12, 55] and listed[1]["label"] == "test"
        assert client.get(f"/api/gardens/{gid}/readings", headers=h).status_code == 200


def test_reading_validation(settings):
    with api(settings) as client:
        gid, h, (pid, _second) = new_garden(client, plants=("tomato", "tomato"))
        key = make_key(client, gid, h, label="ha", scopes=["readings"])
        assert post_reading(client, key, plant_id=pid, value=101).status_code == 422
        assert post_reading(client, key, plant_id=pid, value=-1).status_code == 422
        assert post_reading(client, key, plant_id=pid, value=5, unit="kPa").status_code == 422
        assert post_reading(client, key, value=5).status_code == 422  # which plant?
        assert post_reading(client, key, plant="tomato", value=5).status_code == 409  # two match
        assert post_reading(client, key, plant="rose", value=5).status_code == 404
        assert post_reading(client, key, plant_id="x", value=5).status_code == 404
        # Another garden's plant is not reachable with this key.
        gid2, h2, (other,) = new_garden(client)
        assert post_reading(client, key, plant_id=other, value=5).status_code == 404
        # The web page's test button uses the garden key.
        r = client.post(
            f"/api/gardens/{gid}/readings",
            headers=h,
            json={"plant_id": pid, "value": 5, "test": True},
        )
        assert r.status_code == 200 and r.json()["water"]["action"] == "water"


def reading(value, age_s=0, **kw):
    return Reading(
        id=1,
        garden_id="g",
        plant_id="p-tomato",
        value=value,
        created_at=int(NOON.timestamp()) - age_s,
        **kw,
    )


def test_sensor_advice_freshness_and_the_dry_mark():
    assert sensor_advice(reading(DEFAULT_DRY_BELOW - 0.1)).action == "water"
    assert sensor_advice(reading(DEFAULT_DRY_BELOW)).action == "skip"
    assert sensor_advice(reading(40, dry_below=45)).action == "water"
    adv = sensor_advice(reading(10, label="test"))
    assert "TEST reading" in adv.reason and "TEST" in adv.sources[0]["title"]
    assert is_fresh(reading(10, age_s=FRESH_S), NOON.timestamp())
    assert not is_fresh(reading(10, age_s=FRESH_S + 1), NOON.timestamp())
    assert not is_fresh(None, NOON.timestamp())
    states = [PlantState(plant("tomato"), reading=reading(10, age_s=3600))]
    p = plan_day(
        garden(), states, fake_forecast(TODAY, rain=lambda i: 20.0), None, None, TODAY, NOON
    )
    assert [t.kind for t in p.tasks] == ["water"] and "Sensor reading" in p.tasks[0].reason
    stale = [PlantState(plant("tomato"), reading=reading(10, age_s=FRESH_S + 60))]
    p = plan_day(
        garden(), stale, fake_forecast(TODAY, rain=lambda i: 20.0), None, None, TODAY, NOON
    )
    assert p.tasks == [] and p.water[0]["model"] == "fao56"  # the estimate takes over again
    # A sensor works indoors and with no forecast at all.
    indoor = [PlantState(plant("monstera", "indoor"), reading=reading(50, age_s=60))]
    p = plan_day(garden(), indoor, None, None, None, TODAY, NOON)
    assert p.water[0]["model"] == "sensor" and p.water[0]["action"] == "skip"


# ---------- MCP ----------

MCP_H = {"Accept": "application/json, text/event-stream"}


def rpc(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})}


def modern(method, params=None, rid=1):
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    return rpc(method, {"_meta": meta, **(params or {})}, rid)


def modern_headers(method, name=None):
    h = {**MCP_H, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    return h | ({"Mcp-Name": name} if name else {})


def test_mcp_legacy_handshake_discovery_and_tools(settings):
    with api(settings) as client:
        gid, h, (pid,) = new_garden(client)
        key = make_key(client, gid, h, label="mcp", scopes=["read", "tools"])
        init = client.post(
            "/mcp",
            headers=MCP_H,
            json=rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "c", "version": "1"},
                },
            ),
        ).json()
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert init["result"]["serverInfo"]["name"] == "leaf"
        newest = client.post(
            "/mcp", headers=MCP_H, json=rpc("initialize", {"protocolVersion": "1999-01-01"})
        ).json()
        assert newest["result"]["protocolVersion"] == "2025-11-25"
        note = client.post(
            "/mcp", headers=MCP_H, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert note.status_code == 202 and note.content == b""
        listed = client.post("/mcp", headers=MCP_H, json=rpc("tools/list", rid=2)).json()["result"]
        assert [t["name"] for t in listed["tools"]] == [
            "diagnose",
            "identify",
            "care_plan",
            "season_plan",
            "weather_risk",
        ]
        assert all(
            t["inputSchema"]["type"] == "object" and t["description"] for t in listed["tools"]
        )
        assert "resultType" not in listed  # legacy shape
        assert client.post("/mcp", headers=MCP_H, json=rpc("ping", rid=3)).json()["result"] == {}
        # Tools need a key.
        call = rpc("tools/call", {"name": "care_plan", "arguments": {}}, rid=4)
        denied = client.post("/mcp", headers=MCP_H, json=call)
        assert denied.status_code == 401 and denied.json()["error"]["code"] == -32001
        assert denied.headers["www-authenticate"].startswith("Bearer")
        ok = client.post("/mcp", headers={**MCP_H, **bearer(key)}, json=call).json()["result"]
        assert ok["isError"] is False and ok["structuredContent"]["tasks"]
        assert json.loads(ok["content"][0]["text"]) == ok["structuredContent"]
        # diagnose: the owner token never goes into a model's context.
        diag = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(key)},
            json=rpc(
                "tools/call",
                {"name": "diagnose", "arguments": {"notes": "brown spots", "plant_id": pid}},
                rid=5,
            ),
        ).json()["result"]
        sc = diag["structuredContent"]
        assert diag["isError"] is False and sc["plan"]["steps"] and "owner_token" not in sc
        assert sc["garden_link"]["tasks_added"] >= 1
        # Errors: unknown tool and bad arguments are protocol errors; failures are tool errors.
        unknown = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(key)},
            json=rpc("tools/call", {"name": "nope"}, rid=6),
        ).json()
        assert unknown["error"]["code"] == -32602 and "Unknown tool" in unknown["error"]["message"]
        invalid = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(key)},
            json=rpc("tools/call", {"name": "weather_risk", "arguments": {"lat": 500}}, rid=7),
        ).json()
        assert invalid["error"]["code"] == -32602 and "lat" in invalid["error"]["message"]
        b64 = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(key)},
            json=rpc(
                "tools/call",
                {"name": "identify", "arguments": {"image_base64": "not base64!!"}},
                rid=8,
            ),
        ).json()["result"]
        assert b64["isError"] is True and "base64" in b64["content"][0]["text"]
        method = client.post("/mcp", headers=MCP_H, json=rpc("resources/list", rid=9))
        assert method.status_code == 200 and method.json()["error"]["code"] == -32601
        # A key without the scope gets 403; a read-only key cannot start a plan.
        ro = make_key(client, gid, h, label="ro", scopes=["read"])
        wr = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(ro)},
            json=rpc("tools/call", {"name": "weather_risk", "arguments": {}}),
        )
        assert wr.status_code == 403
        sp = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(ro)},
            json=rpc("tools/call", {"name": "season_plan", "arguments": {"start": True}}),
        )
        assert sp.status_code == 403
        read = client.post(
            "/mcp",
            headers={**MCP_H, **bearer(ro)},
            json=rpc("tools/call", {"name": "season_plan", "arguments": {}}),
        ).json()["result"]
        assert read["structuredContent"]["plan"] is None


def test_mcp_2026_stateless_requests_and_header_rules(settings):
    with api(settings) as client:
        gid, h, _ = new_garden(client)
        key = make_key(client, gid, h, label="mcp", scopes=["read", "tools"])
        disc = client.post(
            "/mcp", headers=modern_headers("server/discover"), json=modern("server/discover")
        ).json()["result"]
        assert disc["resultType"] == "complete" and disc["supportedVersions"][0] == "2026-07-28"
        assert disc["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "leaf"
        assert disc["cacheScope"] == "public" and disc["ttlMs"] > 0
        lst = client.post(
            "/mcp", headers=modern_headers("tools/list"), json=modern("tools/list")
        ).json()["result"]
        assert (
            lst["resultType"] == "complete"
            and len(lst["tools"]) == 5
            and lst["cacheScope"] == "public"
        )
        ok = client.post(
            "/mcp",
            headers={**modern_headers("tools/call", "care_plan"), **bearer(key)},
            json=modern("tools/call", {"name": "care_plan", "arguments": {}}),
        ).json()["result"]
        assert (
            ok["resultType"] == "complete"
            and ok["isError"] is False
            and ok["structuredContent"]["tasks"]
        )
        # Headers must match the body.
        for headers in (
            modern_headers("tools/list"),  # wrong method for the call below
            {**MCP_H, "Mcp-Method": "tools/call", "MCP-Protocol-Version": "2025-11-25"},
        ):
            r = client.post(
                "/mcp",
                headers={**headers, **bearer(key)},
                json=modern("tools/call", {"name": "care_plan", "arguments": {}}),
            )
            assert r.status_code == 400 and r.json()["error"]["code"] == -32020
        wrong_name = client.post(
            "/mcp",
            headers={**modern_headers("tools/call", "diagnose"), **bearer(key)},
            json=modern("tools/call", {"name": "care_plan", "arguments": {}}),
        )
        assert wrong_name.status_code == 400 and "Mcp-Name" in wrong_name.json()["error"]["message"]
        meta = {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}
        old = client.post(
            "/mcp",
            headers={**MCP_H, "MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"},
            json=rpc("tools/list", {"_meta": meta}),
        )
        assert old.status_code == 400 and old.json()["error"]["code"] == -32022
        assert (
            "2026-07-28" in old.json()["error"]["data"]["supported"]
            and old.json()["error"]["data"]["requested"] == "1900-01-01"
        )


def test_mcp_transport_rules(settings):
    with api(settings) as client:
        assert client.get("/mcp").status_code == 405 and client.delete("/mcp").status_code == 405
        assert client.post("/mcp", headers=MCP_H, content=b"not json").status_code == 400
        assert (
            client.post("/mcp", headers=MCP_H, json=[rpc("tools/list")]).status_code == 400
        )  # no batches
        assert (
            client.post(
                "/mcp", headers=MCP_H, json={"jsonrpc": "2.0", "id": 1, "result": {}}
            ).status_code
            == 202
        )

        def with_origin(origin, host=None):
            hdr = {**MCP_H, "Origin": origin} | ({"Host": host} if host else {})
            return client.post("/mcp", headers=hdr, json=rpc("tools/list")).status_code

        assert with_origin("https://evil.example") == 403
        assert with_origin("https://leaf.test") == 200
        # DNS rebinding: the attacker's page and the Host header agree, but neither is Leaf.
        assert with_origin("http://evil.example", host="evil.example") == 403
        assert with_origin("http://testserver", host="testserver") == 403
        assert with_origin("http://localhost:8000") == 403  # a loopback origin, a non-loopback host
        assert with_origin("http://localhost:8000", host="localhost:8000") == 200
        huge = client.post("/mcp", headers=MCP_H, content=b"x" * (20 * 1024 * 1024))
        assert huge.status_code == 413
        pad = json.dumps({**rpc("tools/list"), "padding": "x" * 100_000})
        assert client.post("/mcp", headers=MCP_H, content=pad).status_code == 413  # no key
        gid, h, _ = new_garden(client)
        key = make_key(client, gid, h, label="m", scopes=["read"])
        assert client.post("/mcp", headers={**MCP_H, **bearer(key)}, content=pad).status_code == 200
        # A 2026-07-28 header with no _meta is malformed; an unknown header version is refused.
        no_meta = client.post(
            "/mcp", headers={**MCP_H, "MCP-Protocol-Version": "2026-07-28"}, json=rpc("tools/list")
        )
        assert no_meta.status_code == 400 and no_meta.json()["error"]["code"] == -32602
        odd = client.post(
            "/mcp", headers={**MCP_H, "MCP-Protocol-Version": "1999-01-01"}, json=rpc("tools/list")
        )
        assert odd.status_code == 400 and odd.json()["error"]["code"] == -32022
        unknown = client.post(
            "/mcp", headers=modern_headers("resources/list"), json=modern("resources/list")
        )
        assert unknown.status_code == 400 and unknown.json()["error"]["code"] == -32601
        limited = make_key(client, gid, h, label="l", scopes=["read"], per_hour=1)
        call = rpc("tools/call", {"name": "care_plan", "arguments": {}})
        client.post("/mcp", headers={**MCP_H, **bearer(limited)}, json=call)
        over = client.post("/mcp", headers={**MCP_H, **bearer(limited)}, json=call)
        assert over.status_code == 429 and over.json()["error"]["code"] == -32000
        assert "retry-after" in over.headers
        from backend.connect.mcp import decode_image

        assert decode_image("YWJj\nZGVm\n") == b"abcdef" and decode_image("") is None


# ---------- the data layer ----------


async def test_api_key_hits_count_atomically_and_old_buckets_are_purged():
    from backend.data.driver import SqliteDriver
    from backend.data.migrate import apply
    from backend.data.repo import GardenIn, Repo

    d = SqliteDriver()
    await apply(d)
    repo = Repo(d)
    g, _ = await repo.create_garden(GardenIn(name="g"))
    key, secret = await repo.create_api_key(g.id, "k", ["read"])
    at = datetime(2026, 10, 20, 14, 30, tzinfo=UTC)
    counts = await asyncio.gather(*(repo.take_api_hit(key.id, True, at) for _ in range(5)))
    assert (
        sorted(c["hour"] for c in counts) == [1, 2, 3, 4, 5]
        and max(c["model"] for c in counts) == 5
    )
    nxt = await repo.take_api_hit(key.id, False, at + timedelta(hours=1))
    assert nxt == {"hour": 1, "day": 6}
    assert (await repo.api_key_by_secret(secret)).id == key.id
    assert (
        await repo.api_key_by_secret("lf_" + "x" * 40) is None
        and await repo.api_key_by_secret("short") is None
    )
    await repo.revoke_api_key(g.id, key.id)
    assert await repo.api_key_by_secret(secret) is None
    old = time.time() - 40 * 86400
    await d.batch(
        [("INSERT INTO api_key_hits (key_id, bucket, n) VALUES (?, 'd20200101', 3)", (key.id,))]
    )
    purged = await repo.purge(datetime.fromtimestamp(old, UTC).date() + timedelta(days=40))
    assert purged["api_key_hits"] >= 1


# ---------- review fixes ----------


def test_a_test_reading_is_labelled_in_titles_and_notifications(settings):
    with api(settings) as client:
        gid, h, (pid,) = new_garden(client)
        key = make_key(client, gid, h, label="ha", scopes=["readings", "read"])
        post_reading(client, key, plant_id=pid, value=10, test=True)
        d = client.get(f"/api/gardens/{gid}/today", headers=h).json()
        task = next(t for t in d["tasks"] if t["kind"] == "water")
        assert (
            task["title"] == "Water tomato (TEST reading)"
        )  # the title carries into notifications
        water = next(w for w in d["water"] if w["plant_id"] == pid)
        assert water["label"] == "test"
        v1 = client.get("/api/v1/today", headers=bearer(make_key(client, gid, h, scopes=["read"])))
        assert v1.json()["water"][0]["label"] == "test"
        post_reading(client, key, plant_id=pid, value=10)  # a real sensor reading is not labelled
        d = client.get(f"/api/gardens/{gid}/today", headers=h).json()
        assert next(w for w in d["water"] if w["plant_id"] == pid)["label"] == "sensor"
        assert next(t for t in d["tasks"] if t["kind"] == "water")["title"] == "Water tomato"


def test_a_typed_value_is_never_stored_as_a_sensor_reading(settings):
    with api(settings) as client:
        gid, h, (pid,) = new_garden(client)
        r = client.post(
            f"/api/gardens/{gid}/readings",
            headers=h,
            json={"plant_id": pid, "value": 5, "test": False},
        )
        assert r.status_code == 200 and r.json()["label"] == "test"
        assert client.get(f"/api/gardens/{gid}/readings", headers=h).json()[0]["label"] == "test"


def test_home_assistant_keys_start_with_room_for_every_state_change(settings):
    with api(settings) as client:
        gid, h, _ = new_garden(client)
        ha = make_key(client, gid, h, label="ha", scopes=["readings"])
        assert (ha["per_hour"], ha["per_day"]) == (600, 5000)
        explicit = make_key(client, gid, h, label="x", scopes=["readings"], per_day=50)
        assert (explicit["per_hour"], explicit["per_day"]) == (600, 50)
        general = make_key(client, gid, h, label="g", scopes=["read", "readings"])
        assert (general["per_hour"], general["per_day"]) == (120, 1000)


def test_a_garden_shares_one_model_budget_across_keys(settings, monkeypatch):
    import backend.connect.tools as connect_tools

    monkeypatch.setattr(connect_tools, "GARDEN_MODEL_CALLS_PER_DAY", 2)
    with api(settings) as client:
        gid, h, _ = new_garden(client)
        k1 = make_key(client, gid, h, label="a", scopes=["tools"])
        k2 = make_key(client, gid, h, label="b", scopes=["tools"])
        ok = [
            client.post("/api/v1/diagnose", headers=bearer(k), data={"notes": "spots"}).status_code
            for k in (k1, k2)
        ]
        assert ok == [200, 200]
        over = client.post("/api/v1/diagnose", headers=bearer(k1), data={"notes": "spots"})
        assert over.status_code == 429 and "across all its keys" in over.json()["detail"]
        # Another garden is unaffected.
        gid2, h2, _ = new_garden(client)
        k3 = make_key(client, gid2, h2, label="c", scopes=["tools"])
        assert (
            client.post("/api/v1/diagnose", headers=bearer(k3), data={"notes": "spots"}).status_code
            == 200
        )


async def test_purge_removes_every_kind_of_counter_and_old_model_budgets():
    from backend.data.driver import SqliteDriver
    from backend.data.migrate import apply
    from backend.data.repo import GardenIn, Repo

    d = SqliteDriver()
    await apply(d)
    repo = Repo(d)
    g, _ = await repo.create_garden(GardenIn(name="g"))
    key, _ = await repo.create_api_key(g.id, "k", ["read"])
    for bucket in ("h2020010101", "d20200101", "m20200101", "h2099010101", "d20990101"):
        await d.batch(
            [("INSERT INTO api_key_hits (key_id, bucket, n) VALUES (?, ?, 1)", (key.id, bucket))]
        )
    await repo.add_api_usage(f"model_calls:{g.id}", 1, day="2020-01-01")
    await repo.add_api_usage(f"model_calls:{g.id}", 1)
    await repo.purge()
    left = [
        r["bucket"]
        for r in (await d.batch([("SELECT bucket FROM api_key_hits ORDER BY bucket", ())]))[0].rows
    ]
    assert left == ["d20990101", "h2099010101"]
    assert await repo.api_usage_on(f"model_calls:{g.id}", "2020-01-01") == 0
    assert await repo.api_usage_on(f"model_calls:{g.id}") == 1
