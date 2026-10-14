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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

from backend.checkin import check_in
from backend.config import Settings, get_settings
from backend.deep import CaseStateError, DeepLeaf, DeepResponse
from backend.journal import JournalEntry, open_journal, public_view, thumbnail
from backend.models import ModelError, TokenFactory
from backend.pipeline import Leaf
from backend.schemas import ChatRequest, ChatResponse, DiagnoseResponse
from backend.search import (
    CreditBudget,
    NullSearcher,
    Searcher,
    TavilySearcher,
    search_paused,
    tavily_balance,
)
from backend.store import MemoryCaseStore
from backend.vision import discover_vision_model, prepare_image, prepare_photo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("leaf.app")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DEEP_PHOTOS = 3
PHOTO_LABELS = {"whole plant", "close-up", "underside", "photo"}
MAX_LIMITER_KEYS = 10_000
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
    app.state.slots = asyncio.Semaphore(settings.max_concurrent)
    if not getattr(app.state, "leaf", None):
        app.state.leaf = await build_leaf(settings)
    deep = getattr(app.state, "deep", None)
    if deep is None or deep.leaf is not app.state.leaf:
        app.state.deep = DeepLeaf(app.state.leaf, MemoryCaseStore())
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
    yield


app = FastAPI(title="Leaf", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    BodyLimitMiddleware,
    limits={
        "/api/diagnose": MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/deep": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/deep/stream": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/auto": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/auto/stream": MAX_DEEP_PHOTOS * MAX_UPLOAD_BYTES + 256 * 1024,
        "/api/chat": 256 * 1024,
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
    return response


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
) -> DiagnoseResponse:
    check_limits(request)
    if image is None and not notes.strip():
        raise HTTPException(400, "Add a photo or describe what you see.")
    image_b64 = mime = None
    if image is not None:
        data = await image.read(MAX_UPLOAD_BYTES + 1)
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
        ),
    )
    await save_report(request, result, "quick", [image_b64] if image_b64 else [], ["photo"])
    return result


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    check_limits(request)
    return await run_limited(request, lambda: request.app.state.leaf.chat(body))


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
    request: Request, photos, notes: str, region: str, lat, lon
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
            lambda emit: deep.quick_only(photos=photos, notes=notes, region=region, notice=notice),
            None,
        )
    return (
        lambda emit: deep.start(
            photos=photos, notes=notes, region=region, lat=lat, lon=lon, emit=emit
        ),
        _deep_deadline(request),
    )


def _auto_work(
    request: Request, photos, notes: str, region: str, lat, lon
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
) -> DeepResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _auto_work(request, photos, notes, region, lat, lon)
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
) -> StreamingResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _auto_work(request, photos, notes, region, lat, lon)
    return _event_stream(request, work, timeout, [(v, lb) for lb, _, v in photos])


@app.post("/api/deep", response_model=DeepResponse)
async def deep(
    request: Request,
    notes: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    lat: Annotated[float | None, Form()] = None,
    lon: Annotated[float | None, Form()] = None,
) -> DeepResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _deep_work(request, photos, notes, region, lat, lon)
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
) -> StreamingResponse:
    check_limits(request)
    photos, lat, lon = await _deep_inputs(request, notes, region, lat, lon)
    work, timeout = _deep_work(request, photos, notes, region, lat, lon)
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
    return public_view(fresh)


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")

    @app.get("/c/{case_id}", include_in_schema=False)
    async def case_page(case_id: str) -> FileResponse:
        return FileResponse(FRONTEND / "case.html")  # the page loads /api/cases/<id>
