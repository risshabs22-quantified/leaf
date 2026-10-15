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
from datetime import UTC, date, datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from backend.care.service import (
    CareContext,
    local_today,
    morning_message,
    plan_garden,
    triage_alert,
)
from backend.climate import Climate, cell_center
from backend.climate.nws import EVENTS, Alert
from backend.climate.openmeteo import WeatherUnavailable, climatology_cost
from backend.config import Settings
from backend.context import REQUEST, RequestContext, request_scope
from backend.data.driver import Bridge, DataError
from backend.data.repo import EventIn, Garden, RadarRun, Repo, now_s, plan_hour_utc
from backend.knowledge import budget as knowledge_budget
from backend.knowledge.profile import build_profile
from backend.metering import Meter
from backend.notify import deliver, notify
from backend.radar import regions as radar_regions
from backend.radar.collect import PAGE_RECHECK_DAYS, collect_region
from backend.schemas import ModelCall
from backend.search import SearchPaused
from backend.season.service import replan, replan_job, weather_replans, write_plan

log = logging.getLogger("leaf.jobs")

JOB_TIMEOUT_S = 240  # well under the 300 s lease, so a stuck run never overlaps a retry
# A season plan's outline is one long Ultra call that may fall back to Super; it gets more
# room, still inside the lease. Its months and checks are saved as they finish anyway.
KIND_TIMEOUT_S = {"season_plan": 285, "radar": 270}


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
                timeout = KIND_TIMEOUT_S.get(msg.kind, self.timeout_s)
                result = await asyncio.wait_for(handler(self.ctx, msg.payload), timeout)
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
    located = 0
    for row in await ctx.repo.cells_missing_region(REGIONS_PER_DAY):  # older cells, a few a day
        located += int(await _locate(ctx, row["cell"], row["lat"], row["lon"]) is not None)
    return {"photos": removed, **purged, "regions_found": located}


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
        await _locate(ctx, garden.cell, lat, lon)
        existing = await ctx.repo.get_climatology(garden.cell)
        if existing is None or existing["status"] != "ready":
            await ctx.queue.send([climatology_job(garden.cell)])
            queued = True
    await ctx.repo.log_event(
        EventIn(garden_id=garden.id, kind="garden_ready", payload={"cell": garden.cell})
    )
    return {"cell": garden.cell, "climatology_queued": queued}


REGIONS_PER_DAY = 5  # cells looked up per housekeeping run


async def _locate(ctx: JobContext, cell: str, lat: float, lon: float) -> str | None:
    """The cell's US state (kept for good), so Radar knows which regions have gardens. NWS not
    answering is not an error: it is tried again with the next housekeeping run."""
    lookup = ctx.extra.get("region_lookup")  # the app wires the NWS lookup; tests a fake
    if lookup is None:
        return None
    try:
        region = await lookup(lat, lon)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("region lookup for %s failed: %s", cell, type(exc).__name__)
        return None
    await ctx.repo.set_cell_region(cell, region)
    return region


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


# ---------- Leaf Care (M4) ----------


def _care(ctx: JobContext) -> CareContext:
    return CareContext(ctx.repo, ctx.extra["climate"], ctx.extra["leaf"])


async def _notify(ctx: JobContext, garden: Garden, **kw: Any) -> str:
    return await notify(
        ctx.repo,
        ctx.queue,
        ctx.extra.get("pusher"),
        garden,
        telegram=ctx.extra.get("telegram"),
        **kw,
    )


async def _gardens(ctx: JobContext, cell: str) -> list[Garden]:
    """The gardens a planning job covers: a weather cell, or one garden with no location yet
    ("g:<id>"), which still gets its indoor and cycle tasks every morning."""
    if cell.startswith("g:"):
        g = await ctx.repo.get_garden(cell[2:])
        return [g] if g is not None else []
    return await ctx.repo.gardens_in_cell(cell)


async def _reachable(ctx: JobContext, garden: Garden) -> bool:
    """A garden that can actually be notified (so no wording is paid for when it cannot)."""
    settings = await ctx.repo.get_notifications(garden.id)
    return "push" in settings.channels and bool(await ctx.repo.list_push(garden.id))


async def _weather_replans(ctx: JobContext, care: CareContext, garden: Garden) -> list[str]:
    """Queue a replan of the season plan's weeks a forecast frost or heat wave touches. The
    job key carries the event, so each one replans once however many mornings show it."""
    try:
        triggers = await weather_replans(care, garden)
        if triggers:
            await ctx.queue.send([replan_job(garden, t) for t in triggers])
        return [t["key"] for t in triggers]
    except Exception:  # noqa: BLE001 - the morning plan never fails because of the season plan
        log.exception("weather replan check failed for %s", garden.id)
        return []


@job("plan_cell")
async def plan_cell(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Morning planning for every garden in a cell (05:00 local), then each garden's summary
    notification. The forecast, alerts and degree days are fetched once for the cell."""
    care = _care(ctx)
    done: dict[str, Any] = {}
    for garden in await _gardens(ctx, str(payload.get("cell", ""))):
        hour = plan_hour_utc(garden.timezone)  # follows daylight saving
        if hour != garden.plan_hour_utc:
            await ctx.repo.set_plan_hour(garden.id, hour)
        await plan_garden(care, garden)
        replans = await _weather_replans(ctx, care, garden)
        day = local_today(garden).isoformat()
        tasks = await ctx.repo.list_tasks(garden.id, day, day)
        status = "no_tasks"
        if any(t.status == "open" for t in tasks):
            status = "unreachable"
            if await _reachable(ctx, garden):
                trace: list[ModelCall] = []
                title, body = await morning_message(care, garden, tasks, trace)
                # Never urgent: the morning summary waits for quiet hours to end.
                status = await _notify(
                    ctx,
                    garden,
                    kind="morning",
                    title=title,
                    body=body,
                    dedupe_key=f"morning:{garden.id}:{day}",
                )
        done[garden.id] = {"tasks": len(tasks), "notify": status, "replans": replans}
    return {"gardens": len(done), "results": done}


@job("evening_check")
async def evening_check(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """14:00 local, only on days the Worker's temperature prefilter flagged: re-plan with the
    newest forecast and warn about frost or heat before dark."""
    care = _care(ctx)
    sent = {}
    for garden in await ctx.repo.gardens_in_cell(str(payload.get("cell", ""))):
        plan = await plan_garden(care, garden)
        day = local_today(garden).isoformat()
        for u in plan.urgent:
            if u["kind"] in ("frost", "heat"):
                sent[garden.id] = await _notify(
                    ctx,
                    garden,
                    kind="alert",
                    title=u["title"],
                    body=u["text"][:160],
                    dedupe_key=f"evening:{garden.id}:{day}:{u['kind']}",
                    urgent=True,
                )
    return {"notified": sent}


@job("alert")
async def nws_alert(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A new NWS alert for a cell (found by the Worker): Nano 3 words it, the planner adds
    the protection tasks, and every garden with outdoor plants in the cell is told once per
    event and onset (NWS gives each update of an alert a new id)."""
    alert = Alert.model_validate(payload.get("alert") or {})
    if alert.event not in EVENTS:
        return {"skipped": "not a garden alert"}
    care = _care(ctx)
    gardens = []
    for g in await ctx.repo.gardens_in_cell(str(payload.get("cell", ""))):
        if any(p.setting == "outdoor" for p in await ctx.repo.list_plants(g.id)):
            gardens.append(g)
    if not gardens:
        return {"skipped": "no outdoor plants in this cell"}
    trace: list[ModelCall] = []
    triage = await triage_alert(care, alert, trace)
    sent = {}
    for garden in gardens:
        await plan_garden(care, garden)
        if not triage.relevant:
            sent[garden.id] = "not_relevant"
            continue
        body = triage.summary + (f" Protect plants by {triage.act_by}." if triage.act_by else "")
        sent[garden.id] = await _notify(
            ctx,
            garden,
            kind="alert",
            title=f"NWS {alert.event}",
            body=body[:200],
            dedupe_key=f"alert:{garden.id}:{alert.event}:{alert.onset or alert.id}",
            urgent=True,
        )
    return {"relevant": triage.relevant, "notified": sent}


@job("season_plan")
async def season_plan(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Write, check and verify a garden's season plan (queued by POST .../season). Only the
    newest write is worked on; a newer request supersedes it."""
    garden = await ctx.repo.get_garden(str(payload.get("garden_id", "")))
    if garden is None:
        return {"skipped": "garden deleted"}
    plan = await ctx.repo.get_plan(str(payload.get("plan_id", "")))
    latest = await ctx.repo.latest_season_plan(garden.id)
    if plan is None or latest is None or latest.id != plan.id:
        return {"skipped": "superseded"}
    if plan.status not in ("writing", "unverified"):
        return {"skipped": plan.status}
    return await write_plan(_care(ctx), garden, plan)


@job("replan")
async def replan_season(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Replan the weeks a change touches (forecast, diagnosis, check-in) and tell the
    gardener what changed and why."""
    garden = await ctx.repo.get_garden(str(payload.get("garden_id", "")))
    if garden is None:
        return {"skipped": "garden deleted"}
    trigger = payload.get("trigger") or {}
    result = await replan(_care(ctx), garden, trigger, str(payload.get("context", "")))
    if result.get("changed"):
        body = f"{trigger.get('text', '')} {result.get('summary', '')}".strip()
        result["notify"] = await _notify(
            ctx,
            garden,
            kind="replan",
            title="Season plan updated",
            body=body[:160],
            dedupe_key=f"replan:{garden.id}:{trigger.get('key', '')}"[:200],
            url="/season",
        )
    return result


RADAR_SERVICE = "tavily:radar"
# Tavily's plan is 1,000 credits a month and Leaf spreads what is left over the days left, so
# the daily cap can be as low as 10. Radar takes at most 8 of them: two regions a day.
RADAR_CREDITS_PER_DAY = 4  # one region a day: the rest of the allowance is for diagnoses
RADAR_RUN_CREDITS = 4  # two searches and, for up to ten pages, two page reads
DIAGNOSIS_RESERVE = 6  # Tavily credits left for diagnoses before Radar may run
RADAR_RUN_TIMEOUT_S = 200  # inside the queue runner's own limit


def radar_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


@job("radar")
async def radar(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Weekly, per state with gardens: read that state's extension sites and keep the current
    reports that pass the code checks. Out of credits, the week is recorded as skipped. After its
    Tavily calls the run is never retried by the queue (that would spend them again): a failure
    is recorded and the region is tried again next week."""
    region = str(payload.get("region", ""))
    today = datetime.now(UTC).date()
    week = str(payload.get("week") or radar_week(today))
    if not radar_regions.covered(region):
        return {"skipped": "region not covered"}

    async def record(status: str, note: str, c: Any = None) -> None:
        await ctx.repo.record_radar_run(
            RadarRun(
                region=region,
                week=week,
                status=status,
                searched=getattr(c, "searched", 0),
                read=getattr(c, "read", 0),
                relevant=getattr(c, "relevant", 0),
                undated=getattr(c, "undated", 0),
                rejected=getattr(c, "rejected", 0),
                reports=len(getattr(c, "reports", [])),
                credits=getattr(c, "credits", 0),
                note=note,
                created_at=now_s(),
            )
        )

    used = await ctx.repo.api_usage_on(RADAR_SERVICE)
    if RADAR_CREDITS_PER_DAY - used < RADAR_RUN_CREDITS:
        await record("skipped", "Today's Radar credits were used by other regions.")
        return {"skipped": "daily radar credits"}
    leaf = ctx.extra["leaf"]
    budget = getattr(leaf.searcher, "budget", None)
    if budget is not None and await budget.remaining() < RADAR_RUN_CREDITS + DIAGNOSIS_RESERVE:
        # Diagnosis shares Tavily's daily allowance and comes first.
        await record("skipped", "Search credits are being kept for diagnoses today.")
        return {"skipped": "credits reserved for diagnosis"}
    trace: list[ModelCall] = []

    async def spent(credits: int) -> None:
        await ctx.repo.add_api_usage(RADAR_SERVICE, float(credits))

    async def read_lately(urls: list[str]) -> set[str]:
        return await ctx.repo.radar_pages_checked(urls, now_s() - PAGE_RECHECK_DAYS * 86400)

    try:
        c = await asyncio.wait_for(
            collect_region(
                leaf.llm,
                leaf.settings,
                leaf.searcher,
                region,
                today,
                week,
                read_lately,
                trace,
                on_credits=spent,
            ),
            RADAR_RUN_TIMEOUT_S,
        )
    except TimeoutError:  # slow pages: recorded, not retried (that would pay for them again)
        await record("failed", "The run took too long.")
        return {"failed": "timeout"}
    except SearchPaused:
        await record("skipped", "Tavily credits are used up for now.")
        return {"skipped": "Tavily credits"}
    except Exception as exc:  # noqa: BLE001 - recorded, not retried (credits already spent)
        log.exception("radar %s failed", region)
        await record("failed", f"{type(exc).__name__}")
        return {"failed": type(exc).__name__}
    ctx_trace = REQUEST.get()
    trace_id = ctx_trace.trace_id if ctx_trace else ""
    stored = await ctx.repo.add_radar_reports([{**r, "trace_id": trace_id} for r in c.reports])
    await ctx.repo.mark_radar_pages(region, c.outcomes)
    await record("done", c.note, c)
    return {
        "region": region,
        "searched": c.searched,
        "read": c.read,
        "relevant": c.relevant,
        "reports": stored,
        "rejected": c.rejected,
        "credits": c.credits,
    }


def radar_job(region: str, week: str) -> JobMessage:
    return JobMessage(
        kind="radar", key=f"radar:{region}:{week}", payload={"region": region, "week": week}
    )


@job("notify_send")
async def notify_send(ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    """A notification held back by quiet hours, sent when they end."""
    row = await ctx.repo.get_notification(str(payload.get("id", "")))
    if row is None:
        return {"skipped": "gone"}
    garden = await ctx.repo.get_garden(row["garden_id"])
    if garden is None:
        return {"skipped": "garden deleted"}
    return {
        "status": await deliver(
            ctx.repo,
            ctx.extra.get("pusher"),
            garden,
            row["id"],
            telegram=ctx.extra.get("telegram"),
        )
    }


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
