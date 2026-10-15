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
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from backend.config import Settings
from backend.context import RequestContext, request_scope
from backend.data.driver import Bridge, DataError
from backend.data.repo import EventIn, Repo, now_s
from backend.metering import Meter

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
    """After a garden is created: note its grid cell. M2 queues its climatology here."""
    garden = await ctx.repo.get_garden(str(payload.get("garden_id", "")))
    if garden is None:
        return {"skipped": "garden deleted"}
    await ctx.repo.log_event(
        EventIn(garden_id=garden.id, kind="garden_ready", payload={"cell": garden.cell})
    )
    return {"cell": garden.cell}


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
