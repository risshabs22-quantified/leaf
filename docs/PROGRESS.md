# Leaf progress log

Claude Code reads this at the start of every session and updates it at each milestone.
Keep it short: newest entries first.

## Status (update every milestone)

| Feature | Status | Evidence |
| --- | --- | --- |
| Backend pipeline (vision → Ultra → Tavily → Lightning) | done (offline) | `make check` green (all offline tests) |
| Frontend single page | done (redesigned, checked live at 1440 and 375 px) | served at `/`, live diagnose renders |
| Real Token Factory calls | done | `make smoke-all`: Ultra 627 ms, Super fallback 788 ms, Lightning 477 ms |
| Vision model confirmed | done | `make smoke` picks `moonshotai/Kimi-K2.6` (no NVIDIA vision model listed) |
| Tavily live | done | `make smoke-all` OK; live `/api/diagnose` returned 6 sources |
| Eval set (15-20 photos) | todo | `make eval` summary |
| Deployed on Nebius Serverless Endpoint | todo | public URL + `/api/health` |
| README final, video, Devpost draft | todo | `/submission-check` |

## Next
1. Run Leaf locally (`make run`) on 3 real photos (image path + vision step is still untested live); tune prompts with `/eval-leaf`.
2. Test the photo upload path in the redesigned UI (only the text-only run was checked in the browser).

## Blockers
- None yet.

## Eval log
| Date | Cases | Top-1 | Top-3 | Escalated | Change |
| --- | --- | --- | --- | --- | --- |

## Session log
- 2026-10-12: Frontend redesign: dark nav/hero/footer with a pale workbench (NVIDIA- and LangChain-inspired design language, original leaf mark, no third-party logos or assets). Hero shows a live agent graph (vision, Ultra, Tavily, Lightning) driven by `/api/health` model IDs and the real trace. Checked in a browser: text-only diagnose renders, no console errors, no horizontal overflow at 375 px.
- 2026-10-12: Live check. `make smoke-all` green. Text-only `POST /api/diagnose` (tomato, brown ringed spots, Ohio) returned 200 in 9.7 s: top candidate Early blight (Alternaria solani) 0.55, `escalate: true` (low confidence, enforced in code), trace = Ultra (diagnose, 5.2 s) then Lightning (plan, 2.0 s), 6 Tavily sources. No provider quirks hit.
- 2026-10-12: Scaffold created: backend, frontend, tests, scripts, Claude Code config, playbook.
