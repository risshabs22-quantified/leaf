"""Leaf smoke test: proves your keys work and tells you exactly what to put in .env.

    make smoke            # or: python scripts/smoke_test.py [--tavily]

Checks both Token Factory base URLs (global and us-central1), calls each Nemotron
model Leaf routes to, finds an image-capable model, and optionally spends 1 Tavily
credit to check that key. Costs a fraction of a cent.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import BadRequestError, OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()
from backend.config import GLOBAL_BASE_URL, US_CENTRAL_BASE_URL, get_settings  # noqa: E402
from backend.vision import choose_vision_model  # noqa: E402

_s = get_settings()  # respects LEAF_*_MODEL overrides, same as the running app
TARGETS = {"reason": _s.reason_model, "fallback": _s.fallback_model, "fast": _s.fast_model}


def list_models(base: str, key: str) -> list[dict]:
    try:
        r = httpx.get(
            base.rstrip("/") + "/models",
            params={"verbose": "true"},
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except httpx.HTTPError as exc:
        print(f"  ! could not list models at {base}: {exc}")
        return []


def ping(client: OpenAI, model: str) -> str:
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": "Reply with exactly: Leaf is alive."}],
        max_tokens=200,
    )
    start = time.time()
    try:
        r = client.chat.completions.create(
            **kwargs, extra_body={"chat_template_kwargs": {"enable_thinking": False}}
        )
        note = ""
    except BadRequestError:
        r = client.chat.completions.create(**kwargs)
        note = " (chat_template_kwargs not accepted)"
    ms = (time.time() - start) * 1000
    text = (r.choices[0].message.content or "").strip().replace("\n", " ")[:50]
    u = r.usage
    toks = f"{u.prompt_tokens}/{u.completion_tokens} tok" if u else "tok n/a"
    return f"{ms:.0f} ms, {toks}{note} -> {text!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tavily", action="store_true", help="also spend 1 Tavily credit to test it")
    args = ap.parse_args()

    key = os.environ.get("NEBIUS_API_KEY", "").strip()
    if not key:
        print("NEBIUS_API_KEY is missing. Copy .env.example to .env and add it.")
        return 1

    catalogs = {base: list_models(base, key) for base in (GLOBAL_BASE_URL, US_CENTRAL_BASE_URL)}
    ids = {base: {m.get("id") for m in models} for base, models in catalogs.items()}
    for base, models in catalogs.items():
        nv = sorted(i for i in ids[base] if i and i.lower().startswith("nvidia/"))
        print(f"{base}\n  {len(models)} models, NVIDIA: {', '.join(nv) or 'none'}")
    print()

    failures, overrides = 0, []
    for role, model in TARGETS.items():
        base = next((b for b in catalogs if model in ids[b]), None)
        if base is None:
            print(f"[MISSING] {role:8} {model} is not listed in either region.")
            failures += 1
            continue
        if base != GLOBAL_BASE_URL:
            overrides.append(f"{model}={base}")
        try:
            print(
                f"[OK]      {role:8} {model} @ {base.split('//')[1].split('/')[0]}: "
                f"{ping(OpenAI(base_url=base, api_key=key), model)}"
            )
        except Exception as exc:  # noqa: BLE001 - report every failure plainly
            print(f"[ERROR]   {role:8} {model}: {exc}")
            failures += 1

    vision = choose_vision_model(catalogs[GLOBAL_BASE_URL], os.environ.get("LEAF_VISION_MODEL", ""))
    print(f"\nVision model Leaf will use: {vision or 'NONE FOUND (text-only mode)'}")

    if args.tavily:
        tkey = os.environ.get("TAVILY_API_KEY", "").strip()
        if not tkey:
            print("[MISSING] TAVILY_API_KEY")
            failures += 1
        else:
            from tavily import TavilyClient

            res = TavilyClient(api_key=tkey).search("tomato early blight extension", max_results=1)
            print(f"[OK]      Tavily: {len(res.get('results', []))} result(s)")

    if overrides:
        print("\nAdd this to .env so Leaf calls the right region:")
        print(f'LEAF_MODEL_BASE_URLS="{";".join(overrides)}"')
    print("\nAll good." if not failures else f"\n{failures} problem(s) to fix (see above).")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
