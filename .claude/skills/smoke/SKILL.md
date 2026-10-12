---
name: smoke
description: Check the Nebius Token Factory key, which Nemotron models respond and in which region, which vision model Leaf will use, and optionally the Tavily key. Use after setup, after changing models or env, or whenever model calls fail.
allowed-tools: Bash(make smoke) Bash(make smoke-all)
---
1. Run `make smoke` (add Tavily: `make smoke-all`, costs 1 Tavily credit).
2. Read the output and act:
   - `NEBIUS_API_KEY is missing`: tell the user to run `make setup` and paste their key into `.env`. Never open `.env` yourself.
   - `[MISSING] ... not listed in either region`: the model ID changed. Ask the `researcher` subagent for the current ID from docs.tokenfactory.nebius.com, then update `backend/config.py` (the smoke test imports it).
   - `Add this to .env ... LEAF_MODEL_BASE_URLS=...`: give the user that exact line to add to `.env`.
   - `chat_template_kwargs not accepted`: fine; `backend/models.py` drops it automatically.
   - Empty replies in the smoke test: raise `max_tokens` in `ping()` in `scripts/smoke_test.py`. Empty replies in the app: raise `max_tokens` for that step in `backend/pipeline.py`.
   - `Vision model ... NONE FOUND`: Leaf runs text-only. Tell the user; suggest setting `LEAF_VISION_MODEL`.
3. Reply in at most 6 lines: what works, what doesn't, the exact next action.
