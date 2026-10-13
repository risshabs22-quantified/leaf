# Leaf progress log

Claude Code reads this at the start of every session and updates it at each milestone.
Keep it short: newest entries first.

## Status (update every milestone)

| Feature | Status | Evidence |
| --- | --- | --- |
| Backend pipeline (vision → Ultra → Tavily → Lightning) | done (offline) | `make check` green (all offline tests) |
| Frontend single page | done (minimal one-column redesign, checked live at 1440 and 375 px) | served at `/`, live diagnose renders |
| Backend hardening | done | two audits, 85 offline tests, each fix mutation-checked; live: bomb 400, oversize 413, injection, photo 5/5 |
| Photo path live | done | synthetic leaf through vision, Ultra, Lightning; chat follow-up works |
| Real Token Factory calls | done | `make smoke-all`: Ultra 627 ms, Super fallback 788 ms, Lightning 477 ms |
| Vision model confirmed | done | `make smoke` picks `moonshotai/Kimi-K2.6` (no NVIDIA vision model listed) |
| Tavily live | done | `make smoke-all` OK; live `/api/diagnose` returned 6 sources |
| Eval set (15-20 photos) | todo | `make eval` summary |
| Deployed on Nebius Serverless Endpoint | todo | public URL + `/api/health` |
| README final, video, Devpost draft | todo | `/submission-check` |

## Next
1. Run Leaf locally (`make run`) on 3 real photos (image path + vision step is still untested live); tune prompts with `/eval-leaf`.
2. Try real leaf photos (only a synthetic leaf has been used); add 15-20 to `evals/` and run `/eval-leaf`.
3. Deployment: set `LEAF_TRUSTED_PROXY_HOPS` to the real number of proxies in front of the endpoint (default 1; 0 if none).

## Blockers
- None yet.

## Eval log
| Date | Cases | Top-1 | Top-3 | Escalated | Change |
| --- | --- | --- | --- | --- | --- |

## Session log
- 2026-10-12: Backend hardening after an independent audit and a second review (every claim verified in code before acting). Fixed: decompression-bomb upload crash (500) and pixel cap, EXIF rotation, body size limits enforced before buffering, configurable proxy depth for per-IP limits, limiter eviction, request deadline + concurrency cap, lenient schema normalisation, near-tie escalation, prompt-injection sanitising (notes, answers, region, web snippets, URLs, chat context), bounded chat context, citation filtering, strict trusted-host matching, narrower provider-param learning, generic client errors, JSON extraction, repair prompt keeps the JSON, eval.py per-case errors, security headers + CSP. **Provider quirk found and fixed:** Kimi-K2.6 reasons for 1.4k-2.4k tokens before answering, so the 1500-token vision budget intermittently produced empty or truncated JSON and the vision step failed; vision budget is now 4000 and JSON truncated at max_tokens is retried with a bigger budget like an empty reply. Vision 5/5 live after the fix. Tests mutation-checked (note: a same-size mutation can leave a stale .pyc; clear `__pycache__` after mutation runs).
- 2026-10-12: Frontend rebuilt as a minimal single column (serif prose, soft marker CTA, quiet run list) in Leaf's greens, after the earlier dark NVIDIA/LangChain-style version.
- 2026-10-12: Frontend redesign: dark nav/hero/footer with a pale workbench (NVIDIA- and LangChain-inspired design language, original leaf mark, no third-party logos or assets). Hero shows a live agent graph (vision, Ultra, Tavily, Lightning) driven by `/api/health` model IDs and the real trace. Checked in a browser: text-only diagnose renders, no console errors, no horizontal overflow at 375 px.
- 2026-10-12: Live check. `make smoke-all` green. Text-only `POST /api/diagnose` (tomato, brown ringed spots, Ohio) returned 200 in 9.7 s: top candidate Early blight (Alternaria solani) 0.55, `escalate: true` (low confidence, enforced in code), trace = Ultra (diagnose, 5.2 s) then Lightning (plan, 2.0 s), 6 Tavily sources. No provider quirks hit.
- 2026-10-12: Scaffold created: backend, frontend, tests, scripts, Claude Code config, playbook.
