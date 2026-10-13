# Leaf progress log

Claude Code reads this at the start of every session and updates it at each milestone.
Keep it short: newest entries first.

## Status (update every milestone)

| Feature | Status | Evidence |
| --- | --- | --- |
| Backend pipeline (vision → Ultra → Tavily → Lightning) | done (offline) | `make check` green (all offline tests) |
| Frontend single page | done (redesign in progress) | served at `/` |
| Real Token Factory calls | done | `make smoke-all`: Ultra 627 ms, Super fallback 788 ms, Lightning 477 ms |
| Vision model confirmed | done | `make smoke` picks `moonshotai/Kimi-K2.6` (no NVIDIA vision model listed) |
| Tavily live | done | `make smoke-all` OK; live `/api/diagnose` returned 6 sources |
| Eval set (15-20 photos) | todo | `make eval` summary |
| Deployed on Nebius Serverless Endpoint | todo | public URL + `/api/health` |
| README final, video, Devpost draft | todo | `/submission-check` |

## Next
1. Run Leaf locally (`make run`) on 3 real photos (image path + vision step is still untested live); tune prompts with `/eval-leaf`.
2. Frontend redesign (dark, NVIDIA/LangChain-inspired), then screenshot-check it in a browser.

## Blockers
- None yet.

## Eval log
| Date | Cases | Top-1 | Top-3 | Escalated | Change |
| --- | --- | --- | --- | --- | --- |

## Session log
- 2026-10-12: Live check. `make smoke-all` green. Text-only `POST /api/diagnose` (tomato, brown ringed spots, Ohio) returned 200 in 9.7 s: top candidate Early blight (Alternaria solani) 0.55, `escalate: true` (low confidence, enforced in code), trace = Ultra (diagnose, 5.2 s) then Lightning (plan, 2.0 s), 6 Tavily sources. No provider quirks hit.
- 2026-10-12: Scaffold created: backend, frontend, tests, scripts, Claude Code config, playbook.
