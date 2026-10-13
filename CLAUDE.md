# Leaf

Plant-doctor agent for the Nebius x NVIDIA Global AI Hackathon.
Deadline: **Fri Oct 30, 2026, 10:00 AM PT**. Track: Best Apps and Agents, plus the Best Use of Tavily bonus.
Flow: photo → vision model → Nemotron 3 Ultra diagnosis → Tavily sources → Nemotron 3.5 Lightning care plan.

## Hard rules (breaking one can disqualify Leaf)
- Leaf's runtime model calls go only through `backend/models.py` to Nebius Token Factory. Never OpenRouter, Hugging Face or any other provider, not even for NVIDIA models.
- NVIDIA Nemotron does the reasoning and writing. The vision step is the one exception: Token Factory has no public NVIDIA vision model (Oct 2026). Keep it swappable and prefer NVIDIA if one appears.
- Tavily is called at runtime from `backend/search.py`. Required for the $3,000 Tavily prize.
- Never read, print, log or commit secrets. `.env` is off-limits. If a key or env line is needed, tell the user the exact line to add.
- The repo stays public and MIT-licensed, with an accurate README. No copyrighted images, music or logos in the repo or video.

## Map
- `backend/config.py`: model IDs and env settings (only place IDs live)
- `backend/models.py`: Token Factory client, fallbacks, JSON parsing
- `backend/pipeline.py`: the agent; safety rules enforced in code
- `backend/prompts.py`, `schemas.py`, `vision.py`, `search.py`, `app.py`
- `frontend/`: static page served by FastAPI
- `tests/`: offline fakes, never spend credits
- `scripts/`: smoke test, eval, deploy
- `docs/LEAF_PLAYBOOK.md`: full reference
- `docs/PROGRESS.md`: session log
- `docs/AGENT_V2.md`: Leaf v2 investigator-agent plan, verified facts, tool schemas, milestone checkboxes (planned files: `backend/agent.py`, `critic.py`, `tools.py`, `store.py`). Read it at the start of every v2 session

## Commands
`make setup` · `make smoke` · `make smoke-all` · `make test` · `make check` · `make fmt` · `make run` · `make eval` · `make deploy` · `make help`

## How you work here
- Do the work yourself: run scripts, tests, fixes and git commits without asking. Ask only for secrets, anything that costs money beyond test calls (deploys, GPUs, paid plans) or deleting cloud resources. Never force-push.
- Start of session: read `docs/PROGRESS.md`, run `git log --oneline -5`, run `make check`.
- Loop: explore → plan (skip for one-line changes) → implement → verify → commit.
- "Done" means evidence: `make check` is green, and `make smoke` or a real request works for model changes.
- At each milestone, update `docs/PROGRESS.md` (done / next / blockers) and commit.
- After two failed attempts at the same fix, stop and propose a new approach.
- Keep chat replies short. Code, prompts, README and commit messages are written in full.

## Context budget (save tokens)
- `docs/LEAF_PLAYBOOK.md` is large. Never read it whole. Find sections with `grep -n '^## ' docs/LEAF_PLAYBOOK.md`, then Read with offset and limit.
- Send searches that span more than 3 files to the `explorer` subagent, test runs to `test-runner`, and docs lookups to `researcher`. Keep only their summaries.
- If context-mode is installed, use its `ctx_*` tools for any command or fetch with large output.
- Use `make test` (quiet), not verbose pytest. Grep logs instead of reading them whole.
- Use your installed skills, subagents and MCP tools before hand-writing something they already do.

## Project skills and subagents
- Skills: `/smoke`, `/eval-leaf`; user-invoked only: `/deploy-nebius`, `/submission-check`, `/demo-video`
- Subagents: `explorer` (haiku), `test-runner` (haiku), `researcher` (sonnet), `reviewer` (opus, run before big commits)

## Compact instructions
When compacting, keep the current task, decisions made, files changed, failing tests and open questions. Drop tool output and file dumps.
