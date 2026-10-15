"""Jobs (idempotent, retried, internal only) and the garden API (private links)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi.testclient import TestClient

from backend.app import RateLimiter, app
from backend.config import Settings
from backend.data.driver import SqliteDriver, query
from backend.data.migrate import apply
from backend.data.repo import CallRow, GardenIn, Repo
from backend.jobs import HANDLERS, JobContext, JobMessage, JobRunner, LocalQueue, job
from backend.journal import MemoryJournal
from tests.conftest import make_leaf
from tests.test_agent import RESP


class RecordingQueue:
    def __init__(self):
        self.sent: list[tuple[JobMessage, int]] = []

    async def send(self, jobs, delay_s=0):
        self.sent += [(j, delay_s) for j in jobs]


class FakeObjects:
    def __init__(self):
        self.deleted: list[str] = []

    async def delete(self, keys):
        self.deleted += list(keys)


async def runner_with(**kw) -> tuple[JobRunner, Repo]:
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)
    ctx = JobContext(repo, RecordingQueue(), Settings(nebius_api_key="x"), **kw)
    return JobRunner(ctx), repo


@pytest.fixture
def flaky():
    calls = {"n": 0}

    @job("flaky_test")
    async def flaky_test(ctx, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first try fails")
        return {"n": calls["n"]}

    yield calls
    HANDLERS.pop("flaky_test")


async def test_a_job_runs_once_however_often_it_is_delivered(flaky):
    runner, repo = await runner_with()
    msg = JobMessage(kind="flaky_test", key="flaky:1")
    status, body = await runner.run(msg)
    assert status == 500 and body["attempts"] == 1
    assert (await repo.get_job("flaky:1")).last_error.startswith("RuntimeError")
    status, body = await runner.run(msg)  # the queue retries
    assert status == 200 and body == {"status": "done", "result": {"n": 2}}
    assert await runner.run(msg) == (200, {"status": "already_done"})  # a duplicate delivery
    assert flaky["n"] == 2


async def test_unknown_kinds_are_not_retried():
    runner, _ = await runner_with()
    assert (await runner.run(JobMessage(kind="nope", key="x")))[0] == 422


async def test_a_job_running_elsewhere_is_busy():
    runner, repo = await runner_with()
    await repo.claim_job("housekeeping:d", "housekeeping", {})
    assert (await runner.run(JobMessage(kind="housekeeping", key="housekeeping:d")))[0] == 409


async def test_housekeeping_deletes_expired_photos_object_first_and_old_meter_rows():
    objects = FakeObjects()
    runner, repo = await runner_with(objects=objects)
    garden, _ = await repo.create_garden(GardenIn(name="g"))
    old = await repo.add_photo(garden.id, "photos/old.jpg")
    await repo.add_photo(garden.id, "photos/new.jpg")
    await repo.db.batch([("UPDATE photos SET expires_at = 0 WHERE id = ?", (old.id,))])
    await repo.record_usage([CallRow(day="2020-01-01", task="t", model="m", created_at=1)])
    status, body = await runner.run(JobMessage(kind="housekeeping", key="housekeeping:x"))
    assert status == 200 and body["result"]["photos"] == 1 and body["result"]["model_calls"] == 1
    assert objects.deleted == ["photos/old.jpg"]
    assert [r["r2_key"] for r in await query(repo.db, "SELECT r2_key FROM photos")] == [
        "photos/new.jpg"
    ]


async def test_garden_ready_notes_the_cell():
    runner, repo = await runner_with()
    garden, _ = await repo.create_garden(GardenIn(lat=51.5, lon=-0.12, timezone="Europe/London"))
    status, body = await runner.run(
        JobMessage(kind="garden_ready", key="garden_ready:1", payload={"garden_id": garden.id})
    )
    assert status == 200 and body["result"] == {"cell": "515_-1", "climatology_queued": True}
    assert [e.kind for e in await repo.list_events(garden.id)] == ["garden_ready"]
    # M2: the cell is registered and its climatology queued once for every garden in it.
    assert (await repo.get_cell("515_-1"))["lat"] == 51.5
    sent = runner.ctx.queue.sent
    assert [(m.kind, m.key) for m, _ in sent] == [("climatology", "climatology:515_-1")]


async def test_local_queue_runs_jobs_in_the_background():
    runner, repo = await runner_with()
    q = LocalQueue()
    q.runner = runner
    garden, _ = await repo.create_garden(GardenIn())
    await q.send([JobMessage(kind="garden_ready", key="gr:1", payload={"garden_id": garden.id})])
    for _ in range(50):
        await asyncio.sleep(0.01)
        job_row = await repo.get_job("gr:1")
        if job_row and job_row.status == "done":
            break
    assert (await repo.get_job("gr:1")).status == "done"


def test_job_message_kind_is_checked():
    with pytest.raises(ValueError):
        JobMessage(kind="Drop Table", key="k")


# ---------- endpoints ----------


@contextlib.contextmanager
def api(settings):
    leaf, llm, _ = make_leaf(settings, RESP)
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)
        app.state.queue = RecordingQueue()
        yield client
    app.state.leaf = app.state.deep = app.state.journal = None


GARDEN = {"name": "Allotment", "lat": 52.2, "lon": 0.12, "timezone": "Europe/London"}


def test_create_open_and_use_a_garden_by_private_link(settings):
    with api(settings) as client:
        r = client.post("/api/gardens", json={**GARDEN, "prefs": {"organic": True}})
        assert r.status_code == 200
        made = r.json()
        gid, key = made["garden"]["id"], made["key"]
        assert made["link"].endswith(f"/g/{gid}#k={key}")
        assert [(j.kind, j.key) for j, _ in app.state.queue.sent] == [
            ("garden_ready", f"garden_ready:{gid}")
        ]
        h = {"X-Leaf-Key": key}
        assert client.get(f"/api/gardens/{gid}", headers=h).json()["garden"]["prefs"]["organic"]
        p = client.post(f"/api/gardens/{gid}/plants", headers=h, json={"common_name": "leek"})
        assert p.status_code == 200 and p.json()["setting"] == "outdoor"
        view = client.get(f"/api/gardens/{gid}", headers=h).json()
        assert [x["common_name"] for x in view["plants"]] == ["leek"]
        n = client.put(f"/api/gardens/{gid}/notifications", headers=h, json={"channels": ["push"]})
        assert n.status_code == 200
        got = client.get(f"/api/gardens/{gid}/notifications", headers=h).json()
        assert got["channels"] == ["push"]
        assert client.get(f"/api/gardens/{gid}/tasks", headers=h).json() == []
        assert client.delete(f"/api/gardens/{gid}", headers=h).json() == {"deleted": True}
        assert client.get(f"/api/gardens/{gid}", headers=h).status_code == 404


def test_a_wrong_or_missing_key_looks_like_no_garden(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        gid = made["garden"]["id"]
        for headers in ({}, {"X-Leaf-Key": "wrong"}):
            for method, path in (
                ("GET", f"/api/gardens/{gid}"),
                ("DELETE", f"/api/gardens/{gid}"),
                ("GET", f"/api/gardens/{gid}/events"),
            ):
                assert client.request(method, path, headers=headers).status_code == 404
        assert (
            client.get("/api/gardens/nope", headers={"X-Leaf-Key": made["key"]}).status_code == 404
        )


def test_garden_inputs_are_validated(settings):
    with api(settings) as client:
        assert client.post("/api/gardens", json={"lat": 91}).status_code == 422
        assert client.post("/api/gardens", json={"timezone": "Nowhere/City"}).status_code == 422
        made = client.post("/api/gardens", json=GARDEN).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        bad_plant = client.post(f"/api/gardens/{gid}/plants", headers=h, json={"setting": "moon"})
        assert bad_plant.status_code == 422
        too_long = client.get(f"/api/gardens/{gid}/tasks?from=2026-01-01&to=2026-03-01", headers=h)
        assert too_long.status_code == 422
        unknown_task = client.post(
            f"/api/gardens/{gid}/tasks/nope", headers=h, json={"status": "done"}
        )
        assert unknown_task.status_code == 404


def test_garden_creation_is_rate_limited_per_ip(settings):
    with api(settings) as client:
        app.state.garden_limiter = RateLimiter(per_ip_per_hour=2, daily_cap=100)
        codes = [client.post("/api/gardens", json=GARDEN).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_job_endpoint_is_internal_only(settings):
    with api(settings) as client:
        body = {"kind": "housekeeping", "key": "housekeeping:t"}
        assert client.post("/internal/jobs/run", json=body).status_code == 404
        h = {"X-Leaf-Internal": "1"}
        assert client.post("/internal/jobs/run", json={"kind": "x"}, headers=h).status_code == 422
        r = client.post("/internal/jobs/run", json=body, headers=h)
        assert r.status_code == 200 and r.json()["status"] == "done"
        again = client.post("/internal/jobs/run", json=body, headers=h)
        assert again.json() == {"status": "already_done"}


def test_one_person_using_a_garden_and_the_front_door_is_one_active_user(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        h = {"X-Leaf-Key": made["key"]}
        client.get(f"/api/gardens/{made['garden']['id']}", headers=h)
        client.post("/api/route", json={"text": "my basil is wilting"})
        assert client.get("/api/ops/usage").json()["active_user_days"] == 1


def test_storage_outage_is_a_clear_503_on_every_garden_route(settings):
    from backend.data.driver import DataError

    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}

        async def down(*a, **k):
            raise DataError("bridge down")

        app.state.data.repo.db.batch = down
        for method, path in (
            ("GET", f"/api/gardens/{gid}"),
            ("GET", f"/api/gardens/{gid}/tasks"),
            ("POST", "/api/gardens"),
        ):
            r = client.request(method, path, headers=h, json=GARDEN if method == "POST" else None)
            assert r.status_code == 503, path
            assert "storage" in r.json()["detail"]


def test_archived_plants_count_toward_the_limit(settings, monkeypatch):
    import backend.platform_api as api_mod

    monkeypatch.setattr(api_mod, "MAX_PLANTS", 1)
    with api(settings) as client:
        made = client.post("/api/gardens", json=GARDEN).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        pid = client.post(f"/api/gardens/{gid}/plants", headers=h, json={}).json()["id"]
        client.delete(f"/api/gardens/{gid}/plants/{pid}", headers=h)
        assert client.post(f"/api/gardens/{gid}/plants", headers=h, json={}).status_code == 400


async def test_busy_tells_the_queue_to_wait_out_the_lease():
    runner, repo = await runner_with()
    await repo.claim_job("housekeeping:l", "housekeeping", {}, lease_s=300)
    status, body = await runner.run(JobMessage(kind="housekeeping", key="housekeeping:l"))
    assert status == 409 and 290 <= body["retry_after"] <= 310
