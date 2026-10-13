"""Photo -> structured Observation.

Token Factory has no public NVIDIA vision model right now, so Leaf discovers an
image-capable model at startup and prefers NVIDIA ones if they come back.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from backend.config import VISION_PREFERENCE, Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import VISION_SYSTEM, sanitize
from backend.schemas import ModelCall, Observation

log = logging.getLogger("leaf.vision")

MAX_SIDE = 1024  # downscale before sending: cheaper, faster, enough detail for leaves
# Kimi (the current vision model) reasons before it answers: measured at 1.4k-2.4k tokens of
# thinking for a ~300-token JSON reply. Reasoning counts toward max_tokens, so leave headroom.
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


def prepare_image(data: bytes) -> tuple[str, str]:
    """Validate, downscale and JPEG-encode an upload. Returns (base64, mime).

    Raises ValueError (shown to the user as a 400) for anything that is not a readable,
    reasonably sized image. CPU-bound: call it off the event loop.
    """
    try:
        img = Image.open(io.BytesIO(data))  # reads the header only
        if img.width * img.height > MAX_PIXELS:
            raise ValueError("That photo is too large to process. Try a smaller one.")
        img.load()
        img = ImageOps.exif_transpose(img)  # phones store rotation in EXIF, not in the pixels
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        # Decompression bombs surface here (or as a size error above), never as a 500.
        raise ValueError("That file isn't an image we can read. Try a JPG or PNG.") from exc
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode(), "image/jpeg"


async def describe(
    llm: LLM,
    model: str,
    image_b64: str,
    mime: str,
    notes: str,
    trace: list[ModelCall],
    repair_model: str | None = None,
) -> Observation:
    user_text = "Describe this plant photo."
    if notes:
        user_text += f"\n<gardener_notes>{sanitize(notes)}</gardener_notes>"
    messages = [
        {"role": "system", "content": VISION_SYSTEM},
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
