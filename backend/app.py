"""FastAPI app: serves the API and the single-page frontend from one container.

Run locally:  uvicorn backend.app:app --reload
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, TypeVar

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.config import Settings, get_settings
from backend.models import ModelError, TokenFactory
from backend.pipeline import Leaf
from backend.schemas import ChatRequest, ChatResponse, DiagnoseResponse
from backend.search import NullSearcher, TavilySearcher
from backend.vision import discover_vision_model, prepare_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("leaf.app")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
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

    def __init__(self, app, limits: dict[str, int], default: int):
        self.app, self.limits, self.default = app, limits, default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limits.get(scope["path"], self.default)
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
    searcher = (
        TavilySearcher(settings.tavily_api_key) if settings.tavily_api_key else NullSearcher()
    )
    vision = await discover_vision_model(llm, settings)
    return Leaf(llm, searcher, settings, vision)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.limiter = RateLimiter(settings.per_ip_per_hour, settings.daily_cap)
    app.state.slots = asyncio.Semaphore(settings.max_concurrent)
    if not getattr(app.state, "leaf", None):
        app.state.leaf = await build_leaf(settings)
    yield


app = FastAPI(title="Leaf", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    BodyLimitMiddleware,
    limits={"/api/diagnose": MAX_UPLOAD_BYTES + 256 * 1024, "/api/chat": 256 * 1024},
    default=64 * 1024,
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


async def run_limited(request: Request, work: Callable[[], Awaitable[T]]) -> T:
    """Run one diagnosis or chat under the concurrency cap and the whole-request deadline.

    Each model call has its own timeout and retries, which add up across fallbacks; the
    deadline stops one request from holding a slot (and spending credits) for minutes.
    """
    leaf: Leaf = request.app.state.leaf
    slots: asyncio.Semaphore = request.app.state.slots
    if slots.locked():
        raise HTTPException(503, "Leaf is busy right now. Please try again in a minute.")
    async with slots:
        try:
            return await asyncio.wait_for(work(), timeout=leaf.settings.deadline_s)
        except TimeoutError as exc:
            log.error("request exceeded %ss deadline", leaf.settings.deadline_s)
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
    return await run_limited(
        request,
        lambda: request.app.state.leaf.diagnose(
            image_b64=image_b64,
            mime=mime or "image/jpeg",
            notes=notes[:2000],
            region=region[:100],
            answers=answers[:2000],
        ),
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    check_limits(request)
    return await run_limited(request, lambda: request.app.state.leaf.chat(body))


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")
