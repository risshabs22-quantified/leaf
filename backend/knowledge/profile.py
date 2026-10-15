"""Building a species profile from cited sources.

1. One Tavily search for care, pests and diseases; extension and university pages first
   (search.rank_sources). One batched extract of the top five pages (one credit) gives the
   relevant passages.
2. A separate Tavily search for toxicity to pets and children; its result snippets are
   passages too. (Searched together, care passages crowded toxicity out: a live foxglove
   profile came back "toxicity unknown".) Three credits per species in all.
3. Lightning extracts facts; each must quote its passage word for word (checked in code).
4. Super verifies each fact against its passage; only verified facts are shown.
5. Leaf's own cited tables add reference facts (FAO-56 water use, germination temperatures).
Fields with no verified fact are listed as unsourced.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from backend.climate.crops import find_crop, find_crop_exact
from backend.climate.sources import SOURCES
from backend.config import Settings
from backend.models import LLM, ModelError, SpendCapReached, complete_json
from backend.prompts import FACT_EXTRACT_SYSTEM, FACT_VERIFY_SYSTEM, sanitize
from backend.schemas import ExtractedFact, ExtractedFacts, FactVerdicts, ModelCall
from backend.search import Searcher, rank_sources

log = logging.getLogger("leaf.knowledge")

TEXT_FIELDS = (
    "light",
    "water",
    "ideal_temp_c",
    "hardiness_zones",
    "soil",
    "soil_ph",
    "feeding",
    "timing",
)
NUMBER_FIELDS = ("temp_min_c", "temp_max_c")
LIST_FIELDS = ("pests", "diseases")
ENUM_FIELDS = {
    "frost_tolerance": ("tender", "half_hardy", "hardy"),
    "toxic_to_pets": ("toxic", "mildly_toxic", "non_toxic"),
    "toxic_to_children": ("toxic", "mildly_toxic", "non_toxic"),
}
FIELDS = (*TEXT_FIELDS, *NUMBER_FIELDS, *LIST_FIELDS, *ENUM_FIELDS)
PAGES = 5
MIN_QUOTE_WORDS = 5
MAX_FACTS = 90  # one D1 batch (with the delete and the update) stays atomic


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"')
    text = text.replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _words(text: str) -> str:
    """Words and numbers only: punctuation, symbols and spacing do not change a quote."""
    return " ".join(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", _norm(text)))


def quote_in_passage(quote: str, passage: str) -> bool:
    """The quote's words, in order and unchanged, in the passage (case, spacing and
    punctuation aside); at least MIN_QUOTE_WORDS words."""
    q = _words(quote)
    return len(q.split()) >= MIN_QUOTE_WORDS and f" {q} " in f" {_words(passage)} "


def _plain(value: Any, limit: int) -> str:
    """Plain text for storage and display (the page escapes it; prompts sanitise it)."""
    text = "".join(ch for ch in str(value or "") if ch.isprintable())
    return " ".join(text.split())[:limit]


def clean_value(field: str, value: Any) -> Any:
    """The value in its field's type, or None when it does not fit."""
    if field in TEXT_FIELDS:
        return _plain(value, 200) or None
    if field in NUMBER_FIELDS:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(number, 1) if -60 <= number <= 60 else None
    if field in LIST_FIELDS:
        items = value if isinstance(value, list) else [value]
        names = [_plain(x, 80) for x in items if _plain(x, 80)]
        return names[:10] or None
    if field in ENUM_FIELDS:
        text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
        return text if text in ENUM_FIELDS[field] else None
    return None


NEGATIONS = {"not", "non", "nontoxic", "isn", "isnt", "never", "no", "safe", "harmless"}
PET_WORDS = {
    "dog",
    "dogs",
    "cat",
    "cats",
    "pet",
    "pets",
    "animal",
    "animals",
    "horse",
    "horses",
    "livestock",
    "aspca",
}
CHILD_WORDS = {
    "child",
    "children",
    "kid",
    "kids",
    "human",
    "humans",
    "people",
    "person",
    "infant",
    "infants",
    "toddler",
    "toddlers",
    "adult",
    "adults",
    "swallowed",
    "eaten",
    "ingested",
}
WINDOW = 25  # words around the quote that count as its sentence and subject


def toxicity_consistent(field: str, value: str, quote: str, passage: str) -> str:
    """ "" when a toxicity fact's quote supports its value in code, else the reason not.

    The quote check alone would accept "toxic to cats and dogs" inside "Not toxic to cats
    and dogs"; so the words just before the quote and the quote itself must agree with the
    value, and the passage near it must name who is at risk (pets, or people)."""
    words = _words(passage).split()
    q = _words(quote).split()
    at = next((i for i in range(len(words) - len(q) + 1) if words[i : i + len(q)] == q), -1)
    if at < 0:
        return "quote not found in the passage"
    near = set(words[max(0, at - 4) : at] + q)
    negated = bool(near & NEGATIONS) or "non toxic" in " ".join(words[max(0, at - 4) : at + len(q)])
    if value in ("toxic", "mildly_toxic") and negated:
        return "the quote says it is not toxic"
    if value == "non_toxic" and not negated:
        return "the quote does not say it is safe"
    around = set(words[max(0, at - WINDOW) : at + len(q) + WINDOW])
    subject = PET_WORDS if field == "toxic_to_pets" else CHILD_WORDS
    if not around & subject:
        return "the passage does not say who is at risk"
    return ""


def check_extracted(
    facts: list[ExtractedFact], passages: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Code checks before any model verification: known field, typed value, real passage,
    verbatim quote. Returns (candidates, rejected)."""
    kept, rejected = [], []
    for f in facts:
        p = passages.get(f.passage.strip().upper())
        value = clean_value(f.field, f.value) if f.field in FIELDS else None
        row = {
            "field": f.field if f.field in FIELDS else "unknown",
            "value": value if value is not None else f.value,
            "quote": sanitize(f.quote, 600),
            "source_url": p["url"] if p else "",
            "source_title": p["title"] if p else "",
            "passage": p["text"] if p else "",
        }
        if f.field not in FIELDS or value is None:
            rejected.append({**row, "note": "unknown field or value"})
        elif p is None:
            rejected.append({**row, "note": "no such passage"})
        elif not quote_in_passage(f.quote, p["text"]):
            rejected.append({**row, "note": "quote not found in the passage"})
        elif f.field in ("toxic_to_pets", "toxic_to_children") and (
            why := toxicity_consistent(f.field, value, f.quote, p["text"])
        ):
            rejected.append({**row, "note": why})
        else:
            kept.append(row)
    return kept, rejected


def reference_facts(scientific_name: str, common_names: list[str]) -> list[dict[str, Any]]:
    """Facts from Leaf's own cited tables (backend/climate/crops.py). With a scientific name
    only that name decides; otherwise a common name must equal a crop's name exactly, so
    "sweet pea" (Lathyrus) never gets the garden pea's numbers."""
    if scientific_name:
        crop = find_crop(scientific_name)
    else:
        crop = next((c for c in map(find_crop_exact, common_names) if c), None)
    if crop is None:
        return []
    out = []

    def add(field: str, value: Any, source: str) -> None:
        s = SOURCES[source]
        out.append(
            {
                "field": field,
                "value": value,
                "quote": "",
                "source_url": s["url"],
                "source_title": s["title"],
                "status": "reference",
                "note": crop.key,
            }
        )

    if crop.kc:
        add("kc", {"initial": crop.kc[0], "mid": crop.kc[1], "end": crop.kc[2]}, "fao56_t12")
    if crop.zr:
        add("root_depth_m", list(crop.zr), "fao56_ch8")
    if crop.germ_min_c is not None:
        src = "osu_germination" if "osu_germination" in crop.sources else "ucanr_germination"
        add(
            "germination_soil_c",
            {"min": crop.germ_min_c, "optimum": list(crop.germ_opt_c or ())},
            src,
        )
    return out


async def build_profile(
    llm: LLM,
    settings: Settings,
    searcher: Searcher,
    scientific_name: str,
    common_names: list[str],
    trace: list[ModelCall],
    on_credits: Callable[[int], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Facts (verified, rejected, reference) and the pages they came from. `on_credits`
    records Tavily credits as they are spent."""
    names = [n for n in [scientific_name, *common_names] if n]
    display = (
        f"{scientific_name} ({common_names[0]})"
        if scientific_name and common_names
        else (names[0] if names else "")
    )
    query = f"{display} plant care light water soil temperature feeding pests diseases"

    async def spent(credits: int) -> None:
        if on_credits is not None and credits:
            await on_credits(credits)

    results, credits = await searcher.search_web(query, max_results=8)  # SearchPaused propagates
    await spent(credits)
    sources = rank_sources(results, limit=PAGES)
    pages, credits = await searcher.extract_many(
        [s.url for s in sources],
        "light water soil temperature frost feeding pests diseases planting",
    )
    await spent(credits)
    safety_results, credits = await searcher.search_web(
        f"{display} toxic poisonous to dogs cats children", max_results=5
    )
    await spent(credits)
    safety = rank_sources(safety_results, limit=5)
    passages: dict[str, dict[str, str]] = {}
    for s in sources:
        for text in pages.get(s.url, []):
            passages[f"P{len(passages) + 1}"] = {"url": s.url, "title": s.title, "text": text}
    for s in safety:
        if s.snippet.strip():
            passages[f"P{len(passages) + 1}"] = {"url": s.url, "title": s.title, "text": s.snippet}
    facts: list[dict[str, Any]] = []
    if passages:
        listing = [
            {"id": k, "source": sanitize(v["title"], 200), "text": sanitize(v["text"], 900)}
            for k, v in passages.items()
        ]
        extracted = await complete_json(
            llm,
            step="fact_extract",
            models=settings.models("fact_extract"),
            messages=[
                {"role": "system", "content": FACT_EXTRACT_SYSTEM},
                {
                    "role": "user",
                    "content": f"<plant>{sanitize(display, 200)}</plant>\n"
                    f"<passages>{json.dumps(listing, ensure_ascii=False)}</passages>",
                },
            ],
            schema=ExtractedFacts,
            trace=trace,
            thinking=False,
            max_tokens=settings.budget("fact_extract"),
            temperature=0.2,
        )
        candidates, rejected = check_extracted(extracted.facts, passages)
        verdicts = await _verify(llm, settings, display, candidates, trace)
        for i, f in enumerate(candidates):
            ok, note = verdicts.get(i, (False, "not verified"))
            facts.append({**f, "status": "verified" if ok else "rejected", "note": note})
        facts += [{**f, "status": "rejected"} for f in rejected]
    facts += reference_facts(scientific_name, common_names)
    facts = facts[:MAX_FACTS]
    for f in facts:
        f.pop("passage", None)
    used = {f["source_url"] for f in facts if f["status"] == "verified"}
    every = {s.url: s for s in [*sources, *safety]}
    return {
        "facts": facts,
        "sources": [s.model_dump() for url, s in every.items() if url in used],
        "searched": len(every),
    }


async def _verify(
    llm: LLM, settings: Settings, plant: str, candidates: list[dict[str, Any]], trace: list
) -> dict[int, tuple[bool, str]]:
    """Super's verdict per fact; a fact with no verdict (or a failed call) is not verified."""
    if not candidates:
        return {}
    items = [
        {
            "i": i,
            "field": f["field"],
            "value": f["value"],
            "quote": f["quote"],
            "passage": sanitize(f["passage"], 900),
        }
        for i, f in enumerate(candidates)
    ]
    try:
        out = await complete_json(
            llm,
            step="fact_verify",
            models=settings.models("fact_verify"),
            messages=[
                {"role": "system", "content": FACT_VERIFY_SYSTEM},
                {
                    "role": "user",
                    "content": f"<plant>{sanitize(plant, 200)}</plant>\n"
                    f"<facts>{json.dumps(items, ensure_ascii=False)}</facts>",
                },
            ],
            schema=FactVerdicts,
            trace=trace,
            thinking=True,
            max_tokens=settings.budget("fact_verify"),
            temperature=0.2,
        )
    except SpendCapReached:
        raise
    except ModelError as exc:
        log.warning("fact verification failed: %s", exc)
        return {}
    return {v.i: (v.supported, sanitize(v.note, 300)) for v in out.verdicts}


TOXICITY_RANK = {"toxic": 2, "mildly_toxic": 1, "non_toxic": 0}


def profile_view(row: dict[str, Any]) -> dict[str, Any]:
    """A species profile for people and agents: verified and reference facts per field, the
    conservative toxicity answer, and the fields no source supports."""
    facts = [f for f in row.get("facts", []) if f["status"] in ("verified", "reference")]
    by_field: dict[str, list[dict[str, Any]]] = {}
    for f in facts:
        by_field.setdefault(f["field"], []).append(
            {
                "value": f["value"],
                "quote": f["quote"],
                "source": {"title": f["source_title"], "url": f["source_url"]},
                "kind": f["status"],
            }
        )
    toxicity = {}
    for field in ("toxic_to_pets", "toxic_to_children"):
        values = [x["value"] for x in by_field.get(field, [])]
        # Safety rule in code: when sources disagree, the most toxic answer wins; with no
        # verified source the answer is "unknown", never "non_toxic".
        toxicity[field] = (
            max(values, key=lambda v: TOXICITY_RANK.get(v, 0)) if values else ("unknown")
        )
    return {
        "key": row["key"],
        "scientific_name": row.get("scientific_name", ""),
        "common_names": row.get("common_names", []),
        "status": row.get("status"),
        "facts": by_field,
        "toxicity": toxicity,
        "unsourced": [f for f in FIELDS if f not in by_field],
        "sources": row.get("sources", []),
        "built_at": row.get("built_at"),
    }
