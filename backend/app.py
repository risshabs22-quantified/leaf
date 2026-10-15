"""FastAPI app: serves the API and the single-page frontend from one container.

Run locally:  uvicorn backend.app:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Any, TypeVar

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

from backend.actives import apply_garden
from backend.cache import SharedCache
from backend.calendar_file import Reminder, ics
from backend.care.service import CareContext
from backend.checkin import check_in
from backend.climate import Climate
from backend.conditions import ConditionsError, WeatherClient
from backend.config import ULTRA, Settings, get_settings
from backend.connect.api_v1 import router as api_v1_router
from backend.connect.manage import router as connect_router
from backend.connect.mcp import router as mcp_router
from backend.connect.telegram import drain as drain_telegram
from backend.connect.telegram import router as telegram_router
from backend.context import REQUEST, RequestContext
from backend.data import open_data
from backend.data.driver import DataError
from backend.deep import CaseStateError, DeepLeaf, DeepResponse
from backend.jobs import BridgeQueue, JobContext, JobRunner, LocalQueue, S3Objects
from backend.journal import JournalEntry, open_journal, public_view, thumbnail
from backend.metering import Meter, MeteredLLM
from backend.models import ModelError, SpendCapReached, TokenFactory
from backend.notify.telegram import TelegramApi
from backend.notify.webpush import WebPush
from backend.pipeline import Leaf
from backend.platform_api import refusal_for
from backend.platform_api import router as platform_router
from backend.router import front_door
from backend.schemas import ChatRequest, ChatResponse, DiagnoseResponse, Garden
from backend.search import (
    CreditBudget,
    NullSearcher,
    Searcher,
    TavilySearcher,
    search_paused,
    tavily_balance,
)
from backend.season.service import checkin_loop
from backend.spray import SprayForecast, plan_spray
from backend.store import MemoryCaseStore
from backend.vision import discover_vision_model, prepare_image, prepare_photo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
# httpx logs "POST <url>" for every request, and a Telegram Bot API URL contains the bot token.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("leaf.app")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DEEP_PHOTOS = 3
PHOTO_LABELS = {"whole plant", "close-up", "underside", "photo"}
MAX_LIMITER_KEYS = 10_000
# The front door costs a tiny Nano call; garden creation writes to D1. Both have own limits.
ROUTE_PER_IP_PER_HOUR, ROUTE_DAILY_CAP = 60, 3000
GARDEN_PER_IP_PER_HOUR, GARDEN_DAILY_CAP = 10, 500
# Public /api/climate: about 1.9 Open-Meteo units per new cell-hour, so its own small budget.
CLIMATE_PER_IP_PER_HOUR, CLIMATE_DAILY_CAP = 20, 500
# Species profiles: a lookup only reads; builds (3 Tavily credits) are queued by confirmed
# identifications and garden plants. Identification: vision, a Super check, 1 Tavily credit.
SPECIES_PER_IP_PER_HOUR, SPECIES_DAILY_CAP = 60, 3000
IDENTIFY_PER_IP_PER_HOUR, IDENTIFY_DAILY_CAP = 10, 60
# Every garden request reads or writes D1 and keeps the container awake.
GARDEN_USE_PER_IP_PER_HOUR, GARDEN_USE_DAILY_CAP = 600, 50_000
SPEND_CAP_MESSAGE = (
    "Leaf has used today's model budget, so diagnosis is paused until 00:00 UTC. "
    "Care reminders and weather alerts keep working."
)
T = TypeVar("T")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "style-src-attr 'unsafe-inline'; font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class RateLimiter:
    """Protects Token Factory and Tavily credits on the public demo."""

    def __init__(self, per_ip_per_hour: int, daily_cap: int):
        self.per_ip = per_ip_per_hour
        self.daily_cap = daily_cap
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.day = date.today()
        self.day_count = 0

    def _evict(self, now: float) -> None:
        """Drop idle IPs first, then the oldest entries; never reset everyone's counters."""
        for ip in [ip for ip, q in self.hits.items() if not q or now - q[-1] > 3600]:
            del self.hits[ip]
        while len(self.hits) > MAX_LIMITER_KEYS * 9 // 10:
            del self.hits[next(iter(self.hits))]  # dicts keep insertion order: oldest first

    def check(self, ip: str) -> None:
        now = time.time()
        if date.today() != self.day:
            self.day, self.day_count = date.today(), 0
        if len(self.hits) > MAX_LIMITER_KEYS:  # bound memory if someone sprays fake IPs
            self._evict(now)
        if self.day_count >= self.daily_cap:
            raise HTTPException(429, "Leaf has hit today's demo limit. Please try tomorrow.")
        q = self.hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_ip:
            raise HTTPException(429, "You've hit the hourly limit. Please try again later.")
        q.append(now)
        self.day_count += 1


class DailyCap:
    """A per-day counter (Deep mode has its own, smaller cap than the overall demo cap)."""

    def __init__(self, cap: int):
        self.cap, self.day, self.count = cap, date.today(), 0

    def take(self) -> bool:
        if date.today() != self.day:
            self.day, self.count = date.today(), 0
        if self.count >= self.cap:
            return False
        self.count += 1
        return True

    @property
    def left(self) -> int:
        return max(0, self.cap - self.count) if date.today() == self.day else self.cap


def client_ip(request: Request, hops: int = 1) -> str:
    """Counting from the right, the `hops`-th X-Forwarded-For entry is the one our own
    proxy appended; anything further left is client-supplied and can be spoofed to dodge
    the per-IP limit. With hops=0 (no proxy in front) the header is ignored entirely."""
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",")]
        parts = [p for p in parts if p]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "?"


class BodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(413, "That request is too large.")


class BodyLimitMiddleware:
    """Refuse oversized bodies before they are buffered.

    Starlette spools a whole multipart upload to memory or disk before our handler can
    look at it, so the size check has to happen at the ASGI layer: first from the
    declared Content-Length, then by counting bytes as they stream in.
    """

    def __init__(
        self,
        app,
        limits: dict[str, int],
        default: int,
        pattern_limits: list[tuple[str, str, int]] | None = None,
    ):
        self.app, self.limits, self.default = app, limits, default
        self.pattern_limits = pattern_limits or []  # (path prefix, path suffix, limit)

    def limit_for(self, path: str) -> int:
        if path in self.limits:
            return self.limits[path]
        for prefix, suffix, limit in self.pattern_limits:
            if path.startswith(prefix) and path.endswith(suffix):
                return limit
        return self.default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope["path"])
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            return await JSONResponse({"detail": BodyTooLarge().detail}, 413)(scope, receive, send)

        seen = 0
        started = False

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise BodyTooLarge()
            return msg

        async def tracking_send(msg):
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge as exc:
            if started:
                raise
            await JSONResponse({"detail": exc.detail}, 413)(scope, receive, send)


async def build_leaf(settings: Settings) -> Leaf:
    llm = TokenFactory(settings)
    if settings.tavily_api_key:
        key = settings.tavily_api_key
        budget = CreditBudget(lambda: tavily_balance(key), max_daily=settings.tavily_daily_cap)
        searcher: Searcher = TavilySearcher(key, budget)
    else:
        searcher = NullSearcher()
    vision, _ = await asyncio.gather(discover_vision_model(llm, settings), llm.load_prices())
    return Leaf(llm, searcher, settings, vision)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.limiter = RateLimiter(settings.per_ip_per_hour, settings.daily_cap)
    # Spray windows and calendar files cost no credits; this only protects the weather service.
    app.state.tool_limiter = RateLimiter(TOOL_PER_IP_PER_HOUR, TOOL_DAILY_CAP)
    if not getattr(app.state, "weather", None):
        app.state.weather = WeatherClient()
    app.state.slots = asyncio.Semaphore(settings.max_concurrent)
    if not getattr(app.state, "leaf", None):
        app.state.leaf = await build_leaf(settings)
    await start_platform(app, app.state.leaf.settings)  # before anything holds leaf.llm
    deep = getattr(app.state, "deep", None)
    if deep is None or deep.leaf is not app.state.leaf:
        app.state.deep = DeepLeaf(app.state.leaf, MemoryCaseStore(), weather=app.state.weather)
    app.state.deep_cap = DailyCap(app.state.leaf.settings.deep_daily_cap)
    s = app.state.leaf.settings
    if getattr(app.state, "journal", None) is None:
        app.state.journal = open_journal(s.journal_path, s.journal_bucket)
    try:
        removed = await app.state.journal.purge()
    except Exception:  # noqa: BLE001 - a storage hiccup must not stop the app starting
        log.exception("journal purge failed; expired entries are still hidden on read")
        removed = 0
    if removed:
        log.info("journal: removed %d expired entries", removed)
    flusher = asyncio.create_task(_flush_meter_forever(app.state.meter))
    try:
        yield
    finally:
        await drain_telegram()  # messages still being answered get a moment to finish
        flusher.cancel()
        await app.state.meter.flush()


METER_FLUSH_S = 15  # streamed Deep cases make calls after their response has started


async def _flush_meter_forever(meter: Meter) -> None:
    while True:
        await asyncio.sleep(METER_FLUSH_S)
        try:
            await meter.flush()
        except Exception:  # noqa: BLE001 - the meter must never take the app down
            log.exception("meter flush failed")


async def start_platform(app: FastAPI, s: Settings) -> None:
    """Data, meter, cache, queue and job runner, and the meter around every model call."""
    data = await open_data(s)
    if data.schema_pending:
        log.error("D1 is missing migrations %s; run the deploy script", data.schema_pending)
    meter = Meter(s.spend_cap_usd, s.brownout, data.repo)
    await meter.restore()
    leaf: Leaf = app.state.leaf
    if isinstance(leaf.llm, MeteredLLM):
        leaf.llm.meter = meter
    else:
        leaf.llm = MeteredLLM(leaf.llm, meter)
    app.state.cache = SharedCache(data.kv, meter)
    if getattr(app.state, "climate", None) is None or app.state.climate.repo is not data.repo:
        app.state.climate = Climate(data.repo, app.state.cache)
    weather = getattr(app.state, "weather", None)
    if isinstance(weather, WeatherClient) and weather.om is None:
        weather.om = app.state.climate.om  # every Open-Meteo forecast call shares one allowance
    app.state.climate_limiter = RateLimiter(CLIMATE_PER_IP_PER_HOUR, CLIMATE_DAILY_CAP)
    app.state.species_limiter = RateLimiter(SPECIES_PER_IP_PER_HOUR, SPECIES_DAILY_CAP)
    app.state.identify_limiter = RateLimiter(IDENTIFY_PER_IP_PER_HOUR, IDENTIFY_DAILY_CAP)
    queue = BridgeQueue(data.bridge) if data.bridge else LocalQueue()
    objects = S3Objects(s.journal_bucket) if s.journal_bucket else None
    if getattr(app.state, "pusher", None) is None and s.vapid_private_key:
        try:
            app.state.pusher = WebPush(s.vapid_private_key, s.vapid_subject)
        except Exception:  # noqa: BLE001 - a bad key disables push, never the app
            log.error("VAPID private key is unusable; Web Push is off")
    pusher = getattr(app.state, "pusher", None)
    if getattr(app.state, "telegram", None) is None and (
        s.telegram_bot_token and s.telegram_webhook_secret
    ):
        app.state.telegram = TelegramApi(s.telegram_bot_token)
    app.state.tg_seen = deque(maxlen=2000)
    app.state.tg_limiter = RateLimiter(40, 100_000)  # messages per chat per hour
    app.state.tg_warned = {}  # chat id -> when it was last told to slow down
    runner = JobRunner(
        JobContext(
            data.repo,
            queue,
            s,
            meter,
            objects,
            extra={
                "climate": app.state.climate,
                "leaf": app.state.leaf,
                "pusher": pusher,
                "telegram": getattr(app.state, "telegram", None),
            },
        )
    )
    if isinstance(queue, LocalQueue):
        queue.runner = runner
    app.state.data, app.state.meter, app.state.queue, app.state.runner = data, meter, queue, runner
    app.state.route_limiter = RateLimiter(ROUTE_PER_IP_PER_HOUR, ROUTE_DAILY_CAP)
    app.state.garden_limiter = RateLimiter(GARDEN_PER_IP_PER_HOUR, GARDEN_DAILY_CAP)
    app.state.garden_use_limiter = RateLimiter(GARDEN_USE_PER_IP_PER_HOUR, GARDEN_USE_DAILY_CAP)


app = FastAPI(title="Leaf", version="0.1.0", lifespan=lifespan)
app.include_router(platform_router)
app.include_router(api_v1_router)
app.include_router(connect_router)
app.include_router(mcp_router)
app.include_router(telegram_router)


@app.exception_handler(DataError)
async def data_unavailable(request: Request, exc: DataError) -> JSONResponse:
    """Garden storage (D1 through the bridge) is down: a clear 503, never a bare 500."""
    log.error("data error on %s: %s", request.url.path, exc)
    return JSONResponse(
        {"detail": "Leaf's garden storage is unavailable. Please try again."}, status_code=503
    )


app.add_middleware(
    BodyLimitMiddleware,
    limits={
        "/api/diagnose": MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/deep": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/deep/stream": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/auto": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/auto/stream": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/chat": 256 * 1024,
        "/api/identify": MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/v1/diagnose": MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/v1/identify": MAX_UPLOAD_BYTES + 256 * 1024,
        "/mcp": MAX_UPLOAD_BYTES * 4 // 3 + 512 * 1024,  # an image as base64
    },
    default=64 * 1024,
    pattern_limits=[("/api/cases/", "/checkin", MAX_UPLOAD_BYTES + 256 * 1024)],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
        response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path in (
        "/",
        "/app",
        "/plants",
        "/identify",
        "/today",
    ) or request.url.path.startswith(("/static/", "/c/", "/g/")):
        # Revalidate every time (a cheap 304 via ETag), so a deploy never leaves a returning
        # visitor running last version's script against this version's API.
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


_background: set[asyncio.Task] = set()


@app.middleware("http")
async def trace_context(request: Request, call_next):
    """A trace id for every request (X-Leaf-Trace), stored with each model call it makes."""
    ctx = RequestContext()
    meter: Meter | None = getattr(request.app.state, "meter", None)
    api = request.url.path.startswith("/api/") and meter is not None
    if api:
        ip = client_ip(request, request.app.state.leaf.settings.trusted_proxy_hops)
        try:
            ctx.visitor = await meter.visitor(ip)  # one D1 read a day, then cached
        except DataError as exc:
            log.warning("visitor id unavailable: %s", exc)
    token = REQUEST.set(ctx)
    try:
        response = await call_next(request)
    finally:
        REQUEST.reset(token)
    response.headers["X-Leaf-Trace"] = ctx.trace_id
    if api:
        task = asyncio.create_task(_after_request(meter, ctx))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return response


async def _after_request(meter: Meter, ctx: RequestContext) -> None:
    """Count the active user, then write the meter to D1 off the request path.

    One person is one visitor id (a salted hash of their address), whether they opened a
    garden or used a model (the meter counts model users as it records each call); counting
    the garden as well would count them twice and flatter the cost per user."""
    try:
        if ctx.actor:
            meter.active(ctx.visitor)
        await meter.flush()
    except DataError as exc:
        log.warning("after-request metering failed: %s", exc)


def check_limits(request: Request) -> None:
    if request.app.state.slots.locked():  # shed load first so a 503 costs the user nothing
        raise HTTPException(503, "Leaf is busy right now. Please try again in a minute.")
    settings = request.app.state.leaf.settings
    request.app.state.limiter.check(client_ip(request, settings.trusted_proxy_hops))


async def run_limited(
    request: Request, work: Callable[[], Awaitable[T]], timeout: float | None = None
) -> T:
    """Run one diagnosis or chat under the concurrency cap and the whole-request deadline.

    Each model call has its own timeout and retries, which add up across fallbacks; the
    deadline stops one request from holding a slot (and spending credits) for minutes.
    """
    leaf: Leaf = request.app.state.leaf
    slots: asyncio.Semaphore = request.app.state.slots
    deadline = timeout or leaf.settings.deadline_s
    if slots.locked():
        raise HTTPException(503, "Leaf is busy right now. Please try again in a minute.")
    async with slots:
        try:
            return await asyncio.wait_for(work(), timeout=deadline)
        except TimeoutError as exc:
            log.error("request exceeded %ss deadline", deadline)
            raise HTTPException(504, "That took too long. Please try again.") from exc
        except SpendCapReached as exc:
            log.warning("spend cap: %s", exc)
            raise HTTPException(503, SPEND_CAP_MESSAGE) from exc
        except ModelError as exc:
            log.error("model pipeline failed: %s", exc)
            raise HTTPException(502, "Leaf's models are busy right now. Please try again.") from exc


@app.get("/api/health")
async def health(request: Request) -> dict:
    leaf: Leaf = request.app.state.leaf
    s = leaf.settings
    return {
        "ok": True,
        "models": {
            "vision": leaf.vision_model,
            "reason": s.reason_model,
            "fallback": s.fallback_model,
            "fast": s.fast_model,
            "front_door": (s.models("front_door") or [""])[0],
            "background_screen": (s.models("checkin_screen") or [""])[0],
            "orchestrator": s.role_model(ULTRA),
        },
        "platform": {
            "data": request.app.state.data.db.name,
            "schema_pending": list(await request.app.state.data.recheck()),
            "guard": request.app.state.meter.snapshot(),
        },
        "search": type(leaf.searcher).__name__,
        "search_paused": search_paused(leaf.searcher),
        "deep": {"left_today": request.app.state.deep_cap.left},
    }


@app.post("/api/diagnose", response_model=DiagnoseResponse)
async def diagnose(
    request: Request,
    image: Annotated[UploadFile | None, File()] = None,
    notes: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    answers: Annotated[str, Form()] = "",
    organic: Annotated[bool, Form()] = False,
    pets: Annotated[bool, Form()] = False,
) -> DiagnoseResponse:
    check_limits(request)
    data = await image.read(MAX_UPLOAD_BYTES + 1) if image is not None else None
    return await diagnose_photo(
        request, data, notes, region, answers, Garden(organic=organic, pets=pets)
    )


async def diagnose_photo(
    request: Request,
    data: bytes | None,
    notes: str,
    region: str,
    answers: str,
    garden: Garden,
) -> DiagnoseResponse:
    """A Quick diagnosis from a photo (raw bytes) and/or notes; the web form, the API, MCP and
    Telegram all come through here. The caller has already applied its own rate limit."""
    if data is None and not notes.strip():
        raise HTTPException(400, "Add a photo or describe what you see.")
    image_b64 = mime = None
    if data is not None:
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Photo is over 10 MB. Try a smaller one.")
        try:  # image decoding is CPU-bound: keep it off the event loop
            image_b64, mime = await asyncio.to_thread(prepare_image, data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    result = await run_limited(
        request,
        lambda: request.app.state.leaf.diagnose(
            image_b64=image_b64,
            mime=mime or "image/jpeg",
            notes=notes[:2000],
            region=region[:100],
            answers=answers[:2000],
            garden=garden,
        ),
    )
    await save_report(request, result, "quick", [image_b64] if image_b64 else [], ["photo"])
    return result


TOOL_PER_IP_PER_HOUR, TOOL_DAILY_CAP = 40, 4000


class SprayRequest(BaseModel):
    region: str = Field("", max_length=100)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    heat_limit_c: float = Field(32.0, ge=20, le=40)
    bees: bool = False


@app.post("/api/spray-window", response_model=SprayForecast)
async def spray_window(request: Request, body: SprayRequest) -> SprayForecast:
    """The best hours to spray in the next five days, scored in code from the forecast."""
    request.app.state.tool_limiter.check(
        client_ip(request, request.app.state.leaf.settings.trusted_proxy_hops)
    )
    weather: WeatherClient = request.app.state.weather
    try:
        if body.lat is not None and body.lon is not None:
            lat, lon, place = body.lat, body.lon, "your location"
        elif body.region.strip():
            found = await weather.geocode(body.region)
            if found is None:
                raise HTTPException(404, "Leaf could not find that place. Try a city and state.")
            lat, lon, place = found.lat, found.lon, found.label
        else:
            raise HTTPException(400, "Add your town or use your location to plan a spray.")
        data = await weather.forecast(lat, lon)
    except ConditionsError as exc:
        raise HTTPException(502, str(exc)) from exc
    return plan_spray(data, None, body.heat_limit_c, body.bees, place)


@app.post("/api/calendar.ics")
async def calendar_file(request: Request, body: Reminder) -> Response:
    """A calendar reminder (check-in or spray window) as an .ics file to open or import."""
    request.app.state.tool_limiter.check(
        client_ip(request, request.app.state.leaf.settings.trusted_proxy_hops)
    )
    text = ics(body, request.app.state.leaf.settings.public_url or str(request.base_url))
    return Response(
        text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leaf-reminder.ics"'},
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    check_limits(request)
    leaf: Leaf = request.app.state.leaf

    async def work() -> ChatResponse:
        # The front door screens the message first (one Nano call, or code alone); a message
        # it stops gets a fixed reply and never reaches the chat model.
        door = await front_door(
            leaf.llm, leaf.settings, body.message, request.app.state.cache, intent=None
        )
        if not door.safe:
            return ChatResponse(reply=refusal_for(door), trace=door.trace)
        resp = await leaf.chat(body)
        resp.trace = door.trace + resp.trace
        return resp

    return await run_limited(request, work)


async def _read_photos(
    pairs: list[tuple[StarletteUploadFile, str]],
) -> list[tuple[str, str, str]]:
    photos = []
    for up, raw_label in pairs:
        data = await up.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "A photo is over 10 MB. Try a smaller one.")
        try:
            full, view = await asyncio.to_thread(prepare_photo, data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        label = raw_label.strip().lower()
        photos.append((label if label in PHOTO_LABELS else "photo", full, view))
    return photos


def _deep_deadline(request: Request) -> float:
    s = request.app.state.leaf.settings
    # The case's own clock covers first looks, the first differential and the investigation.
    # After it, the second opinion (up to 35 s) runs alongside reads, plan and claim check
    # (up to 50 s), or a Quick fallback runs (up to 40 s).
    return s.deep_wall_s + 60


class AnswersRequest(BaseModel):
    answers: list[Annotated[str, Field(max_length=400)]] = Field(..., max_length=3)


async def _deep_inputs(
    request: Request, notes: str, region: str, lat: float | None, lon: float | None
) -> tuple[list[tuple[str, str, str]], float | None, float | None]:
    """Shared by the JSON and streaming endpoints. Photos and labels are read from the form
    directly: browsers differ in how they send an empty file input (a file with no name, or
    a plain string), and either must be skipped without shifting the labels after it."""
    form = await request.form()
    labels = [str(x) for x in form.getlist("labels")]
    pairs = [
        (im, labels[i] if i < len(labels) else "photo")
        for i, im in enumerate(form.getlist("images"))
        if isinstance(im, StarletteUploadFile) and im.filename
    ]
    if len(pairs) > MAX_DEEP_PHOTOS:
        raise HTTPException(400, f"Send at most {MAX_DEEP_PHOTOS} photos.")
    if not pairs and not notes.strip():
        raise HTTPException(400, "Add a photo or describe what you see.")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    return await _read_photos(pairs), lat, lon


def _deep_work(
    request: Request, photos, notes: str, region: str, lat, lon, garden: Garden
) -> tuple[Callable[[Any], Awaitable[DeepResponse]], float | None]:
    """What to run for a new case: Deep, or Quick with a notice once today's Deep cap is used."""
    deep: DeepLeaf = request.app.state.deep
    notes, region = notes[:2000], region[:100]
    if request.app.state.slots.locked():  # a request that will be refused must not spend the cap
        raise HTTPException(503, "Leaf is busy right now. Please try again in a minute.")
    if not request.app.state.deep_cap.take():
        cap = request.app.state.leaf.settings.deep_daily_cap
        notice = (
            f"Deep mode has reached today's limit of {cap} cases, so this is a Quick diagnosis."
        )
        return (
            lambda emit: deep.quick_only(
                photos=photos, notes=notes, region=region, notice=notice, garden=garden
            ),
            None,
        )
    return (
        lambda emit: deep.start(
            photos=photos, notes=notes, region=region, lat=lat, lon=lon, emit=emit, garden=garden
        ),
        _deep_deadline(request),
    )


def _auto_work(
    request: Request, photos, notes: str, region: str, lat, lon, garden: Garden
) -> tuple[Callable[[Any], Awaitable[DeepResponse]], float]:
    """Auto: Quick first; today's Deep cap is spent only if Leaf decides to investigate."""
    deep: DeepLeaf = request.app.state.deep
    notes, region = notes[:2000], region[:100]
    cap: DailyCap = request.app.state.deep_cap

    def may_investigate() -> str:
        if cap.take():
            return ""
        return f"Deep mode has reached today's limit of {cap.cap} cases, so Leaf stopped here."

    return (
        lambda emit: deep.auto(
            photos=photos,
            notes=notes,
            region=region,
            lat=lat,
            lon=lon,
            emit=emit,
            may_investigate=may_investigate,
            garden=garden,
        ),
        request.app.state.leaf.settings.deadline_s + _deep_deadline(request),
    )


async def _ignore_event(_: dict[str, Any]) -> None:
    return None


@app.post("/api/auto", response_model=DeepResponse)
async def auto(
    request: Request,
    notes: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
    organic: Annotated[bool, Form()] = False,
    pets: Annotated[bool, Form()] = False,
) -> DeepResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _auto_work(
        request, photos, notes, region, lat, lon, Garden(organic=organic, pets=pets)
    )
    result = await run_limited(request, lambda: work(_ignore_event), timeout=timeout)
    await save_report(
        request, result, result.mode, [v for _, _, v in photos], [lb for lb, _, _ in photos]
    )
    return result


@app.post("/api/auto/stream")
async def auto_stream(
    request: Request,
    notes: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
    organic: Annotated[bool, Form()] = False,
    pets: Annotated[bool, Form()] = False,
) -> StreamingResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _auto_work(
        request, photos, notes, region, lat, lon, Garden(organic=organic, pets=pets)
    )
    return _event_stream(request, work, timeout, [(v, lb) for lb, _, v in photos])


@app.post("/api/deep", response_model=DeepResponse)
async def deep(
    request: Request,
    notes: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
    organic: Annotated[bool, Form()] = False,
    pets: Annotated[bool, Form()] = False,
) -> DeepResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _deep_work(
        request, photos, notes, region, lat, lon, Garden(organic=organic, pets=pets)
    )
    result = await run_limited(request, lambda: work(_ignore_event), timeout=timeout)
    await save_report(
        request, result, result.mode, [v for _, _, v in photos], [lb for lb, _, _ in photos]
    )
    return result


async def _waiting_case(request: Request, case_id: str):
    case = await request.app.state.deep.store.get(case_id[:64])
    if case is None:
        raise HTTPException(404, "This case has expired or does not exist. Start a new one.")
    return case


@app.post("/api/deep/{case_id}/answers", response_model=DeepResponse)
async def deep_answers(request: Request, case_id: str, body: AnswersRequest) -> DeepResponse:
    check_limits(request)
    case = await _waiting_case(request, case_id)
    deep: DeepLeaf = request.app.state.deep
    try:
        result = await run_limited(
            request, lambda: deep.resume(case, body.answers), timeout=_deep_deadline(request)
        )
    except CaseStateError as exc:
        raise HTTPException(409, str(exc)) from exc
    await save_report(
        request,
        result,
        result.mode,
        [p.view_b64 for p in case.photos],
        [p.label for p in case.photos],
    )
    return result


KEEPALIVE_S = 15  # comment lines keep proxies from closing a quiet stream


def _event_stream(
    request: Request,
    work: Callable[[Any], Awaitable[DeepResponse]],
    timeout: float | None,
    photos: list[tuple[str, str]] | None = None,
) -> StreamingResponse:
    """Run one case and stream its events as server-sent events, ending with the result.

    The case runs as a task under the concurrency cap and deadline; events reach the client
    as they happen. If the client goes away, the case is cancelled.
    """
    slots: asyncio.Semaphore = request.app.state.slots
    if slots.locked():
        raise HTTPException(503, "Leaf is busy right now. Please try again in a minute.")
    deadline = timeout or request.app.state.leaf.settings.deadline_s
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def runner() -> None:
        async with slots:
            try:
                res = await asyncio.wait_for(work(emit), timeout=deadline)
                await save_report(
                    request,
                    res,
                    res.mode,
                    [v for v, _ in photos or []],
                    [lb for _, lb in photos or []],
                )
                await queue.put({"type": "result", "response": res.model_dump(mode="json")})
            except TimeoutError:
                await queue.put(
                    {
                        "type": "error",
                        "status": 504,
                        "detail": "That took too long. Please try again.",
                    }
                )
            except SpendCapReached:
                await queue.put({"type": "error", "status": 503, "detail": SPEND_CAP_MESSAGE})
            except ModelError:
                await queue.put(
                    {
                        "type": "error",
                        "status": 502,
                        "detail": "Leaf's models are busy right now. Please try again.",
                    }
                )
            except CaseStateError as exc:
                await queue.put({"type": "error", "status": 409, "detail": str(exc)})
            except Exception:  # noqa: BLE001 - the stream must end cleanly, never hang
                log.exception("streamed case failed")
                await queue.put(
                    {
                        "type": "error",
                        "status": 500,
                        "detail": "Something went wrong. Please try again.",
                    }
                )
            finally:
                await queue.put(None)

    task = asyncio.create_task(runner())

    async def body():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    break
                yield f"data: {json.dumps(item, default=str)}\n\n"
        finally:
            if not task.done():
                task.cancel()  # the client left: stop spending on this case

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/deep/stream")
async def deep_stream(
    request: Request,
    notes: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
    organic: Annotated[bool, Form()] = False,
    pets: Annotated[bool, Form()] = False,
) -> StreamingResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _deep_work(
        request, photos, notes, region, lat, lon, Garden(organic=organic, pets=pets)
    )
    return _event_stream(request, work, timeout, [(v, lb) for lb, _, v in photos])


@app.post("/api/deep/{case_id}/answers/stream")
async def deep_answers_stream(
    request: Request, case_id: str, body: AnswersRequest
) -> StreamingResponse:
    check_limits(request)
    case = await _waiting_case(request, case_id)
    if case.status != "needs_answers":
        raise HTTPException(409, "This case is not waiting for answers.")
    deep: DeepLeaf = request.app.state.deep
    return _event_stream(
        request,
        lambda emit: deep.resume(case, body.answers, emit),
        _deep_deadline(request),
        [(p.view_b64, p.label) for p in case.photos],
    )


async def save_report(
    request: Request, report: DiagnoseResponse, mode: str, views: list[str], labels: list[str]
) -> None:
    """Keep a finished report in the journal and put its id on the response. A case waiting
    for answers is saved when it finishes. A journal failure never fails the diagnosis."""
    if getattr(report, "status", "done") == "needs_answers":
        return
    apply_garden(report.plan, report.garden)  # in code, whatever the plan writer did
    settings = request.app.state.leaf.settings
    try:
        thumbs = [await asyncio.to_thread(thumbnail, v) for v in views[:MAX_DEEP_PHOTOS]]
        entry = JournalEntry(
            id=getattr(report, "case_id", "") or JournalEntry().id,
            expires=time.time() + settings.journal_days * 86400,
            mode="deep" if mode == "deep" else "quick",
            photos=thumbs,
            labels=labels[: len(thumbs)],
        )
        report.case_id = entry.id
        report.owner_token = entry.owner_token  # only the creator gets it; it allows check-ins
        entry.report = report.model_dump(mode="json", exclude={"owner_token"})
        await request.app.state.journal.put(entry)
    except Exception:  # noqa: BLE001 - the gardener still gets the diagnosis
        log.exception("could not save the report to the journal")
        report.owner_token = ""  # nothing was saved, so the page offers no share link


async def _entry(request: Request, case_id: str) -> JournalEntry:
    try:
        entry = await request.app.state.journal.get(case_id[:64])
    except Exception as e:  # noqa: BLE001 - storage down or misconfigured
        log.exception("could not read the journal")
        raise HTTPException(503, "Saved reports are unavailable right now. Try again later.") from e
    if entry is None:
        raise HTTPException(404, "This report has expired or does not exist.")
    return entry


@app.get("/api/cases/{case_id}")
async def get_case(request: Request, case_id: str) -> dict:
    return public_view(await _entry(request, case_id))


_checkin_locks: dict[str, asyncio.Lock] = {}


@app.post("/api/cases/{case_id}/checkin")
async def checkin(
    request: Request,
    case_id: str,
    image: Annotated[UploadFile, File()],
    notes: Annotated[str, Form()] = "",
) -> dict:
    check_limits(request)
    entry = await _entry(request, case_id)
    # A share link is read-only: check-ins need the owner token given to the report's creator.
    token = request.headers.get("x-leaf-owner", "")
    if not token or not secrets.compare_digest(token, entry.owner_token):
        raise HTTPException(403, "Only the person who created this report can add a check-in.")
    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Photo is over 10 MB. Try a smaller one.")
    try:
        view_b64, _ = await asyncio.to_thread(prepare_image, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    leaf: Leaf = request.app.state.leaf
    trace: list = []
    try:
        record = await run_limited(
            request,
            lambda: check_in(
                leaf.llm, leaf.settings, leaf.vision_model, entry, view_b64, notes[:1000], trace
            ),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # Re-read under a per-report lock, so two check-ins at once cannot overwrite each other.
    lock = _checkin_locks.setdefault(entry.id, asyncio.Lock())
    async with lock:
        fresh = await _entry(request, case_id)
        fresh.checkins.append(record)
        await request.app.state.journal.put(fresh)
    if len(_checkin_locks) > 1000:
        _checkin_locks.clear()
    await _close_loop(request, fresh, record)
    return public_view(fresh)


async def _close_loop(request: Request, entry: JournalEntry, record: Any) -> None:
    """A report linked to a garden (Leaf Doctor wiring): the check-in closes its task and
    keeps the garden's plan in step. A failure here never fails the check-in."""
    data = getattr(request.app.state, "data", None)
    if data is None:
        return
    try:
        ctx = CareContext(data.repo, request.app.state.climate, request.app.state.leaf)
        await checkin_loop(
            ctx, request.app.state.queue, entry.id, entry.report, record, len(entry.checkins)
        )
    except Exception:  # noqa: BLE001
        log.exception("could not update the garden after a check-in")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")  # the landing page

    @app.get("/app", include_in_schema=False)
    async def app_page() -> FileResponse:
        return FileResponse(FRONTEND / "app.html")  # diagnose a plant

    @app.get("/today", include_in_schema=False)
    async def today_page() -> FileResponse:
        return FileResponse(FRONTEND / "today.html")  # the garden's day

    @app.get("/g/{garden_id}", include_in_schema=False)
    async def garden_link(garden_id: str) -> FileResponse:
        return FileResponse(FRONTEND / "today.html")  # a private link; the key is in #k=

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        # Served from the root so it controls every page; never cached, so updates land.
        return FileResponse(
            FRONTEND / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(
            FRONTEND / "manifest.webmanifest", media_type="application/manifest+json"
        )

    @app.get("/season", include_in_schema=False)
    async def season_page() -> FileResponse:
        return FileResponse(FRONTEND / "season.html")

    @app.get("/connect", include_in_schema=False)
    async def connect_page() -> FileResponse:
        return FileResponse(FRONTEND / "connect.html")

    @app.get("/identify", include_in_schema=False)
    async def identify_page() -> FileResponse:
        return FileResponse(FRONTEND / "identify.html")  # name a plant from a photo

    @app.get("/plants", include_in_schema=False)
    async def plants_page() -> FileResponse:
        return FileResponse(FRONTEND / "plants.html")  # the gardener's saved cases

    @app.get("/c/{case_id}", include_in_schema=False)
    async def case_page(case_id: str) -> FileResponse:
        return FileResponse(FRONTEND / "case.html")  # the page loads /api/cases/<id>
