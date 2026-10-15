"""The typed repository: the only code that writes SQL. Rows come back as pydantic models.

Gardens have no accounts. Creating one returns a private key once; the private link carries
it (`/g/<id>#k=<key>`) and only its SHA-256 is stored, so a database leak opens no garden.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from backend.data.driver import Driver, Statement, execute, query, write_chunked
from backend.schemas import Garden as GardenPrefs

PHOTO_DAYS = 30
METER_DAYS = 30
JOB_DAYS = 14
TOUCH_EVERY_S = 3600  # how often opening a garden updates last_seen (saves D1 writes)
CELL_DEG = 0.1  # grid cell for shared forecasts and climatology (~11 km, ERA5-Land's grid)
PLAN_LOCAL_HOUR = 5


def now_s() -> int:
    return int(time.time())


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), UTC).date().isoformat()


def new_id(nbytes: int = 9) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def grid_cell(lat: float, lon: float) -> str:
    """The 0.1-degree cell a point falls in, e.g. "404_-740" for New York."""
    return f"{round(lat / CELL_DEG)}_{round(lon / CELL_DEG)}"


def plan_hour_utc(timezone: str, on: date | None = None) -> int:
    """The UTC hour when it is 05:00 in `timezone` (DST moves it; the planner re-checks)."""
    tz = ZoneInfo(timezone)
    day = on or datetime.now(tz).date()
    local = datetime(day.year, day.month, day.day, PLAN_LOCAL_HOUR, tzinfo=tz)
    return local.astimezone(UTC).hour


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(text: str | None, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


# ---------- rows ----------


class GardenIn(BaseModel):
    name: str = Field("", max_length=80)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    timezone: str = Field("UTC", max_length=64)
    prefs: GardenPrefs = Field(default_factory=GardenPrefs)

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return v


class Garden(BaseModel):
    id: str
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    cell: str | None = None
    timezone: str = "UTC"
    plan_hour_utc: int | None = None
    prefs: GardenPrefs = Field(default_factory=GardenPrefs)
    created_at: int
    last_seen: int


class PlantIn(BaseModel):
    species: str = Field("", max_length=120)
    common_name: str = Field("", max_length=120)
    nickname: str = Field("", max_length=80)
    setting: Literal["outdoor", "indoor"] = "outdoor"
    container: dict[str, Any] = Field(default_factory=dict)  # e.g. {"pot_litres": 10}
    planted_on: date | None = None
    stage: str = Field("", max_length=40)

    @field_validator("container")
    @classmethod
    def _small(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(_json(v)) > 1000:
            raise ValueError("container details are too long")
        return v


class Plant(PlantIn):
    id: str
    garden_id: str
    status: Literal["active", "archived"] = "active"
    created_at: int
    updated_at: int


TaskStatus = Literal["open", "done", "skipped", "expired"]


class TaskIn(BaseModel):
    garden_id: str
    plant_id: str | None = None
    day: str  # the garden's local date
    kind: str
    title: str
    reason: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    due_at: int | None = None
    urgency: Literal["normal", "soon", "urgent"] = "normal"
    created_by: str = "rule"
    trace_id: str = ""


class Task(TaskIn):
    id: str
    status: TaskStatus = "open"
    created_at: int
    updated_at: int


class EventIn(BaseModel):
    garden_id: str
    plant_id: str | None = None
    kind: str
    ref: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""


class Event(EventIn):
    id: int
    created_at: int


class NotificationSettings(BaseModel):
    channels: list[str] = Field(default_factory=list, max_length=5)
    quiet_start: str = Field("21:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str = Field("07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    max_per_day: int = Field(3, ge=0, le=20)


class Photo(BaseModel):
    id: str
    garden_id: str
    plant_id: str | None = None
    r2_key: str
    created_at: int
    expires_at: int


class Job(BaseModel):
    key: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["queued", "running", "done", "failed"] = "queued"
    attempts: int = 0
    last_error: str = ""
    lease_until: int = 0
    created_at: int
    updated_at: int


class CallRow(BaseModel):
    """One metered model call."""

    day: str
    trace_id: str = ""
    task: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    ok: bool = True
    created_at: int


def _garden(row: dict[str, Any]) -> Garden:
    return Garden(**{**row, "prefs": GardenPrefs.model_validate(_load(row.get("prefs"), {}))})


def _plant(row: dict[str, Any]) -> Plant:
    return Plant(**{**row, "container": _load(row.get("container"), {})})


def _task(row: dict[str, Any]) -> Task:
    return Task(**{**row, "source": _load(row.get("source"), {})})


def _job(row: dict[str, Any]) -> Job:
    return Job(**{**row, "payload": _load(row.get("payload"), {})})


class Repo:
    def __init__(self, db: Driver):
        self.db = db

    # ---------- gardens ----------

    async def create_garden(self, data: GardenIn) -> tuple[Garden, str]:
        """A new garden and its private key (shown once; only the hash is kept)."""
        key = secrets.token_urlsafe(32)
        now = now_s()
        lat = round(data.lat, 2) if data.lat is not None else None
        lon = round(data.lon, 2) if data.lon is not None else None
        garden = Garden(
            id=new_id(),
            name=data.name.strip(),
            lat=lat,
            lon=lon,
            cell=grid_cell(lat, lon) if lat is not None and lon is not None else None,
            timezone=data.timezone,
            plan_hour_utc=plan_hour_utc(data.timezone),
            prefs=data.prefs,
            created_at=now,
            last_seen=now,
        )
        await execute(
            self.db,
            "INSERT INTO gardens (id, key_hash, name, lat, lon, cell, timezone, plan_hour_utc,"
            " prefs, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            garden.id,
            hash_key(key),
            garden.name,
            garden.lat,
            garden.lon,
            garden.cell,
            garden.timezone,
            garden.plan_hour_utc,
            garden.prefs.model_dump_json(),
            now,
            now,
        )
        return garden, key

    async def get_garden(self, garden_id: str) -> Garden | None:
        rows = await query(self.db, "SELECT * FROM gardens WHERE id = ?", garden_id)
        return _garden(rows[0]) if rows else None

    async def open_garden(self, garden_id: str, key: str) -> Garden | None:
        """The garden if `key` is its private key, else None (constant-time compare)."""
        if not garden_id or not key:
            return None
        rows = await query(self.db, "SELECT * FROM gardens WHERE id = ?", garden_id)
        if not rows or not hmac.compare_digest(rows[0]["key_hash"], hash_key(key)):
            return None
        garden = _garden(rows[0])
        now = now_s()
        if now - garden.last_seen >= TOUCH_EVERY_S:
            await execute(self.db, "UPDATE gardens SET last_seen = ? WHERE id = ?", now, garden.id)
            garden.last_seen = now
        return garden

    async def update_prefs(self, garden_id: str, prefs: GardenPrefs) -> None:
        await execute(
            self.db,
            "UPDATE gardens SET prefs = ? WHERE id = ?",
            prefs.model_dump_json(),
            garden_id,
        )

    async def delete_garden(self, garden_id: str) -> bool:
        """Deletes the garden and, by cascade, its plants, tasks, events and settings."""
        return await execute(self.db, "DELETE FROM gardens WHERE id = ?", garden_id) > 0

    async def cells_due(self, hour_utc: int) -> list[str]:
        rows = await query(
            self.db,
            "SELECT DISTINCT cell FROM gardens WHERE plan_hour_utc = ? AND cell IS NOT NULL",
            hour_utc,
        )
        return [r["cell"] for r in rows]

    # ---------- plants ----------

    async def add_plant(self, garden_id: str, data: PlantIn) -> Plant:
        now = now_s()
        plant = Plant(
            **data.model_dump(), id=new_id(), garden_id=garden_id, created_at=now, updated_at=now
        )
        await execute(
            self.db,
            "INSERT INTO plants (id, garden_id, species, common_name, nickname, setting,"
            " container, planted_on, stage, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            plant.id,
            garden_id,
            plant.species,
            plant.common_name,
            plant.nickname,
            plant.setting,
            _json(plant.container),
            plant.planted_on.isoformat() if plant.planted_on else None,
            plant.stage,
            plant.status,
            now,
            now,
        )
        return plant

    async def list_plants(self, garden_id: str, include_archived: bool = False) -> list[Plant]:
        sql = "SELECT * FROM plants WHERE garden_id = ?"
        if not include_archived:
            sql += " AND status = 'active'"
        rows = await query(self.db, sql + " ORDER BY created_at, rowid", garden_id)
        return [_plant(r) for r in rows]

    async def archive_plant(self, garden_id: str, plant_id: str) -> bool:
        return (
            await execute(
                self.db,
                "UPDATE plants SET status = 'archived', updated_at = ?"
                " WHERE id = ? AND garden_id = ?",
                now_s(),
                plant_id,
                garden_id,
            )
            > 0
        )

    # ---------- tasks and events ----------

    async def add_tasks(self, tasks: Sequence[TaskIn]) -> int:
        """Insert tasks, skipping any (garden, plant, kind, day) that already exists, so a
        re-run of the planner is harmless. Returns how many were new."""
        if not tasks:
            return 0
        now = now_s()
        stmts: list[Statement] = [
            (
                "INSERT OR IGNORE INTO tasks (id, garden_id, plant_id, day, kind, title, reason,"
                " source, due_at, urgency, created_by, trace_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    t.garden_id,
                    t.plant_id,
                    t.day,
                    t.kind,
                    t.title,
                    t.reason,
                    _json(t.source),
                    t.due_at,
                    t.urgency,
                    t.created_by,
                    t.trace_id,
                    now,
                    now,
                ),
            )
            for t in tasks
        ]
        return sum(r.changes for r in await write_chunked(self.db, stmts))

    async def list_tasks(
        self, garden_id: str, day_from: str, day_to: str, status: TaskStatus | None = None
    ) -> list[Task]:
        sql = "SELECT * FROM tasks WHERE garden_id = ? AND day >= ? AND day <= ?"
        params: list[Any] = [garden_id, day_from, day_to]
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = await query(self.db, sql + " ORDER BY day, due_at, rowid", *params)
        return [_task(r) for r in rows]

    async def set_task_status(
        self, garden_id: str, task_id: str, status: TaskStatus, trace_id: str = ""
    ) -> bool:
        """Mark a task and log what the gardener did, atomically."""
        now = now_s()
        res = await self.db.batch(
            [
                (
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND garden_id = ?",
                    (status, now, task_id, garden_id),
                ),
                (
                    "INSERT INTO events (garden_id, plant_id, kind, ref, payload, trace_id,"
                    " created_at) SELECT garden_id, plant_id, ?, id, ?, ?, ? FROM tasks"
                    " WHERE id = ? AND garden_id = ?",
                    (status, "{}", trace_id, now, task_id, garden_id),
                ),
            ]
        )
        return res[0].changes > 0

    async def log_event(self, event: EventIn) -> None:
        await execute(
            self.db,
            "INSERT INTO events (garden_id, plant_id, kind, ref, payload, trace_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            event.garden_id,
            event.plant_id,
            event.kind,
            event.ref,
            _json(event.payload),
            event.trace_id,
            now_s(),
        )

    async def list_events(self, garden_id: str, limit: int = 50) -> list[Event]:
        rows = await query(
            self.db,
            "SELECT * FROM events WHERE garden_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            garden_id,
            max(1, min(limit, 500)),
        )
        return [Event(**{**r, "payload": _load(r.get("payload"), {})}) for r in rows]

    # ---------- notification settings ----------

    async def get_notifications(self, garden_id: str) -> NotificationSettings:
        rows = await query(
            self.db, "SELECT * FROM notification_settings WHERE garden_id = ?", garden_id
        )
        if not rows:
            return NotificationSettings()
        r = rows[0]
        return NotificationSettings(**{**r, "channels": _load(r.get("channels"), [])})

    async def put_notifications(self, garden_id: str, s: NotificationSettings) -> None:
        await execute(
            self.db,
            "INSERT INTO notification_settings (garden_id, channels, quiet_start, quiet_end,"
            " max_per_day, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (garden_id) DO"
            " UPDATE SET channels = excluded.channels, quiet_start = excluded.quiet_start,"
            " quiet_end = excluded.quiet_end, max_per_day = excluded.max_per_day,"
            " updated_at = excluded.updated_at",
            garden_id,
            _json(s.channels),
            s.quiet_start,
            s.quiet_end,
            s.max_per_day,
            now_s(),
        )

    # ---------- photos (30-day retention) ----------

    async def add_photo(self, garden_id: str, r2_key: str, plant_id: str | None = None) -> Photo:
        now = now_s()
        photo = Photo(
            id=new_id(),
            garden_id=garden_id,
            plant_id=plant_id,
            r2_key=r2_key,
            created_at=now,
            expires_at=now + PHOTO_DAYS * 86400,
        )
        await execute(
            self.db,
            "INSERT INTO photos (id, garden_id, plant_id, r2_key, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            photo.id,
            garden_id,
            plant_id,
            r2_key,
            photo.created_at,
            photo.expires_at,
        )
        return photo

    async def expired_photos(self, now: int | None = None, limit: int = 200) -> list[Photo]:
        rows = await query(
            self.db,
            "SELECT * FROM photos WHERE expires_at <= ? ORDER BY expires_at LIMIT ?",
            now if now is not None else now_s(),
            limit,
        )
        return [Photo(**r) for r in rows]

    async def delete_photos(self, ids: Iterable[str]) -> int:
        stmts: list[Statement] = [("DELETE FROM photos WHERE id = ?", (i,)) for i in ids]
        return sum(r.changes for r in await write_chunked(self.db, stmts)) if stmts else 0

    # ---------- jobs ----------

    async def claim_job(
        self, key: str, kind: str, payload: dict[str, Any], lease_s: int = 300
    ) -> tuple[str, Job | None]:
        """Take a job to run. Returns ("run", job), ("done", None) when it already finished,
        or ("busy", job) when another delivery holds an unexpired lease."""
        now = now_s()
        res = await self.db.batch(
            [
                (
                    "INSERT OR IGNORE INTO jobs (key, kind, payload, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (key, kind, _json(payload), now, now),
                ),
                (
                    "UPDATE jobs SET status = 'running', attempts = attempts + 1,"
                    " lease_until = ?, updated_at = ? WHERE key = ? AND (status IN"
                    " ('queued', 'failed') OR (status = 'running' AND lease_until < ?))"
                    " RETURNING *",
                    (now + lease_s, now, key, now),
                ),
                ("SELECT * FROM jobs WHERE key = ?", (key,)),
            ]
        )
        if res[1].rows:
            return "run", _job(res[1].rows[0])
        current = _job(res[2].rows[0]) if res[2].rows else None
        if current is not None and current.status == "done":
            return "done", None
        return "busy", current  # current.lease_until says when a retry can take it

    async def finish_job(self, key: str) -> None:
        await execute(
            self.db,
            "UPDATE jobs SET status = 'done', last_error = '', lease_until = 0, updated_at = ?"
            " WHERE key = ?",
            now_s(),
            key,
        )

    async def fail_job(self, key: str, error: str) -> None:
        await execute(
            self.db,
            "UPDATE jobs SET status = 'failed', last_error = ?, lease_until = 0, updated_at = ?"
            " WHERE key = ?",
            error[:500],
            now_s(),
            key,
        )

    async def get_job(self, key: str) -> Job | None:
        rows = await query(self.db, "SELECT * FROM jobs WHERE key = ?", key)
        return _job(rows[0]) if rows else None

    # ---------- meter ----------

    async def record_usage(
        self,
        calls: Sequence[CallRow],
        cache: dict[tuple[str, str], tuple[int, int]] | None = None,
        actors: Iterable[tuple[str, str]] = (),
    ) -> None:
        """One batch per flush: model calls, cache hit counts and newly active users."""
        stmts: list[Statement] = [
            (
                "INSERT INTO model_calls (day, trace_id, task, model, prompt_tokens,"
                " completion_tokens, cost_usd, latency_ms, ok, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.day,
                    c.trace_id,
                    c.task,
                    c.model,
                    c.prompt_tokens,
                    c.completion_tokens,
                    c.cost_usd,
                    c.latency_ms,
                    int(c.ok),
                    c.created_at,
                ),
            )
            for c in calls
        ]
        for (day, ns), (hits, misses) in (cache or {}).items():
            stmts.append(
                (
                    "INSERT INTO cache_stats (day, namespace, hits, misses) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (day, namespace) DO UPDATE SET hits = hits + excluded.hits,"
                    " misses = misses + excluded.misses",
                    (day, ns, hits, misses),
                )
            )
        stmts += [
            ("INSERT OR IGNORE INTO active_daily (day, actor) VALUES (?, ?)", (day, actor))
            for day, actor in actors
        ]
        await write_chunked(self.db, stmts)  # a backlog after an outage can be long

    async def spend_on(self, day: str) -> float:
        rows = await query(
            self.db,
            "SELECT COALESCE(SUM(cost_usd), 0) AS spent FROM model_calls WHERE day = ?",
            day,
        )
        return float(rows[0]["spent"]) if rows else 0.0

    async def day_salt(self, day: str) -> str:
        """The salt for hashing visitors on `day`, created on first use."""
        res = await self.db.batch(
            [
                (
                    "INSERT OR IGNORE INTO day_salts (day, salt) VALUES (?, ?)",
                    (day, secrets.token_hex(16)),
                ),
                ("SELECT salt FROM day_salts WHERE day = ?", (day,)),
            ]
        )
        return res[1].rows[0]["salt"]

    async def usage(self, day_from: str, day_to: str) -> dict[str, Any]:
        """Aggregates for /api/ops/usage. Nothing here identifies a person."""
        res = await self.db.batch(
            [
                (
                    "SELECT day, task, model, COUNT(*) AS calls, SUM(1 - ok) AS failed,"
                    " SUM(prompt_tokens) AS prompt_tokens,"
                    " SUM(completion_tokens) AS completion_tokens, SUM(cost_usd) AS cost_usd,"
                    " AVG(latency_ms) AS avg_latency_ms FROM model_calls"
                    " WHERE day >= ? AND day <= ? GROUP BY day, task, model ORDER BY day, task",
                    (day_from, day_to),
                ),
                (
                    "SELECT day, namespace, hits, misses FROM cache_stats"
                    " WHERE day >= ? AND day <= ? ORDER BY day, namespace",
                    (day_from, day_to),
                ),
                (
                    "SELECT day, COUNT(*) AS active FROM active_daily"
                    " WHERE day >= ? AND day <= ? GROUP BY day ORDER BY day",
                    (day_from, day_to),
                ),
            ]
        )
        return {"calls": res[0].rows, "cache": res[1].rows, "active": res[2].rows}

    # ---------- housekeeping ----------

    async def purge(self, today: date | None = None) -> dict[str, int]:
        """Drop meter rows past 30 days, old visitor salts and finished jobs past 14 days."""
        today = today or datetime.now(UTC).date()
        meter_cut = (today - timedelta(days=METER_DAYS)).isoformat()
        salt_cut = (today - timedelta(days=2)).isoformat()
        job_cut = int(datetime.combine(today, datetime.min.time(), UTC).timestamp()) - (
            JOB_DAYS * 86400
        )
        res = await self.db.batch(
            [
                ("DELETE FROM model_calls WHERE day < ?", (meter_cut,)),
                ("DELETE FROM cache_stats WHERE day < ?", (meter_cut,)),
                ("DELETE FROM active_daily WHERE day < ?", (meter_cut,)),
                ("DELETE FROM day_salts WHERE day < ?", (salt_cut,)),
                (
                    "DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
                    (job_cut,),
                ),
            ]
        )
        names = ["model_calls", "cache_stats", "active_daily", "day_salts", "jobs"]
        return {n: r.changes for n, r in zip(names, res, strict=True)}
