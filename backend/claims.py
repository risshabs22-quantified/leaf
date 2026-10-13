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

from backend.case import Case, SourceRef
from backend.config import Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import CLAIM_CHECK_SYSTEM, PLAN_DEEP_SYSTEM, sanitize
from backend.schemas import CarePlan, ModelCall, PlanStep
from backend.search import Searcher

log = logging.getLogger("leaf.claims")

MAX_AUTO_READS = 2
CHEMICAL = re.compile(
    r"\b(fungicide|bactericide|insecticide|pesticide|miticide|herbicide|copper|mancozeb|"
    r"chlorothalonil|sulfur|sulphur|neem|spinosad|pyrethr\w*|bacillus|potassium bicarbonate|"
    r"spray)\b",
    re.IGNORECASE,
)
RATE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|ml|millilit\w*|l\b|liters?|litres?|oz|ounces?|fl\.? ?oz|"
    r"tbsp|tablespoons?|tsp|teaspoons?|g\b|grams?|kg|lbs?|pounds?|gal\w*|quarts?|pints?|"
    r"cups?|ppm)",
    re.IGNORECASE,
)
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
    plan always rests on passages, never on search snippets alone."""
    if any(s.passages for s in case.sources) or not case.differential:
        return
    top = case.differential[0].name
    candidates = sorted(case.sources, key=lambda s: not s.trusted)[:MAX_AUTO_READS]
    query = f"{top} management and treatment for home gardeners"

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
        f"<region>{sanitize(region) or 'unknown'}</region>"
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
        missing = [
            r
            for r in RATE.findall(text)
            if r.lower().replace(",", ".") not in cited.replace(",", ".")
        ]
        if missing:
            flags.append(
                ClaimFlag(
                    step=step.action,
                    verdict="dropped",
                    reason=f"The amount {missing[0]!r} is not in its source.",
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
        return []
    verdicts = {v.step: v for v in out.verdicts}
    flags: list[ClaimFlag] = []
    kept: list[PlanStep] = []
    for i, step in enumerate(plan.steps, start=1):
        v = verdicts.get(i)
        if v and v.verdict == "unsupported":
            flags.append(
                ClaimFlag(
                    step=step.action,
                    verdict="dropped",
                    reason=sanitize(v.note, 200) or "Its sources do not say this.",
                )
            )
            continue
        if v and v.verdict == "partial":
            step.flag = sanitize(v.note, 200) or "Only partly supported by its sources."
            flags.append(ClaimFlag(step=step.action, verdict="partial", reason=step.flag))
        kept.append(step)
    plan.steps = kept
    return flags
