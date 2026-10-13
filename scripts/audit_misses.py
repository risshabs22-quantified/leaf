"""List every wrong top-1 answer in a logged run with a suggested cause. No model calls.

    python scripts/audit_misses.py --label sprint-q1 [--mode quick]

Suggested class, to be confirmed by looking at the photo:
  (a top answer that names no condition in backend/diseases.py is marked "unmatched": check
  by hand whether it is the right condition in other words, a naming miss)
  vision     the true condition's key sign is absent or unsure in the vision step's checklist
  reasoning  the key sign was reported present, yet the diagnosis went elsewhere
  unknown    no checklist was logged, or the condition has no single key sign
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backend.diseases import identify  # noqa: E402
from scripts.eval import target  # noqa: E402

# The sign an extension guide leads with, as checklist fields (any one present counts).
KEY_SIGNS: dict[str, list[tuple[str, str]]] = {
    "early_blight": [("concentric_rings", "present")],
    "septoria": [("dark_dots_in_lesions", "present")],
    "late_blight": [("lesion_margin", "water_soaked"), ("fuzzy_growth", "present")],
    "powdery_mildew": [("powdery_growth", "present")],
    "downy_mildew": [("lesion_shape", "angular"), ("fuzzy_growth", "present")],
    "rust": [("rust_pustules", "present")],
    "bacterial_spot": [("lesion_shape", "angular"), ("yellow_halo", "present")],
    "magnesium_deficiency": [("yellowing_pattern", "interveinal")],
    "iron_chlorosis": [("yellowing_pattern", "interveinal")],
    "manganese_deficiency": [("yellowing_pattern", "interveinal")],
    "potassium_deficiency": [("yellowing_pattern", "margins")],
    "nitrogen_deficiency": [("yellowing_pattern", "uniform")],
    "spider_mites": [("webbing", "present"), ("yellowing_pattern", "mottled")],
    "aphids": [("insects", "present")],
    "scale_insects": [("insects", "present")],
    "chewing_insects": [("insects", "present"), ("frass", "present")],
    "gray_mold": [("fuzzy_growth", "present")],
    "mosaic_virus": [("yellowing_pattern", "mottled")],
}


def suggest(row: dict) -> str:
    feats = (row.get("observation") or {}).get("features")
    signs = KEY_SIGNS.get(target(row["case"]) if isinstance(target(row["case"]), str) else "")
    if not feats or not signs:
        return "unknown"
    return "reasoning" if any(feats.get(k) == v for k, v in signs) else "vision"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--mode", default="quick")
    ap.add_argument(
        "--features-from",
        default="",
        help="label of a run with the checklist on, to show what the vision step saw",
    )
    args = ap.parse_args()
    all_rows = [
        json.loads(line)
        for line in (ROOT / "evals" / "results.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = [r for r in all_rows if r.get("label") == args.label and r["mode"] == args.mode]
    if args.features_from:
        seen = {
            r["case"]["image"]: r.get("observation")
            for r in all_rows
            if r.get("label") == args.features_from and r["mode"] == "quick"
        }
        for r in rows:
            feats = (seen.get(r["case"]["image"]) or {}).get("features")
            r.setdefault("observation", {})["features"] = feats
    misses = [r for r in rows if not r["top1"]]
    print(f"{len(misses)} misses of {len(rows)} ({args.mode}, {args.label})\n")
    for r in sorted(misses, key=lambda r: r["case"]["image"]):
        obs = r.get("observation") or {}
        feats = {
            k: v for k, v in (obs.get("features") or {}).items() if v not in ("absent", "none")
        }
        print(
            f"{r['case']['image'].split('/')[-1]} [{r['case'].get('tag')}] "
            f"truth={target(r['case'])} top3={'yes' if r['top3'] else 'no'} -> {suggest(r)}"
            + (" (top answer unmatched)" if r.get("names") and not identify(r["names"][0]) else "")
        )
        print(f"   said: {[n[:45] for n in (r.get('names') or [])[:3]]}")
        print(f"   saw:  {'; '.join(obs.get('symptoms') or [])[:220]}")
        if feats:
            print(f"   checklist: {feats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
