"""Score Quick and Deep on labelled photos. Spends real credits (Quick about 1 cent per case,
Deep about 5 cents plus a few Tavily credits).

    make eval                                         # both modes, all cases
    python scripts/eval.py --split tune --mode both   # cases you tune on
    python scripts/eval.py --split test               # held-out cases you report
    python scripts/eval.py --mode deep --no-seed      # Deep without the first differential

Each line of evals/cases.jsonl (lines starting with // are ignored):
    {"image": "evals/images/tune/tomato_early_blight_1.jpg", "expected": ["early blight"],
     "crop": "tomato", "notes": "", "region": "", "split": "tune", "tag": "clear"}

Metrics per mode: top-1 and top-3 hit rate (case-insensitive substring match of an expected
name in a candidate name), Brier score of the reported confidence against top-1 being right,
a reliability table, escalation precision and recall (an escalation is "right" when top-1 is
wrong), and mean cost and latency per case. If Deep asks questions, the eval answers "I'm not
sure", as a gardener who cannot tell would. Results go to evals/results.jsonl and a summary to
evals/summary_<split>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.app import build_leaf  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.deep import DeepLeaf  # noqa: E402
from backend.models import ModelError  # noqa: E402
from backend.store import MemoryCaseStore  # noqa: E402
from backend.vision import prepare_image, prepare_photo  # noqa: E402

BINS = [0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]


def hit(names: list[str], expected: list[str], k: int) -> bool:
    top = [n.lower() for n in names[:k]]
    return any(e.lower() in n for e in expected for n in top)


def load_cases(path: str, split: str, limit: int) -> list[dict]:
    lines = [ln for ln in Path(path).read_text().splitlines() if ln.strip()]
    cases = [json.loads(ln) for ln in lines if not ln.lstrip().startswith("//")]
    if split != "all":
        cases = [c for c in cases if c.get("split", "tune") == split]
    return cases[:limit] if limit else cases


async def run_quick(leaf, case: dict, data: bytes) -> dict[str, Any]:
    b64, mime = prepare_image(data)
    res = await leaf.diagnose(
        image_b64=b64, mime=mime, notes=case.get("notes", ""), region=case.get("region", "")
    )
    names = [c.name for c in res.diagnosis.candidates]
    return {
        "names": names,
        "confidence": res.diagnosis.candidates[0].confidence if names else 0.0,
        "escalate": res.plan.escalate,
        "cost": sum(t.cost_usd or 0 for t in res.trace),
        "credits": None,  # Quick does not count Tavily credits per case
    }


async def run_deep(deep: DeepLeaf, case: dict, data: bytes) -> dict[str, Any]:
    full, view = prepare_photo(data)
    res = await deep.start(
        photos=[("photo", full, view)], notes=case.get("notes", ""), region=case.get("region", "")
    )
    asked = res.status == "needs_answers"
    if asked:  # a gardener who cannot tell
        stored = await deep.store.get(res.case_id)
        res = await deep.resume(stored, ["I'm not sure."] * len(res.questions))
    names = [h.name for h in res.differential] or [c.name for c in res.diagnosis.candidates]
    if res.confidence:
        conf = res.confidence.value
    elif res.differential:
        conf = res.differential[0].probability
    else:  # fell back to Quick: use Quick's own top confidence
        conf = res.diagnosis.candidates[0].confidence if res.diagnosis.candidates else 0.0
    return {
        "names": names,
        "confidence": conf,
        "escalate": res.plan.escalate,
        "cost": res.cost_usd,
        "credits": res.tavily_credits,
        "asked": asked,
        "mode_used": res.mode,
        "tools": [s.tool for s in res.steps if s.ok],
        # Inputs for scripts/calibrate.py, which refits the blend and confidence offline.
        "first": [[h.name, h.probability] for h in res.first_differential],
        "investigator": [
            [h.name, h.probability, len(h.evidence_for)] for h in res.investigator_differential
        ],
        "critic": res.critic.model_dump() if res.critic else None,
        "confidence_parts": res.confidence.model_dump() if res.confidence else None,
    }


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]
    n = len(rows)
    if not ok:
        return {"cases": n, "errors": n}
    right = [r["top1"] for r in ok]
    esc = [r["escalate"] for r in ok]
    wrong_escalated = sum(1 for r in ok if r["escalate"] and not r["top1"])
    reliability = []
    for lo, hi in zip(BINS, BINS[1:], strict=False):
        b = [r for r in ok if lo <= r["confidence"] < hi]
        if b:
            reliability.append(
                {
                    "bin": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                    "cases": len(b),
                    "mean_confidence": round(sum(r["confidence"] for r in b) / len(b), 3),
                    "accuracy": round(sum(r["top1"] for r in b) / len(b), 3),
                }
            )
    return {
        "cases": n,
        "errors": n - len(ok),
        "top1": round(sum(right) / n, 3),
        "top3": round(sum(r["top3"] for r in ok) / n, 3),
        "brier": round(sum((r["confidence"] - r["top1"]) ** 2 for r in ok) / len(ok), 4),
        "escalated": round(sum(esc) / len(ok), 3),
        "escalation_precision": round(wrong_escalated / sum(esc), 3) if sum(esc) else None,
        "escalation_recall": round(wrong_escalated / (len(ok) - sum(right)), 3)
        if len(ok) - sum(right)
        else None,
        "mean_cost_usd": round(sum(r["cost"] for r in ok) / len(ok), 4),
        "mean_tavily_credits": round(sum(r["credits"] for r in ok) / len(ok), 1)
        if all(r.get("credits") is not None for r in ok)
        else None,
        "mean_seconds": round(sum(r["seconds"] for r in ok) / len(ok), 1),
        "reliability": reliability,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.jsonl"))
    ap.add_argument("--split", choices=["tune", "test", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--mode", choices=["quick", "deep", "both"], default="both")
    ap.add_argument("--no-seed", action="store_true", help="Deep without the first differential")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--label", default="", help="tag for this run in the results file")
    args = ap.parse_args()

    cases = load_cases(args.cases, args.split, args.limit)
    if not cases:
        print("No cases yet. Add photos to evals/images and lines to evals/cases.jsonl.")
        return 1
    settings = get_settings()
    if args.no_seed:
        settings = dataclasses.replace(settings, deep_seed=False)
    leaf = await build_leaf(settings)
    deep = DeepLeaf(leaf, MemoryCaseStore())
    modes = ["quick", "deep"] if args.mode == "both" else [args.mode]
    label = args.label or ("deep-noseed" if args.no_seed else "")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: dict[str, list[dict[str, Any]]] = {m: [] for m in modes}

    async def one(mode: str, case: dict) -> None:
        img = ROOT / case["image"]
        if not img.exists():
            print(f"skip (missing image): {case['image']}")
            return
        async with sem:
            start = time.time()
            try:
                data = img.read_bytes()
                out = await (
                    run_quick(leaf, case, data) if mode == "quick" else run_deep(deep, case, data)
                )
            except (ValueError, ModelError, TimeoutError) as exc:
                row = {
                    "mode": mode,
                    "case": case,
                    "error": str(exc)[:300],
                    "top1": False,
                    "top3": False,
                }
                print(f"✗ [{mode}] {case['image']}: error, counted as a miss ({exc})")
                results[mode].append(row)
                return
        secs = round(time.time() - start, 1)
        h1, h3 = hit(out["names"], case["expected"], 1), hit(out["names"], case["expected"], 3)
        row = {
            "mode": mode,
            "label": label,
            "case": case,
            "top1": h1,
            "top3": h3,
            "seconds": secs,
            **out,
        }
        results[mode].append(row)
        mark = "✓" if h1 else ("~" if h3 else "✗")
        print(
            f"{mark} [{mode}] {case['image'].split('/')[-1]}: expected {case['expected'][0]!r} "
            f"got {[n[:30] for n in out['names'][:2]]} conf {out['confidence']:.2f} "
            f"{'ESC ' if out['escalate'] else ''}{secs}s ${out['cost']:.3f}"
        )

    await asyncio.gather(*(one(m, c) for m in modes for c in cases))

    out_path = ROOT / "evals" / "results.jsonl"
    with out_path.open("a") as f:
        for rows in results.values():
            for r in rows:
                f.write(json.dumps(r) + "\n")
    summary = {m: summarise(rows) for m, rows in results.items()}
    (ROOT / "evals" / f"summary_{args.split}{'_' + label if label else ''}.json").write_text(
        json.dumps({"split": args.split, "label": label, "summary": summary}, indent=1)
    )
    print()
    for m, s in summary.items():
        if "top1" not in s:
            print(f"{m}: all {s['cases']} cases failed")
            continue
        print(
            f"{m:>5}: {s['cases']} cases | top-1 {s['top1']:.0%} | top-3 {s['top3']:.0%} | "
            f"Brier {s['brier']} | escalated {s['escalated']:.0%} "
            f"(precision {s['escalation_precision']}, recall {s['escalation_recall']}) | "
            f"${s['mean_cost_usd']}/case | {s['mean_seconds']}s | errors {s['errors']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
