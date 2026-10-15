"""Care plan from read passages only, then a claim check that drops what the sources don't say.

    passages --Lightning--> plan --code checks--> --Lightning judge--> kept / flagged / dropped

Code checks (deterministic): every step cites a source Leaf actually read; any rate or dose in a
step appears in a cited passage; any chemical step tells the gardener to follow the label.
Then Lightning judges whether each remaining step is supported, partly supported or not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.actives import garden_prompt
from backend.case import Case, SourceRef
from backend.config import Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import CLAIM_CHECK_SYSTEM, PLAN_DEEP_SYSTEM, sanitize
from backend.schemas import CarePlan, ModelCall, PlanStep
from backend.search import Searcher, _trust, rank_sources

log = logging.getLogger("leaf.claims")

MAX_AUTO_READS = 2
CHEMICAL = re.compile(
    r"\b(fungicid\w*|bactericid\w*|insecticid\w*|pesticid\w*|miticid\w*|herbicid\w*|"
    r"spray\w*|copper|mancozeb|chlorothalonil|sulfur|sulphur|neem|spinosad|pyrethr\w*|"
    r"bacillus|bicarbonate|baking soda|hydrogen peroxide|bleach|horticultural oil|"
    r"(?:insecticidal |potassium )?soaps?|imidacloprid|captan|myclobutanil|streptomycin|"
    r"bordeaux|azoxystrobin|propiconazole|tebuconazole|dust\w*)\b",
    re.IGNORECASE,
)
NUMBER_WORDS = {
    "a half": 0.5,
    "half": 0.5,
    "a quarter": 0.25,
    "quarter": 0.25,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
UNITS = {
    "%": "%",
    "percent": "%",
    "per cent": "%",
    "ml": "ml",
    "milliliter": "ml",
    "millilitre": "ml",
    "cc": "ml",
    "l": "l",
    "liter": "l",
    "litre": "l",
    "oz": "oz",
    "ounce": "oz",
    "fl oz": "floz",
    "tbsp": "tbsp",
    "tablespoon": "tbsp",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "g": "g",
    "gram": "g",
    "kg": "kg",
    "lb": "lb",
    "pound": "lb",
    "gal": "gal",
    "gallon": "gal",
    "qt": "qt",
    "quart": "qt",
    "pt": "pt",
    "pint": "pt",
    "cup": "cup",
    "ppm": "ppm",
    "capful": "capful",
}
_UNIT_RE = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
_NUM_RE = r"\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
# "(?![a-z])", not "\b": a "%" followed by a space has no word boundary after it.
AMOUNT = re.compile(
    rf"(?<![\d.,])({_NUM_RE})\s*-?\s*({_UNIT_RE})(?:ful)?(?:e?s)?(?![a-z])", re.IGNORECASE
)
RATIO = re.compile(r"(?<![\d.])(\d+)\s*:\s*(\d+)(?!\d|\.\d)")  # "1:10." ends a sentence
RATE = AMOUNT  # whether a text states an amount at all


def _value(num: str) -> float | None:
    num = num.strip().lower().replace(",", ".")
    if num in NUMBER_WORDS:
        return float(NUMBER_WORDS[num])
    if "/" in num:
        a, _, b = num.partition("/")
        try:
            return float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(num)
    except ValueError:
        return None


def amounts(text: str) -> set[tuple[float, str]]:
    """Every dose, rate or ratio in a text as normalised (value, unit) pairs, so "5 ml" never
    matches "0.5 ml" and "two tablespoons" equals "2 tbsp"."""
    found: set[tuple[float, str]] = set()
    for num, unit in AMOUNT.findall(text):
        value = _value(num)
        if value is not None:
            found.add((round(value, 4), UNITS[unit.lower()]))
    for a, b in RATIO.findall(text):
        if float(b):
            found.add((round(float(a) / float(b), 4), "ratio"))
    return found


def unsourced_amounts(text: str, sources_text: str) -> set[tuple[float, str]]:
    return amounts(text) - amounts(sources_text)


FOLLOW_LABEL = "Follow the product label."


class ClaimFlag(BaseModel):
    step: str
    verdict: Literal["dropped", "partial", "label_added"]
    reason: str


class _Verdict(BaseModel):
    step: int
    verdict: Literal["supported", "partial", "unsupported"] = "supported"
    note: str = ""


class _Verdicts(BaseModel):
    verdicts: list[_Verdict] = Field(default_factory=list)


def _sid(source_id: str) -> int:
    return int(source_id.lstrip("S") or 0)


async def ensure_passages(case: Case, searcher: Searcher) -> None:
    """If the investigator read nothing, read the best trusted sources before planning, so the
    plan always rests on passages, never on search snippets alone. If it never searched at
    all, search for the leading diagnosis first (Tavily), as Quick does."""
    if any(s.passages for s in case.sources) or not case.differential:
        return
    top = case.differential[0].name
    query = f"{top} management and treatment for home gardeners"
    if not case.sources:  # the investigator never searched: find guides for the leader first
        crop = f"{case.crop} " if case.crop else ""
        try:
            results, credits = await searcher.search_web(
                f"{crop}{top} extension guide", max_results=6
            )
        except Exception as exc:  # noqa: BLE001 - no sources only means no cited plan
            log.warning("auto search failed for %s: %s", case.id, exc)
            return
        case.tavily_credits += credits
        for r in rank_sources(results, limit=MAX_AUTO_READS):
            case.add_source(
                r.url, sanitize(r.title, 200), sanitize(r.snippet, 400), bool(_trust(r.url))
            )
    candidates = sorted(case.sources, key=lambda s: not s.trusted)[:MAX_AUTO_READS]

    async def read(src: SourceRef) -> None:
        try:
            passages, credits = await searcher.extract(src.url, query)
        except Exception as exc:  # noqa: BLE001 - a failed read only means fewer passages
            log.warning("auto read failed for %s: %s", src.id, exc)
            return
        case.tavily_credits += credits
        src.passages = [sanitize(p, 900) for p in passages if p.strip()][:3]

    await asyncio.gather(*(read(s) for s in candidates))


def _passages_for_prompt(case: Case) -> list[dict[str, Any]]:
    return [
        {
            "id": _sid(s.id),
            "title": sanitize(s.title, 200),
            "trusted": s.trusted,
            "passages": s.passages,
        }
        for s in case.sources
        if s.passages
    ]


async def write_plan(
    llm: LLM, settings: Settings, case: Case, region: str, trace: list[ModelCall]
) -> CarePlan:
    passages = _passages_for_prompt(case)
    diagnosis = {
        "differential": [{"name": h.name, "probability": h.probability} for h in case.differential],
        "summary": case.summary,
    }
    user = (
        f"<diagnosis>{sanitize(json.dumps(diagnosis, ensure_ascii=False))}</diagnosis>\n"
        f"<passages>{sanitize(json.dumps(passages, ensure_ascii=False))}</passages>\n"
        f"<region>{sanitize(region) or 'unknown'}</region>\n"
        f"<garden>{garden_prompt(case.garden)}</garden>"
    )
    return await complete_json(
        llm,
        step="plan",
        models=[settings.fast_model, settings.fallback_model],
        messages=[
            {"role": "system", "content": PLAN_DEEP_SYSTEM},
            {"role": "user", "content": user},
        ],
        schema=CarePlan,
        trace=trace,
        thinking=False,
        max_tokens=2500,
        temperature=0.6,
        repair_model=settings.fast_model,
    )


def code_checks(plan: CarePlan, case: Case) -> list[ClaimFlag]:
    """Deterministic checks; mutates plan.steps. Returns what was dropped or changed."""
    read = {_sid(s.id): " ".join(s.passages).lower() for s in case.sources if s.passages}
    flags: list[ClaimFlag] = []
    kept: list[PlanStep] = []
    for step in plan.steps:
        step.source_ids = [i for i in dict.fromkeys(step.source_ids) if i in read]
        text = f"{step.action} {step.why}"
        if not step.source_ids:
            flags.append(
                ClaimFlag(
                    step=step.action, verdict="dropped", reason="It cites no source Leaf read."
                )
            )
            continue
        cited = " ".join(read[i] for i in step.source_ids)
        missing = unsourced_amounts(text, cited)
        if missing:
            value, unit = sorted(missing)[0]
            flags.append(
                ClaimFlag(
                    step=step.action,
                    verdict="dropped",
                    reason=f"The amount {value:g} {unit} is not in its source.",
                )
            )
            continue
        if CHEMICAL.search(text) and "label" not in text.lower():
            step.action = f"{step.action.rstrip('. ')}. {FOLLOW_LABEL}"
            flags.append(
                ClaimFlag(
                    step=step.action,
                    verdict="label_added",
                    reason="Chemical step: added the product-label reminder.",
                )
            )
        kept.append(step)
    plan.steps = kept
    return flags


async def judge(
    llm: LLM, settings: Settings, plan: CarePlan, case: Case, trace: list[ModelCall]
) -> list[ClaimFlag]:
    """Lightning reads each step against its cited passages. Unsupported steps are dropped and
    partly supported ones flagged. If the judge is unavailable, the code checks stand."""
    if not plan.steps:
        return []
    passages = {_sid(s.id): s.passages for s in case.sources if s.passages}
    items = [
        {
            "step": i + 1,
            "text": f"{st.action} {st.why}".strip(),
            "passages": [p for sid in st.source_ids for p in passages.get(sid, [])][:4],
        }
        for i, st in enumerate(plan.steps)
    ]
    try:
        out = await complete_json(
            llm,
            step="claim_check",
            models=[settings.fast_model],
            messages=[
                {"role": "system", "content": CLAIM_CHECK_SYSTEM},
                {
                    "role": "user",
                    "content": f"<steps>{sanitize(json.dumps(items, ensure_ascii=False))}</steps>",
                },
            ],
            schema=_Verdicts,
            trace=trace,
            thinking=False,
            max_tokens=1500,
            temperature=0.2,
        )
    except ModelError as exc:
        log.warning("claim judge unavailable: %s", exc)
        return unchecked(plan)
    verdicts = {v.step: v for v in out.verdicts}
    flags: list[ClaimFlag] = []
    kept: list[PlanStep] = []
    for i, step in enumerate(plan.steps, start=1):
        v = verdicts.get(i)
        if v is None or v.verdict == "unsupported":  # no verdict counts as not supported
            reason = sanitize(v.note, 200) if v and v.note else "Its sources do not say this."
            flags.append(ClaimFlag(step=step.action, verdict="dropped", reason=reason))
            continue
        if v and v.verdict == "partial":
            step.flag = sanitize(v.note, 200) or "Only partly supported by its sources."
            flags.append(ClaimFlag(step=step.action, verdict="partial", reason=step.flag))
        kept.append(step)
    plan.steps = kept
    return flags


def unchecked(plan: CarePlan) -> list[ClaimFlag]:
    """The judge could not run: say so on every step, and drop chemical steps, which are the
    ones where an unchecked claim could do harm."""
    flags, kept = [], []
    for step in plan.steps:
        if CHEMICAL.search(f"{step.action} {step.why}"):
            reason = "Leaf could not check this chemical step against its source."
            flags.append(ClaimFlag(step=step.action, verdict="dropped", reason=reason))
            continue
        step.flag = step.flag or "Not checked against its source this time."
        flags.append(ClaimFlag(step=step.action, verdict="partial", reason=step.flag))
        kept.append(step)
    plan.steps = kept
    return flags


def check_summary(plan: CarePlan, sources_text: str) -> list[ClaimFlag]:
    """The summary is shown too: an amount it states must also be in the sources."""
    if unsourced_amounts(plan.summary, sources_text):
        plan.summary = "Follow the steps below; each one cites its source."
        return [
            ClaimFlag(
                step="summary",
                verdict="dropped",
                reason="The summary stated an amount its sources do not.",
            )
        ]
    return []
