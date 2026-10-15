"""Identify a plant from a photo: the vision model proposes, Nemotron checks against sources,
the gardener confirms in one tap.

1. The vision model lists up to three candidates and the features it saw.
2. One Tavily search (one credit, cached a day) finds identification pages for the top two.
3. Super compares the features with the sources and re-ranks the same candidates (it cannot
   add species). Code keeps only the vision model's candidates and real source ids.
4. Nothing is final until the gardener taps one (POST /api/identify/{id}/confirm).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from backend.config import Settings
from backend.knowledge import species_key
from backend.models import LLM, ModelError, SpendCapReached, complete_json
from backend.prompts import IDENTIFY_CHECK_SYSTEM, IDENTIFY_SYSTEM, sanitize
from backend.schemas import IdentifyCheck, IdentifyOut, ModelCall
from backend.search import Searcher, SearchPaused, rank_sources

log = logging.getLogger("leaf.identify")

CONFIDENT_TOP = 0.6
CONFIDENT_MARGIN = 0.2


async def propose(
    llm: LLM, settings: Settings, vision_model: str, image_b64: str, notes: str, trace: list
) -> IdentifyOut:
    user = "Identify this plant."
    if notes.strip():
        user += f"\n<gardener_notes>{sanitize(notes, 500)}</gardener_notes>"
    return await complete_json(
        llm,
        step="identify",
        models=[vision_model],
        messages=[
            {"role": "system", "content": IDENTIFY_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ],
        schema=IdentifyOut,
        trace=trace,
        thinking=False,
        max_tokens=settings.budget("identify"),
        temperature=0.3,
        repair_model=settings.repair_model,
    )


def _dedupe(seen: IdentifyOut) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in seen.candidates:
        key = species_key(c.scientific_name, binomial=True)
        if key and key not in out:
            out[key] = {
                "key": key,
                "scientific_name": sanitize(c.scientific_name, 120),
                "common_name": sanitize(c.common_name, 80),
                "vision_confidence": c.confidence,
            }
    return list(out.values())[:3]


async def identify(
    llm: LLM,
    settings: Settings,
    searcher: Searcher,
    vision_model: str,
    image_b64: str,
    notes: str,
    trace: list[ModelCall],
    use_search: bool = True,
    on_credits: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """`use_search` False (the day's knowledge credits are spent) skips Tavily; the result is
    then never marked checked. `on_credits` records the Tavily credits spent."""
    seen = await propose(llm, settings, vision_model, image_b64, notes, trace)
    candidates = _dedupe(seen)
    features = [sanitize(f, 160) for f in seen.features[:10]]
    base = {
        "plant_visible": seen.plant_visible and bool(candidates),
        "image_quality": seen.image_quality,
        "features": features,
        "sources": [],
        "checked": False,
        "summary": "",
    }
    if not candidates:
        return {**base, "candidates": [], "confident": False}
    for c in candidates:
        c.update(probability=c["vision_confidence"], consistent=None, reason="", source_ids=[])

    sources = []
    top_two = [c["scientific_name"] for c in candidates[:2]]
    if use_search:
        try:
            query = " vs ".join(top_two) + " plant identification leaves flowers"
            results, credits = await searcher.search_web(query, max_results=6)
            sources = rank_sources(results, limit=5)
            if on_credits is not None:
                await on_credits(credits)
        except SearchPaused:
            log.info("identify: Tavily paused; checking without sources")
    listing = [
        {"id": f"S{s.id}", "title": sanitize(s.title, 200), "text": sanitize(s.snippet, 600)}
        for s in sources
    ]
    shown = [
        {k: c[k] for k in ("scientific_name", "common_name", "vision_confidence")}
        for c in candidates
    ]
    try:
        check = await complete_json(
            llm,
            step="identify_check",
            models=settings.models("identify_check"),
            messages=[
                {"role": "system", "content": IDENTIFY_CHECK_SYSTEM},
                {
                    "role": "user",
                    "content": f"<features>{json.dumps(features, ensure_ascii=False)}</features>\n"
                    f"<candidates>{json.dumps(shown, ensure_ascii=False)}</candidates>\n"
                    f"<sources>{json.dumps(listing, ensure_ascii=False)}</sources>",
                },
            ],
            schema=IdentifyCheck,
            trace=trace,
            thinking=True,
            max_tokens=settings.budget("identify_check"),
            temperature=0.2,
        )
    except SpendCapReached:
        raise
    except ModelError as exc:
        log.warning("identify check failed; the vision ranking stands: %s", exc)
        check = None
    valid_ids = {f"S{s.id}" for s in sources}
    if check is not None:
        by_key = {c["key"]: c for c in candidates}
        assessed: set[str] = set()
        for r in check.ranking:
            c = by_key.get(species_key(r.scientific_name, binomial=True))
            if c is None:
                continue  # Super may only re-rank the vision model's candidates
            assessed.add(c["key"])
            c.update(
                probability=r.probability,
                consistent=r.consistent,
                reason=sanitize(r.reason, 300),
                source_ids=[s for s in r.source_ids if s in valid_ids],
            )
        for c in candidates:  # left out by the check: never left standing on the vision score
            if c["key"] not in assessed:
                c.update(probability=0.0, consistent=None, reason="Not assessed by the check.")
        # "Checked" means checked against sources; a ranking without any is only a re-guess.
        base.update(checked=bool(sources), summary=sanitize(check.summary, 300))
    ranked = sorted(candidates, key=lambda c: c["probability"], reverse=True)
    top = ranked[0]["probability"]
    second = ranked[1]["probability"] if len(ranked) > 1 else 0.0
    return {
        **base,
        "candidates": ranked,
        "sources": [{"id": f"S{s.id}", "title": s.title, "url": s.url} for s in sources],
        # Never final without the gardener: "confident" only preselects the first choice.
        "confident": base["checked"] and top >= CONFIDENT_TOP and top - second >= CONFIDENT_MARGIN,
    }
