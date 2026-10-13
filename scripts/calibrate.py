"""Refit Deep's blend weight and confidence rules from logged eval runs. No model calls.

    python scripts/calibrate.py --label calib     # rows written by `eval.py --label calib`

For every logged Deep case, eval.py records the first differential, the investigator's own,
the second opinion and the confidence inputs. This script rebuilds each case and runs the
production functions (blend, add_alternative, combine, escalation_reason) for a grid of
settings, reporting top-1, top-3, Brier score and escalation precision and recall, so the
chosen values are measured on the tune split, not guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.case import Case, Hypothesis, Photo  # noqa: E402
from backend.critic import CriticOpinion, add_alternative, blend, combine  # noqa: E402
from backend.deep import escalation_reason  # noqa: E402
from backend.schemas import Observation  # noqa: E402
from scripts.eval import hit  # noqa: E402


def replay(row: dict, w_first: float, margin: float, critic_mode: str) -> dict:
    """Rebuild the case from the log and run the production combination on it."""
    case = Case()
    quality = (row.get("confidence_parts") or {}).get("quality", "none")
    if quality != "none":
        case.photos = [
            Photo(id="P1", full_b64="", view_b64="", observation=Observation(image_quality=quality))
        ]
    first = [Hypothesis(name=n, probability=p) for n, p in row["first"]]
    inv = [
        Hypothesis(name=n, probability=p, evidence_for=[f"E{i}" for i in range(k)])
        for n, p, k in row["investigator"]
    ]
    case.differential = blend(first, inv, w_first)
    critic = CriticOpinion(**row["critic"]) if row.get("critic") else None
    if critic is not None and critic_mode == "favoured":
        critic.agrees = not critic.alternative_probability > critic.leading_probability
    elif critic is not None and critic_mode == "margin":
        critic.agrees = critic.agrees and critic.alternative_probability < (
            critic.leading_probability - margin
        )
    if critic_mode == "ignore":
        critic = None
    add_alternative(case, critic)
    conf = combine(case, critic)
    names = [h.name for h in case.differential]
    expected = row["case"]["expected"]
    return {
        "top1": hit(names, expected, 1),
        "top3": hit(names, expected, 3),
        "conf": conf.value,
        "escalate": bool(escalation_reason(case, conf, critic)),
    }


def score(rows: list[dict]) -> dict:
    n = len(rows)
    right = sum(r["top1"] for r in rows)
    esc = [r for r in rows if r["escalate"]]
    wrong = n - right
    caught = sum(1 for r in esc if not r["top1"])
    return {
        "top1": round(right / n, 3),
        "top3": round(sum(r["top3"] for r in rows) / n, 3),
        "brier": round(sum((r["conf"] - r["top1"]) ** 2 for r in rows) / n, 4),
        "escalated": round(len(esc) / n, 3),
        "precision": round(caught / len(esc), 3) if esc else None,
        "recall": round(caught / wrong, 3) if wrong else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    args = ap.parse_args()
    rows = [
        json.loads(line)
        for line in (ROOT / "evals" / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = [
        r
        for r in rows
        if r.get("label") == args.label
        and r["mode"] == "deep"
        and "investigator" in r
        and "error" not in r
    ]
    if not rows:
        print("No logged Deep rows with that label; run eval.py --mode deep --label <label> first.")
        return 1
    print(f"{len(rows)} Deep cases\n")
    print(f"{'w_first':>7} {'critic':>8} {'margin':>6} | top-1 top-3  Brier  esc  prec  recall")
    results = []
    for w in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]:
        for mode in ["ignore", "vote", "favoured", "margin"]:
            for margin in [0.0, 0.1, 0.15, 0.25] if mode == "margin" else [0.0]:
                s = score([replay(r, w, margin, mode) for r in rows])
                results.append((w, mode, margin, s))
                print(
                    f"{w:>7} {mode:>8} {margin:>6} | {s['top1']:.2f}  {s['top3']:.2f}  "
                    f"{s['brier']:.3f}  {s['escalated']:.2f}  {s['precision']}  {s['recall']}"
                )
    best = min(results, key=lambda x: (-x[3]["top1"], x[3]["brier"]))
    print(
        f"\nbest by top-1 then Brier: w_first={best[0]} critic={best[1]} margin={best[2]} {best[3]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
