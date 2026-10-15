# Devpost submission draft

Field by field, ready to paste. Numbers come from `README.md` and `evals/`; keep them in sync.

## Project name

Leaf

## Tagline

Snap a sick leaf. Get a cited care plan, or a case ready for a plant clinic.

## Inspiration

Early plant problems look alike. A fungal leaf spot, a nutrient deficiency and sun scorch can all start as brown patches, and the advice online is generic or contradictory. Plant diagnostic clinics work differently: they take a history, look closely, check the weather, list the possible causes, test each one, and only then advise, or ask for a sample. We wanted that workflow in a phone, including the part where it says "this needs a lab".

## What it does

You upload up to three photos of a struggling plant, add a note and your town if you like, and Leaf:

- reads the photos and ranks the likely causes with a probability for each;
- checks your last 14 days of weather against published disease-risk rules (TOMCAST for tomato; Hutton, Smith and Wallin for potato);
- searches extension-service and university sources with Tavily, reads the relevant passages, and checks recent outbreak news for your region;
- gets a second opinion from a different model that sees only the evidence;
- writes a short care plan where every step cites a passage Leaf actually read, and drops steps it cannot support;
- ends with one of three verdicts decided in code: **Confident**, **Likely** (with what to watch for and when to send a sample), or **Send a sample to a lab** (with a printable case summary and your state's plant diagnostic lab).

Serious conditions such as late blight, citrus greening and sudden oak death always go to a lab, because a wrong guess there hurts more than one garden.

There are three modes. **Auto** (the default) takes a quick look first and investigates further only when it is not sure, and tells you why. **Quick** takes about 15 seconds and costs under a cent. **Deep** runs an investigator agent with tools, streamed live to the page, in about 40 seconds for about 5 cents.

Every report gets a private share link for 30 days, and you can come back with a new photo to check in: Leaf compares it with the last one and updates the advice.

## How we built it

**Models, all on Nebius Token Factory** (every call goes through one client, `backend/models.py`):

- **NVIDIA Nemotron 3 Ultra** (`nvidia/Nemotron-3-Ultra-550b-a55b`): the first differential and the Deep investigator, with reasoning on and native tool calling.
- **NVIDIA Nemotron 3 Super** (`nvidia/nemotron-3-super-120b-a12b`): the fallback for every reasoning step and the second opinion.
- **NVIDIA Nemotron 3.5 Lightning** (`nvidia/Nemotron-3_5-Lightning`): the care plan, the claim check, chat, check-in advice and JSON repair.
- **Kimi K2.6** (`moonshotai/Kimi-K2.6`) reads the photos, because Token Factory serves no NVIDIA vision model yet. Leaf picks the vision model at startup and would switch to an NVIDIA one if it appeared.

**The investigator** is Nemotron 3 Ultra calling read-only tools: `look_closer` (crops a photo region in code and asks a targeted question), `get_conditions` (Open-Meteo weather and the cited disease-risk rules), `search_sources` and `read_source` (Tavily), `check_alerts` (Tavily news), `ask_user` (up to three questions that would separate the top two causes) and `finish`. Limits are enforced in code: 8 counted tool calls, per-tool caps, a dollar budget, a hard 90-second clock with a forced finish, and a fall back to Quick if anything fails.

**Tavily** is used at runtime for search (extension and .edu/.gov sources ranked first, open-web results marked untrusted), extract (only on URLs the case's own searches returned, never a URL a model wrote) and news (recent outbreaks). A daily credit cap sized from the live monthly balance keeps a scraper from using it up; when it runs out, Leaf still answers and says it has no fresh sources.

**Decisions in code, not prompts:** the verdict, the high-stakes rule, probability caps, the final confidence (investigation, second opinion, photo quality and evidence coverage combined), and the claim check that removes plan steps no source supports.

**Hosting:** FastAPI and a static frontend in one container on Cloudflare Containers behind a small Worker, with reports in a Cloudflare R2 bucket. The container sleeps when idle; a visitor sees a "Leaf is waking up" page for a few seconds instead of a blank screen.

**Testing:** 398 offline tests with fake models and search, and an evaluation harness that scores top-1 and top-3 accuracy, each verdict, lab referrals, calibration (Brier score), cost and time per case.

## Challenges we ran into

- **Tool calling went silent** when `tools` and `response_format` were sent together: Nemotron wrote JSON instead of calling tools, with no error. We found it with a dedicated probe and now never combine them.
- **Thinking switches differ by model family.** Kimi ignored the key Nemotron uses, so it reasoned on every photo until a benchmark showed it; the right key cut the vision step from 11 s to 4 s.
- **Reasoning tokens count toward `max_tokens`**, so structured answers came back empty or cut off until we raised budgets and added a retry.
- **The Deep investigator sometimes finished without searching**, which left the care plan with nothing to cite and sent too many cases to a lab. Leaf now searches for the leading diagnosis itself.
- **Small evaluations are noisy.** Repeat runs of the same mode varied by several points, so we report every run and treat differences under about 10 points as noise.
- **Late blight is the condition Leaf misses most**: the vision model sometimes reports a white powdery coating that is not there, which points to powdery mildew.

Details and suggestions for the platform teams are in `docs/FEEDBACK.md`.

## Accomplishments that we're proud of

On 52 licensed photos (31 diseases, 15 abiotic problems, 6 pests), one run per mode. These photos were also used for tuning, so the numbers are optimistic; a held-out test set of our own photos is still being collected.

| Mode | Top-1 | Top-3 | When it said Confident | Sent to a lab | Cost per case | Time per case |
| --- | --- | --- | --- | --- | --- | --- |
| Quick | 52% | 75% | right 11 of 12 | 6% | $0.009 | 16 s |
| Deep | 56% | 73% | right 7 of 8 | 38% | $0.054 | 40 s |
| Auto | 58% | 73% | right 14 of 16 | 25% | $0.037 | 31 s |

- "Confident" means something: in Auto it was right 14 of 16 times.
- 9 of Auto's 13 lab referrals were warranted (the top answer was wrong, or the condition was late blight).
- Every care-plan step cites a passage Leaf read, and unsupported steps are removed before you see them.
- The whole case is visible: the differential re-ranking live, the weather, the sources and the second opinion.

## What we learned

- Let the model reason and let code decide. Verdicts, safety rules and limits are far more reliable as code than as instructions in a prompt.
- Measure before trusting a change: a symptom checklist that looked helpful lowered accuracy (48% vs 52%), so it is off.
- Saying "I don't know, send a sample" is a feature. Calibrated confidence mattered more to us than a higher top-1.

## What's next

- Finish the held-out test set of our own phone photos and report it without tuning on it.
- Switch the vision step to an NVIDIA vision model as soon as Token Factory serves one.
- A targeted close-up question for late blight's tell-tale signs (water-soaked margins, white growth on the underside) to fix its most common miss.

## Built with

nvidia-nemotron · nebius-token-factory · tavily · python · fastapi · pydantic · openai-sdk · open-meteo · cloudflare-workers · cloudflare-containers · cloudflare-r2 · docker · javascript · html · css

## Links

- Live: https://leaf.risshabs-22.workers.dev
- Code: https://github.com/risshabs22-quantified/leaf
- Video: _to add_
