"""Leaf's three verdicts, decided in code from the final ranking, never by the model.

    confident  a care plan
    likely     a care plan, plus what to watch for and when to send a sample
    lab        send a sample to a diagnostic lab: the causes are too spread out, the photo is
               too poor, the second opinion strongly disagrees, or a high-stakes condition
               (late blight, a quarantined pest) is in the top two or has 20% or more anywhere

Quick scores are the model's raw candidate confidences; Deep's are its combined confidence,
which runs lower, so each mode has its own thresholds. Both are fitted on the tune split
(scripts/fit_verdicts.py) and the numbers behind them are in docs/NUMBERS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.diseases import high_stakes

Verdict = Literal["confident", "likely", "lab"]

# A high-stakes condition sends the case to a lab when Leaf gives it a real chance: at least
# HIGH_STAKES_FLOOR in the top two, or HIGH_STAKES_ANYWHERE at any rank of the differential.
HIGH_STAKES_FLOOR = 0.15
HIGH_STAKES_ANYWHERE = 0.2


@dataclass(frozen=True)
class Thresholds:
    confident_at: float  # top score at or above this, with a clear lead: Confident
    lab_below: float  # top score under this: the causes are too spread out, send a sample
    margin: float  # a lead smaller than this is a near-tie, never Confident


# Quick with Kimi K2.6 (the pinned photo model), fitted on the 52 tune photos
# (scripts/fit_verdicts.py, run sprint-q0) at the strictest level where Confident was right on
# every tune case: 0.85 with a 0.3 lead was right 6 of 6 there and 4 of 4 on a second tune run
# (0.8 with a 0.15 lead, used until Oct 15, was right 11 of 12). On the 24 held-out photos the
# same thresholds were right only 6 of 9 times, so even this level over-claims on new photos.
QUICK = Thresholds(confident_at=0.85, lab_below=0.4, margin=0.3)
# Quick with an NVIDIA vision model (Nemotron 3.5 Super VL), fitted on the same 52 tune photos
# (run tune-nvl0, Oct 16): Confident was right only 2 of 4 times at 0.8 or 0.85 and 0 of 1 at
# 0.9, so no threshold reaches the 85% target. Quick never says Confident with it (scores are
# capped at 0.9); Auto then investigates, and the investigation's own verdict can be Confident.
QUICK_NVIDIA_VL = Thresholds(confident_at=2.0, lab_below=0.4, margin=0.15)


def quick_thresholds(vision_model: str | None) -> Thresholds:
    """Quick's thresholds depend on the vision model that wrote the observation."""
    if vision_model and vision_model.lower().startswith("nvidia/"):
        return QUICK_NVIDIA_VL
    return QUICK


# Deep with Kimi, fitted on the same photos (run sprint-deep, replayed) at the strictest level
# where Confident was right on every tune case: a combined confidence of 0.65 was right 7 of 7
# (0.6, used until Oct 15, was right 7 of 8). No case fell below 0.35, so lab_below rarely decides.
DEEP = Thresholds(confident_at=0.65, lab_below=0.3, margin=0.15)
# Deep with the NVIDIA vision model has not been validated: a Deep tune run costs about 130
# Tavily credits, more than the 60 set aside. On the held-out photos, the first two runs with
# it said Confident and were right 2 of 4 times, so it never says Confident until a tune run
# shows 85%. Lab referrals still work.
DEEP_NVIDIA_VL = Thresholds(confident_at=2.0, lab_below=0.3, margin=0.15)


def deep_thresholds(vision_model: str | None) -> Thresholds:
    if vision_model and vision_model.lower().startswith("nvidia/"):
        return DEEP_NVIDIA_VL
    return DEEP


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    high_stakes: str = ""  # the condition's display name, when that is why it went to a lab


def _pct(p: float) -> str:
    return f"{round(p * 100)}%"


_RANK = {"lab": 0, "likely": 1, "confident": 2}


def more_cautious(raw: Decision, with_prior: Decision) -> Decision:
    """The less confident of two decisions (a lab beats likely beats confident); on a tie the
    one made with the prior, which matches the ranking shown. Used so that a regional prior
    can lower a verdict but never raise it."""
    return raw if _RANK[raw.verdict] < _RANK[with_prior.verdict] else with_prior


def decide(
    ranking: list[tuple[str, float]],
    image_quality: str,
    t: Thresholds,
    *,
    top_score: float | None = None,
    disagreement: str = "",
    contested_by: str | None = None,
    also_named: list[tuple[str, float]] = (),
) -> Decision:
    """`ranking` is (name, probability), best first. `top_score` overrides the leader's
    probability when the mode has a better estimate (Deep's combined confidence).
    `disagreement` is a non-empty reason when a second opinion strongly disagrees; a milder
    disagreement (`contested_by`, the alternative it named, or "") still rules out Confident.
    `also_named` are causes outside the ranking (the second opinion's alternative), checked
    only for high-stakes conditions, at the anywhere threshold."""
    if not ranking:
        return Decision("lab", "Leaf could not reach a diagnosis.")
    name, p = ranking[0]
    top = p if top_score is None else top_score
    named = [
        (c, p, HIGH_STAKES_FLOOR if r < 2 else HIGH_STAKES_ANYWHERE)
        for r, (c, p) in enumerate(ranking)
    ] + [(c, p, HIGH_STAKES_ANYWHERE) for c, p in also_named]
    for cand, prob, floor in named:
        cond = high_stakes(cand)
        if cond and prob >= floor:
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
