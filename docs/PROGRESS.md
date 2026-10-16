# Leaf progress log

Claude Code reads this at the start of every session and updates it at each milestone.
Keep it short: newest entries first.

## Status (update every milestone)

| Feature | Status | Evidence |
| --- | --- | --- |
| Backend pipeline (vision → Ultra → Tavily → Lightning) | done (offline) | `make check` green (all offline tests) |
| Frontend single page | done (minimal one-column redesign, checked live at 1440 and 375 px) | served at `/`, live diagnose renders |
| Backend hardening | done | two audits, 85 offline tests, each fix mutation-checked; live: bomb 400, oversize 413, injection, photo 5/5 |
| Photo path live | done | synthetic leaf through vision, Ultra, Lightning; chat follow-up works |
| Real Token Factory calls | done | `make smoke-all`: Ultra 627 ms, Super fallback 788 ms, Lightning 477 ms |
| Vision model | done | Kimi K2.6 pinned for photos (`LEAF_VISION_MODEL`), Nemotron 3.5 Super VL the automatic fallback; Kimi measured about 16 points better first time (README Numbers, `docs/FEEDBACK.md` item 10) |
| Tavily live | done | `make smoke-all` OK; live `/api/diagnose` returned 6 sources |
| Eval set | tune split (52 photos) and held-out test split (24 photos, leak rules in `evals/README.md`) done; held-out Quick and Auto run once | `evals/summary_test_heldout-*.json` |
| Three verdicts and Auto mode (Auto is the default) | done | README "Quick, Deep and Auto (measured)" |
| Deployed on Cloudflare Containers (every model call goes to Nebius Token Factory) | done | https://leafathon.site; live: health OK, Quick, Auto, lab handoff, share link from R2 |
| Landing page, organic and pet-safe plans, spray windows, My plants | done | `/`, `/app`, `/plants`; 422 tests |
| Platform M1: data layer (D1 via Worker bridge), routing table, Nano front door, job queue, meter and spend guard | done | https://leafathon.site on D1; 530 tests; production check below |
| Platform M2: climate engine (forecast per cell, 1991-2020 climatology, zone, frost dates, NWS, FAO-56 water balance, indoor model, GDD, sowing) | done | `docs/LEAF_PLATFORM.md` M2; 610 tests; five-city zone check |
| Platform M3: knowledge graph (cited species profiles, verified facts), identify from a photo with one-tap confirm, plant-ID eval | done | `docs/LEAF_PLATFORM.md` M3; 663 tests; 40-photo eval |
| Platform M4: Leaf Care (morning planner, Today, notifications service, Web Push PWA, 14:00 frost/heat check, NWS alerts, Nano triage) | done | `docs/LEAF_PLATFORM.md` M4; 703 tests; push accepted by FCM |
| Platform M5: Season Plan (Ultra outline + monthly chunks, code rules, Super verification, replanning, calendar export, printable page, Doctor wiring) | done | `docs/LEAF_PLATFORM.md` M5; 731 tests; live plan $0.21 |
| Platform M6: Connect (REST API v1 and OpenAPI, API keys with scopes and per-key limits, MCP server, Home Assistant readings) | built and checked locally; a Telegram bot was built and then cancelled on 2026-10-15 (code removed) | `docs/LEAF_PLATFORM.md` M6; 765 tests |
| Platform M7: Leaf Radar, `/ops`, landing page and README as a platform | done | `docs/LEAF_PLATFORM.md` M7; 822 tests; real Tavily and Nemotron runs; production check |
| README final, video, Devpost draft | Devpost drafted (`docs/DEVPOST.md`); video todo | `/submission-check` |

## Next
0. **Test split:** done on Oct 15 (24 Commons photos, run once). Do not tune on it. The user's own phone photos can be added later as a second held-out set.
1. **Hosting:** keep the Worker up until judging ends (Dec 15), then `npx --prefix cloudflare wrangler delete leaf`. Redeploy with `make deploy-cloudflare`; it ships the secrets with the version.
2. Late blight is the most-missed condition (the vision model reports a powdery coating on its lesions); a targeted `look_closer` question for sporulation or water-soaked margins is the next accuracy lead.

## Release-candidate checklist (Oct 15)
- [x] `make check` green (829 tests, lint, Worker `tsc`)
- [x] Held-out split built under written leak rules; Quick and Auto run once; README, landing and Devpost lead with the held-out numbers
- [x] Whole-platform review run; blockers and should-fix items fixed or listed above
- [x] Judge simulation at 375 and 1280 px on production; top issues fixed
- [x] Temporary files removed (`/tmp/leaf-m7`, `/tmp/leaf-m6`, `/tmp/leafwork`, `/tmp/leaf-judge`, `.playwright-mcp/`)
- [x] Pushed and deployed (`make deploy-cloudflare`), then a real-browser pass on production at 375 px: 21 steps, no console errors or failed requests (it caught a `today.js` syntax error that my first deploy shipped; fixed, and `tests/test_hardening.py` now parses every frontend script)
- [ ] Real-device checks by the maintainer: push on a phone, Claude Code MCP (steps in the final report of this session)
- [ ] Freeze Oct 22; submission by Oct 30, 10:00 AM PT; demo video; Devpost

## Blockers
- None yet.

## Eval log
| Date | Cases | Top-1 | Top-3 | Escalated | Change |
| --- | --- | --- | --- | --- | --- |
| 2026-10-13 | 52 tune | Quick 52% / Deep 56% / Auto 58% | 75% / 73% / 73% | lab 6% / 38% / 25% | three verdicts, Auto, fitted thresholds; checklist off (48% with it) |
| 2026-10-15 | 24 held-out test | Quick 33% / Auto 33% | 46% / 50% | lab 4% / 42% | first run on the held-out split; vision model was Nemotron 3.5 Super VL (tune runs: Kimi). Auto Confident right 6 of 9, Quick 3 of 7 |
| 2026-10-15 | 52 tune, Quick, search off | Nemotron VL 36% (Kimi 52%) | 62% (Kimi 75%) | lab 4% | vision model swap; experiments: thinking on 40/52, 1568 px 33/58, detailed prompt 31/50, none better than default beyond noise |
| 2026-10-15 | 24 held-out, Quick, search off | Kimi control 50% (Nemotron VL 33%) | 67% (46%) | lab 17% (4%) | control for the vision model: same photos |
| 2026-10-15 | 24 held-out, Auto, second run (same photos) | 46% (first run 33%) | 50% (50%) | lab 33% (42%) | Quick and Deep never say Confident with the NVIDIA vision model; Confident right 2 of 4 before that rule |

## Session log
- 2026-10-15: **Kimi pinned for photos.** `LEAF_VISION_MODEL=moonshotai/Kimi-K2.6` in wrangler vars; `vision_chain` falls back to Nemotron 3.5 Super VL when a Kimi call fails; Quick Confident at 0.85 with a 0.3 lead (6 of 6 on tune, 6 of 9 on held-out), Deep at 0.65 (7 of 7 on tune); no Confident when the fallback read the photo. CLAUDE.md vision rule updated; the comparison is in README, landing page, Devpost and `docs/FEEDBACK.md` item 10.
- 2026-10-15: **Vision-model accuracy work** (Kimi K2.6 against Nemotron 3.5 Super VL). Spend about $2.9 of the $3 set, 22 of 60 Tavily credits. Findings: the held-out drop was mostly the vision model (Kimi: 52% tune, 50% held-out Quick; Nemotron VL: 36% and 33%), not overfitting. No tune-split knob closed the gap. No Quick threshold gives 85% Confident precision with Nemotron VL, so Quick and Deep never say Confident with an NVIDIA vision model (`quick_thresholds`, `deep_thresholds` in `backend/verdict.py`). Deep with Nemotron VL could not be fitted within the credit budget. Decision for the maintainer: keep all-NVIDIA (about 16 points lower on Quick) or pin Kimi for photos (`LEAF_VISION_MODEL=moonshotai/Kimi-K2.6` in `cloudflare/wrangler.jsonc` vars). Decision (maintainer, Oct 15): pin Kimi, keep Nemotron VL as the automatic fallback; thresholds follow the model that actually read the photo (`observed_by`). MCP connected to Claude Code and tested (care plan, weather risk, diagnose via a real client).
- 2026-10-15: **Telegram bot cancelled and removed.** The maintainer cancelled the bot. Deleted `backend/connect/telegram.py`, `backend/notify/telegram.py` and `scripts/telegram_setup.py`, with the webhook route, state, settings, link-code endpoints and repo methods, the Telegram channel in the notifications service (Web Push is the only channel), the Worker secrets and the deploy-script block, the Connect page section, and the claims in the README, Devpost and landing page. Migration 0006 is untouched (its two Telegram tables stay unused). `make check` green, 832 tests.
- 2026-10-15: **Release-candidate pass** (no new features). Judge simulation, whole-platform review, held-out accuracy.
  - **Balances:** Tavily 749 of 1,000 used (251 left) before the held-out runs; Auto spent about 26 credits and Quick none. Token Factory: production meter $0.43 over 95 calls (Oct 15) and the local dev meter $0.65; Nebius has no balance API, so credit left is read in the console (Billing, then Usage). Reserve for diagnoses: production keeps 20 credits back (`LEAF_SEARCH_RESERVE`), Radar takes 4 a day (one state).
  - **Held-out accuracy** (24 photos, never tuned on, one run each): Quick 33% top-1, 46% top-3, Confident right 3 of 7; Auto 33%, 50%, Confident right 6 of 9, 10 of 24 sent to a lab (9 warranted), Brier 0.18 against 0.36. Far below the tuned 58%: hard photos, and the vision model changed from Kimi K2.6 to Nemotron 3.5 Super VL between the two runs. README, landing page and Devpost now lead with these; tune numbers are labelled as tune.
  - **Judge simulation** (new user in headless Chromium at 375 and 1280 px on production: landing, three plants, Today, season plan 75 s, diagnosis, report page, check-in, notifications, Radar, ops, Connect): no console errors or failed requests; every page under 1 s except the model steps. Found and fixed: the season plan wait said nothing about how long it takes; the notification error read "Registration failed - permission denied"; Today had no way to delete a garden; Radar's empty state implied which states have gardens; a landing claim about location rounding was wrong for gardens; the landing "always go to a lab" wording overstated the rule. One phone-width run of the diagnosis step never produced a result in my script while a repeat worked (and the site made no model call in the failed one); not reproduced, recheck below.
  - **Whole-platform review (opus):** nothing disqualifying (no secrets in 58 commits; CSP; NVIDIA-only text models; Tavily only in `search.py`). Fixed: a global daily cap on season plans (8, `LEAF_SEASON_DAILY_CAP`) so a script cannot spend the day's $3 before anyone diagnoses a plant; per-IP limits count an IPv6 /64 as one client; Telegram secrets now reach the container (the bot could never have turned on); the Tavily day allowance is stored in KV so a restart cannot size a second one; vision discovery retries after a failed start; the journal purge runs beside the first requests; `safeUrl` on source links and non-web URLs dropped from sources (stored XSS path); `/api/ops/usage` rate limit; cron picks recently used gardens first; gardens unseen for 120 days are deleted; `scripts/deploy_nebius.sh` and `make deploy` removed (Nebius hosting is forbidden); README and Devpost claims corrected (free services, test count, Devpost now describes the whole platform). Left open: IPv6/WAF rules at Cloudflare, HSTS (zone setting), a cap on junk species names, R2 photo cleanup beyond the startup purge.
  - **Tests:** 843 pass (21 new, including one that runs `node --check` on every frontend script).
- 2026-10-15: **Leaf Platform M7, Radar, ops and story** (`backend/radar/`, `/radar`, `/ops`). Details are in `docs/LEAF_PLATFORM.md` M7.
  - **Radar:** a weekly job per US state with gardens searches that state's extension hosts, reads pages, dates them in code, screens (Nano 3), extracts (Lightning) and checks every report in code: quote on the page, names the pest, says it was seen, not negated, not another year, not another state. A capped regional prior (at most 5 points) reaches Quick and Deep; the verdict is the more cautious of the raw and boosted decisions.
  - **`/ops`:** counts and sums only. **README and landing page** rewritten as a platform with measured numbers and honest limits.
  - **Real runs found what the offline tests did not:** Lightning returned nothing on a clear sighting until the prompt had an example; a tracker heading passed as a sighting; and in production a quote about New York counties was stored as Pennsylvania's. All fixed with tests; Pennsylvania's clean re-run stores nothing, and `/radar` says so.
  - **Review:** one blocker (negated and historical sentences stored as current) and twelve other findings, fixed with tests.
  - **Speed in production (real browser):** pages shown with data in 206 to 828 ms; API calls 63 to 588 ms; a Quick diagnosis with garden link 8 s; Auto on late blight 38 s.
  - **Tavily credits** are the scarcest thing (the real cap is about 10 a day this month), so Radar takes at most 8 a day and leaves diagnoses theirs.
- 2026-10-15: **Leaf Platform M6, Connect** (`backend/connect/`, `/connect`). Details are in `docs/LEAF_PLATFORM.md` M6.
  - **API and keys:** `/api/v1` with OpenAPI; keys with scopes (`read`, `readings`, `tools`), hashed, with per-key limits counted in D1 (hour, day, model budget) and a 30-call daily model budget per garden.
  - **MCP:** `/mcp` serves both protocol eras (the 2025 handshake and the stateless 2026-07-28), written from the spec pages read today. Tools: diagnose, identify, care_plan, season_plan, weather_risk.
  - **Home Assistant:** `POST /api/v1/readings`. A fresh soil-moisture reading replaces the water balance for its plant, in both directions, and a test reading is labelled TEST in the task title, reason, source and notifications.
  - **Telegram:** webhook with a secret token, linking by one-time code, `/today`, `/season`, photo diagnosis, notifications through the same service as Web Push. Off and hidden without a token.
  - **Review:** one blocker (the bot token reached the logs through the HTTP library's request log) and five should-fix items, fixed with tests, one confirmed by mutation.
  - **Also fixed:** four M4 tests that failed between 21:00 and 07:00 UTC (quiet hours).
  - **Production check:** migration 0006 applied; the Connect page, a key, a TEST reading, the REST API and MCP (both protocol eras) worked in a real browser session, Telegram stayed off and hidden; test garden deleted.
  - **Telegram:** cancelled by the maintainer on 2026-10-15 and removed (see the top of this log); nothing left to finish.
  - **Live, local:** the sensor override, REST diagnose (12 s) and MCP calls with real weather, Tavily and Nemotron; the `/connect` page in headless Chromium at 375 px.
- 2026-10-15: **Leaf Platform M5, Season Plan** (`backend/season/`, `/season`). Details are in `docs/LEAF_PLATFORM.md` M5.
  - **Writing:** one Ultra outline call fixes every planting date. Then one Ultra call per month, saved as each is written, so a timeout retry writes only what is missing.
  - **Checking:** code rules on every item: frost dates and temperature minimums from CSU Extension's frost classes, organic and pet choices through `actives.py`, toxic-plant precautions, and caps on repeated jobs. Then Super verifies in chunks of 25 and can only remove. Nothing unverified is shown, and a rewrite keeps the current plan visible.
  - **Replanning:** a forecast frost or heat in a week with planting, a linked diagnosis, or a spreading check-in rewrites only the affected weeks. The diff is computed in code, and the gardener sees "Changed: why" and gets a notification.
  - **Doctor wiring:** "Add to my garden" on a report turns its code-checked steps into tasks, schedules a check-in and replans. Check-ins close that task and keep the loop going.
  - **Export:** a printable `/season` page and a `.ics` calendar export.
  - **Review:** the reviewer found 2 blockers. Each month was not saved as written, so a timeout retry rewrote the whole plan. Stored items pointed to plants by list position, so archiving a plant shifted them to the wrong one. Both are fixed, along with 7 should-fix items, each covered by a test.
  - **Live, local:**
    - The first run had the tomato sown indoors in four different months, and Super ran out of tokens. That led to the outline, and to an 8,000-token budget with 25-item chunks.
    - Second run: 76 coherent items in 110 s, 18 calls, $0.21, verified by Super.
    - A labelled test frost replan moved the tomato a week later, for $0.011.
  - **Production check:** migration 0005 applied. The first production plan never planted the tomato out: the outline picked the week containing the earliest date, code allowed it with a caution, and Super removed it. Fixed with a deterministic rule (a planting week must start on or after the earliest date; the outline moves a milestone to the next week), tests, and a redeploy. The second plan planted out on May 10, verified by Super.
  - **Doctor wiring and replan checked in production:** a real diagnosis linked to a test garden added tasks and a check-in. The replan ran once, was verified by Super, and wrote its changes. The `.ics` export returned 34 events. Test gardens deleted.
  - **Still open:** a photo check-in on a linked report (offline only) and a push on a real phone (the M4 item).
  - **Local dev DB:** `data/platform.db` had been built from an earlier draft of 0003 (no `species.started_at`). It was rebuilt locally; production D1 was not affected.
- 2026-10-15: **Leaf Platform M4, Leaf Care** (`backend/care/`, `backend/notify/`, `/today`). Details are in `docs/LEAF_PLATFORM.md` M4.
  - **Planner (code):** each task has a reason, a due time and a source.
    - Water or skip per plant.
    - Frost, heat, wind and flood protection from the forecast and NWS alerts.
    - Feeding (only with a verified feeding fact).
    - Harvest, prune and photo check-ins.
    - Pest scouting for six pests with published degree-day thresholds; a researcher fetched these.
  - **Notifications:** channels, dedupe, quiet hours (urgent alerts skip them), daily limits.
  - **Web Push:** VAPID keys were generated into a user-only file and shipped as a Worker secret, never printed. The app is installable (manifest, service worker, icons).
  - **Worker crons:**
    - hourly: planning at 05:00 local, and a 14:00 frost/heat prefilter that wakes the container only on risk days;
    - every 3 hours: NWS checks for US cells, with seen ids in KV.
  - **Models:** Lightning words the morning message (code rejects any number or plant not in the tasks); Nano 3 triages NWS alerts.
  - **Local run with real services:** a Minneapolis garden got the right tasks (rain day, no watering outdoors; "check the soil" indoors). Chromium subscribed through Google's push service, and a test push was accepted (1/1 devices).
  - **Review fixes:**
    - Alerts now work without a forecast, and a Freeze Watch two nights out no longer reads as "tonight".
    - Urgent alerts skip quiet hours; iPhone permission is asked first on the tap; NWS updates are sent once.
    - Gardens without a location are planned, and a location can be added later.
    - Nano's "relevant" flag and act-by time are used safely.
    - The service worker refuses off-site URLs; the nav fits 375 px; Worker runs are bounded.
  - **Production check:** at 375 px, a Minneapolis garden was set up with plants, Today was right, notifications were turned on, and the test push was accepted by Google's push service (D1: sent, 1/1 devices). Zero console errors; test garden deleted.
  - **Still to do:** a push received on a real Android phone and an iPhone (Home Screen app).
  - 703 tests.
- 2026-10-16: **Leaf Platform M3, knowledge graph and identify** (`backend/knowledge/`, `/identify`). Details are in `docs/LEAF_PLATFORM.md` M3.
  - **Profiles:** each profile comes from Tavily sources (a care search with a batched extract, plus its own toxicity search). Lightning extracts facts with exact quotes, code checks them, and Super verifies them. Unsourced fields are listed, and toxicity fails safe in code.
  - **Identify:** the vision model proposes, Super re-ranks against sources, and the gardener taps one (or types a name). The profile is then built and shown with every fact's source.
  - **Plant-ID eval** (40 licensed Commons photos): species top-1 was 92.5% for the vision model alone and 95% after Nemotron's check (fixed 1, broke 0). Genus top-1 was 97.5%. "Confident" was right 31 of 33 times. Cost $0.0026 a photo, median 6.9 s.
  - **Found live:**
    - Foxglove first came back "toxicity unknown" (one combined search crowded toxicity out), so toxicity got its own search; foxglove then came back toxic, citing poison.org.
    - Lightning gave an ideal range as a survival limit; Super rejected it, and `ideal_temp_c` now exists.
    - The vision model listed synonyms as separate candidates; its prompt changed (not re-measured).
  - **Review fixes:**
    - Common names no longer collide ("string of pearls" and "string of hearts" had one key).
    - Anonymous lookups can no longer queue builds.
    - The daily cap counts attempts; a separate daily Tavily budget covers the knowledge features; failed builds are not retried but can be requeued.
    - Confirm checks the garden key first.
    - A negated toxicity quote is rejected in code; reference data needs the right species.
    - "Checked" requires sources; web titles are sanitised in prompts; only http(s) links are rendered.
  - **Production check:**
    - A hosta photo on `/identify` (375 px) gave *Hosta plantaginea* at 72%; the two other hostas were ruled out with sourced reasons.
    - One tap built the profile in about 30 s: 6 verified facts, 4 rejected, pets toxic, children unknown, 9 fields listed as unsourced.
    - 4 knowledge credits were counted in D1; zero console errors.
  - 663 tests.
- 2026-10-15: **Leaf Platform M2, climate engine** (`backend/climate/`). Details are in `docs/LEAF_PLATFORM.md` M2.
  - **Sources:** two researchers fetched FAO-56 Tables 12, 19 and 22 and the Chapter 8 equations and Examples 36 and 38, plus the USDA PHZM method, NWS frost and wind criteria, Rutgers heat, the OSU/UC ANR germination tables and the MRCC/USBR degree-day methods. Every value cites one; where none exists, the value is labelled a heuristic.
  - **Found live:** Open-Meteo's `era5_land` returns no precipitation or ET0, so climatology uses `era5_seamless`. A 5-year archive chunk once took over 30 s, so archive calls now allow 90 s, retry once and cache their chunks.
  - **Zone check:** 5 US cities were within one half zone of USDA 2023 (none exact; 4 colder, 1 warmer). This is shown as a caveat, with no fitted correction.
  - **Review fixes:**
    - Open-Meteo is now limited per hour as well as per day, and deferred builds are staggered.
    - Litres logged on open ground no longer count as a full refill.
    - The USBR degree-day method raises Tmin to the base; this changed one old test.
    - Missing years no longer count as frost-free, and leap years no longer shift dates.
    - Late-season Kc is on FAO's linear decline, and the 70% rain rule is labelled.
    - A passing NWS 400 no longer marks a cell non-US, and the public climate endpoint has its own budget.
    - The older weather client now counts against the same allowance.
    - Spray windows, the disease-risk rules, the next 48 hours and season degree days are wired into the garden view.
  - **Production check** (one browser session, after deploy):
    - The garden view returned forecast, risks, NWS (covered; London correctly not covered), soil 12.4 C, tomato sowing "possible", 1,792 degree days since Jan 1, a spray window, tomato disease risk and the next 48 hours.
    - The FAO-56 advice was "skip, next on Oct 18".
    - The cell's climatology was built through the real queue in about 2 minutes (zone 4b, identical to the local build). Two gardens in the cell queued it and it was built once. Open-Meteo use that day: 806.5 units. Test gardens deleted.
  - The deploy script now retries the D1 migration step once (the D1 API failed the first call of two deploys in a row).
  - 610 tests.
- 2026-10-14: **Platform decisions after M1** (recorded in `docs/LEAF_PLATFORM.md`, "Decisions").
  - **Front door eval:** a 65-message eval (`evals/front_door.jsonl`, `make eval-router`) with a gate fixed before the first run.
    - Lightning passed: 0 false refusals, 0/6 harmful missed, 1/7 injections missed, intent 97%, median 0.4 s.
    - Nano 3 missed 2/7 injections on the first run, with a median of 1.4 s.
    - A code rule for any mention of the system prompt was added after run 1; both models then passed.
  - **Routing:** the live front door is now Lightning (Nano as fallback). Nano 3 does background screening; check-in notes are screened now, and notes it flags are set aside with a note to the gardener.
  - **Jobs:** planning once per populated timezone was approved (about $0.60 a month), and an afternoon frost and heat check at 14:00 local, with a Worker temperature prefilter, was added to the M4 plan.
  - **Zero console errors:** the edge was injecting the Web Analytics beacon even though the zone's RUM setting was off. A zone configuration rule (`disable_rum`) stops it. The waking page and `/__leaf/ready` answer 200 instead of 503.
  - **CI:** GitHub Actions has not started since at least Oct 14 (account billing), so `make check` runs before every push.
  - 535 tests.
- 2026-10-14: **Leaf Platform M1, Foundation.** `docs/LEAF_PLATFORM.md` written. It covers the architecture, data model, routing table with per-task budgets, job schedules, a cost model (about $0.0057 per active user per day), risks and the cut list, and its limits were verified from Cloudflare, Open-Meteo and NWS pages. What M1 added:
  - Data layer: D1 for gardens (private links, only the key hash stored), plants, tasks, events, notification settings, photos (30 days), jobs and the meter. Migrations are the same SQL files for Wrangler and local SQLite, behind a typed repository (`backend/data/`). The container reaches D1, KV and the job queue through the Worker's outbound interception at `http://leaf-platform.internal`, with no API token involved.
  - `ROUTES` in `config.py` is now the only place model chains and token budgets live; every call site uses it.
  - Nano 3 front door (`backend/router.py`): code screen, then explicit intent, then a 24 h shared cache, then one Nano call for intent and safety, falling back to keyword rules. It also screens chat.
  - Jobs: cron → Queue → consumer → `/internal/jobs/run`, idempotent through the `jobs` table, with lease-aware retries and a DLQ (`housekeeping` daily, `garden_ready`).
  - Meter and spend guard (`backend/metering.py`): every call is recorded by task, model, tokens, cost and trace id. $3/day cap, brownout at 80%, rules-only at 100%, restored from D1 after a restart. `GET /api/ops/usage` reports cost per active user, cache hit rate and model mix.
  - **Found locally with `wrangler dev`:** a `static outboundByHost` class field bypasses the library's setter, so the bridge silently went to the internet; it is now assigned through the setter. A bridge failure at startup used to crash the whole app; it is now a 503 on garden routes only.
  - **Measured live:** Nano 3 was 7/7 on labelled front-door messages, at a median of 1.8 s and $0.000036 a call. Lightning was also 7/7, at 381 ms and the same cost, so whether to swap is an open decision for the user.
  - **Review fixes:** the code screen refused real toxicity questions ("Can foxglove kill a person?") and is now narrow; meter backlogs are written in chunks of at most 200 statements (the bridge limit); a spend cap in streamed cases is a 503; cancelled calls are metered at the route's typical cost; one person counts as one active user; a busy job retries after its lease; `/%69nternal/` is refused; unpriced models are charged at the dearest known price.
  - Also: `Settings` repr no longer shows API keys (a failing test had printed one locally); `.gitignore`'s `data/` was hiding `backend/data/`.
  - **Deployed and checked on production** (one headless-browser session after the deploy reported ready):
    - The garden was created in D1, and a wrong key or `/internal/` answered 404.
    - Its `garden_ready` job went through the real Queue and consumer in 6.4 s.
    - A Nano front-door call (2.1 s, $0.000036) was metered in D1 under the same trace id as `X-Leaf-Trace`.
    - `/api/ops/usage`: 1 active user, $0.000036 that day.
    - The test gardens were deleted.
  - **Two deploy-time lessons:**
    - (1) A rollout restarts the running container with the env the previous Worker gave it, so new variables are missing until the next cold start. `LEAF_PLATFORM_URL` is now baked into the image.
    - (2) Right after a rollout, the container can start before the Worker wires the bridge. Health now retries the schema check, and the meter retries its restore. During that window garden routes answer 503 and diagnosis keeps working.
  - Pre-existing and unrelated: the CSP blocks Cloudflare's injected Web Analytics beacon (a console error on every page).
  - 530 tests.
- 2026-10-15: Rainforest. Every page now sits over an animated tropical rainforest drawn in code (`frontend/rainforest.js`, no images): sky and sun, stippled canopy ridges, emergent giants, a tepui waterfall with plume, mid-forest, mist at two depths, buttress trees with lianas, light shafts, palms, tree ferns, banana and monstera leaves that bend with gusty wind, motes, falling leaves, blue morphos (fireflies at dusk) and passing macaws; parallax with scroll and mouse. Content sits on frosted panels. Measured: 30 fps steady on a phone profile with 4x CPU throttle, no long tasks; desktop capped at 60 fps; still frame with reduced motion; Lighthouse mobile 100/100/100. Landing copy cut by about a third, with details in "How exactly?" expanders.
- 2026-10-15: Website and three features. New landing page at `/` (white with green text, Newsreader and Instrument Sans; an animated example case in the hero, a step line that draws as you scroll, working miniatures of each feature, honest accuracy numbers, a FAQ); the app moved to `/app`; `/plants` is new. Features, each decided in code: organic and pet-safe plans (`backend/actives.py`, 33 active ingredients, enforced on plans and check-ins), the best time to spray (`backend/spray.py`, five-day Open-Meteo forecast, best 3-hour window), My plants with calendar reminders (`.ics` and Google Calendar links; a private link moves the list between devices). Live local run: powdery mildew Confident, organic plan with potassium bicarbonate or neem oil, spray window found. Deploy docs now point only at `make deploy-cloudflare`; `docs/DEVPOST.md` drafted. 422 tests.
- 2026-10-14: Shipped. Leaf runs on Cloudflare Containers (`cloudflare/`, one `basic` instance behind a Worker, sleeps after 30 idle minutes) with reports in an R2 bucket; every model call goes to Nebius Token Factory. Nebius AI Cloud hosting was dropped because the account's billing is suspended. The Worker replaces X-Forwarded-For with CF-Connecting-IP so the per-IP limit can't be spoofed. Daily caps: 60 diagnoses, Tavily credits sized from the live monthly balance (Leaf answers without fresh sources when they run out). Live checks in a headless browser: Quick 11.5 s, Auto 41 s with the printable lab handoff on a late-blight photo, the share page loads from R2. First visit after the container sleeps took 8.2, 5.6 and 9.5 s to first byte, close to the 10 s bar, so the Worker now answers a page visit to a sleeping container at once with a "Leaf is waking up" page that reloads itself when Leaf is ready (`cloudflare/src/waking.ts`). Found and fixed on the live site: a share link offered when the save had failed; secrets set after `wrangler deploy` missed the container that the rollout had already started (now `--secrets-file`); browsers kept last version's script after a deploy (now `Cache-Control: no-cache`). `docs/FEEDBACK.md` written. Token Factory spend about $5.75.
- 2026-10-13: Accuracy sprint. Canonical condition names with look-alikes and high-stakes conditions (`backend/diseases.py`); naming caused 0 misses. Tune split grown to 52 Commons photos (CC0, public domain, CC BY, CC BY-SA; every file's licence, author, source and changes in `evals/README.md`). Every Quick miss classified (`docs/ACCURACY_AUDIT.md`): 12 reasoning, 6 vision, 7 ambiguous. The vision checklist did not help (5 fixed, 7 broken) and is off. Three verdicts in code (Confident / Likely / Send to a lab; a high-stakes condition at 20% anywhere goes to a lab), thresholds fitted on the tune split. Auto mode (Quick, then Deep only when unsure, seeded with Quick's work) beat Quick (58% vs 52% top-1, Brier 0.204 vs 0.264) and is the default. Found and fixed: Deep sent cases to a lab because the investigator finished without searching (no sources, every plan step dropped); Leaf now searches itself. Object Storage journal backend (`LEAF_JOURNAL_BUCKET`, not created) and `scripts/pin_case.py` for long-lived demo links. Token Factory spend about $5.7.
- 2026-10-13: Leaf v2 M4-M6. M4: live Deep interface over server-sent events (live log, re-ranking differential, weather strip, second opinion, confidence, printable lab handoff), Quick/Deep toggle. M5: plant journal (SQLite behind an interface, 30 days, thumbnails only), share page `/c/<id>`, check-ins with an owner token. M6: Quick vs Deep evals with offline calibration; three review passes fixed, among others, a claim check that could pass a 10x dose, a fail-open claim judge, a blend that merged distinct diseases, and share links that exposed notes. Calibrated on the tune split: blend 0.7, critic vote, Deep threshold 0.5. Quick stays the default until the test split is in. 311 tests.
- 2026-10-12: Leaf v2 M3 and the M2 review fixes. Second opinion (Super, evidence only), confidence combined in code, plan from read passages only, claim check (code then Lightning), lab handoff with NPDN state link and sampling checklist, first differential seeding. Review fixes: a forced finish that times out keeps the recorded differential, a failed Quick fallback answers instead of a 504, per-turn budget estimates add up, refunds for calls skipped before running, geocoding prefers big cities and uses every comma part, TOMCAST banded in the source's Fahrenheit labels, Wallin runs break at data gaps, stricter URL checks, alerts work with browser location. Live: the critic caught the late-blight case (argued from the white growth) and escalated; on early blight Deep still sometimes drifts to Septoria. Eval tooling (Quick vs Deep, Brier, reliability, escalation precision/recall) built. 264 tests.
- 2026-10-12: Leaf v2 M2 and the M1 review fixes. Tools: `get_conditions` (Open-Meteo, cached; Smith, Hutton, Wallin for potato, TOMCAST for tomato, general summary otherwise), `read_source` (Tavily extract, case URLs only), `check_alerts` (news, last month). Review fixes: a single hard deadline for the whole case (a hanging model now falls back to Quick in under 8 s instead of a 504), billed cost of retried and failed attempts counted, per-call budget check, cancelled cases marked failed, model-written text sanitised before reuse, photo labels stay with their photo, the fake LLM enforces the tool-message protocol. Honest live result: Quick beat Deep on a late-blight photo; prompt and caps improved Deep to 1 of 2. 197 tests.
- 2026-10-12: Leaf v2 M1. Investigator agent (`agent.py`, `tools.py`, `case.py`, `store.py`, `deep.py`), `POST /api/deep` and `/api/deep/{id}/answers`, Deep daily cap 20, live prices and per-call cost in every trace entry. Live runs found and fixed: invented tool names (now answered with the real list), prose instead of `finish` (now forces finish), a display bug that crashed a case (any agent exception now falls back to Quick). The model repeatedly tried to open sources it found, which M2's `read_source` provides. 128 tests.
- 2026-10-12: Pre-M1 repo check for speed and tokens. Benchmarked 6 vision models on 4 real photos: Kimi-K2.6 with its own `thinking: False` template key kept specific observations at 3.9 s and $0.002 per photo (was 11.2 s, $0.0083). Nemotron reads `enable_thinking`, Kimi reads `thinking`; `config.thinking_key()` maps them. Quick photo case end to end: 10.7-13.3 s (was 23-28 s). GLM-5.3-Flash timed out and DeepSeek-V4.1-Flash took ~57 s, so neither is a fallback. Added a 22-image licensed Commons tune split (`evals/README.md`). Approved v2 decisions recorded in `docs/AGENT_V2.md`.
- 2026-10-12: Leaf v2 Milestone 0. Three researcher passes (Token Factory, Tavily, Open-Meteo plus published disease rules) and two live probes (under 1 cent) wrote `docs/AGENT_V2.md`. Findings that change the spec: tools and `response_format` must not be combined (the model stops calling tools); the model emits parallel tool calls on its own; no NVIDIA guard/embedding/rerank model exists on Token Factory (optional Nano screen instead); `get_conditions` needs a `crop` argument because the published rules are crop-specific; Open-Meteo free tier is non-commercial; `ask_user` resume needs case state before M5, so M1 uses an in-memory registry. Estimated Deep cost about 8.5 cents.
- 2026-10-12: Backend hardening after an independent audit and a second review (every claim verified in code before acting). Fixed: decompression-bomb upload crash (500) and pixel cap, EXIF rotation, body size limits enforced before buffering, configurable proxy depth for per-IP limits, limiter eviction, request deadline + concurrency cap, lenient schema normalisation, near-tie escalation, prompt-injection sanitising (notes, answers, region, web snippets, URLs, chat context), bounded chat context, citation filtering, strict trusted-host matching, narrower provider-param learning, generic client errors, JSON extraction, repair prompt keeps the JSON, eval.py per-case errors, security headers + CSP. **Provider quirk found and fixed:** Kimi-K2.6 reasons for 1.4k-2.4k tokens before answering, so the 1500-token vision budget intermittently produced empty or truncated JSON and the vision step failed; vision budget is now 4000 and JSON truncated at max_tokens is retried with a bigger budget like an empty reply. Vision 5/5 live after the fix. Tests mutation-checked (note: a same-size mutation can leave a stale .pyc; clear `__pycache__` after mutation runs).
- 2026-10-12: Frontend rebuilt as a minimal single column (serif prose, soft marker CTA, quiet run list) in Leaf's greens, after the earlier dark NVIDIA/LangChain-style version.
- 2026-10-12: Frontend redesign: dark nav/hero/footer with a pale workbench (NVIDIA- and LangChain-inspired design language, original leaf mark, no third-party logos or assets). Hero shows a live agent graph (vision, Ultra, Tavily, Lightning) driven by `/api/health` model IDs and the real trace. Checked in a browser: text-only diagnose renders, no console errors, no horizontal overflow at 375 px.
- 2026-10-12: Live check. `make smoke-all` green. Text-only `POST /api/diagnose` (tomato, brown ringed spots, Ohio) returned 200 in 9.7 s: top candidate Early blight (Alternaria solani) 0.55, `escalate: true` (low confidence, enforced in code), trace = Ultra (diagnose, 5.2 s) then Lightning (plan, 2.0 s), 6 Tavily sources. No provider quirks hit.
- 2026-10-12: Scaffold created: backend, frontend, tests, scripts, Claude Code config, playbook.
