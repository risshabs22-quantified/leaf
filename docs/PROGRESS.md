# Leaf progress log

Claude Code reads this at the start of every session and updates it at each milestone.
Keep it short: newest entries first.

## Status (update every milestone)

| Feature | Status | Evidence |
| --- | --- | --- |
| Backend pipeline (vision → Ultra → Tavily → Lightning) | done (offline) | `make check` green (all offline tests) |
| Frontend single page | done (untested live) | served at `/` |
| Real Token Factory calls | todo | run `make smoke` with real key |
| Vision model confirmed | todo | `make smoke` prints the chosen model |
| Tavily live | todo | `make smoke-all` |
| Eval set (15-20 photos) | todo | `make eval` summary |
| Deployed on Nebius Serverless Endpoint | todo | public URL + `/api/health` |
| README final, video, Devpost draft | todo | `/submission-check` |

## Next
1. User: `make setup`, add `NEBIUS_API_KEY` and `TAVILY_API_KEY` to `.env`, run `make smoke-all`.
2. Run Leaf locally (`make run`) on 3 real photos; tune prompts with `/eval-leaf`.

## Blockers
- None yet.

## Eval log
| Date | Cases | Top-1 | Top-3 | Escalated | Change |
| --- | --- | --- | --- | --- | --- |

## Session log
- 2026-10-12: Scaffold created: backend, frontend, tests, scripts, Claude Code config, playbook.
