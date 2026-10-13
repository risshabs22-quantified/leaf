# Leaf

**Snap a sick leaf. Get a cited care plan.**

Leaf is a plant-doctor agent for home gardeners and small growers. Upload a photo of a struggling plant and Leaf ranks the likely causes with a confidence for each, asks a follow-up question when it isn't sure, and writes a short care plan whose steps cite the web sources they rely on, with university extension and government pages ranked first.

Built for the [Nebius x NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/), Best Apps and Agents track.

**Try it:** _demo URL coming soon_ · **Video:** _coming soon_

## Why

Gardeners lose plants because early symptoms look alike: a fungal leaf spot, a nutrient problem and sun scorch can all start as brown patches. Search results are generic and forums disagree. Leaf gives a calibrated answer, says when it's unsure, and points to sources and the local extension office instead of guessing.

## How it works

```text
photo ──► vision model ──► observations (symptoms, pattern, image quality)
                │
                ▼
      NVIDIA Nemotron 3 Ultra (reasoning on)
      ranked causes + confidence + follow-up questions + search queries
                │
                ▼
      Tavily search (parallel) ──► extension-service sources, trusted sites first
                │
                ▼
      NVIDIA Nemotron 3.5 Lightning (reasoning off)
      plain-language care plan with citations, check-in date, follow-up chat
```

| Step | Model (all on Nebius Token Factory) | Why this model |
| --- | --- | --- |
| Diagnose | `nvidia/Nemotron-3-Ultra-550b-a55b` | The one step that needs deep reasoning over evidence |
| Fallback | `nvidia/nemotron-3-super-120b-a12b` | Used automatically if Ultra is busy or fails |
| Write plan, chat, JSON repair | `nvidia/Nemotron-3_5-Lightning` | Fast and cheap for frequent calls |
| Read the photo | Auto-detected image model on Token Factory | Token Factory has no public NVIDIA vision model as of Oct 2026; Leaf prefers an NVIDIA one if it appears |

Design choices:
- **Routing by difficulty.** Only one expensive Ultra call per diagnosis; Lightning handles everything frequent.
- **Uncertainty is enforced in code.** If the top cause is below 60% confidence, Leaf always escalates to the local extension office, whatever the model wrote.
- **Grounded plans.** Steps that rely on a source cite it, and only sources Tavily actually returned can be cited; invented source ids are removed.
- **Resilient.** Automatic fallback across Nemotron models, JSON repair, parameter fallbacks, and a text-only mode if no vision model is available.
- **Transparent.** The "How Leaf thought" panel shows each step's model, latency and tokens.

## Nebius and NVIDIA usage

- **Nebius Token Factory**: every model call (OpenAI-compatible API, `backend/models.py`).
- **NVIDIA Nemotron 3 Ultra, Super and 3.5 Lightning**: diagnosis, fallback, care plan, chat.
- **Nebius Serverless Endpoint**: deploys the app container (`scripts/deploy_nebius.sh`).
- **Tavily**: runtime web search for extension-service guidance (`backend/search.py`).

## Run it

Requirements: Python 3.11+, a Nebius Token Factory API key, a Tavily API key.

```bash
git clone https://github.com/risshabs22-quantified/leaf.git && cd leaf
make setup          # creates .venv and .env
# add NEBIUS_API_KEY and TAVILY_API_KEY to .env
make smoke-all      # checks keys, models, regions, vision model (under 1 cent + 1 Tavily credit)
make run            # http://localhost:8000
```

Other commands: `make test` (offline tests, no credits), `make check`, `make eval`, `make docker-build`, `make deploy`. See `make help`.

## Project layout

```text
backend/    FastAPI app, Token Factory client, pipeline, prompts, schemas, search, vision
frontend/   single page (no build step), served by the backend
tests/      offline tests with fake models and search
scripts/    smoke test, eval runner, Nebius deploy
evals/      labelled photo cases
docs/       LEAF_PLAYBOOK.md (full build reference), PROGRESS.md
.claude/    Claude Code settings, subagents, skills and rules used to build Leaf
```

## Safety

Leaf gives guidance, not certainty. Its prompts tell it to send people to product labels and never to give chemical mixing ratios that aren't in a cited source. In code, any low-confidence diagnosis, any case where the top two causes are nearly tied, and any poor photo always escalates to the local extension office, whatever the model said. Source ids a model invents are removed from plans and chat replies.

The public demo protects itself and its credits: uploads are size- and pixel-limited and decoded off the event loop, request bodies are capped before they are read, every diagnosis or chat runs under a concurrency cap and an overall deadline, and per-IP and daily limits apply. Text typed by users and text from the web is sanitised before it enters a prompt and treated as data, not instructions. Errors shown to users are generic; provider details stay in the server log.

## Feedback on Nebius and NVIDIA

_To be written from our build experience before submission._

## License

[MIT](LICENSE)
