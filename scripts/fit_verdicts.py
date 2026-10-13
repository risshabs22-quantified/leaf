"""Fit the verdict thresholds (backend/verdict.py) from logged eval runs. No model calls.

    python scripts/fit_verdicts.py --label sprint          # rows from `eval.py --label sprint`

Quick rows are replayed from their logged candidate scores; Deep rows are rebuilt from the
first differential, the investigator's and the second opinion, through the production blend,
add_alternative and combine (as scripts/calibrate.py does). For each grid point it reports how
often each verdict was right, and picks the thresholds that keep Confident at or above the
target precision while saying Confident as often as possible. Tune split only: the script
refuses rows from any other split.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.verdict import Thresholds, decide  # noqa: E402
from scripts.calibrate import rebuild  # noqa: E402
from scripts.eval import _high_stakes, hit, target  # noqa: E402


def quick_inputs(row: dict) -> tuple[list[tuple[str, float]], str, dict]:
    ranking = sorted(((n, p) for n, p in row["candidates"]), key=lambda x: -x[1])
    quality = (row.get("observation") or {}).get("image_quality", "good")
    return ranking, quality, {}


def deep_inputs(row: dict) -> tuple[list[tuple[str, float]], str, dict]:
    from backend.deep import CRITIC_ESCALATES_BELOW

    case, conf, critic = rebuild(row)
    ranking = [(h.name, h.probability) for h in case.differential]
    strong = critic is None or critic.leading_probability < CRITIC_ESCALATES_BELOW
    extra = {
        "top_score": conf.value,
        "disagreement": "disagree" if conf.disagreement and strong and ranking else "",
        "contested_by": (critic.alternative if critic else "") if conf.disagreement else None,
    }
    return ranking, conf.quality, extra


def score(rows: list[dict], inputs, t: Thresholds) -> dict:
    out = {v: [0, 0, 0] for v in ("confident", "likely", "lab")}  # cases, top-1, top-3/warranted
    for r in rows:
        ranking, quality, extra = inputs(r)
        d = decide(ranking, quality, t, **extra)
        names = [n for n, _ in ranking]
        h1 = hit(names, target(r["case"]), 1)
        h3 = hit(names, target(r["case"]), 3)
        b = out[d.verdict]
        b[0] += 1
        b[1] += h1
        b[2] += (not h1 or _high_stakes(r["case"])) if d.verdict == "lab" else h3
    return out


def fmt(out: dict) -> str:
    c, li, la = out["confident"], out["likely"], out["lab"]
    return (
        f"confident {c[1]}/{c[0]} | likely {li[1]}/{li[0]} (top-3 {li[2]}) | "
        f"lab {la[0]} ({la[2]} warranted)"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--mode", choices=["quick", "deep"], default="quick")
    ap.add_argument("--precision", type=float, default=0.85, help="target for Confident")
    args = ap.parse_args()
    rows = [
        json.loads(line)
        for line in (ROOT / "evals" / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = [r for r in rows if r.get("label") == args.label and r["mode"] == args.mode]
    rows = [r for r in rows if "error" not in r]
    if any(r["case"].get("split", "tune") != "tune" for r in rows):
        print("Refusing: these rows include held-out cases. Fit on the tune split only.")
        return 1
    need = "candidates" if args.mode == "quick" else "investigator"
    rows = [r for r in rows if need in r]
    if not rows:
        print(f"No logged {args.mode} rows with label {args.label!r}.")
        return 1
    inputs = quick_inputs if args.mode == "quick" else deep_inputs
    grid = [0.65, 0.7, 0.75, 0.8, 0.85] if args.mode == "quick" else [0.45, 0.5, 0.55, 0.6, 0.65]
    lows = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55] if args.mode == "quick" else [0.2, 0.25, 0.3, 0.35]
    print(f"{len(rows)} {args.mode} cases\n")
    best = None
    for conf_at, lab_below, margin in itertools.product(grid, lows, [0.1, 0.15, 0.2]):
        if lab_below >= conf_at:
            continue
        t = Thresholds(conf_at, lab_below, margin)
        out = score(rows, inputs, t)
        c = out["confident"]
        precision = c[1] / c[0] if c[0] else 0.0
        print(f"confident>={conf_at} lab<{lab_below} margin {margin}: {fmt(out)}")
        # Lab referrals that were not warranted are the cost; Confident coverage the benefit.
        # Below the target, the most precise point wins instead.
        met = precision >= args.precision
        key = (met, c[0] if met else precision, out["lab"][2] - out["lab"][0])
        if best is None or key > best[0]:
            best = (key, t, out)
    print(f"\nchosen: {best[1]}  ->  {fmt(best[2])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
