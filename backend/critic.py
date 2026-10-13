"""Second opinion and final confidence.

Nemotron Super reviews the case from the evidence alone (never the investigator's reasoning or
probabilities) and argues for the strongest alternative. Code, not a model, then combines both
opinions with photo quality and evidence coverage into the confidence the gardener sees.
The weights are provisional; M6 calibrates them on the tune split (Brier score).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.case import MAX_PROBABILITY, Case, Hypothesis
from backend.config import Settings
from backend.models import LLM, ModelError, complete_json
from backend.prompts import CRITIC_SYSTEM, sanitize
from backend.schemas import ModelCall, _unit

log = logging.getLogger("leaf.critic")

AGREEMENT_MARGIN = 0.15  # the critic "disagrees" when its alternative is this close or ahead
QUALITY_FACTOR = {"good": 1.0, "fair": 0.95, "poor": 0.7, "none": 0.9}  # "none": notes only
COVERAGE_FACTOR = {0: 0.8, 1: 0.9}  # evidence items behind the leading hypothesis; 2+ -> 1.0


class CriticOpinion(BaseModel):
    alternative: str = ""
    alternative_probability: float = Field(0.0, ge=0.0, le=1.0)
    leading_probability: float = Field(0.5, ge=0.0, le=1.0)
    agrees: bool = True
    key_evidence: list[str] = Field(default_factory=list)
    argument: str = ""

    @field_validator("alternative_probability", "leading_probability", mode="before")
    @classmethod
    def _p(cls, v: Any) -> Any:
        return _unit(v)


class Confidence(BaseModel):
    """The final confidence and every factor behind it, so the UI can explain it."""

    value: float
    investigator: float
    critic: float | None = None
    disagreement: bool = False
    quality: str = "none"
    quality_factor: float = 1.0
    coverage: int = 0
    coverage_factor: float = 1.0


def _evidence_for_critic(case: Case) -> list[dict[str, Any]]:
    """Evidence only: tool results and first looks. No probabilities, no summary."""
    items = [
        {
            "id": e.id,
            "tool": e.tool,
            "finding": e.summary,
            "detail": {k: v for k, v in e.data.items() if k not in ("indices",)}
            | (
                {
                    "indices": [
                        {"name": i.get("name"), "value": i.get("last_7_days", i.get("count"))}
                        for i in e.data.get("indices", [])
                    ]
                }
                if e.data.get("indices")
                else {}
            ),
        }
        for e in case.evidence
    ]
    looks = [
        {"photo": p.id, "label": p.label, "first_look": p.observation.model_dump()}
        for p in case.photos
        if p.observation
    ]
    passages = [
        {"source_id": s.id, "trusted": s.trusted, "passages": s.passages[:2]}
        for s in case.sources
        if s.passages
    ]
    return [{"first_looks": looks}, {"evidence": items}, {"passages": passages}]


async def second_opinion(
    llm: LLM, settings: Settings, case: Case, trace: list[ModelCall]
) -> CriticOpinion | None:
    if not case.differential:
        return None
    leading = case.differential[0].name
    others = [h.name for h in case.differential[1:]]
    payload = sanitize(json.dumps(_evidence_for_critic(case), ensure_ascii=False))
    user = (
        f"<leading_diagnosis>{sanitize(leading, 120)}</leading_diagnosis>\n"
        f"<other_candidates>{sanitize(json.dumps(others), 600)}</other_candidates>\n"
        f"<gardener_notes>{sanitize(case.notes) or 'none'}</gardener_notes>\n"
        f"<case_evidence>{payload[:12000]}</case_evidence>"
    )
    try:
        opinion = await complete_json(
            llm,
            step="critic",
            models=[settings.fallback_model, settings.fast_model],
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM},
                {"role": "user", "content": user},
            ],
            schema=CriticOpinion,
            trace=trace,
            thinking=True,
            max_tokens=3000,
            repair_model=settings.fast_model,
        )
    except ModelError as exc:
        log.warning("second opinion unavailable: %s", exc)
        return None
    known = case.evidence_ids()
    opinion.key_evidence = [e for e in opinion.key_evidence if e in known][:6]
    opinion.alternative = sanitize(opinion.alternative, 120)
    opinion.argument = sanitize(opinion.argument, 600)
    return opinion


def _quality(case: Case) -> str:
    qualities = [p.observation.image_quality for p in case.photos if p.observation]
    if not qualities:
        return "none"
    for q in ("good", "fair"):  # the best photo decides: a good close-up rescues a poor one
        if q in qualities:
            return q
    return "poor"


def combine(case: Case, critic: CriticOpinion | None) -> Confidence:
    top = case.differential[0] if case.differential else None
    p_inv = top.probability if top else 0.0
    quality = _quality(case)
    coverage = len(set(top.evidence_for)) if top else 0
    q_factor = QUALITY_FACTOR[quality]
    c_factor = COVERAGE_FACTOR.get(coverage, 1.0)
    if critic is None:
        base, disagreement, p_crit = p_inv * 0.9, False, None  # no second opinion: less sure
    else:
        p_crit = critic.leading_probability
        disagreement = (not critic.agrees) or (
            critic.alternative_probability >= critic.leading_probability - AGREEMENT_MARGIN
        )
        base = (p_inv + p_crit) / 2
        if disagreement:
            base = min(base, p_crit)
    value = round(min(MAX_PROBABILITY, max(0.0, base * q_factor * c_factor)), 3)
    return Confidence(
        value=value,
        investigator=round(p_inv, 3),
        critic=None if p_crit is None else round(p_crit, 3),
        disagreement=disagreement,
        quality=quality,
        quality_factor=q_factor,
        coverage=coverage,
        coverage_factor=c_factor,
    )


def core_name(name: str) -> str:
    """'Bacterial spot (Xanthomonas spp.)' -> 'bacterial spot': compare diseases, not spellings."""
    import re

    name = re.sub(r"\([^)]*\)", " ", name.lower())
    return " ".join(re.sub(r"[^a-z ]", " ", name).split())


def add_alternative(case: Case, critic: CriticOpinion | None) -> None:
    """Show the critic's alternative in the differential if the investigator left it out."""
    if critic is None or not critic.alternative:
        return
    alt = core_name(critic.alternative)
    if not alt or any(
        alt == core_name(h.name) or alt in core_name(h.name) or core_name(h.name) in alt
        for h in case.differential
    ):
        return
    case.differential.append(
        Hypothesis(
            name=f"{critic.alternative} (second opinion)",
            probability=min(critic.alternative_probability, MAX_PROBABILITY),
            evidence_for=critic.key_evidence,
        )
    )
    case.differential = case.differential[:5]
