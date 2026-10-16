# Devpost form answers

Short answers for the submission form. Numbers match `docs/NUMBERS.md`.

## Elevator pitch (200 characters)

Leaf plans your garden's day and year from your own weather, and works out what's wrong with a sick plant from a photo, with sources. When it isn't sure, it says so and sends you to a lab.

## Which model(s) did you use, and why that size/variant?

Four Nemotron sizes, each for the job it fits. Nemotron 3 Ultra (550B) diagnoses, runs the investigator agent and writes the 52-week season plan, because those need real reasoning. Nemotron 3 Super (120B) verifies plans, gives the second opinion and backs Ultra up. Nemotron 3.5 Lightning does the fast, cheap work: the front door, care plans, claim checks. Nemotron Nano 3 screens web pages for hidden instructions. Photos are read by Kimi K2.6, with Nemotron 3.5 Super VL as the fallback (see the comparison answer).

## Did you fine-tune, prompt-engineer, or use Nemotron out of the box? What was your approach?

Out of the box plus prompt engineering. No fine-tuning. Each step has a prompt that names the exact JSON shape, and code checks everything the model returns: the verdict, safety rules and limits are code, not prompt. What actually moved results was settings, not wording: the thinking switch (Nemotron reads enable_thinking, Kimi reads thinking), generous max_tokens because reasoning counts against it, and a worked example in Lightning's Radar prompt, which returned an empty list for an obvious sighting until we added one.

## How did Nemotron's performance compare to other models you've used for similar tasks?

For reasoning and writing it held up well, and we found no reason to swap it out. Ultra's tool calling was good (1 to 2 seconds a turn). Nano was slower than Lightning at the same price (1.6 s against 0.4 s), so nothing a user waits for uses it. Vision is where it lost: on plant photos, Nemotron 3.5 Super VL got 33% right first time on 24 held-out photos and 36% on 52 tuning photos. Kimi K2.6 got 50% and 52% on the same photos with the same code. We wanted an all-NVIDIA pipeline, but 16 points is too many, so Kimi reads photos and Nemotron VL is the fallback. We wrote it up for NVIDIA with repro steps (docs/FEEDBACK.md).

## How would you rate Nemotron's output quality for your use case? (1 to 10)

8. Good: reasoning, following a JSON schema, tool calling, and staying inside the evidence it was given. Fell short: plant-disease vision (above), run-to-run variance at temperature 1.0 (we treat differences under about 10 points as noise), and the occasional prose answer or invented tool name mid-agent, which code guards against. Multilingual: not tested.

## Which Nebius platform capabilities were most valuable to your project and how?

Token Factory through its OpenAI-compatible API. Every model call goes there: native tool calling for the investigator, one endpoint for all four Nemotron sizes plus Kimi, and per-token prices in `/models?verbose=true`, which let us meter every call and enforce a $3 daily spend cap in code. A full diagnosis costs about 4 cents. We used no GPU instances or Nebius hosting; the app runs on Cloudflare Containers and calls Token Factory over the API.

## How likely are you to recommend running Nemotron on Nebius to other developers? (1 to 10)

8. One API key got us four Nemotron sizes with working tool calling, and the costs are low enough that a hackathon budget goes a long way. I'd hold it back from a 9 or 10 for the missing balance API, the catalog that showed Super as "error" while it answered every request, and no cached-input pricing for agent loops.

## How would you rate your experience running Nemotron inference on Nebius compared to previous cloud or local environments? (1 to 10)

8. No servers, no drivers, no GPU quota to chase. Compared with running models ourselves it saved days. Compared with other hosted APIs the gaps are small but real: no way to read the credit balance by API, no cached-input discount, and thinking switches that differ per model family.

## What additional features or improvements would have made the Nemotron on Nebius experience more effective?

1. A balance and usage API. 2. Catalog status that reflects live health. 3. Cached-input pricing for agent loops. 4. A separate reasoning-token budget, so the answer always has room. 5. Tool names constrained to the declared set. 6. One documented thinking switch across models. 7. A seed that is honoured, for reproducible evals.

## What do you most hope to see from the Nemotron team next?

A vision-language model that can read plant disease. Ours missed fine cues like concentric rings and halos, and that single step cost 16 points. After that: NVIDIA guard and reranker models served on Token Factory, steadier tool calling, and the same Ultra-level reasoning at a smaller size.

## Did you use Tavily in your project?

Yes, at runtime from `backend/search.py`. Search and extract find extension-service guides for each diagnosis, news checks recent outbreaks nearby, and Leaf Radar reads state extension sites weekly. Every care step cites a passage Leaf actually read. Credits are rationed by code from the live monthly balance so diagnoses keep working all month.

## Which AI tools have you leveraged while working on this project?

Claude Code did most of the building: code, tests (835 offline), and adversarial review passes by separate subagents before each milestone. At runtime: Nebius Token Factory (Nemotron 3 Ultra, Super, 3.5 Lightning, Nano 3, Nemotron 3.5 Super VL, Kimi K2.6) and Tavily. Playwright drove real-browser checks of the live site.

## Built with (tags)

Python, FastAPI, JavaScript, HTML, CSS, SQLite, OpenAPI, Nebius Token Factory, NVIDIA Nemotron 3 Ultra, NVIDIA Nemotron 3 Super, NVIDIA Nemotron 3.5 Lightning, NVIDIA Nemotron Nano 3, Kimi K2.6, Tavily, Cloudflare Workers, Cloudflare Containers, Cloudflare D1, Cloudflare R2, Cloudflare Queues, Open-Meteo, National Weather Service API, Model Context Protocol, Web Push, Claude Code

## About the project

## Inspiration

Early plant problems look alike. A fungal spot, a nutrient shortage and sun scorch can all start as brown patches, and the advice online contradicts itself. A plant clinic works differently: it takes a history, checks the weather, lists causes, and sometimes says "send us a sample". We wanted that in a phone, including the part where it admits it doesn't know.

## What it does

Leaf is a garden platform at leafathon.site, with no account.

- **Today:** each morning, what to water, protect or check, from your forecast and NWS alerts. Every task has a reason and a source.
- **Season plan:** 52 weeks of sowing, planting and protection around your frost dates. Code removes anything unsafe, and Nemotron Super checks the rest.
- **Doctor:** send a photo. You get ranked causes, a care plan where each step cites a passage Leaf read, and one of three verdicts: Confident, Likely, or Send a sample to a lab. Code decides the verdict, not the model.
- **Radar** (what extension services report in your state), an **MCP server and REST API**, and Home Assistant soil readings.

## How we built it

Python and FastAPI in one Cloudflare container, behind a Worker that holds D1, KV, a queue and cron. Every model call goes to Nebius Token Factory. Ultra diagnoses and writes, Super verifies, Lightning handles the fast steps, Nano screens web text, Kimi K2.6 reads photos. Tavily supplies the sources. All safety rules, thresholds and limits are code, with 835 offline tests.

## Challenges

- Reasoning tokens count toward max_tokens, so JSON came back empty until budgets went up.
- Combining `tools` with `response_format` made Ultra silently stop calling tools.
- Nemotron 3.5 Super VL read plant photos about 16 points worse than Kimi: 33% against 50% right first time on 24 held-out photos. We kept Kimi for photos and made Nemotron the fallback.
- "Confident" over-claimed. Set at the strictest level that was right on every tuning photo, it was still right only 6 of 9 times on new ones. We publish that number.

## Accomplishments

It runs live on a real forecast, and on the day we tested it caught a real NWS freeze watch. We checked it in real browsers at phone width, and Claude Code talks to it over MCP. A diagnosis costs about 4 cents and a season plan 21 cents. We built a held-out photo set under written leak rules and ran the evals once.

## What we learned

Settings mattered more than prompt wording. Small evals are noisy. Saying "I'm not sure" is a feature, and it's the one we measured hardest.

## What's next

Re-measure NVIDIA's vision model when a new one ships, run Auto with Kimi on the held-out photos, and test push on more real phones.
