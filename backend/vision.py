"""Photo -> structured Observation.

Token Factory has no public NVIDIA vision model right now, so Leaf discovers an
image-capable model at startup and prefers NVIDIA ones if they come back.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any, Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field, field_validator

from backend.config import VISION_PREFERENCE, Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import (
    LOOK_CLOSER_SYSTEM,
    VISION_CHECKLIST_SYSTEM,
    VISION_SYSTEM,
    sanitize,
)
from backend.schemas import ModelCall, Observation, _unit

log = logging.getLogger("leaf.vision")

MAX_SIDE = 1024  # downscale before sending: cheaper, faster, enough detail for leaves
# Describing a photo needs no long reasoning: with thinking on, Kimi spent 1.4k-2.4k tokens
# thinking for a ~300-token reply. It runs with thinking off; the budget keeps headroom in
# case a model ignores the switch (reasoning counts toward max_tokens).
VISION_MAX_TOKENS = 4000
MAX_PIXELS = 40_000_000  # decoded size cap; a tiny file can declare a huge canvas


def accepts_images(model: dict[str, Any]) -> bool:
    arch = model.get("architecture") or {}
    inputs = arch.get("input_modalities") or model.get("input_modalities") or []
    modality = str(arch.get("modality") or model.get("modality") or "")
    return "image" in [str(i).lower() for i in inputs] or "image" in modality.split("->")[0]


def choose_vision_model(models: list[dict[str, Any]], override: str = "") -> str | None:
    if override:
        return override
    capable = [m["id"] for m in models if m.get("id") and accepts_images(m)]
    nvidia = [m for m in capable if m.lower().startswith("nvidia/")]
    if nvidia:
        return nvidia[0]
    for preferred in VISION_PREFERENCE:
        if preferred in capable:
            return preferred
    return capable[0] if capable else None


async def discover_vision_model(llm: LLM, settings: Settings) -> str | None:
    if settings.vision_model:
        return settings.vision_model
    try:
        models = await llm.list_models()
    except Exception as exc:  # noqa: BLE001 - discovery must never crash startup
        log.warning("vision model discovery failed: %s", exc)
        return None
    chosen = choose_vision_model(models)
    log.info("vision model: %s", chosen)
    return chosen


FULL_SIDE = 2048  # kept for look_closer crops, so a crop still has detail
MIN_CROP_SIDE = 32
CROP_TARGET_SIDE = 768  # small crops are upscaled so the vision model sees the detail


def _load(data: bytes) -> Image.Image:
    """Decode an upload safely. Raises ValueError (a 400) for anything unreadable or huge."""
    try:
        img = Image.open(io.BytesIO(data))  # reads the header only
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("That photo is too large to process. Try a smaller one.")
        img.load()
        img = ImageOps.exif_transpose(img)  # phones store rotation in EXIF, not in the pixels
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        # Decompression bombs surface here (or as a size error above), never as a 500.
        raise ValueError("That file isn't an image we can read. Try a JPG or PNG.") from exc
    return img.convert("RGB")


def _jpeg_b64(img: Image.Image, side: int, quality: int = 85) -> str:
    img = img.copy()
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return base64.b64encode(out.getvalue()).decode()


def prepare_image(data: bytes) -> tuple[str, str]:
    """Validate, downscale and JPEG-encode an upload. Returns (base64, mime).

    Raises ValueError (shown to the user as a 400) for anything that is not a readable,
    reasonably sized image. CPU-bound: call it off the event loop.
    """
    return _jpeg_b64(_load(data), MAX_SIDE), "image/jpeg"


def prepare_photo(data: bytes) -> tuple[str, str]:
    """Decode once, return (full, view): base64 JPEGs at 2048 px for crops and 1024 px for
    the first look. Same validation as prepare_image. CPU-bound: call it off the loop."""
    img = _load(data)
    return _jpeg_b64(img, FULL_SIDE, quality=88), _jpeg_b64(img, MAX_SIDE)


def crop_photo(full_b64: str, x: float, y: float, w: float, h: float) -> str:
    """Crop a region given as 0-1 fractions of the photo; small crops are upscaled."""
    img = Image.open(io.BytesIO(base64.b64decode(full_b64))).convert("RGB")
    x0 = min(max(x, 0.0), 1.0) * img.width
    y0 = min(max(y, 0.0), 1.0) * img.height
    x1 = min(max(x + w, 0.0), 1.0) * img.width
    y1 = min(max(y + h, 0.0), 1.0) * img.height
    if x1 - x0 < MIN_CROP_SIDE or y1 - y0 < MIN_CROP_SIDE:
        raise ValueError("That region is too small to see anything. Choose a larger area.")
    crop = img.crop((int(x0), int(y0), int(x1), int(y1)))
    if max(crop.size) < CROP_TARGET_SIDE:
        scale = CROP_TARGET_SIDE / max(crop.size)
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
    return _jpeg_b64(crop, MAX_SIDE)


async def describe(
    llm: LLM,
    model: str,
    image_b64: str,
    mime: str,
    notes: str,
    trace: list[ModelCall],
    repair_model: str | None = None,
    checklist: bool = False,
) -> Observation:
    user_text = "Describe this plant photo."
    if notes:
        user_text += f"\n<gardener_notes>{sanitize(notes)}</gardener_notes>"
    messages = [
        {"role": "system", "content": VISION_CHECKLIST_SYSTEM if checklist else VISION_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
            ],
        },
    ]
    try:
        return await complete_json(
            llm,
            step="vision",
            models=[model],
            messages=messages,
            schema=Observation,
            trace=trace,
            thinking=False,
            max_tokens=VISION_MAX_TOKENS,
            temperature=0.4,
            repair_model=repair_model,
        )
    except ModelError as exc:
        log.warning("vision step failed: %s", exc)
        return Observation(
            image_quality="poor",
            quality_notes="The photo could not be analysed; diagnosis uses your notes only.",
        )


class RegionAnswer(BaseModel):
    answer: str
    visible_features: list[str] = Field(default_factory=list)
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    image_quality: Literal["good", "fair", "poor"] = "fair"

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        return _unit(v)

    @field_validator("image_quality", mode="before")
    @classmethod
    def _quality(cls, v: Any) -> Any:
        v = v.strip().lower() if isinstance(v, str) else v
        return v if v in ("good", "fair", "poor") else "fair"


async def look_closer(
    llm: LLM,
    model: str,
    crop_b64: str,
    question: str,
    trace: list[ModelCall],
    repair_model: str | None = None,
) -> RegionAnswer:
    """Ask the vision model one targeted question about a crop. Raises ModelError."""
    messages = [
        {"role": "system", "content": LOOK_CLOSER_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"<question>{sanitize(question, 300)}</question>"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{crop_b64}"}},
            ],
        },
    ]
    return await complete_json(
        llm,
        step="look_closer",
        models=[model],
        messages=messages,
        schema=RegionAnswer,
        trace=trace,
        thinking=False,
        max_tokens=VISION_MAX_TOKENS,
        temperature=0.4,
        repair_model=repair_model,
    )
