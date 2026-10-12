---
paths:
  - "backend/**/*.py"
  - "tests/**/*.py"
  - "scripts/**/*.py"
---
# Backend rules
- Every runtime model call goes through `LLM.complete` or `complete_json` in `backend/models.py` (`scripts/smoke_test.py` is the one dev-only exception). Add model IDs only in `config.py`.
- Every new model step appends a `ModelCall` to the trace, so the UI and README can show the routing.
- Rules that protect users (escalation, source-id filtering, upload limits) are enforced in code, not only in prompts.
- Prompts live in `prompts.py` and name the exact JSON shape; schemas live in `schemas.py`. Change both together.
- Nemotron 3.x: temperature 1.0 and top_p 0.95 by default; reasoning tokens count toward max_tokens, so keep max_tokens generous for Ultra.
- Unit tests use `FakeLLM` and `FakeSearcher` from `tests/conftest.py`. They never touch the network.
- Async everywhere in request paths; run independent calls concurrently with `gather_limited`.
