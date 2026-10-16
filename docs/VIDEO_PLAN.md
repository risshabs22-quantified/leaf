# Leaf demo video: plan

Phase 1 of the video brief. Evidence: [`VIDEO_RESEARCH.md`](VIDEO_RESEARCH.md). Numbers: [`NUMBERS.md`](NUMBERS.md) only. Nothing is built until the maintainer approves this plan.

> **v2 amendment (16 Oct 2026):** the shipped video (`~/leaf-video/out/demo.mp4`) follows §0's honest beat, NOT the aspirational narration in §§S06–S08 below (Likely verdict, Tavily sources, cited plan, spray window). The captured run went Deep → lab with search paused, so v2 shows the skip on screen with real trace numbers (vision 2.3 s, critic 3.8 s, $0.044, 0 credits). Full field-by-field audit: `~/leaf-video/REVIEW_AUDITS.md`; milestone: `PROGRESS.md`.

## 0. What is true on the live site today (checked 16 Oct 2026)

One real-browser pass on https://leafathon.site at phone width, with the MCP test garden in Minneapolis.

| Check | Result | What it means for the video |
| --- | --- | --- |
| Landing, Today, Season, Connect, Radar, Ops | All load in under 4 s, no console errors | Usable as-is |
| Today | A real urgent banner: "Frost tonight. Tonight's low is forecast at 0 °C … Cover tomato, basil before dark" | A real forecast-driven alert exists. Whatever is real on capture day is what we show |
| Deep diagnosis | Worked end to end in 45 s. Live log: first look 4 s, look closer 13 s, weather 18 s, search 19 s, second opinion 44 s | The investigation stream is real and visual |
| Search | **"Leaf has used today's web-search allowance"**: the run had no sources | **Biggest risk.** Tavily has 100 credits left this month and production allows about 5 a day. See Risk 1 |
| Verdict | Send a sample to a lab (the second opinion disagreed) | The honest beat happens naturally |
| Follow-up chat | Answered in 8 s ("Can I still eat the tomatoes?") | Usable |
| Live log | Names each step ("Looking closer at a photo") but **not the model** | Model labels must be overlays, generated from that run's own trace (the "How Leaf thought" table), never typed by hand |
| Season plan | 73 items, "Built on" list visible | Usable |

Spend for this check: one Deep run and one chat, about $0.05 of Token Factory credit, no Tavily credits (search was paused).

## 1. Creative brief

**The single story.** My plant is sick. I don't know why. Leaf works the case like a plant clinic, shows its evidence, and tells me how sure it is. Then it keeps looking after the garden.

**The memorable moment.** The ranked causes slide into a new order as evidence arrives: weather lands, sources land, then the second opinion. Leaf changing its mind on screen, for reasons you can see. If the run doesn't re-rank, the fallback moment is the second opinion disagreeing and Leaf saying "send a sample". Both are real behaviour we observed.

**Tone.** Calm, first person, plain words. No hype words, no "revolutionary". Anthropic-style restraint: show the work, state the limits, label every cut.

**Visual system.**

| Element | Rule |
| --- | --- |
| Colours | Background wash `#f3faf4`; ink `#12391f`; accent `#18793a`. Title and end cards on the logo's dark `#0a120c`. Amber `#8a5200` only for "Likely" and warnings |
| Type | Newsreader for titles, Instrument Sans for labels and captions (both Google Fonts, OFL), the same as the site |
| Logo | Leaf's own mark (`assets/logo.svg`). Sponsor names as plain text only |
| Phone | The phone-width run sits in a plain rounded rectangle in the palette. No device artwork |
| Stage labels | A chip beside the phone: model name, then the job in under 6 words ("Nemotron 3 Ultra · weighs the causes"). Generated from the run's trace |
| On-screen text | Under 10 words, high contrast, readable with sound off |
| Zoom | 1.5x to 2.2x for emphasis, held 2 to 4 s; 3.5x to 5x only for tiny UI (the "How Leaf thought" table). Ease in and out over 400 ms. Never zoom while text is being read |
| Cuts | Anything over 6 s that doesn't change the screen is cut. A corner badge "Real run · waits trimmed" stays on screen for every trimmed segment |
| Captions | Burned in, two lines max, about 42 characters a line, above the bottom 12% |
| Motion | Only what explains: labels slide in, the diagram builds in narration order. No decorative motion |

## 2. Beat sheet (target 2:45)

I kept your structure and made two changes the evidence supports. The flow gets 5 more seconds (the research puts the real flow at the centre). The garden beat follows the same tomato instead of touring features: the diagnosis is added to the garden, and the next shots show what Leaf then does with it.

| Time | Beat | What's on screen | Narration (summary) |
| --- | --- | --- | --- |
| 0:00-0:12 | Hook | 0:00 my sick plant, close-up (my footage). 0:04 cut to Leaf at phone width with that photo going in. Leaf is on screen by 0:05 | I don't know what's wrong with it |
| 0:12-0:25 | What Leaf is | The phone shows the investigation starting. Title chip: "Leaf · a plant clinic in your pocket" | One sentence: takes a history, checks weather, lists causes, says when it isn't sure |
| 0:25-1:40 | One real run | Photo and note in; Kimi reads it; Nemotron 3 Ultra builds the differential and calls tools; weather and Tavily sources arrive; the ranking moves; Nemotron 3 Super's second opinion; the verdict; the cited plan; one source card opened; a follow-up answered by Lightning. "Real run · waits trimmed" badge | Names each model as it runs. The honest beat: Likely or Send a sample, with what to watch and when to send it |
| 1:40-2:05 | The garden around it | "Add to my garden" on the same result; Today with the real forecast or NWS alert and the check-in task; a real push arriving on my phone; 3 s of the season plan; Claude Code asking Leaf over MCP | Leaf keeps going after the diagnosis |
| 2:05-2:30 | How it's built | One animated diagram: Token Factory in the middle, five models with their jobs, Tavily, weather, "decided in code". Then the eval card | Why Kimi reads photos (measured). Held-out numbers as the reason for three verdicts |
| 2:30-2:45 | Close | Dark end card: logo, leafathon.site, the GitHub URL | Who it helps. Where to try it |

## 3. Narration script (329 words; about 2:21 at 140 wpm, leaving 24 s for pauses and the end card)

Lines in [brackets] depend on what the real run does. Variants are below; read the one that matches.

**0:00 Hook (28 words)**
This is my tomato plant. Brown spots, yellow rings, climbing up from the bottom leaves. I don't know what it is, and the internet gives me six answers.

**0:12 What Leaf is (30 words)**
I built Leaf. It works like a plant clinic in your pocket: it takes a history, checks your weather, lists what it could be, and says when it isn't sure.

**0:25 The run (137 words)**
I take a photo and say what I've noticed. Kimi K2.6 reads the photo. Nemotron 3 Ultra, running on Nebius Token Factory, ranks the possible causes and starts investigating. It looks closer at the leaf, pulls two weeks of my local weather, and searches university extension guides with Tavily. Watch the ranking move as the evidence comes in. Then Nemotron 3 Super, a second model that only sees the evidence, gives its own opinion. Code, not a model, decides the verdict. [Here it says "Likely", and tells me what to watch for and when to send a sample to a lab.] Every step in the care plan cites a passage Leaf actually read, so I can open the source. And I can ask: can I still eat the tomatoes? Nemotron 3.5 Lightning answers from the same sources.

**1:40 The garden (56 words)**
I add it to my garden. Each morning, Today turns my forecast and National Weather Service alerts into tasks, like covering the basil before a frost, and sends them to my phone. It plans the season around my frost dates. And it's an MCP server, so Claude Code can ask it: what should I do today?

**2:05 How it's built (55 words)**
Each step uses the model that fits it, all through Nebius Token Factory. Kimi reads photos because on plants it measured about 16 points better than Nemotron 3.5 Super VL, which is the fallback. On 24 photos Leaf was never tuned on, it's right first time half the time. That's why it has three verdicts.

**2:30 Close (23 words)**
Leaf is for anyone with a garden and no plant doctor on call. It's live at leafathon.site, and the code is on GitHub.

**Verdict variants for the bracketed line**
- Likely: *Here it says "Likely", and tells me what to watch for and when to send a sample to a lab.*
- Send a sample: *Here the second opinion disagrees, so Leaf says send a sample to a lab, and hands me the case summary.*
- Confident: *Here it's confident. But on photos it was never tuned on, "Confident" was right six times out of nine, and Leaf shows that too.*

**Traceability** (every spoken or on-screen number):

| Claim | Source |
| --- | --- |
| "about 16 points better" | NUMBERS.md, *Which model reads the photos*: tune 52% vs 36%, held-out 50% vs 33% |
| "right first time half the time" on 24 photos | NUMBERS.md held-out row: Kimi, Quick, search off, 50% |
| "six times out of nine" | NUMBERS.md: Confident right 6 of 9 on held-out |
| "two weeks of weather" | HOW_IT_WORKS.md: `get_conditions`, 14 days |
| Cost per diagnosis on the diagram | NUMBERS.md *Cost*: Auto about 3.7 cents (52-photo tune split); Quick under 1 cent |

## 4. Teleprompter version

One phrase per line. `/` is a short pause, `//` a breath. Say model names slowly the first time.

```
This is my tomato plant. /
Brown spots, / yellow rings, / climbing up from the bottom leaves. /
I don't know what it is, / and the internet gives me six answers. //

I built Leaf. /
It works like a plant clinic in your pocket: /
it takes a history, / checks your weather, / lists what it could be, /
and says when it isn't sure. //

I take a photo / and say what I've noticed. /
KEE-mee K2.6 reads the photo. /
NEM-oh-tron 3 Ultra, / running on NEB-ee-us Token Factory, /
ranks the possible causes / and starts investigating. /
It looks closer at the leaf, / pulls two weeks of my local weather, /
and searches university extension guides with TAV-ih-lee. /
Watch the ranking move / as the evidence comes in. //
Then NEM-oh-tron 3 Super, / a second model that only sees the evidence, /
gives its own opinion. /
Code, not a model, / decides the verdict. /
[verdict line] //
Every step in the care plan / cites a passage Leaf actually read, /
so I can open the source. /
And I can ask: / can I still eat the tomatoes? /
NEM-oh-tron 3.5 Lightning answers / from the same sources. //

I add it to my garden. /
Each morning, / Today turns my forecast and National Weather Service alerts into tasks, /
like covering the basil before a frost, / and sends them to my phone. /
It plans the season around my frost dates. /
And it's an M-C-P server, / so Claude Code can ask it: / what should I do today? //

Each step uses the model that fits it, / all through NEB-ee-us Token Factory. /
KEE-mee reads photos / because on plants it measured about sixteen points better /
than NEM-oh-tron 3.5 Super V-L, / which is the fallback. /
On twenty-four photos Leaf was never tuned on, /
it's right first time half the time. /
That's why it has three verdicts. //

Leaf is for anyone with a garden / and no plant doctor on call. /
It's live at leaf-a-thon dot site, / and the code is on GitHub.
```

Pronunciation: Nemotron *NEM-oh-tron*; Nebius *NEB-ee-us*; Kimi *KEE-mee*; Tavily *TAV-ih-lee* (check against a Tavily team video before recording; if they say it differently, use theirs). "K2.6" is "K two point six".

## 5. Shot list (1920x1080, 60 fps)

Sources: **A** automated capture of the live site (Playwright), **M** motion graphic (Remotion), **F** my own footage, **T** automated terminal capture of a real Claude Code session.

| # | Time | Source | Shot | Zoom target | On-screen label |
| --- | --- | --- | --- | --- | --- |
| 1 | 0:00-0:04 | F | Sick leaf close-up, handheld, slow push | | |
| 2 | 0:04-0:08 | A | Phone view: /app, photo slot fills with my photo | Photo slot 1.6x | "Real run · waits trimmed" (corner, stays for the run) |
| 3 | 0:08-0:12 | A | Note typed in (real typing, 1x) | Notes field 1.5x | |
| 4 | 0:12-0:25 | A+M | Diagnose tapped; run starts. Title chip slides in | | "Leaf · a plant clinic in your pocket" |
| 5 | 0:25-0:33 | A+M | First look done | Log item 1.8x | Chip: "Kimi K2.6 · reads the photo" |
| 6 | 0:33-0:45 | A+M | Differential appears; look closer, weather steps | Differential 1.6x | Chip: "Nemotron 3 Ultra · weighs causes, calls tools" |
| 7 | 0:45-0:55 | A+M | Search step; ranking re-orders | Differential 1.8x, held 3 s | Chip: "Tavily · extension guides" |
| 8 | 0:55-1:03 | A+M | Second opinion log item | Log item 1.8x | Chip: "Nemotron 3 Super · second opinion" |
| 9 | 1:03-1:12 | A | Verdict and banner | Verdict 1.6x | "Decided in code" |
| 10 | 1:12-1:24 | A | Care plan; tap a [1] citation; source card | Citation 2x, then source 1.6x | Chip: "Nemotron 3.5 Lightning · writes the plan" |
| 11 | 1:24-1:34 | A | Follow-up asked and answered | Answer 1.6x | |
| 12 | 1:34-1:40 | A | "How Leaf thought" table opened | Table 3.5x | "Every model call, from this run" |
| 13 | 1:40-1:46 | A | "Add to my garden" on the result | Button 1.8x | |
| 14 | 1:46-1:54 | A | Today: alert banner and the new check-in task | Banner 1.5x | "Real forecast · [date]" |
| 15 | 1:54-1:58 | F | My phone: push notification arrives | | "Real push · test button" |
| 16 | 1:58-2:01 | A | Season plan "Built on" and first weeks | | |
| 17 | 2:01-2:05 | T | Claude Code: the question, then Leaf's answer | Answer 1.4x | "Real Claude Code session" |
| 18 | 2:05-2:22 | M | Architecture diagram builds in narration order | | Model names and jobs, "decided in code", "Auto ≈ 3.7¢ per diagnosis" |
| 19 | 2:22-2:30 | M | Eval card (Kimi vs Nemotron VL, held-out) | | "24 photos never tuned on · Quick · one run" |
| 20 | 2:30-2:45 | M | End card: logo, leafathon.site, github.com/risshabs22-quantified/leaf | | |

Eval card content (exact, from NUMBERS.md):

| Photo model | Right first time | In top three | When it said Confident |
| --- | --- | --- | --- |
| Kimi K2.6 | 50% | 67% | right 6 of 9 |
| Nemotron 3.5 Super VL (fallback) | 33% | 46% | right 3 of 7 |

Footnote on the card: "24 held-out photos · Quick mode, search off · one run each, 15 Oct 2026 · docs/NUMBERS.md".

Diagram content (from the routing table, `docs/HOW_IT_WORKS.md`): Kimi K2.6 reads photos (fallback Nemotron 3.5 Super VL); Nemotron 3 Ultra weighs causes, investigates, writes season plans; Nemotron 3 Super gives the second opinion and verifies plans; Nemotron 3.5 Lightning writes care plans and answers; Nemotron Nano 3 screens web pages. All through Nebius Token Factory. Tavily: search and extract. Open-Meteo and NWS: weather and alerts. Code: verdicts, safety, limits, spend cap.

## 6. What I film myself

Separate file: [`VIDEO_SHOTS_FOR_ME.md`](VIDEO_SHOTS_FOR_ME.md).

## 7. Risks and fallbacks

| # | Risk | Likelihood | Fallback |
| --- | --- | --- | --- |
| 1 | **Tavily allowance runs out mid-capture**, so the run shows "no fresh sources" (it happened during this check) | High | Before capture, add a one-day override (an env var for the day's search allowance, capped by the real balance) with a test, deploy, capture, then remove it and redeploy. About 25 credits for capture leaves about 75 for 17 to 31 October; the plan resets on 1 November. **Needs your OK** (it's a small code change and two deploys) |
| 2 | Cold start: the "Leaf is waking up" page | Medium | Open the site once 2 minutes before capture and wait on the platform's container state, not by polling the site |
| 3 | Slow model call | Medium | Cut the wait and keep the badge. Never speed up anything with audio |
| 4 | Wrong or odd diagnosis on my real plant | Medium | It stays in. It is the honest beat, and the narration has a variant for each verdict. If a take is unusable (an error, the fallback model, no sources), record another and say "best of N real takes" in the YouTube description |
| 5 | No re-ranking during the run | Medium | Memorable moment falls back to the second opinion and the verdict |
| 6 | No NWS alert on capture day | Medium | Show the real forecast-driven tasks instead and change the label to "Real forecast" |
| 7 | Push doesn't arrive on iPhone | Medium | iPhone needs Leaf installed to the Home Screen. Fallback: Android Chrome, then desktop Chrome. Last resort: drop the shot |
| 8 | A secret appears on screen (garden key in "Your private link", API key on Connect, bearer header in the terminal) | Medium | Capture never scrolls Today to "Your private link" and never opens a key. A frame scan of the final cut checks for `lf_` and `#k=`. Phone footage is filmed from the Home Screen app, which has no address bar. The demo garden is deleted after capture |
| 9 | A number on screen drifts from NUMBERS.md | Low | The story editor checks every number against NUMBERS.md before render, and the judges re-check |
| 10 | Over 3:00 | Low | Target 2:45, hard stop 2:55 checked with ffprobe |
| 11 | YouTube processing fails close to the deadline | Low | Upload by 28 October, two days early |

## 8. Budget

| | Plan | Cap |
| --- | --- | --- |
| Token Factory | About 6 Deep takes ($0.30), 1 season plan for the demo garden ($0.21), chats and MCP calls ($0.05): about $0.60 | $2 |
| Tavily | About 6 takes at 3 to 4 credits: about 25 | 60 |
| Spent so far | about $0.05, 0 Tavily credits | |

## 9. Phase 2 (after your OK)

Four opus subagents in parallel, as in the brief: capture engineer, motion designer, story editor, sound and packaging. Their work lives in `~/leaf-video`, outside the repo. Two changes from the brief:

- **Terminal capture.** Shot 17 needs a real terminal recording of Claude Code. I'll add VHS (MIT licence) to the capture engineer's brief, so it's recorded from a script rather than by hand.
- **Screen capture.** Playwright 1.64 is current, so the newer `page.screencast` path from the research brief is available. Its quality is still to be checked with a 10-second ffprobe test.

The first rough cut uses placeholder cards for shots 1 and 15 and caption-only narration.
