"""The platform data layer: migrations, the typed repository, and the D1/KV bridge clients.

Everything runs on in-memory SQLite (D1 is SQLite); the bridge clients run against a fake
Worker (httpx.MockTransport) that answers the way cloudflare/src/index.ts does.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from backend.data.driver import Bridge, D1Driver, DataError, SqliteDriver, execute, query
from backend.data.kv import BridgeKV, MemoryKV
from backend.data.migrate import apply, migration_files, pending
from backend.data.repo import (
    CallRow,
    EventIn,
    GardenIn,
    NotificationSettings,
    PlantIn,
    Repo,
    TaskIn,
    grid_cell,
    hash_key,
    plan_hour_utc,
    utc_day,
)
from backend.jobs import BridgeQueue, JobMessage
from backend.schemas import Garden as Prefs


@pytest.fixture
async def repo() -> Repo:
    db = SqliteDriver()
    await apply(db)
    return Repo(db)


async def make_garden(repo: Repo, **kw):
    data = {"name": "Back yard", "lat": 40.7128, "lon": -74.006, "timezone": "America/New_York"}
    return await repo.create_garden(GardenIn(**{**data, **kw}))


# ---------- migrations ----------


async def test_migrations_apply_once_and_are_tracked_like_wrangler():
    db = SqliteDriver()
    first = await apply(db)
    assert first == [f.name for f in migration_files()] and first[0] == "0001_platform.sql"
    assert await apply(db) == []  # idempotent
    assert await pending(db) == []
    rows = await query(db, "SELECT name, applied_at FROM d1_migrations")
    assert [r["name"] for r in rows] == first and all(r["applied_at"] for r in rows)


async def test_a_failed_statement_rolls_back_the_whole_batch(repo: Repo):
    garden, _ = await make_garden(repo)
    with pytest.raises(DataError):
        await repo.db.batch(
            [
                ("UPDATE gardens SET name = 'changed' WHERE id = ?", (garden.id,)),
                ("INSERT INTO nope VALUES (1)", ()),
            ]
        )
    assert (await repo.get_garden(garden.id)).name == "Back yard"


async def test_more_than_100_parameters_is_refused_like_d1(repo: Repo):
    with pytest.raises(DataError, match="100"):
        await repo.db.batch([("SELECT " + ",".join("?" * 101), tuple(range(101)))])


# ---------- gardens and the private link ----------


async def test_garden_keeps_only_the_key_hash_and_a_rounded_location(repo: Repo):
    garden, key = await make_garden(repo)
    row = (await query(repo.db, "SELECT * FROM gardens WHERE id = ?", garden.id))[0]
    assert row["key_hash"] == hash_key(key) and key not in json.dumps(row)
    assert (garden.lat, garden.lon) == (40.71, -74.01)  # about 1 km, not the doorstep
    assert garden.cell == grid_cell(40.71, -74.01) == "407_-740"
    assert garden.plan_hour_utc in (9, 10)  # 05:00 in New York is 09:00 or 10:00 UTC
    assert len(key) >= 40


async def test_open_garden_needs_the_exact_key(repo: Repo):
    garden, key = await make_garden(repo)
    assert (await repo.open_garden(garden.id, key)).id == garden.id
    assert await repo.open_garden(garden.id, key[:-1] + "x") is None
    assert await repo.open_garden(garden.id, "") is None
    assert await repo.open_garden("missing", key) is None


async def test_opening_a_garden_touches_last_seen_at_most_hourly(repo: Repo, monkeypatch):
    import backend.data.repo as repo_mod

    garden, key = await make_garden(repo)
    t = garden.last_seen
    monkeypatch.setattr(repo_mod, "now_s", lambda: t + 60)
    assert (await repo.open_garden(garden.id, key)).last_seen == t  # no write
    monkeypatch.setattr(repo_mod, "now_s", lambda: t + 3600)
    assert (await repo.open_garden(garden.id, key)).last_seen == t + 3600


def test_plan_hour_follows_daylight_saving():
    assert plan_hour_utc("America/New_York", date(2026, 1, 15)) == 10
    assert plan_hour_utc("America/New_York", date(2026, 7, 15)) == 9
    assert plan_hour_utc("UTC") == 5


def test_unknown_time_zone_is_rejected():
    with pytest.raises(ValueError):
        GardenIn(timezone="Mars/Olympus")


async def test_cells_due_finds_gardens_planned_this_hour(repo: Repo):
    await make_garden(repo, timezone="UTC")
    await make_garden(repo, timezone="UTC", lat=None, lon=None)  # no location: no cell
    assert await repo.cells_due(5) == ["407_-740"]
    assert await repo.cells_due(6) == []


async def test_prefs_round_trip(repo: Repo):
    garden, _ = await make_garden(repo, prefs={"organic": True, "pets": True})
    assert (await repo.get_garden(garden.id)).prefs == Prefs(organic=True, pets=True)
    await repo.update_prefs(garden.id, Prefs(organic=False, pets=True))
    assert (await repo.get_garden(garden.id)).prefs.organic is False


# ---------- plants, tasks, events ----------


async def test_plants_add_list_archive_and_setting_is_checked(repo: Repo):
    garden, _ = await make_garden(repo)
    tomato = await repo.add_plant(garden.id, PlantIn(species="Solanum lycopersicum"))
    fern = await repo.add_plant(
        garden.id, PlantIn(common_name="Boston fern", setting="indoor", container={"pot_l": 5})
    )
    assert [p.id for p in await repo.list_plants(garden.id)] == [tomato.id, fern.id]
    assert (await repo.list_plants(garden.id))[1].container == {"pot_l": 5}
    assert await repo.archive_plant(garden.id, tomato.id)
    assert [p.id for p in await repo.list_plants(garden.id)] == [fern.id]
    assert len(await repo.list_plants(garden.id, include_archived=True)) == 2
    with pytest.raises(ValueError):
        PlantIn(setting="greenhouse")


async def test_another_garden_cannot_archive_my_plant(repo: Repo):
    mine, _ = await make_garden(repo)
    theirs, _ = await make_garden(repo)
    plant = await repo.add_plant(mine.id, PlantIn(common_name="basil"))
    assert not await repo.archive_plant(theirs.id, plant.id)


async def test_rerunning_the_planner_adds_no_duplicate_tasks(repo: Repo):
    garden, _ = await make_garden(repo)
    plant = await repo.add_plant(garden.id, PlantIn(common_name="tomato"))
    tasks = [
        TaskIn(garden_id=garden.id, plant_id=plant.id, day="2026-10-15", kind="water", title="W"),
        TaskIn(garden_id=garden.id, day="2026-10-15", kind="frost_cover", title="Cover"),
    ]
    assert await repo.add_tasks(tasks) == 2
    assert await repo.add_tasks(tasks) == 0  # same plant, kind, day; garden-level too
    listed = await repo.list_tasks(garden.id, "2026-10-15", "2026-10-15")
    assert sorted(t.kind for t in listed) == ["frost_cover", "water"]


async def test_task_status_and_its_event_are_written_together(repo: Repo):
    garden, _ = await make_garden(repo)
    other, _ = await make_garden(repo)
    await repo.add_tasks([TaskIn(garden_id=garden.id, day="2026-10-15", kind="w", title="W")])
    task = (await repo.list_tasks(garden.id, "2026-10-15", "2026-10-15"))[0]
    assert not await repo.set_task_status(other.id, task.id, "done")  # not their task
    assert await repo.list_events(other.id) == []
    assert await repo.set_task_status(garden.id, task.id, "done", trace_id="t1")
    assert (await repo.list_tasks(garden.id, "2026-10-15", "2026-10-15", "done"))[0].id == task.id
    events = await repo.list_events(garden.id)
    assert [(e.kind, e.ref, e.trace_id) for e in events] == [("done", task.id, "t1")]


async def test_deleting_a_garden_removes_everything_in_it(repo: Repo):
    garden, _ = await make_garden(repo)
    plant = await repo.add_plant(garden.id, PlantIn(common_name="mint"))
    await repo.add_tasks([TaskIn(garden_id=garden.id, plant_id=plant.id, day="d", kind="k",
                                 title="t")])  # fmt: skip
    await repo.log_event(EventIn(garden_id=garden.id, kind="note"))
    await repo.put_notifications(
        garden.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    assert await repo.delete_garden(garden.id)
    for table in ("plants", "tasks", "events", "notification_settings"):
        assert await query(repo.db, f"SELECT * FROM {table}") == []


async def test_notification_settings_default_then_upsert(repo: Repo):
    garden, _ = await make_garden(repo)
    assert await repo.get_notifications(garden.id) == NotificationSettings()
    s = NotificationSettings(channels=["push"], quiet_start="22:30", max_per_day=5)
    await repo.put_notifications(garden.id, s)
    await repo.put_notifications(garden.id, s.model_copy(update={"max_per_day": 2}))
    got = await repo.get_notifications(garden.id)
    assert got.channels == ["push"] and got.max_per_day == 2
    with pytest.raises(ValueError):
        NotificationSettings(quiet_start="25:00")


async def test_photos_expire_after_30_days(repo: Repo):
    garden, _ = await make_garden(repo)
    photo = await repo.add_photo(garden.id, "photos/g/1.jpg")
    assert photo.expires_at - photo.created_at == 30 * 86400
    assert await repo.expired_photos(now=photo.expires_at - 1) == []
    assert [p.id for p in await repo.expired_photos(now=photo.expires_at)] == [photo.id]
    assert await repo.delete_photos([photo.id]) == 1


# ---------- jobs ----------


async def test_job_claim_states(repo: Repo, monkeypatch):
    import backend.data.repo as repo_mod

    state, job = await repo.claim_job("k1", "housekeeping", {"a": 1})
    assert state == "run" and job.attempts == 1 and job.payload == {"a": 1}
    assert (await repo.claim_job("k1", "housekeeping", {}))[0] == "busy"  # leased
    await repo.fail_job("k1", "boom")
    state, job = await repo.claim_job("k1", "housekeeping", {})
    assert state == "run" and job.attempts == 2  # a failed job can be retried
    await repo.finish_job("k1")
    assert (await repo.claim_job("k1", "housekeeping", {}))[0] == "done"
    # A delivery whose worker died gets the job back once the lease has expired.
    await repo.claim_job("k2", "housekeeping", {}, lease_s=10)
    now = repo_mod.now_s()
    monkeypatch.setattr(repo_mod, "now_s", lambda: now + 11)
    assert (await repo.claim_job("k2", "housekeeping", {}))[0] == "run"


# ---------- meter rows ----------


async def test_usage_aggregates_spend_cache_and_active_users(repo: Repo):
    day = utc_day()
    calls = [
        CallRow(day=day, task="front_door", model="nano", prompt_tokens=400,
                completion_tokens=80, cost_usd=0.00004, latency_ms=300, created_at=1),
        CallRow(day=day, task="front_door", model="nano", cost_usd=0.0, ok=False, created_at=2),
        CallRow(day=day, task="diagnose", model="ultra", cost_usd=0.009, latency_ms=5000,
                created_at=3),
    ]  # fmt: skip
    await repo.record_usage(calls, {(day, "front_door"): (3, 1)}, [(day, "g:1"), (day, "v:2")])
    await repo.record_usage([], {(day, "front_door"): (1, 0)}, [(day, "g:1")])  # no double count
    assert await repo.spend_on(day) == pytest.approx(0.00904)
    raw = await repo.usage(day, day)
    fd = next(r for r in raw["calls"] if r["task"] == "front_door")
    assert fd["calls"] == 2 and fd["failed"] == 1 and fd["prompt_tokens"] == 400
    assert raw["cache"] == [{"day": day, "namespace": "front_door", "hits": 4, "misses": 1}]
    assert raw["active"] == [{"day": day, "active": 2}]


async def test_day_salt_is_stable_within_a_day(repo: Repo):
    a = await repo.day_salt("2026-10-14")
    assert a == await repo.day_salt("2026-10-14") != await repo.day_salt("2026-10-15")


async def test_purge_drops_old_meter_rows_salts_and_finished_jobs(repo: Repo):
    await repo.record_usage(
        [
            CallRow(day="2026-09-01", task="t", model="m", created_at=1),
            CallRow(day="2026-10-14", task="t", model="m", created_at=2),
        ]
    )
    await repo.day_salt("2026-10-10")
    await repo.day_salt("2026-10-14")
    out = await repo.purge(today=date(2026, 10, 14))
    assert out["model_calls"] == 1 and out["day_salts"] == 1
    assert [r["day"] for r in await query(repo.db, "SELECT day FROM model_calls")] == ["2026-10-14"]


async def test_purge_deletes_gardens_nobody_has_opened_for_four_months(repo: Repo):
    (idle, _), (fresh, _) = await make_garden(repo), await make_garden(repo)
    await repo.add_plant(idle.id, PlantIn(common_name="tomato"))
    await execute(repo.db, "UPDATE gardens SET last_seen = ? WHERE id = ?", 1, idle.id)
    out = await repo.purge()
    assert out["idle_gardens"] == 1
    assert [r["id"] for r in await query(repo.db, "SELECT id FROM gardens")] == [fresh.id]
    assert await query(repo.db, "SELECT id FROM plants WHERE garden_id = ?", idle.id) == []


# ---------- the bridge clients (a fake Worker) ----------


def fake_worker(handler):
    return Bridge(
        "http://leaf-platform.internal", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_d1_driver_sends_one_batch_and_reads_rows_and_changes():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(
            200, json={"results": [{"rows": [{"n": 1}], "changes": 0}, {"rows": [], "changes": 3}]}
        )

    db = D1Driver(fake_worker(handler))
    res = await db.batch([("SELECT ? AS n", (1,)), ("DELETE FROM t WHERE x = ?", ("y",))])
    assert seen["path"] == "/d1"
    assert seen["body"] == {
        "statements": [
            {"sql": "SELECT ? AS n", "params": [1]},
            {"sql": "DELETE FROM t WHERE x = ?", "params": ["y"]},
        ]
    }
    assert res[0].rows == [{"n": 1}] and res[1].changes == 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "D1_ERROR"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"nope": 1}),
    ],
)
async def test_d1_driver_turns_worker_failures_into_data_errors(response):
    db = D1Driver(fake_worker(lambda req: response))
    with pytest.raises(DataError):
        await db.batch([("SELECT 1", ())])


async def test_d1_driver_network_error_is_a_data_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(DataError):
        await D1Driver(fake_worker(handler)).batch([("SELECT 1", ())])


async def test_bridge_kv_quotes_keys_enforces_min_ttl_and_maps_404():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.raw_path.decode(), req.content))
        if req.method == "GET":
            return (
                httpx.Response(404) if "missing" in str(req.url) else httpx.Response(200, text="v")
            )
        return httpx.Response(204)

    kv = BridgeKV(fake_worker(handler))
    await kv.put("front_door:a/b c", "v", ttl_s=5)
    assert await kv.get("front_door:a/b c") == "v"
    assert await kv.get("missing") is None
    assert calls[0] == ("PUT", "/kv/front_door%3Aa%2Fb%20c?ttl=60", b"v")


async def test_memory_kv_expires_and_is_bounded():
    kv = MemoryKV(max_items=2)
    await kv.put("a", "1", 60)
    await kv.put("b", "2", 60)
    await kv.put("c", "3", 60)
    assert await kv.get("a") is None and await kv.get("c") == "3"
    await kv.put("d", "4", 0)  # ttl floors at 1 s, so it is readable now
    assert await kv.get("d") == "4"


async def test_bridge_queue_batches_by_100_and_clamps_delay():
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"sent": 1})

    q = BridgeQueue(fake_worker(handler))
    jobs = [JobMessage(kind="housekeeping", key=f"k{i}") for i in range(150)]
    await q.send(jobs, delay_s=10**6)
    assert [len(b["messages"]) for b in bodies] == [100, 50]
    assert bodies[0]["messages"][0] == {
        "body": {"kind": "housekeeping", "key": "k0", "payload": {}},
        "delaySeconds": 86400,
    }


async def test_an_unreachable_bridge_does_not_stop_leaf_starting(monkeypatch):
    from backend.config import Settings
    from backend.data import open_data

    def handler(req):
        return httpx.Response(500, json={"error": "down"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.data.driver.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler)),
    )
    data = await open_data(
        Settings(nebius_api_key="x", platform_url="http://leaf-platform.internal")
    )
    assert data.db.name == "d1" and data.schema_pending[0].startswith("unknown")


async def test_a_long_meter_backlog_is_written_in_chunks_d1_accepts(repo: Repo):
    """After an outage the meter can hold 2,000 rows; the bridge takes 200 statements a batch."""
    day = utc_day()
    rows = [CallRow(day=day, task="t", model="m", cost_usd=0.001, created_at=i) for i in range(450)]
    await repo.record_usage(rows)
    assert await repo.spend_on(day) == pytest.approx(0.45)
    with pytest.raises(DataError, match="max 200"):
        await repo.db.batch([("SELECT 1", ())] * 201)


async def test_purge_keeps_jobs_that_are_still_queued_or_running(repo: Repo):
    await repo.claim_job("running", "housekeeping", {})
    await repo.claim_job("done", "housekeeping", {})
    await repo.finish_job("done")
    await repo.claim_job("failed", "housekeeping", {})
    await repo.fail_job("failed", "x")
    await repo.db.batch([("UPDATE jobs SET updated_at = 0", ())])
    assert (await repo.purge())["jobs"] == 2
    assert (await repo.get_job("running")) is not None


async def test_a_failed_startup_schema_check_recovers_once_the_bridge_answers(monkeypatch):
    from backend.config import Settings
    from backend.data import UNKNOWN, open_data

    up = {"ok": False}

    def handler(req):
        if not up["ok"]:
            return httpx.Response(500, json={"error": "not wired yet"})
        return httpx.Response(200, json={"results": [{"rows": [{"name": "d1_migrations"}]}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "backend.data.driver.httpx.AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler)),
    )
    data = await open_data(
        Settings(nebius_api_key="x", platform_url="http://leaf-platform.internal")
    )
    assert data.schema_pending == (UNKNOWN,)
    up["ok"] = True
    assert await data.recheck() != (UNKNOWN,)
