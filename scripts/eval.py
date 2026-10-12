"""Run Leaf on labelled photos and score it. Spends real credits (about 1-3 cents per case).

    make eval                              # all cases
    python scripts/eval.py --split tune    # cases you tune prompts on
    python scripts/eval.py --split test    # held-out cases you report (never tune on these)

Each line of evals/cases.jsonl (lines starting with // are ignored):
    {"image": "evals/images/tomato_early_blight_1.jpg", "expected": ["early blight"],
     "notes": "", "region": "Ohio", "split": "tune", "tag": "clear"}

`split` is "tune" or "test" (default "tune"). `tag` groups cases for the escalation report,
e.g. "clear", "poor_photo", "abiotic". Metrics: top-1 and top-3 hit rate (case-insensitive
substring match on candidate names), escalation rate overall and per tag, mean latency,
tokens. Results go to evals/results.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.app import build_leaf  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.vision import prepare_image  # noqa: E402


def hit(names: list[str], expected: list[str], k: int) -> bool:
    top = [n.lower() for n in names[:k]]
    return any(e.lower() in n for e in expected for n in top)


def load_cases(path: str, split: str, limit: int) -> list[dict]:
    lines = [ln for ln in Path(path).read_text().splitlines() if ln.strip()]
    cases = [json.loads(ln) for ln in lines if not ln.lstrip().startswith("//")]
    if split != "all":
        cases = [c for c in cases if c.get("split", "tune") == split]
    return cases[:limit] if limit else cases


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.jsonl"))
    ap.add_argument("--split", choices=["tune", "test", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cases = load_cases(args.cases, args.split, args.limit)
    if not cases:
        print("No cases yet. Add photos to evals/images and lines to evals/cases.jsonl.")
        return 1

    leaf = await build_leaf(get_settings())
    out_path = ROOT / "evals" / "results.jsonl"
    top1 = top3 = 0
    latencies: list[float] = []
    tokens = 0
    by_tag: dict[str, list[bool]] = defaultdict(list)
    with out_path.open("w") as out:
        for c in cases:
            img_path = ROOT / c["image"]
            if not img_path.exists():
                print(f"skip (missing image): {c['image']}")
                continue
            b64, mime = prepare_image(img_path.read_bytes())
            start = time.time()
            res = await leaf.diagnose(
                image_b64=b64, mime=mime, notes=c.get("notes", ""), region=c.get("region", "")
            )
            secs = time.time() - start
            names = [cand.name for cand in res.diagnosis.candidates]
            h1, h3 = hit(names, c["expected"], 1), hit(names, c["expected"], 3)
            top1, top3 = top1 + h1, top3 + h3
            by_tag[c.get("tag", "untagged")].append(res.plan.escalate)
            latencies.append(secs)
            tokens += sum((t.prompt_tokens or 0) + (t.completion_tokens or 0) for t in res.trace)
            mark = "✓" if h1 else ("~" if h3 else "✗")
            print(f"{mark} {c['image']}: expected {c['expected']} got {names[:3]} ({secs:.1f}s)")
            record = {
                "case": c,
                "top3": names[:3],
                "top1_hit": h1,
                "top3_hit": h3,
                "escalate": res.plan.escalate,
                "seconds": round(secs, 1),
            }
            out.write(json.dumps(record) + "\n")

    n = len(latencies)
    if not n:
        return 1
    escalated = sum(sum(v) for v in by_tag.values())
    print(
        f"\n{n} cases ({args.split}) | top-1 {top1 / n:.0%} | top-3 {top3 / n:.0%} | "
        f"escalated {escalated / n:.0%} | mean {sum(latencies) / n:.1f}s | {tokens} tokens"
    )
    print("escalation by tag: " + ", ".join(f"{t} {sum(v)}/{len(v)}" for t, v in by_tag.items()))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
