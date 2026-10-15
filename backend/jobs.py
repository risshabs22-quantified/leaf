"""Background jobs: Cron Triggers and the API put jobs on a Cloudflare Queue; the Worker's
consumer posts each one to /internal/jobs/run; JobRunner runs it here.

The queue delivers at least once, so every job has an idempotency key and the `jobs` table
records its state: a key that already finished is acknowledged without running again, and
one that is running elsewhere (an unexpired lease) is retried later. Status codes tell the
consumer what to do (cloudflare/src/index.ts):

    200 done, or already done -> ack       409 running elsewhere -> retry after its lease
    422 unknown kind or bad message -> ack (retrying cannot help; it is logged)
    500 the handler failed -> retry with backoff; after 5 attempts the DLQ keeps it
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from backend.climate import Climate, cell_center
from backend.climate.openmeteo import WeatherUnavailable, climatology_cost
from backend.config import Settings
from backend.context import REQUEST, RequestContext, request_scope
from backend.data.driver import Bridge, DataError
from backend.data.repo import EventIn, Repo, now_s
from backend.knowledge import budget as knowledge_budget
from backend.knowledge.profile import build_profile
from backend.metering import Meter
from backend.schemas import ModelCall
from backend.search import SearchPaused

log = logging.getLogger("leaf.jobs")

JOB_TIMEOUT_S = 240  # well under the 300 s lease, so a stuck run never overlaps a retry


class JobMessage(BaseModel):
    kind: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-z_]+$")
    key: str = Field(..., min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)


class JobQueue(Protocol):
    async def send(self, jobs: Sequence[JobMessage], delay_s: int = 0) -> None: ...


class ObjectStore(Protocol):
    async def delete(self, keys: Sequence[str]) -> None: ...


@dataclass
class JobContext:
    repo: Repo
    queue: JobQueue
    settings: Settings
    meter: Meter | None = None
    objects: ObjectStore | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[JobContext, dict[str, Any]], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def job(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


class JobRunner:
    def __init__(self, ctx: JobContext, timeout_s: float = JOB_TIMEOUT_S):
        self.ctx = ctx
        self.timeout_s = timeout_s

    async def run(self, msg: JobMessage) -> tuple[int, dict[str, Any]]:
        handler = HANDLERS.get(msg.kind)
        if handler is None:
            log.error("unknown job kind %r (key %s)", msg.kind, msg.key)
            return 422, {"status": "unknown_kind"}
        state, row = await self.ctx.repo.claim_job(msg.key, msg.kind, msg.payload)
        if state == "done":
            return 200, {"status": "already_done"}
        if state == "busy" or row is None:
            # Retry once the lease is over, so a crashed run is not retried into the DLQ.
            left = row.lease_until - now_s() if row is not None else 0
            return 409, {"status": "busy", "retry_after": max(30, min(left + 5, 3600))}
        with request_scope(RequestContext(trace_id=f"job-{row.key}"[:64])):
            try:
                result = await asyncio.wait_for(handler(self.ctx, msg.payload), self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - any failure is recorded and retried
                log.exception("job %s failed (attempt %d)", msg.key, row.attempts)
                await self.ctx.repo.fail_job(msg.key, f"{type(exc).__name__}: {exc}")
                return 500, {"status": "failed", "attempts": row.attempts}
            finally:
                if self.ctx.meter is not None:
                    await self.ctx.meter.flush()
        await self.ctx.repo.finish_job(msg.key)
        return 200, {"status": "done", "result": result}


class BridgeQueue:
    """Cloudflare Queue through the Worker bridge: POST /queue."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge

    async def send(self, jobs: Sequence[JobMessage], delay_s: int = 0) -> None:
        if not jobs:
            return
        delay = max(0, min(delay_s, 24 * 3600))  # Queues allow up to 24 h
        for i in range(0, len(jobs), 100):  # sendBatch takes at most 100 messages
            body = {
                "messages": [
                    {"body": j.model_dump(), "delaySeconds": delay} for j in jobs[i : i + 100]
                ]
            }
            await self.bridge.call("POST", "/queue", json=body)


class LocalQueue:
    """Local development without Cloudflare: runs each job in this process, in the
    background, through the same runner and idempotency checks."""

    def __init__(self) -> None:
        self.runner: JobRunner | None = None
        self._tasks: set[asyncio.Task] = set()

    async def send(self, jobs: Sequence[JobMessage], delay_s: int = 0) -> None:
        for j in jobs:
            task = asyncio.create_task(self._run(j, delay_s))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, msg: JobMessage, delay_s: int) -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        if self.runner is not None:
            status, body = await self.runner.run(msg)
            log.info("local job %s: %s %s", msg.key, status, body.get("status"))


# ---------- handlers ----------


@job("housekeeping")
async def housekeeping(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Daily: photos past 30 days (object first, then row), old meter rows, salts and jobs."""
    photos = await ctx.repo.expired_photos()
    if photos and ctx.objects is not None:  # no store configured: no objects to delete
        await ctx.objects.delete([p.r2_key for p in photos])
    removed = await ctx.repo.delete_photos(p.id for p in photos)
    purged = await ctx.repo.purge()
    return {"photos": removed, **purged}


@job("garden_ready")
async def garden_ready(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """After a garden is created: register its grid cell and queue the cell's climatology
    (built once per cell, shared by every garden in it)."""
    garden = await ctx.repo.get_garden(str(payload.get("garden_id", "")))
    if garden is None:
        return {"skipped": "garden deleted"}
    queued = False
    if garden.cell:
        lat, lon = cell_center(garden.cell)
        await ctx.repo.register_cell(garden.cell, lat, lon)
        existing = await ctx.repo.get_climatology(garden.cell)
        if existing is None or existing["status"] != "ready":
            await ctx.queue.send([climatology_job(garden.cell)])
            queued = True
    await ctx.repo.log_event(
        EventIn(garden_id=garden.id, kind="garden_ready", payload={"cell": garden.cell})
    )
    return {"cell": garden.cell, "climatology_queued": queued}


CLIMATOLOGY_PER_DAY = 8  # each build is about 780 of Open-Meteo's 10,000 daily units
STAGGER_SLOTS = 32  # deferred builds spread over 8 hours in 15-minute slots


def climatology_job(cell: str, attempt: str = "") -> JobMessage:
    key = f"climatology:{cell}" + (f":{attempt}" if attempt else "")
    return JobMessage(kind="climatology", key=key, payload={"cell": cell})


def _stagger_s(cell: str) -> int:
    """A stable 0-8 h offset per cell, so deferred builds do not all start at once."""
    slot = int(hashlib.sha256(cell.encode()).hexdigest(), 16) % STAGGER_SLOTS
    return slot * 15 * 60


async def _defer(ctx: JobContext, cell: str, until: int, why: str) -> dict[str, Any]:
    now = now_s()
    attempt = datetime.fromtimestamp(until, UTC).strftime("%Y%m%dT%H%M")
    await ctx.queue.send([climatology_job(cell, attempt)], delay_s=max(60, until - now))
    return {"deferred_until": attempt, "why": why}


@job("climatology")
async def climatology(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a cell's 1991-2020 climatology once. At most CLIMATOLOGY_PER_DAY new cells a UTC
    day, and only within Open-Meteo's daily and hourly allowance; otherwise the build waits
    (next hour, or tomorrow, staggered per cell) instead of failing."""
    cell = str(payload.get("cell", ""))
    existing = await ctx.repo.get_climatology(cell)
    if existing is not None and existing["status"] == "ready":
        return {"skipped": "already built"}
    climate: Climate = ctx.extra["climate"]
    now = now_s()
    day_start = now - now % 86400
    tomorrow = day_start + 86400 + _stagger_s(cell)
    next_hour = now - now % 3600 + 3600 + _stagger_s(cell) % 3600
    if await ctx.repo.climatologies_built_since(day_start) >= CLIMATOLOGY_PER_DAY:
        return await _defer(ctx, cell, tomorrow, "daily cell limit")
    full = await climate.om.can_spend(climatology_cost())
    if full:
        return await _defer(
            ctx, cell, tomorrow if full == "day" else next_hour, f"{full} allowance"
        )
    try:
        data = await climate.build_climatology(cell)
    except WeatherUnavailable as exc:
        if "allowance" in str(exc):  # another caller used it up meanwhile: wait, do not fail
            return await _defer(ctx, cell, next_hour, "allowance")
        await ctx.repo.put_climatology(cell, "failed", error=str(exc))
        raise
    except BaseException as exc:  # including a cancelled run: record it, then let it retry
        await ctx.repo.put_climatology(cell, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    await ctx.repo.put_climatology(cell, "ready", data, source=data.get("model", ""))
    return {"cell": cell, "zone": data["hardiness_zone"]["zone"]}


SPECIES_PER_DAY = 5  # build attempts a UTC day; each profile is 3 Tavily credits


@job("species_profile")
async def species_profile(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a species profile once (shared by every user): search, extract, verify, store.

    The daily cap counts attempts, not successes. Out of credits, the build waits for the next
    day. After its Tavily calls a build is never retried by the queue (that would spend them
    again): a failure is recorded and can be queued again after a cooldown."""
    key = str(payload.get("key", ""))
    row = await ctx.repo.get_species(key)
    if row is None:
        return {"skipped": "unknown species"}
    if row["status"] == "ready":
        return {"skipped": "already built"}
    now = now_s()
    day_start = now - now % 86400
    tomorrow = day_start + 86400 + _stagger_s(key)
    if await ctx.repo.species_started_since(day_start) >= SPECIES_PER_DAY:
        return await _defer_species(ctx, key, tomorrow, "daily species limit")
    if await knowledge_budget.credits_left(ctx.repo) < knowledge_budget.PROFILE_CREDITS:
        return await _defer_species(ctx, key, tomorrow, "daily knowledge credits")
    await ctx.repo.start_species(key)
    leaf = ctx.extra["leaf"]
    trace: list[ModelCall] = []

    async def spent(credits: int) -> None:
        await knowledge_budget.record(ctx.repo, credits)

    try:
        built = await build_profile(
            leaf.llm,
            leaf.settings,
            leaf.searcher,
            row["scientific_name"],
            row["common_names"],
            trace,
            on_credits=spent,
        )
    except SearchPaused:
        return await _defer_species(ctx, key, tomorrow, "Tavily credits")
    except Exception as exc:  # noqa: BLE001 - recorded, not retried (credits already spent)
        log.exception("species profile %s failed", key)
        await ctx.repo.fail_species(key, f"{type(exc).__name__}: {exc}")
        return {"failed": type(exc).__name__}
    ctx_trace = REQUEST.get()
    await ctx.repo.save_profile(
        key,
        row["scientific_name"],
        row["common_names"],
        built["sources"],
        built["facts"],
        ctx_trace.trace_id if ctx_trace else "",
    )
    verified = sum(1 for f in built["facts"] if f["status"] == "verified")
    return {"key": key, "verified": verified, "facts": len(built["facts"])}


def species_job(key: str, attempt: str = "") -> JobMessage:
    return JobMessage(
        kind="species_profile",
        key=f"species:{key}" + (f":{attempt}" if attempt else ""),
        payload={"key": key},
    )


async def _defer_species(ctx: JobContext, key: str, until: int, why: str) -> dict[str, Any]:
    attempt = datetime.fromtimestamp(until, UTC).strftime("%Y%m%dT%H%M")
    await ctx.queue.send([species_job(key, attempt)], delay_s=max(60, until - now_s()))
    return {"deferred_until": attempt, "why": why}


class S3Objects:
    """Deletes objects from the R2 bucket (S3 API, the same keys the journal uses)."""

    def __init__(self, bucket: str, client: Any = None):
        if client is None:
            import boto3  # only needed when the bucket is configured

            client = boto3.client("s3")
        self.bucket, self.client = bucket, client

    async def delete(self, keys: Sequence[str]) -> None:
        for i in range(0, len(keys), 1000):
            chunk = [{"Key": k} for k in keys[i : i + 1000]]
            try:
                await asyncio.to_thread(
                    self.client.delete_objects, Bucket=self.bucket, Delete={"Objects": chunk}
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as a job failure, then retried
                raise DataError(f"R2 delete failed: {type(exc).__name__}") from exc
