# Numbers

Every number Leaf publishes, with the method and its limits. *Measured* means measured on real runs; *estimated* says so. Reproduce the accuracy runs with `scripts/eval.py` (see [`evals/README.md`](../evals/README.md)).

| Section | Covers |
| --- | --- |
| [Cost](#cost) | What each kind of work costs |
| [Speed](#speed-in-production) | Page and API times in a real browser |
| [Accuracy](#accuracy) | Diagnosis, identification, router, zone, Radar |
| [Which model reads the photos](#which-model-reads-the-photos) | Kimi against Nemotron VL, same photos |
| [How the verdicts were fitted](#how-the-verdicts-were-fitted) | The "Confident" thresholds and how they held up |
| [Tune-split runs](#tune-split-runs) | The 52 photos Leaf was tuned on |

## Cost

Measured on real runs, October 2026, at Token Factory per-token prices.

| Work | Measured | Notes |
| --- | --- | --- |
| A Quick diagnosis | under 1 cent, about 15 s | |
| An Auto diagnosis (investigates when unsure) | about 3.7 cents, about 31 s | 52-photo tune split |
| A full season plan, written and verified | $0.21, 70 to 110 s | 18 calls: 1 outline, 13 months, 4 Super chunks, in a real Minneapolis garden |
| A replan of the affected weeks, verified | about $0.011, 12 s | |
| Identifying a plant from a photo | about $0.003 | |
| A species profile (once per species, shared by every user) | 3 Tavily credits, plus a few cents of model calls | |
| A Radar run for one state | 4 Tavily credits at most, under $0.001 of model calls | |

| Per active user per day | Value | Basis |
| --- | --- | --- |
| **Estimated cost** | **about half a cent** (target: under one cent) | One garden of five plants, three messages a day, one diagnosis a fortnight, two replans a month, priced from the measured costs above |
| Measured average | Not available | Leaf has too few users; `/ops` says so rather than printing one |

| Spend guard | Level |
| --- | --- |
| Hard daily cap | $3 by default |
| Brownout | At 80%: optional features fall back to rules and templates |
| At the cap | Rules-only mode; care and alerts keep working |

## Speed in production

A real headless browser, after the deploy.

| Measure | Result |
| --- | --- |
| First byte | 73 to 280 ms |
| A page loaded with its data, phone width | 206 to 513 ms |
| API calls | 63 to 588 ms (Today is the slowest: it plans the garden on demand) |
| A Quick diagnosis through the API | 8 s |
| An Auto diagnosis of a high-stakes disease | 38 s |
| First load after a deploy | 8 s, while the container starts |

## Accuracy

| Test | Result |
| --- | --- |
| **Diagnosis, 24 held-out photos** (never tuned on) | Quick with Kimi K2.6 reading: right first time **50%**, in the top three **67%**; when it said Confident, right **6 of 9** times. With Nemotron 3.5 Super VL: 33% and 46% |
| Diagnosis, 52 licensed photos (tune split: tuned on, so optimistic) | With Kimi K2.6: Auto 58% first time, 73% top three, Confident right 14 of 16 at the earlier thresholds. Quick: 52% and 75% (Nemotron VL: 36% and 62%) |
| Plant identification, 40 licensed photos | 92.5% right with the vision model alone, 95% after the check against sources. Well-lit Commons photos likely seen in training, so a ceiling |
| Front-door router, 65 labelled messages (attacks, toxicity questions, real garden questions) | 0 false refusals, 0 of 6 harmful requests missed, 0 to 1 of 7 prompt injections missed, intent 97% (Lightning) |
| Hardiness zone from 30 years of ERA5-Land, five US cities | Within half a zone of the USDA map in all five, exact in none |

Radar, real runs on 15 October 2026:

| State | What happened |
| --- | --- |
| Minnesota | The pages had no current report |
| Michigan | The extension site blocks the page reader |
| Pennsylvania | The final production run kept none (a debugging run found one valid corn earworm report) |
| First production run | Stored a wrongly attributed report; the real run found it and the check was tightened |

Radar says "nothing current" rather than guess.

| Tests | |
| --- | --- |
| Offline tests | 835 with scripted fake models and search (`make test`); they never spend credits |
| Also | A type check of the Worker, lint, and a check that every frontend script parses |
| CI | Runs `make check`; while GitHub Actions billing is broken it is run locally before every push |

| Not yet verified live | Status |
| --- | --- |
| Web Push on a real phone | Built, tested offline, accepted by FCM |
| Home Assistant webhook from a real Home Assistant | Built and tested offline |
| MCP server from a real MCP client | **Verified** from Claude Code on 15 October 2026 (care plan, weather risk, diagnose) |

## Which model reads the photos

On 15 October Token Factory started listing `nvidia/Nemotron-3.5-Super-VL`. We measured it against Kimi K2.6 on the same photos and the same code, in Quick mode with search off. The diagnosis and its confidence come before the search, so scores are the same and no Tavily credits are spent.

| Photo model | Tune split, 52 photos: top-1 / top-3 | Held-out, 24 photos: top-1 / top-3 | Held-out: Confident was right | Held-out Brier |
| --- | --- | --- | --- | --- |
| **Kimi K2.6** (pinned) | **52% / 75%** | **50% / 67%** | 6 of 9 | 0.295 |
| Nemotron 3.5 Super VL (fallback) | 36% / 62% | 33% / 46% | 3 of 7 | 0.363 |

| What we found | Detail |
| --- | --- |
| The gap is the model | Kimi is about 16 points better first time on both splits, and scored about the same on both, so it is not luck or overfitting |
| Why | Nemotron's observations are shorter and miss specifics the diagnosis needs, such as "concentric rings" on early blight |
| Decision | A gap of a few points would have kept all-NVIDIA; 16 points does not. Kimi reads photos, Nemotron VL is the automatic fallback, every other step is a Nemotron model |
| Written up for NVIDIA | [`FEEDBACK.md`](FEEDBACK.md) item 10, with reproduction steps |

What we tried to close the gap, on the tune split only:

| Change for the NVIDIA model | Top-1 / top-3 | Result |
| --- | --- | --- |
| Default | 36% / 62% | Kept |
| Thinking on for the vision step | 40% / 52% | Within noise |
| Larger images (1568 px) | 33% / 58% | Within noise |
| A prompt that demands specifics | 31% / 50% | Worse |

The held-out split is 24 photos (12 diseases, 6 pests, 6 nutrient, weather or other problems). [`evals/README.md`](../evals/README.md) lists every photo, author, licence and the rules that keep it clean.

## How the verdicts were fitted

"Confident" thresholds, fitted on the tune split only:

| Mode | Photo model | Threshold | Right on the tune photos | Right on the 24 held-out photos |
| --- | --- | --- | --- | --- |
| Quick | Kimi | 0.85 with a lead of 0.3 (the strictest level right on every tune case) | 6 of 6, and 4 of 4 on a second run | **6 of 9** |
| Quick | Kimi | 0.80 with a lead of 0.15 (used until 15 October) | 11 of 12 | 6 of 9 |
| Deep | Kimi | Combined confidence 0.65 | 7 of 7 | Not run |
| Deep | Kimi | Combined confidence 0.60 (used until 15 October) | 7 of 8 | Not run |
| Quick | Nemotron VL | 0.80 or 0.85 | 2 of 4 | |
| Quick | Nemotron VL | 0.90 | 0 of 1 | |
| Deep | Nemotron VL | Could not be fitted: a Deep run on 52 photos costs about 130 Tavily credits | | |

So with Kimi, Quick says Confident only at the strict level, and **on new photos that level was still right only 6 of 9 times**: a fit that is perfect on the tuned photos over-claims on new ones. Leaf shows that rather than hiding it. When Nemotron VL read the photo (the fallback), neither mode says Confident; Leaf says Likely or Send a sample to a lab.

Auto on the held-out photos was run only with Nemotron 3.5 Super VL, before Kimi was pinned. Auto with Kimi has not been measured on this split (about 25 Tavily credits).

| Run | Photo model | Top-1 | Top-3 | When it said Confident | Brier | Cost / case |
| --- | --- | --- | --- | --- | --- | --- |
| Auto, first run | Nemotron 3.5 Super VL | 33% | 50% | right 6 of 9 | 0.183 | $0.031 |
| Auto, second run | Nemotron 3.5 Super VL | 46% (11 of 24) | 50% | right 2 of 4 | 0.282 | $0.042 |

| Read these two runs like this | |
| --- | --- |
| The second run saw the same 24 photos | Only the verdict thresholds changed between the runs. That can change which cases get investigated but not what the vision model sees, so it is a second look, not a fresh test |
| 33% to 46% is not an improvement | Earlier repeat runs of the same mode varied by several points, and one photo here is about 4 points. Read the two runs together, not the better one |
| Scoring | By name, so near-misses count as wrong ("Iron deficiency" for iron chlorosis, "Septoria" for cercospora). It was not changed after seeing results |
| Deep | Not run on this split |

Every run on the held-out split is listed in [`evals/README.md`](../evals/README.md).

## Tune-split runs

52 licensed photos (31 diseases, 15 abiotic problems, 6 pests), used to tune Leaf including the verdict thresholds, so the numbers are optimistic. One run per mode with Kimi, 13 October 2026:

| Mode | Top-1 | Top-3 | When it said Confident | Likely right | Sent to a lab | Brier | Cost / case | Time / case |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Quick | 52% | 75% | right 11 of 12 | 15 of 37 (top-3: 25) | 3 (6%) | 0.264 | $0.009 | 16 s |
| Deep | 56% | 73% | right 7 of 8 | 14 of 24 (top-3: 18) | 20 (38%) | 0.210 | $0.054 | 40 s |
| **Auto** | **58%** | 73% | **right 14 of 16** | 10 of 23 | 13 (25%) | **0.204** | $0.037 | 31 s |

| Notes on this table | |
| --- | --- |
| Live or replayed | Auto's row is live. Quick's and Deep's verdict columns replay their logged runs through the final thresholds, which were fitted on those same runs, so they flatter Quick and Deep slightly |
| Why Auto is the default | It beat Quick here (58% against 52%, better calibrated, about four times the cost). On 52 cases one case is about 2 points and repeat runs vary by several, so that is a modest lead, not a proven one |
| Where the misses are | In "Likely", which is why it comes with what to watch for |
| Late blight | Leaf's most common miss. Auto once said Confident: powdery mildew on a late blight leaf, because the vision model reported a white coating that was not there |
| Symptom checklist in the vision step | Did not help (48% with it, 52% without), so it is off |
| More | `scripts/eval.py` reruns all of this; [`ACCURACY_AUDIT.md`](ACCURACY_AUDIT.md) examines every miss |
