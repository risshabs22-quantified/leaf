"""FastAPI app: serves the API and the single-page frontend from one container.

Run locally:  uvicorn backend.app:app --reload
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
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


class RateLimiter:
    """Protects Token Factory and Tavily credits on the public demo."""

    def __init__(self, per_ip_per_hour: int, daily_cap: int):
        self.per_ip = per_ip_per_hour
        self.daily_cap = daily_cap
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.day = date.today()
        self.day_count = 0

    def check(self, ip: str) -> None:
        now = time.time()
        if date.today() != self.day:
            self.day, self.day_count = date.today(), 0
        if len(self.hits) > 10_000:  # bound memory if someone sprays fake IPs
            self.hits.clear()
        if self.day_count >= self.daily_cap:
            raise HTTPException(429, "Leaf has hit today's demo limit. Please try tomorrow.")
        q = self.hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_ip:
            raise HTTPException(429, "You've hit the hourly limit. Please try again later.")
        q.append(now)
        self.day_count += 1


def client_ip(request: Request) -> str:
    """The last X-Forwarded-For hop is the one our platform's proxy appended; earlier
    entries are client-supplied and can be spoofed to dodge the per-IP limit."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "?"


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
    if not getattr(app.state, "leaf", None):
        app.state.leaf = await build_leaf(settings)
    yield


app = FastAPI(title="Leaf", version="0.1.0", lifespan=lifespan)


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
    request.app.state.limiter.check(client_ip(request))
    if image is None and not notes.strip():
        raise HTTPException(400, "Add a photo or describe what you see.")
    image_b64 = mime = None
    if image is not None:
        data = await image.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Photo is over 10 MB. Try a smaller one.")
        try:
            image_b64, mime = prepare_image(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        return await request.app.state.leaf.diagnose(
            image_b64=image_b64,
            mime=mime or "image/jpeg",
            notes=notes[:2000],
            region=region[:100],
            answers=answers[:2000],
        )
    except ModelError as exc:
        log.error("diagnose failed: %s", exc)
        raise HTTPException(502, "Leaf's models are busy right now. Please try again.") from exc


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    request.app.state.limiter.check(client_ip(request))
    try:
        return await request.app.state.leaf.chat(body)
    except ModelError as exc:
        log.error("chat failed: %s", exc)
        raise HTTPException(502, "Leaf's models are busy right now. Please try again.") from exc


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")
