"""Leaf's three verdicts, decided in code from the final ranking, never by the model.

    confident  a care plan
    likely     a care plan, plus what to watch for and when to send a sample
    lab        send a sample to a diagnostic lab: the causes are too spread out, the photo is
               too poor, the second opinion strongly disagrees, or a high-stakes condition
               (late blight, a quarantined pest) is in the top two

Quick scores are the model's raw candidate confidences; Deep's are its combined confidence,
which runs lower, so each mode has its own thresholds. Both are fitted on the tune split
(scripts/fit_verdicts.py) and the numbers behind them are in README.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.diseases import high_stakes

Verdict = Literal["confident", "likely", "lab"]

# A high-stakes condition sends the case to a lab only when Leaf gives it a real chance.
HIGH_STAKES_FLOOR = 0.15


@dataclass(frozen=True)
class Thresholds:
    confident_at: float  # top score at or above this, with a clear lead: Confident
    lab_below: float  # top score under this: the causes are too spread out, send a sample
    margin: float  # a lead smaller than this is a near-tie, never Confident


QUICK = Thresholds(confident_at=0.75, lab_below=0.5, margin=0.15)
DEEP = Thresholds(confident_at=0.6, lab_below=0.35, margin=0.15)


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    high_stakes: str = ""  # the condition's display name, when that is why it went to a lab


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


def decide(
    ranking: list[tuple[str, float]],
    image_quality: str,
    t: Thresholds,
    *,
    top_score: float | None = None,
    disagreement: str = "",
    contested_by: str | None = None,
) -> Decision:
    """`ranking` is (name, probability), best first. `top_score` overrides the leader's
    probability when the mode has a better estimate (Deep's combined confidence).
    `disagreement` is a non-empty reason when a second opinion strongly disagrees; a milder
    disagreement (`contested_by`, the alternative it named, or "") still rules out Confident."""
    if not ranking:
        return Decision("lab", "Leaf could not reach a diagnosis.")
    name, p = ranking[0]
    top = p if top_score is None else top_score
    for cand, prob in ranking[:2]:
        cond = high_stakes(cand)
        if cond and prob >= HIGH_STAKES_FLOOR:
            lead = "Leaf thinks this is" if cand == name else "Leaf can't rule out"
            return Decision(
                "lab",
                f"{lead} {cond.name.lower()}. {cond.high_stakes} A lab should confirm it.",
                high_stakes=cond.name,
            )
    if image_quality == "poor":
        return Decision("lab", "The photos are too unclear for a diagnosis Leaf can stand behind.")
    if disagreement:
        return Decision("lab", disagreement)
    if top < t.lab_below:
        return Decision("lab", f"The causes are too spread out: the best is {_pct(top)}.")
    second = ranking[1] if len(ranking) > 1 else None
    if second and p - second[1] < t.margin:
        return Decision(
            "likely",
            f"{name} is most likely, but {second[0]} is close behind "
            f"({_pct(p)} vs {_pct(second[1])}).",
        )
    if top < t.confident_at:
        return Decision("likely", f"{name} is the most likely cause, at {_pct(top)}.")
    if contested_by is not None:
        alt = f", which leans toward {contested_by}" if contested_by else ""
        return Decision("likely", f"{name} is most likely, but the second opinion disagrees{alt}.")
    return Decision("confident", f"{name}, at {_pct(top)}, well ahead of the alternatives.")
