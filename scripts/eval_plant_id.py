"""Score photo identification on evals/plant_id.jsonl (40 licensed Commons photos).

    .venv/bin/python -m scripts.eval_plant_id            # all 40
    .venv/bin/python -m scripts.eval_plant_id --limit 5

Spends credits: one vision call and one Super check per photo (about $0.004 a photo) and one
Tavily credit per photo (cached for a day). Reports top-1 and top-3 accuracy at species and
genus level, both for the vision model alone and after Nemotron's check against sources,
plus time and cost. Results go to evals/summary_plant_id.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.knowledge import genus, species_key
from backend.knowledge.identify import identify
from backend.models import TokenFactory
from backend.search import CreditBudget, NullSearcher, TavilySearcher, tavily_balance
from backend.vision import discover_vision_model, prepare_image

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "plant_id.jsonl"


def load_cases(path: Path = CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def accepted(case: dict[str, Any]) -> set[str]:
    return {
        species_key(n, binomial=True) for n in [case["scientific_name"], *case.get("synonyms", [])]
    }


def score_case(case: dict[str, Any], ranked: list[str]) -> dict[str, bool]:
    """Species and genus hits at rank 1 and within the top 3 of a ranked list of keys."""
    ok = accepted(case)
    genera = {genus(k) for k in ok}
    return {
        "species_top1": bool(ranked) and ranked[0] in ok,
        "species_top3": any(k in ok for k in ranked[:3]),
        "genus_top1": bool(ranked) and genus(ranked[0]) in genera,
    }


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1

    def rate(key: str, which: str) -> float:
        return round(sum(r[which][key] for r in rows) / n, 3)

    return {
        "cases": len(rows),
        "vision_only": {
            k: rate(k, "vision") for k in ("species_top1", "species_top3", "genus_top1")
        },
        "checked": {k: rate(k, "checked") for k in ("species_top1", "species_top3", "genus_top1")},
        "check_fixed": [
            r["file"]
            for r in rows
            if r["checked"]["species_top1"] and not r["vision"]["species_top1"]
        ],
        "check_broke": [
            r["file"]
            for r in rows
            if r["vision"]["species_top1"] and not r["checked"]["species_top1"]
        ],
        "confident_right": sum(1 for r in rows if r["confident"] and r["checked"]["species_top1"]),
        "confident_total": sum(1 for r in rows if r["confident"]),
        "median_s": round(statistics.median(r["seconds"] for r in rows), 1) if rows else None,
        "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
    }


async def main() -> int:  # pragma: no cover - spends credits
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    settings = Settings()
    llm = TokenFactory(settings)
    vision, _ = await asyncio.gather(discover_vision_model(llm, settings), llm.load_prices())
    if not vision:
        raise SystemExit("no vision model available")
    if settings.tavily_api_key:
        key = settings.tavily_api_key
        searcher = TavilySearcher(key, CreditBudget(lambda: tavily_balance(key), max_daily=60))
    else:
        searcher = NullSearcher()
    cases = load_cases()[: args.limit or None]
    rows = []
    for case in cases:
        data = (ROOT / "evals" / "images" / case["file"]).read_bytes()
        image_b64, _ = prepare_image(data)
        trace: list = []
        start = time.perf_counter()
        out = await identify(llm, settings, searcher, vision, image_b64, "", trace)
        seconds = time.perf_counter() - start
        vision_ranked = [
            c["key"] for c in sorted(out["candidates"], key=lambda c: -c["vision_confidence"])
        ]
        checked_ranked = [c["key"] for c in out["candidates"]]
        row = {
            "file": case["file"],
            "label": case["scientific_name"],
            "vision_top": vision_ranked[:3],
            "checked_top": checked_ranked[:3],
            "vision": score_case(case, vision_ranked),
            "checked": score_case(case, checked_ranked),
            "was_checked": out["checked"],
            "confident": out["confident"],
            "seconds": round(seconds, 1),
            "cost_usd": round(sum(c.cost_usd or 0 for c in trace), 5),
            "models": [c.model for c in trace],
        }
        rows.append(row)
        mark = "OK " if row["checked"]["species_top1"] else "MISS"
        print(
            f"{mark} {case['scientific_name']:28s} -> {checked_ranked[:2]} ({seconds:.0f} s)",
            flush=True,
        )
    summary = summarise(rows)
    out_path = ROOT / "evals" / "summary_plant_id.json"
    out_path.write_text(
        json.dumps({"vision_model": vision, "summary": summary, "rows": rows}, indent=1)
    )
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
