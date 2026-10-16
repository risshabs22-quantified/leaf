# Feedback on Nebius Token Factory and NVIDIA Nemotron

What we ran into while building Leaf (October 2026), from live probes and production-style runs,
not from reading docs. Each item says what happened, how to reproduce it, what it cost us, and a
concrete suggestion. Model IDs are the ones Leaf uses: `nvidia/Nemotron-3-Ultra-550b-a55b`,
`nvidia/nemotron-3-super-120b-a12b`, `nvidia/Nemotron-3_5-Lightning`, and
`moonshotai/Kimi-K2.6` for vision (with `nvidia/Nemotron-3.5-Super-VL` as the fallback). The repro snippets use the OpenAI Python SDK against
`https://api.tokenfactory.nebius.com/v1/`.

## Summary

| # | Finding | Impact on Leaf | Suggestion |
| --- | --- | --- | --- |
| 1 | `tools` together with `response_format` silently stops tool calling | The investigator wrote JSON instead of calling tools, with no error | Return an error, or honour both |
| 2 | The thinking switch has a different key per model family | Vision step 11.2 s and $0.0083 per photo with the wrong key, 3.9 s and $0.002 with the right one | List the supported keys per model, or accept one normalised key |
| 3 | The public catalog listed Super as `error` while it answered | We nearly dropped Super; we now check `/models` at startup instead | Make catalog status reflect live health |
| 4 | No NVIDIA guard, embedding or reranker model served | Screening and source ranking done by prompt and rules | Serve one size of each |
| 5 | Reasoning tokens count toward `max_tokens` | Failed diagnoses until we raised budgets and added a retry | A separate reasoning budget and a distinct `finish_reason` |
| 6 | `tool_choice="required"` produced duplicate calls | Four identical calls for one useful one; we never use `required` | Deduplicate server-side, or document it |
| 7 | Nemotron sometimes answers in prose or invents tools mid-agent | Cases ended early or crashed without guards | Constrain tool names to the declared set |
| 8 | Run-to-run variance at the recommended sampling settings | Small evals are noisy: differences under about 10 points are treated as noise | Document seed support and safe lower temperatures |
| 9 | No cached-input pricing for agent loops | Agentic modes cost several times single-shot ones | Prefix caching with a cached-input price |
| 10 | Nemotron 3.5 Super VL reads plant photos about 16 points worse than Kimi K2.6 | Kimi reads photos; the NVIDIA model is the fallback | Plant-pathology imagery in training, richer lesion description |

## 1. `tools` together with `response_format` silently stops tool calling

**What happened.** Our investigator agent sent `tools=[...]` and, to get a clean final answer,
`response_format={"type": "json_object"}` in the same request. Nemotron 3 Ultra stopped calling
tools entirely and wrote JSON text in `content` instead. No error, no warning.

**Reproduce.**

```python
client.chat.completions.create(
    model="nvidia/Nemotron-3-Ultra-550b-a55b",
    messages=[{"role": "user", "content": "Look up the weather in Paris, then answer in JSON."}],
    tools=[{"type": "function", "function": {"name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
    response_format={"type": "json_object"},   # remove this line and tool_calls come back
)
# -> finish_reason "stop", tool_calls None, content is a JSON object
```

**Impact.** An agent that looks healthy but never uses its tools. It took a dedicated probe to
find; in a larger system it would show up only as worse answers.

**Suggestion.** Reject the combination with a 400 that names both parameters, or document it on
the tool-calling page. The fix on our side is simple once known: never combine them, and make the
final answer a `finish` tool.

## 2. The "thinking" switch has a different key per model family

**What happened.** `chat_template_kwargs` is honoured, but Nemotron reads `enable_thinking` while
Kimi-K2.6 reads `thinking`. Our first vision step sent `enable_thinking: false` to Kimi; it was
silently ignored and Kimi kept reasoning on every photo until a benchmark showed it.

**Reproduce.** Send the same image request to `moonshotai/Kimi-K2.6` with
`extra_body={"chat_template_kwargs": {"enable_thinking": False}}`, then with
`{"thinking": False}`, and compare `reasoning_content` and `usage.completion_tokens`. Only the
second turns reasoning off.

**Impact.** Measured on our vision step: 11.2 s and $0.0083 per photo with the wrong key, 3.9 s
and $0.002 with the right one, with equally specific observations. Our whole Quick diagnosis went
from 23-28 s to 11-13 s.

**Suggestion.** List the supported `chat_template_kwargs` per model in `/models?verbose=true` (or
on each model card), or accept one normalised key such as `reasoning: {"enabled": false}` and map
it per template. An unknown template key could at least produce a warning header.

## 3. The public catalog listed Super as "error" while it answered every request

**What happened.** The public model catalog showed `nvidia/nemotron-3-super-120b-a12b` with
`status: "error"` during our build. Every probe and every eval call to it succeeded.

**Reproduce.** Compare the catalog entry's status with a direct call to the model at the same
time (we saw this on 12 October 2026, while our probes to Super succeeded).

**Impact.** We nearly dropped Super as the fallback for every reasoning step and as the second
opinion. We now ignore the catalog and check `/models` at startup instead, and show the result in
`/api/health`.

**Suggestion.** Make catalog status reflect live health (or show the time it was last checked),
and expose the same status in `/models?verbose=true` so apps can route around a model that is
really down.

## 4. No NVIDIA vision, guard, embedding or reranker model is served

**What happened.** For a plant-doctor app the first step is reading a photo. Token Factory serves
no NVIDIA vision-language model, so the one non-NVIDIA model in Leaf was the vision step (Kimi). On 15 October `nvidia/Nemotron-3.5-Super-VL` appeared, which is good news; item 10 shows how it compares on plant photos.
There is also no NVIDIA guard/safety model (Nemotron 3.5 Content Safety exists on Hugging Face but
is not served), no NVIDIA embedding model (the only embedding model listed is
`Qwen/Qwen3-Embedding-8B`), and no reranker.

**Reproduce.** `GET /v1/models?verbose=true` and filter for `nvidia/` models with image input,
or with guard, embedding or rerank in the name or task.

**Impact.** Leaf cannot run end to end on NVIDIA models, which is the story the hackathon asks
for. Without a reranker we rank web sources with simple rules (extension and .edu/.gov domains
first, then Tavily's score); without a guard model, input screening is done by prompt and code.

**Suggestion.** Serve one NVIDIA VLM (a Nemotron Nano VL class model would do for leaf photos),
plus the Nemotron safety, embedding and reranking models. Even one size each would let teams build
fully NVIDIA pipelines.

## 5. Reasoning tokens count toward `max_tokens`, so JSON comes back empty or cut off

**What happened.** With reasoning on, Ultra spends much of `max_tokens` on `reasoning_content`.
With a budget that looks generous for the answer alone, we got empty `content` or JSON cut off
mid-object, with `finish_reason: "length"`.

**Reproduce.** Ask Ultra with reasoning on for a structured differential diagnosis as JSON with a
small `max_tokens` on a hard case; some replies end with `reasoning_content` and no answer.

**Impact.** Failed diagnoses until we raised budgets (6,000 for Ultra) and added a retry with a
doubled budget whose tokens we count toward cost.

**Suggestion.** A separate `max_reasoning_tokens` (or a reasoning budget in `chat_template_kwargs`)
so the answer always has room, and a distinct `finish_reason` when reasoning used the whole budget.

## 6. `tool_choice="required"` produced duplicate calls

**What happened.** In a probe, `tool_choice="required"` made Ultra emit four identical tool calls
in one turn. `tool_choice="auto"` emitted one or two distinct parallel calls, which is what we
wanted.

**Reproduce.** One tool, a prompt that needs it once, `tool_choice="required"`; count
`message.tool_calls`.

**Impact.** Four times the tool cost for one useful call. We never use `required`; to force a
final answer we name the `finish` tool, and fall back to `auto` with only `finish` offered.

**Suggestion.** Deduplicate identical calls server-side under `required`, or document the
behaviour.

## 7. Nemotron sometimes answers in prose, or calls tools that do not exist, mid-agent

**What happened.** In live investigator runs, Ultra occasionally replied with prose instead of a
tool call, or called a plausible but undefined tool name (for example one to open a page before we
had a `read_source` tool).

**Reproduce.** A multi-turn tool loop with several tools and a long case history; across a batch
of cases, some turns end with prose or an unknown function name.

**Impact.** Without guards, cases ended early or crashed. We answer an invented name with the real
tool list, nudge once after prose, then force `finish`; these are tested in `tests/test_agent.py`.

**Suggestion.** Constrained decoding of tool names to the declared set when `tools` is present,
and guidance in the Nemotron agent docs on recommended loop guards.

## 8. Run-to-run variance at the recommended sampling settings

**What happened.** Nemotron 3.x is documented for temperature 1.0 and top_p 0.95. Repeating the
same 22-photo Deep evaluation gave top-1 accuracy from 36% to 55% across five runs.

**Reproduce.** Run the same eval twice (`scripts/eval.py --mode deep`) and compare.

**Impact.** Small evaluations cannot distinguish real improvements from noise; we had to report
every run and treat differences under about 10 points as noise.

**Suggestion.** Document whether a `seed` is honoured and which lower temperatures are safe for
evaluation without degrading reasoning, so teams can measure changes reliably.

## 9. No cached-input pricing for agent loops

**What happened.** The catalog lists no cached-input discount. An agent loop re-sends the whole
growing conversation every turn, so input tokens dominate Deep's cost (about $0.054 per case).

**Impact.** Agentic modes cost several times more than single-shot ones (Leaf's Quick is $0.009).

**Suggestion.** Prefix caching with a cached-input price, as other providers offer, would cut
agent costs substantially.

## 10. Nemotron 3.5 Super VL reads plant-disease photos about 16 points worse than Kimi K2.6

**What happened.** On 15 October `nvidia/Nemotron-3.5-Super-VL` started showing up in
`/v1/models`. We wanted an all-NVIDIA pipeline, so we measured it against `moonshotai/Kimi-K2.6`
as the photo-reading step of Leaf, with everything else identical (same code, same Ultra
diagnosis step, same prompts, Quick mode, search off). Leaf's vision step turns a photo into a
structured description (symptoms, affected parts, pattern, image quality) and Nemotron 3 Ultra
diagnoses from that description alone, so the score measures how well the vision model perceives
the diagnostic cues.

| Photo model | 52 tune photos: top-1 / top-3 | 24 held-out photos: top-1 / top-3 | Held-out Brier | Vision call time |
| --- | --- | --- | --- | --- |
| Kimi K2.6 | 52% / 75% | 50% / 67% | 0.295 | about 4 s (thinking off) |
| Nemotron 3.5 Super VL | 36% / 62% | 33% / 46% | 0.363 | about 1.5 s |

Kimi scored about the same on both splits, so the gap is the model, not luck or overfitting. The
photos are 76 licensed Wikimedia Commons images (CC0, public domain, CC BY, CC BY-SA) of tomato,
potato, rose, squash, pepper and other crops; the held-out set has 12 diseases, 6 pests and 6
nutrient or weather problems, labelled from the uploader's own file title or description. They
are real-world photos, not a clean leaf-on-white-background dataset.

**What we saw.** Nemotron VL's descriptions are shorter (about 720 characters of JSON against
about 990 on average) and drop the cues a plant pathologist keys on. On a tomato early-blight leaf
Kimi wrote "dark brown to black circular lesions with concentric rings (target-like pattern)" and
"yellow chlorotic halos"; Nemotron wrote "dark brown to black circular spots" with halos but no
rings. On another leaf it reported fine webbing that Kimi did not, which pushed spider mites up
the diagnosis.

**What we tried, on the tune split only.** Thinking on for the vision call (40% top-1, 52%
top-3), 1568 px images instead of 1024 px (33%, 58%), and a prompt that demands 5 to 8 specific
symptoms with colour, shape, rings, halo, margin, leaf age and surface (31%, 50%). None beat the
default (36%, 62%) beyond the run-to-run noise on 52 photos (a few points). We did not try
few-shot photos or a different resize filter.

**Reproduce.** Clone https://github.com/risshabs22-quantified/leaf, run `make setup`, put a Token
Factory key in the env file the README describes, then:

```
LEAF_VISION_MODEL=moonshotai/Kimi-K2.6 \
  .venv/bin/python -m scripts.eval --split tune --mode quick --no-search --label kimi
LEAF_VISION_MODEL=nvidia/Nemotron-3.5-Super-VL \
  .venv/bin/python -m scripts.eval --split tune --mode quick --no-search --label nvl
# the same with --split test for the 24 held-out photos
```

Each run is about 50 model calls and costs under $0.50. `evals/README.md` lists every photo with
its author and licence, and the harness writes per-case observations and rankings to
`evals/results.jsonl`.

**Impact.** Leaf keeps Kimi for photo reading and uses Nemotron 3.5 Super VL only as the
automatic fallback, so a Kimi outage does not take photos offline; every other step is a
Nemotron model. We also switch "Confident" off whenever the NVIDIA model read the photo: on the
tune split no threshold made its Confident right 85% of the time.

**Suggestion.** This looks like a domain gap, not a general vision gap: fine, texture-level cues
(rings, halos, water-soaked margins, powdery versus downy growth, insect frass). Training or
fine-tuning on extension-service and plant-pathology imagery, or a "describe every visible lesion
feature" mode with longer structured output, would likely close it. We will re-run this
comparison against any newer Nemotron VL release; the harness takes one environment variable.

## What worked well

- Native tool calling on Ultra and Super through the OpenAI-compatible API, 1-2 s per turn, with
  parallel calls and streamed tool-call deltas.
- Reasoning returned separately in `reasoning_content`, never mixed into `content`.
- `/models?verbose=true` includes per-token prices, which let Leaf show the cost of every case
  live and enforce a per-case dollar budget in code.
- Lightning is fast and cheap enough (about $0.0002 per plan) to run a claim check on every care
  plan.
