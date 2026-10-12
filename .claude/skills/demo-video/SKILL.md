---
name: demo-video
description: Write the under-3-minute demo video script and shot list for Leaf from what currently works. Use when the app is deployed and ready to record.
disable-model-invocation: true
---
Write `docs/VIDEO_SCRIPT.md`: a script that fits in 2:50 at a natural speaking pace (about 130 words per minute, so at most about 370 spoken words), with a shot list.

Structure:
1. 0:00–0:20 Hook: a real gardener's problem (a sick plant, no idea why). Name the audience.
2. 0:20–1:40 Live demo on the deployed URL: upload a photo, show ranked causes with confidence, answer a follow-up question, show the cited care plan and the check-in date, ask one chat question.
3. 1:40–2:30 How it works: open "How Leaf thought". Say which model does each step: the vision model reads the photo, Nemotron 3 Ultra diagnoses, Tavily finds extension guides, Nemotron 3.5 Lightning writes the plan and chats, all on Nebius Token Factory, deployed on a Nebius Serverless Endpoint. Mention fallbacks and that uncertainty always escalates.
4. 2:30–2:50 Impact and next steps; end on the repo URL.

Rules: only show features that work today; no copyrighted music or logos; use your own photos; English. Check timing by counting words.
