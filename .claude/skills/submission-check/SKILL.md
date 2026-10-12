---
name: submission-check
description: Audit Leaf against every Devpost submission requirement and the official rules, and list the gaps. Use before saving the Devpost draft and again before the Oct 30 deadline.
disable-model-invocation: true
---
Check each item, mark PASS / FAIL / CAN'T CHECK, and fix what you can yourself.

Repo
- [ ] LICENSE is MIT and at the repo root (GitHub shows it in About).
- [ ] README has: what Leaf does and who it's for, a "Try it" demo URL, setup steps that work from a clean clone (`make setup && make smoke-all && make run`), which NVIDIA models do what, how Token Factory is used, other Nebius services (Serverless Endpoint), the Tavily runtime call, and a feedback section.
- [ ] No secrets in the repo or history: `git log -p | grep -nE "tvly-|NEBIUS_API_KEY=[A-Za-z0-9]"` returns nothing.
- [ ] `make check` passes; CI is green.

Demo
- [ ] `curl -s <demo-url>/api/health` returns ok with `"search": "TavilySearcher"`; one real diagnosis works end to end and shows sources.
- [ ] Rate limits are on so judges can't drain credits (LEAF_PER_IP_PER_HOUR, LEAF_DAILY_CAP).

Video (public YouTube, under 3 minutes, English)
- [ ] Shows Leaf working, which Nemotron model does what, Token Factory, Tavily, the Serverless Endpoint.
- [ ] No copyrighted music, logos or third-party trademarks beyond naming the tech used.

Devpost form
- [ ] Track: Best Apps and Agents. Description: what, why, how.
- [ ] Feedback on Token Factory, AI Cloud and NVIDIA models (enters the $100 feedback prize).
- [ ] Statement that all code was written during the submission period (Aug 26 to Oct 30, 2026).

Reply with the checklist results and the 3 most important fixes, in at most 30 lines.
