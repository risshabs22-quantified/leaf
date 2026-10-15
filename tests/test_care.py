"""M4 Leaf Care: the planner, the notifications service, the jobs and the Today endpoints."""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.app import RateLimiter, app
from backend.care import PlantState, plan_day
from backend.care.pests import PESTS
from backend.climate.crops import CROPS
from backend.climate.nws import Alert, Alerts
from backend.config import Settings
from backend.data.driver import SqliteDriver
from backend.data.migrate import apply
from backend.data.repo import GardenIn, NotificationSettings, Plant, PlantIn, Repo
from backend.jobs import JobContext, JobMessage, JobRunner
from backend.journal import MemoryJournal
from backend.notify import deliver, notify, quiet_until
from backend.notify.webpush import PushResult
from backend.platform_api import push_host_ok
from tests.conftest import make_leaf
from tests.test_agent import RESP
from tests.test_climate import fake_forecast

TODAY = date(2026, 7, 15)
NOON = datetime(2026, 7, 15, 12, tzinfo=UTC)


def plant(name="tomato", setting="outdoor", stage="", created=TODAY, **kw) -> Plant:
    ts = int(datetime.combine(created, datetime.min.time(), UTC).timestamp())
    return Plant(
        id=f"p-{name}",
        garden_id="g",
        common_name=name,
        setting=setting,
        stage=stage,
        created_at=ts,
        updated_at=ts,
        **kw,
    )


def garden(**kw):
    from backend.data.repo import Garden

    base = dict(
        id="g",
        name="Plot",
        lat=40.7,
        lon=-74.0,
        cell="407_-740",
        timezone="UTC",
        plan_hour_utc=5,
        created_at=0,
        last_seen=0,
    )
    return Garden(**{**base, **kw})


def kinds(plan):
    return sorted(t.kind for t in plan.tasks)


# ---------- the planner ----------


def test_every_task_has_a_reason_a_due_time_and_a_source():
    states = [PlantState(plant(), crop=CROPS["tomato"])]
    plan = plan_day(garden(), states, fake_forecast(TODAY), None, None, TODAY, NOON)
    assert plan.tasks
    for t in plan.tasks:
        assert t.reason and t.due_at and t.source and t.day == "2026-07-15"
        assert t.source.get("url") or t.source.get("heuristic")


def test_water_or_skip_from_the_balance():
    dry = plan_day(
        garden(),
        [PlantState(plant(), crop=CROPS["tomato"])],
        fake_forecast(TODAY),
        None,
        None,
        TODAY,
        NOON,
    )
    assert "water" in kinds(dry) and dry.water[0]["action"] == "water"
    watered = PlantState(
        plant(), crop=CROPS["tomato"], waterings=[{"date": (TODAY - timedelta(days=1)).isoformat()}]
    )
    wet = plan_day(garden(), [watered], fake_forecast(TODAY), None, None, TODAY, NOON)
    assert "water" not in kinds(wet) and wet.water[0]["action"] == "skip"


def test_indoor_plants_get_the_indoor_model_not_the_weather():
    s = PlantState(plant("fern", setting="indoor"), last_watered=TODAY - timedelta(days=10))
    plan = plan_day(garden(), [s], None, None, None, TODAY, NOON)
    assert plan.water[0]["model"] == "indoor" and "water" in kinds(plan)


def frosty(today=TODAY):
    def temp(h):
        late = h.date() == today and h.hour >= 22
        early = h.date() == today + timedelta(days=1) and h.hour < 6
        return -1.0 if late or early else 12.0

    return fake_forecast(today, temp=temp)


def test_frost_tonight_is_urgent_due_before_dark_and_spares_hardy_plants():
    tender = PlantState(plant("tomato"), crop=CROPS["tomato"])
    hardy = PlantState(
        plant("kale"),
        profile={
            "facts": {
                "frost_tolerance": [
                    {"value": "hardy", "source": {"title": "x", "url": "https://x.edu"}}
                ]
            }
        },
    )
    plan = plan_day(garden(), [tender, hardy], frosty(), None, None, TODAY, NOON)
    frost = [t for t in plan.tasks if t.kind == "protect_frost"][0]
    assert frost.urgency == "urgent" and "tomato" in frost.reason and "kale" not in frost.reason
    sunset = datetime(2026, 7, 15, 18, 30, tzinfo=UTC).timestamp()
    assert frost.due_at == sunset - 3600
    assert plan.urgent and plan.urgent[0]["kind"] == "frost"
    only_hardy = plan_day(garden(), [hardy], frosty(), None, None, TODAY, NOON)
    assert "protect_frost" not in kinds(only_hardy)


def test_nws_alerts_add_protection_even_without_a_forecast_risk():
    alerts = Alerts(
        covered=True,
        alerts=[
            Alert(
                id="a1", event="Flood Warning", kind="flood", headline="Flood Warning until 6 PM."
            ),
            Alert(id="a2", event="Freeze Warning", kind="freeze", headline="Freeze tonight."),
        ],
    )
    plan = plan_day(
        garden(),
        [PlantState(plant(), crop=CROPS["tomato"])],
        fake_forecast(TODAY),
        alerts,
        None,
        TODAY,
        NOON,
    )
    assert {"flood", "protect_frost"} <= set(kinds(plan))
    frost = [t for t in plan.tasks if t.kind == "protect_frost"][0]
    assert frost.source["title"].startswith("NWS Freeze Warning")


def test_heat_and_wind_days():
    fc = fake_forecast(TODAY, gust=80.0)
    fc.daily["temperature_2m_max"][7] = 35.0
    plan = plan_day(
        garden(), [PlantState(plant(), crop=CROPS["tomato"])], fc, None, None, TODAY, NOON
    )
    assert {"protect_heat", "protect_wind"} <= set(kinds(plan))


def test_indoor_only_gardens_get_no_weather_protection():
    s = PlantState(plant("fern", setting="indoor"))
    plan = plan_day(garden(), [s], frosty(), None, None, TODAY, NOON)
    assert "protect_frost" not in kinds(plan)


def test_feeding_needs_a_verified_fact_and_keeps_its_source():
    p = plant(created=TODAY - timedelta(days=14))
    fact = {
        "facts": {
            "feeding": [
                {
                    "value": "side-dress monthly with compost",
                    "source": {"title": "Ext", "url": "https://ext.edu/t"},
                }
            ]
        }
    }
    with_fact = plan_day(
        garden(),
        [PlantState(p, crop=CROPS["tomato"], profile=fact)],
        fake_forecast(TODAY),
        None,
        None,
        TODAY,
        NOON,
    )
    feed = [t for t in with_fact.tasks if t.kind == "feed"][0]
    assert "side-dress" in feed.reason and feed.source["url"] == "https://ext.edu/t"
    without = plan_day(
        garden(),
        [PlantState(p, crop=CROPS["tomato"])],
        fake_forecast(TODAY),
        None,
        None,
        TODAY,
        NOON,
    )
    assert "feed" not in kinds(without)


def test_harvest_and_photo_cycles():
    p = plant(stage="fruiting", created=TODAY - timedelta(days=3))
    plan = plan_day(
        garden(),
        [PlantState(p, crop=CROPS["tomato"])],
        fake_forecast(TODAY),
        None,
        None,
        TODAY,
        NOON,
    )
    assert {"harvest_check", "photo_checkin"} <= set(kinds(plan))


def test_pest_scouting_follows_degree_days():
    svb = next(p for p in PESTS if p.key == "squash_vine_borer")
    s = PlantState(plant("zucchini"), crop=CROPS["squash"])
    at = (svb.gdd_f + 10) * 5 / 9  # just past the threshold, in C degree days
    plan = plan_day(garden(), [s], fake_forecast(TODAY), None, {"10": at, "14.4": 0}, TODAY, NOON)
    scout = [t for t in plan.tasks if t.kind == "scout_squash_vine_borer"][0]
    assert "squash vine borer" in scout.title and scout.source["url"].startswith("https://")
    early = plan_day(garden(), [s], fake_forecast(TODAY), None, {"10": at / 2}, TODAY, NOON)
    assert "scout_squash_vine_borer" not in kinds(early)
    late = plan_day(garden(), [s], fake_forecast(TODAY), None, {"10": at + 500}, TODAY, NOON)
    assert "scout_squash_vine_borer" not in kinds(late)


def test_every_pest_threshold_names_its_source_and_real_hosts():
    for p in PESTS:
        assert p.source_url.startswith("https://") and p.gdd_f > 0
        assert set(p.host_crops) <= set(CROPS)


# ---------- notifications ----------


def test_quiet_hours_across_midnight():
    s = NotificationSettings(quiet_start="21:00", quiet_end="07:00")
    late = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)
    assert quiet_until(s, "UTC", late) == datetime(2026, 7, 16, 7, 0, tzinfo=ZoneInfo("UTC"))
    early = datetime(2026, 7, 16, 6, 0, tzinfo=UTC)
    assert quiet_until(s, "UTC", early) == datetime(2026, 7, 16, 7, 0, tzinfo=ZoneInfo("UTC"))
    assert quiet_until(s, "UTC", datetime(2026, 7, 15, 12, tzinfo=UTC)) is None
    day = NotificationSettings(quiet_start="13:00", quiet_end="15:00")
    assert quiet_until(day, "UTC", datetime(2026, 7, 15, 14, tzinfo=UTC)) is not None


class FakePusher:
    def __init__(self, results=None):
        self.sent = []
        self.results = results or {}

    async def send(self, sub, payload):
        self.sent.append((sub["endpoint"], payload))
        return self.results.get(sub["endpoint"], PushResult(ok=True, status=201))


class Q:
    def __init__(self):
        self.sent = []

    async def send(self, jobs, delay_s=0):
        self.sent += [(j, delay_s) for j in jobs]


async def notify_setup(channels=("push",), max_per_day=2, devices=1):
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)
    g, _ = await repo.create_garden(GardenIn(name="g", timezone="UTC"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=list(channels), max_per_day=max_per_day)
    )
    for i in range(devices):
        await repo.add_push(g.id, f"https://fcm.googleapis.com/fcm/send/{i}", "p" * 30, "a" * 12)
    return repo, g


async def test_dedupe_limits_and_channels():
    repo, g = await notify_setup()
    push, q = FakePusher(), Q()

    async def send(key, urgent=False):
        return await notify(
            repo,
            q,
            push,
            g,
            kind="morning",
            title="t",
            body="b",
            dedupe_key=key,
            urgent=urgent,
            now=NOON,
        )

    assert await send("a") == "sent"
    assert await send("a") == "duplicate"
    assert await send("b") == "sent"
    assert await send("c") == "daily_limit"  # max 2 a day
    assert await send("d", urgent=True) == "sent"  # urgent alerts are not counted
    repo2, g2 = await notify_setup(channels=())
    assert (
        await notify(repo2, q, push, g2, kind="x", title="t", body="b", dedupe_key="z", now=NOON)
        == "no_channels"
    )


async def test_quiet_hours_hold_routine_messages_but_not_urgent_alerts():
    repo, g = await notify_setup(max_per_day=5)
    push, q = FakePusher(), Q()
    late = datetime(2026, 7, 15, 23, 0, tzinfo=UTC)
    status = await notify(
        repo, q, push, g, kind="morning", title="t", body="b", dedupe_key="k", now=late
    )
    assert status == "queued" and push.sent == []
    msg, delay = q.sent[0]
    assert msg.kind == "notify_send" and 8 * 3600 <= delay <= 8 * 3600 + 60
    assert await deliver(repo, push, g, msg.payload["id"], now=late) == "sent"
    assert await deliver(repo, push, g, msg.payload["id"], now=late) == "duplicate"
    # A freeze warning at 23:00 goes out at once: by 07:00 it would be too late.
    urgent = await notify(
        repo,
        q,
        push,
        g,
        kind="alert",
        title="t",
        body="b",
        dedupe_key="frost",
        urgent=True,
        now=late,
    )
    assert urgent == "sent" and len(q.sent) == 1


async def test_gone_devices_are_removed():
    repo, g = await notify_setup(devices=2)
    gone = "https://fcm.googleapis.com/fcm/send/0"
    push = FakePusher({gone: PushResult(ok=False, gone=True, status=410)})
    assert (
        await notify(repo, Q(), push, g, kind="t", title="t", body="b", dedupe_key="k", now=NOON)
        == "sent"
    )
    assert [s["endpoint"] for s in await repo.list_push(g.id)] == [
        "https://fcm.googleapis.com/fcm/send/1"
    ]


async def test_webpush_maps_410_to_gone(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import WebPushException

    import backend.notify.webpush as wp

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )

    class Resp:
        status = 410

    async def boom(*a, **k):
        raise WebPushException("gone", response=Resp())

    monkeypatch.setattr("pywebpush.webpush_async", boom)
    sender = wp.WebPush(pem, "https://leafathon.site")
    r = await sender.send(
        {"endpoint": "https://fcm.googleapis.com/x", "p256dh": "p", "auth": "a"}, {"title": "t"}
    )
    assert not r.ok and r.gone


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://web.push.apple.com/QGx", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
        ("http://fcm.googleapis.com/x", False),
        ("https://evil.example.com/fcm.googleapis.com", False),
        ("https://169.254.169.254/latest", False),
        ("https://fcm.googleapis.com.evil.com/x", False),
    ],
)
def test_push_endpoints_must_be_browser_push_services(endpoint, ok):
    assert push_host_ok(endpoint) == ok


# ---------- jobs ----------


# These tests run jobs on the real clock. Quiet hours (default 21:00-07:00) would hold their
# routine messages at night, so they switch quiet hours off (an empty window).


async def care_runner(llm_responses=None, pusher=None):
    db = SqliteDriver()
    await apply(db)
    repo = Repo(db)
    leaf, llm, _ = make_leaf(Settings(nebius_api_key="x"), llm_responses or {})

    class Climate:
        async def forecast(self, cell, now=None):
            return frosty(datetime.now(UTC).date())

        async def alerts(self, cell):
            return Alerts(covered=True, alerts=[])

        async def season_gdd(self, cell, bases):
            return {"by_base_c": {f"{b:g}": 0 for b in bases}}

    q = Q()
    ctx = JobContext(
        repo,
        q,
        Settings(nebius_api_key="x"),
        extra={"climate": Climate(), "leaf": leaf, "pusher": pusher},
    )
    return JobRunner(ctx), repo, q, llm


async def test_morning_plan_stores_tasks_updates_dst_and_notifies_once():
    push = FakePusher()
    runner, repo, _, _ = await care_runner(pusher=push)
    g, _ = await repo.create_garden(
        GardenIn(name="g", lat=40.7, lon=-74.0, timezone="America/New_York")
    )
    await repo.set_plan_hour(g.id, 3)  # stale hour, as after a clock change
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    msg = JobMessage(kind="plan_cell", key="plan_cell:407_-740:d", payload={"cell": "407_-740"})
    status, body = await runner.run(msg)
    assert status == 200 and body["result"]["results"][g.id]["notify"] == "sent"
    assert (await repo.get_garden(g.id)).plan_hour_utc in (9, 10)
    assert push.sent and push.sent[0][1]["title"]  # template wording (no model scripted)
    again = await runner.run(
        JobMessage(kind="plan_cell", key="plan_cell:407_-740:e", payload={"cell": "407_-740"})
    )
    assert again[1]["result"]["results"][g.id]["notify"] == "duplicate" and len(push.sent) == 1


async def test_lightning_words_the_morning_message_when_it_can():
    push = FakePusher()
    runner, repo, _, llm = await care_runner(
        {"care_wording": {"title": "Frost tonight: cover the tomato", "body": "Cover it."}}, push
    )
    g, _ = await repo.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    await runner.run(JobMessage(kind="plan_cell", key="k", payload={"cell": "407_-740"}))
    assert push.sent[0][1]["title"] == "Frost tonight: cover the tomato"
    assert ("care_wording", "nvidia/Nemotron-3_5-Lightning") in llm.calls


async def test_nws_alert_job_is_triaged_by_nano_and_sent_once_per_garden():
    push = FakePusher()
    runner, repo, _, llm = await care_runner(
        {
            "job_triage": {
                "relevant": True,
                "summary": "Freezing temperatures tonight.",
                "act_by": "18:00",
            }
        },
        push,
    )
    g, _ = await repo.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    alert = {"id": "urn:1", "event": "Freeze Warning", "kind": "freeze", "headline": "h"}
    msg = JobMessage(
        kind="alert", key="alert:407_-740:urn:1", payload={"cell": "407_-740", "alert": alert}
    )
    status, body = await runner.run(msg)
    assert status == 200 and body["result"]["notified"][g.id] == "sent"
    assert push.sent[0][1]["body"] == "Freezing temperatures tonight. Protect plants by 18:00."
    assert ("job_triage", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B") in llm.calls
    other = JobMessage(
        kind="alert",
        key="alert:x",
        payload={"cell": "407_-740", "alert": {**alert, "event": "Special Marine Warning"}},
    )
    assert (await runner.run(other))[1]["result"] == {"skipped": "not a garden alert"}


# ---------- endpoints ----------


@contextlib.contextmanager
def api(settings):
    leaf, llm, _ = make_leaf(settings, RESP)
    app.state.leaf, app.state.deep, app.state.journal = leaf, None, MemoryJournal()
    with TestClient(app) as client:
        app.state.limiter = RateLimiter(per_ip_per_hour=1000, daily_cap=10_000)

        class Climate:
            repo = None

            async def forecast(self, cell, now=None):
                return fake_forecast(datetime.now(UTC).date())

            async def alerts(self, cell):
                return Alerts(covered=True, alerts=[])

            async def season_gdd(self, cell, bases):
                return {"by_base_c": {}}

            async def climatology(self, cell):
                return None

        app.state.climate = Climate()
        yield client
    app.state.leaf = app.state.deep = app.state.journal = None
    app.state.climate = None


def test_today_plans_on_demand_and_keeps_done_tasks(settings):
    with api(settings) as client:
        made = client.post(
            "/api/gardens", json={"name": "g", "lat": 40.7, "lon": -74.0, "timezone": "UTC"}
        ).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        client.post(f"/api/gardens/{gid}/plants", headers=h, json={"common_name": "tomato"})
        d = client.get(f"/api/gardens/{gid}/today", headers=h).json()
        water = [t for t in d["tasks"] if t["kind"] == "water"]
        assert water and d["water"][0]["action"] == "water"
        client.post(
            f"/api/gardens/{gid}/tasks/{water[0]['id']}", headers=h, json={"status": "done"}
        )
        again = client.get(f"/api/gardens/{gid}/today", headers=h).json()
        assert [t["status"] for t in again["tasks"] if t["kind"] == "water"] == ["done"]
        assert client.get(f"/api/gardens/{gid}/today").status_code == 404  # no key


def test_push_subscribe_test_and_history(settings):
    with api(settings) as client:
        app.state.pusher = FakePusher()
        made = client.post("/api/gardens", json={"name": "g", "timezone": "UTC"}).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        sub = {
            "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
            "keys": {"p256dh": "B" * 40, "auth": "a" * 16},
        }
        bad = {**sub, "endpoint": "https://attacker.example/x"}
        assert client.post(f"/api/gardens/{gid}/push", headers=h, json=bad).status_code == 422
        r = client.post(f"/api/gardens/{gid}/push", headers=h, json=sub).json()
        assert r == {"subscribed": True, "devices": 1}
        channels = client.get(f"/api/gardens/{gid}/notifications", headers=h).json()["channels"]
        assert channels == ["push"]
        t = client.post(f"/api/gardens/{gid}/push/test", headers=h).json()
        assert t["status"] in ("sent", "queued")
        hist = client.get(f"/api/gardens/{gid}/notifications/history", headers=h).json()
        assert hist[0]["kind"] == "test"
        removed = client.post(
            f"/api/gardens/{gid}/push/remove", headers=h, json={"endpoint": sub["endpoint"]}
        ).json()
        assert removed == {"removed": True}
    app.state.pusher = None


def test_app_pages_manifest_and_service_worker(settings):
    with api(settings) as client:
        assert "Today" in client.get("/today").text
        assert client.get("/g/abc").status_code == 200
        sw = client.get("/sw.js")
        assert sw.headers["service-worker-allowed"] == "/" and "push" in sw.text
        m = client.get("/manifest.webmanifest")
        assert m.headers["content-type"].startswith("application/manifest+json")
        assert m.json()["display"] == "standalone"
        sizes = {i["sizes"] for i in m.json()["icons"]}
        assert {"192x192", "512x512"} <= sizes
        assert client.get("/static/icon-192.png").status_code == 200
        k = client.get("/api/push/key").json()
        assert set(k) == {"public_key", "enabled"}


# ---------- review fixes ----------


def test_nws_alerts_work_without_a_forecast_and_only_when_they_start_soon():
    soon = Alert(
        id="a",
        event="Freeze Warning",
        kind="freeze",
        headline="h",
        onset="2026-07-15T22:00:00+00:00",
        ends="2026-07-16T09:00:00+00:00",
    )
    later = Alert(
        id="b", event="Freeze Watch", kind="freeze", headline="h", onset="2026-07-17T22:00:00+00:00"
    )
    s = [PlantState(plant(), crop=CROPS["tomato"])]
    plan = plan_day(garden(), s, None, Alerts(covered=True, alerts=[soon]), None, TODAY, NOON)
    frost = [t for t in plan.tasks if t.kind == "protect_frost"][0]
    assert "NWS Freeze Warning from Wed 22:00 to Thu 09:00" in frost.reason
    two_nights = plan_day(
        garden(), s, None, Alerts(covered=True, alerts=[later]), None, TODAY, NOON
    )
    assert "protect_frost" not in kinds(two_nights)


def test_no_pest_scouting_in_the_southern_hemisphere():
    s = PlantState(plant("zucchini"), crop=CROPS["squash"])
    plan = plan_day(
        garden(lat=-33.9, lon=151.2),
        [s],
        fake_forecast(TODAY),
        None,
        {"10": 520.0, "14.4": 120.0},
        TODAY,
        NOON,
    )
    assert not [t for t in plan.tasks if t.kind.startswith("scout_")]


def test_southern_growing_season_runs_october_to_march():
    p = plant(created=TODAY - timedelta(days=14))
    fact = {
        "facts": {
            "feeding": [{"value": "compost", "source": {"title": "E", "url": "https://e.edu"}}]
        }
    }
    nov = date(2026, 11, 20)
    s = PlantState(plant(created=nov - timedelta(days=14)), crop=CROPS["tomato"], profile=fact)
    south = plan_day(garden(lat=-33.9), [s], None, None, None, nov, NOON)
    north = plan_day(garden(), [s], None, None, None, nov, NOON)
    assert "feed" in kinds(south) and "feed" not in kinds(north)
    assert p  # unused helper value


def test_morning_wording_may_not_add_numbers_or_plants():
    from backend.care.service import adds_nothing
    from backend.data.repo import Task
    from backend.schemas import CareWording

    t = Task(
        id="1",
        garden_id="g",
        day="d",
        kind="water",
        title="Water tomato about 12 mm",
        reason="r",
        created_at=0,
        updated_at=0,
    )
    assert adds_nothing(CareWording(title="Water the tomato", body="About 12 mm today."), [t])
    assert not adds_nothing(CareWording(title="Water the tomato", body="Give 30 mm."), [t])
    assert not adds_nothing(CareWording(title="Water the basil", body="Today."), [t], ["basil"])
    assert not adds_nothing(CareWording(title="Water the pepper", body="Today."), [t])


async def test_triage_keeps_only_a_real_time_and_irrelevant_alerts_are_not_pushed():
    push = FakePusher()
    runner, repo, _, _ = await care_runner(
        {"job_triage": {"relevant": False, "summary": "Coastal flooding only.", "act_by": "soon"}},
        push,
    )
    g, _ = await repo.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    alert = {"id": "urn:2", "event": "Flood Warning", "kind": "flood", "headline": "h"}
    _, body = await runner.run(
        JobMessage(kind="alert", key="k", payload={"cell": "407_-740", "alert": alert})
    )
    assert body["result"]["notified"][g.id] == "not_relevant" and push.sent == []


async def test_alert_updates_are_sent_once_and_indoor_only_gardens_are_skipped():
    push = FakePusher()
    runner, repo, _, _ = await care_runner({"job_triage": {"relevant": True, "summary": "s"}}, push)
    g, _ = await repo.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    for urn in ("urn:a", "urn:a-update"):  # NWS gives an update a new id
        alert = {
            "id": urn,
            "event": "Freeze Warning",
            "kind": "freeze",
            "headline": "h",
            "onset": "2026-10-20T22:00:00-05:00",
        }
        await runner.run(
            JobMessage(
                kind="alert", key=f"alert:{urn}", payload={"cell": "407_-740", "alert": alert}
            )
        )
    assert len(push.sent) == 1
    indoor_runner, repo2, _, _ = await care_runner({"job_triage": {"relevant": True}}, push)
    g2, _ = await repo2.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo2.add_plant(g2.id, PlantIn(common_name="fern", setting="indoor"))
    _, body = await indoor_runner.run(
        JobMessage(
            kind="alert",
            key="z",
            payload={
                "cell": "407_-740",
                "alert": {"id": "u", "event": "Freeze Warning", "kind": "freeze"},
            },
        )
    )
    assert body["result"] == {"skipped": "no outdoor plants in this cell"}


async def test_morning_is_never_urgent_and_costs_nothing_when_nobody_listens():
    runner, repo, _, llm = await care_runner(
        {"care_wording": {"title": "Cover the tomato", "body": "Frost tonight."}}, FakePusher()
    )
    g, _ = await repo.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    _, body = await runner.run(JobMessage(kind="plan_cell", key="m", payload={"cell": "407_-740"}))
    assert body["result"]["results"][g.id]["notify"] == "unreachable" and llm.calls == []
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    await runner.run(JobMessage(kind="plan_cell", key="m2", payload={"cell": "407_-740"}))
    row = (await repo.list_notifications(g.id))[0]
    assert row["kind"] == "morning" and row["urgent"] == 0


async def test_gardens_without_a_location_still_get_their_morning_plan():
    runner, repo, _, _ = await care_runner()
    g, _ = await repo.create_garden(GardenIn(name="g", timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="fern", setting="indoor"))
    _, body = await runner.run(JobMessage(kind="plan_cell", key="x", payload={"cell": f"g:{g.id}"}))
    assert body["result"]["results"][g.id]["tasks"] == 1  # the indoor "check the soil"


async def test_evening_check_warns_before_dark_once():
    push = FakePusher()
    runner, repo, _, _ = await care_runner(pusher=push)
    g, _ = await repo.create_garden(GardenIn(name="g", lat=40.7, lon=-74.0, timezone="UTC"))
    await repo.add_plant(g.id, PlantIn(common_name="tomato"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    for key in ("e1", "e2"):
        await runner.run(JobMessage(kind="evening_check", key=key, payload={"cell": "407_-740"}))
    assert len(push.sent) == 1 and push.sent[0][1]["title"] == "Frost tonight"
    assert push.sent[0][1]["urgent"] is True


async def test_notify_send_job_delivers_a_held_notification():
    push = FakePusher()
    runner, repo, _, _ = await care_runner(pusher=push)
    g, _ = await repo.create_garden(GardenIn(name="g", timezone="UTC"))
    await repo.put_notifications(
        g.id, NotificationSettings(channels=["push"], quiet_start="00:00", quiet_end="00:00")
    )
    await repo.add_push(g.id, "https://fcm.googleapis.com/x", "p" * 30, "a" * 12)
    nid = await repo.create_notification(g.id, "held", "morning", "t", "b")
    _, body = await runner.run(JobMessage(kind="notify_send", key="s", payload={"id": nid}))
    assert body["result"] == {"status": "sent"} and len(push.sent) == 1


def test_a_location_can_be_added_later(settings):
    with api(settings) as client:
        made = client.post("/api/gardens", json={"name": "g", "timezone": "UTC"}).json()
        gid, h = made["garden"]["id"], {"X-Leaf-Key": made["key"]}
        r = client.post(
            f"/api/gardens/{gid}/location", headers=h, json={"lat": 44.98, "lon": -93.27}
        )
        assert r.json()["garden"]["cell"] == "450_-933"
        assert (
            client.post(f"/api/gardens/{gid}/location", json={"lat": 1, "lon": 1}).status_code
            == 404
        )
