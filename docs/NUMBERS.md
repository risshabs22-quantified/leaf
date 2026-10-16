# Numbers

Every number Leaf publishes, with the method and its limits. Measured means measured on real runs; estimated says so. Reproduce the accuracy runs with `scripts/eval.py` (see `evals/README.md`).

## Cost, speed and accuracy

**What the models cost** (measured on real runs, Oct 2026; Token Factory per-token prices):

| Work | Measured | Notes |
| --- | --- | --- |
| A Quick diagnosis | under 1 cent, about 15 s | |
| An Auto diagnosis (investigates when unsure) | about 3.7 cents, about 31 s | 52-photo tune split |
| A full season plan, written and verified | $0.21, 70 to 110 s | 18 calls: 1 outline, 13 months, 4 Super chunks, in a real Minneapolis garden |
| A replan of the affected weeks, verified | about $0.011, 12 s | |
| Identifying a plant from a photo | about $0.003 | |
| A species profile (once per species, shared by every user) | 3 Tavily credits, plus a few cents of model calls | |
| A Radar run for one state | 4 Tavily credits at most, under $0.001 of model calls | |

**Estimated cost per active user per day: about half a cent** (target: under one cent). That is an estimate from the measured per-call costs above, for a user with one garden of five plants, three messages a day, one diagnosis a fortnight and two replans a month. It is not measured: Leaf has too few users for a real average, and `/ops` says so rather than printing one. The platform has a hard daily spend cap ($3 by default), a brownout at 80% (optional features fall back to rules and templates), and rules-only mode at the cap, which keeps care and alerts working.

**Accuracy:**

| Test | Result |
| --- | --- |
| **Diagnosis, 24 held-out photos** (never tuned on) | Quick with Kimi K2.6 reading the photos: right first time **50%**, in the top three **67%**; when it said Confident it was right **6 of 9** times. With Nemotron 3.5 Super VL: 33% and 46%. Details and the comparison below |
| Diagnosis, 52 licensed photos (tune split: tuned on, so optimistic) | With Kimi K2.6: Auto right first time 58%, top three 73%, Confident right 14 of 16 at the earlier thresholds. Quick: 52% and 75% (Nemotron VL: 36% and 62%) |
| Plant identification, 40 licensed photos | 92.5% right with the vision model alone, 95% after the check against sources. Well-lit, well-framed Commons photos likely seen in training, so a ceiling rather than what a phone photo gets |
| Front-door router, 65 labelled messages (attacks, toxicity questions, real garden questions) | 0 false refusals, 0 of 6 harmful requests missed, 0 to 1 of 7 prompt injections missed, intent 97% (Lightning) |
| Hardiness zone from 30 years of ERA5-Land, five US cities | Within half a zone of the USDA map in all five, exact in none |
| Radar, real runs on Oct 15 2026 | Most states have little current: Minnesota's pages had no current report, Michigan's site blocks the page reader, and Pennsylvania's final production run kept none (a debugging run found one valid corn earworm report). A first production run stored a wrongly attributed report; the real run found it and the check was tightened. Radar says "nothing current" rather than guess |

**Speed in production** (a real headless browser, after the deploy): the first byte in 73 to 280 ms, each page loaded with its data in 206 to 513 ms at phone width, API calls 63 to 588 ms (Today is the slowest because it plans the garden on demand); a Quick diagnosis through the API took 8 s, an Auto diagnosis of a high-stakes disease 38 s; the first load after a deploy took 8 s while the container started.

**Tests:** 820+ offline tests with scripted fake models and search (`make test`; they never spend credits), a type check of the Worker, lint. CI runs `make check`; while GitHub Actions billing is broken it is run locally before every push.

**Not yet verified live:** the MCP server from a real MCP client (it is tested with the raw JSON-RPC calls such a client sends, in both protocol versions), the Home Assistant webhook from a real Home Assistant, and a Web Push notification on a real phone. Each is built, tested offline and, where it could be, checked against the real services.

## Which model reads the photos, and how the verdicts were fitted

**Which model reads the photos: a measured decision.** On Oct 15 Token Factory started listing `nvidia/Nemotron-3.5-Super-VL`, an NVIDIA vision model, and Leaf's all-NVIDIA pipeline was tempting. We measured it against Kimi K2.6 on the same photos, same code, Quick mode with search off (the diagnosis and its confidence come before the search, so scores are the same and no Tavily credits are spent):

| Photo model | Tune split, 52 photos: top-1 / top-3 | Held-out, 24 photos: top-1 / top-3 | Held-out: Confident was right | Held-out Brier |
| --- | --- | --- | --- | --- |
| **Kimi K2.6** (pinned) | **52% / 75%** | **50% / 67%** | 6 of 9 | 0.295 |
| Nemotron 3.5 Super VL (fallback) | 36% / 62% | 33% / 46% | 3 of 7 | 0.363 |

The held-out split is 24 photos (12 diseases, 6 pests, 6 nutrient, weather or other problems; `evals/README.md` lists every photo, author, licence and the rules that keep it clean). Kimi is about 16 points better first time on both splits, so the gap is the model, not luck or overfitting (Kimi scored about the same on the tune and held-out photos). Nemotron's observations are shorter and miss the specifics the diagnosis needs, for example "concentric rings" on early blight. On the tune split only we tried to close the gap with thinking on for the vision step (40% top-1, 52% top-3), larger images at 1568 px (33%, 58%) and a prompt that demands specifics (31%, 50%); none beat the default (36%, 62%) beyond noise. A gap of a few points would have kept all-NVIDIA; 16 points does not, so Kimi does perception, Nemotron VL is the automatic fallback, and every other step is a Nemotron model. The result is also written up as feedback for NVIDIA in `docs/FEEDBACK.md`, with the steps to reproduce it.

**"Confident", fitted on the tune split only.** With Kimi, Quick says Confident only at 0.85 with a lead of 0.3, the strictest level where it was right on every tune case (6 of 6, and 4 of 4 on a second tune run); Deep says it at a combined 0.65 (7 of 7 on tune). The earlier looser levels were 11 of 12 and 7 of 8. **On the held-out photos the strict Quick level was still right only 6 of 9 times**, so even a fit that is perfect on the tune photos over-claims on new ones. We show that rather than hide it. When Nemotron VL is the one that read the photo (the fallback), no threshold reached 85% on the tune split (2 of 4 at 0.8 or 0.85, 0 of 1 at 0.9), and Deep with it could not be fitted inside the credit budget, so neither mode says Confident then: Leaf says Likely or Send a sample to a lab.

**Auto on the held-out photos** was run only with Nemotron 3.5 Super VL, before Kimi was pinned; Auto with Kimi has not been measured on this split (it costs about 25 Tavily credits).

| Run | Photo model | Top-1 | Top-3 | When it said Confident | Brier | Cost / case |
| --- | --- | --- | --- | --- | --- | --- |
| Auto, first run | Nemotron 3.5 Super VL | 33% | 50% | right 6 of 9 | 0.183 | $0.031 |
| Auto, second run | Nemotron 3.5 Super VL | 46% (11 of 24) | 50% | right 2 of 4 | 0.282 | $0.042 |

**The second Auto run saw the same 24 photos as the first.** Between the two runs the only change was to the verdict thresholds, which can change which cases get investigated but not what the vision model sees, so it is a second look at the same photos, not a fresh test. Earlier in the project repeat runs of the same mode varied by several points, and one photo here is about 4 points, so the gain from 33% to 46% should not be read as an improvement. Read the two runs together, not the better one. Deep was not run on this split. Answers are scored by name, so some near-misses count as wrong ("Iron deficiency" for iron chlorosis, "Septoria" for cercospora); the scoring was not changed after seeing results. Every run on the held-out split is listed in `evals/README.md`.

**Tune split, 52 licensed photos** (31 diseases, 15 abiotic problems, 6 pests). These were used to tune Leaf, including the verdict thresholds, so the numbers are optimistic. One run per mode, 13 October 2026:

| Mode | Top-1 | Top-3 | When it said Confident | Likely right | Sent to a lab | Brier | Cost / case | Time / case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Quick | 52% | 75% | right 11 of 12 | 15 of 37 (top-3: 25) | 3 (6%) | 0.264 | $0.009 | 16 s |
| Deep | 56% | 73% | right 7 of 8 | 14 of 24 (top-3: 18) | 20 (38%) | 0.210 | $0.054 | 40 s |
| **Auto** | **58%** | 73% | **right 14 of 16** | 10 of 23 | 13 (25%) | **0.204** | $0.037 | 31 s |

Auto's row is live; Quick's and Deep's verdict columns replay their logged runs through the final thresholds, which were fitted on those same runs, so they flatter Quick and Deep slightly. Read plainly, on the tune split: Auto is the default because it beat Quick there (58% against 52%, better calibrated, about four times the cost), but on 52 cases one case is about 2 points and repeat runs vary by several, so that is a modest lead, not a proven one. "Confident" means something (14 of 16 in Auto). "Likely" is where the misses are, which is why it comes with what to watch for. Late blight is Leaf's most common miss: Auto once said Confident: powdery mildew on a late blight leaf, because the vision model reported a white coating that was not there. A structured symptom checklist in the vision step did not help (48% with it, 52% without), so it is off. `scripts/eval.py` reruns all of this; `docs/ACCURACY_AUDIT.md` examines every miss.
