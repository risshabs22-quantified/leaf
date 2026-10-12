"""Photo -> structured Observation.

Token Factory has no public NVIDIA vision model right now, so Leaf discovers an
image-capable model at startup and prefers NVIDIA ones if they come back.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

from PIL import Image, UnidentifiedImageError

from backend.config import VISION_PREFERENCE, Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import VISION_SYSTEM
from backend.schemas import ModelCall, Observation

log = logging.getLogger("leaf.vision")

MAX_SIDE = 1024  # downscale before sending: cheaper, faster, enough detail for leaves


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
    """Validate, downscale and JPEG-encode an upload. Returns (base64, mime)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
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
) -> Observation:
    user_text = "Describe this plant photo."
    if notes:
        user_text += f"\n<gardener_notes>{notes}</gardener_notes>"
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
            max_tokens=1500,
            temperature=0.4,
        )
    except ModelError as exc:
        log.warning("vision step failed: %s", exc)
        return Observation(
            image_quality="poor",
            quality_notes="The photo could not be analysed; diagnosis uses your notes only.",
        )
