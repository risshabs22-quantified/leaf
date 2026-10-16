# Leaf demo video: research brief

Compiled 16 Oct 2026 from three research passes (hackathon judging, frontier-lab demo craft, free
production toolchain). The researchers read articles, official pages and video descriptions; they
could not watch videos, so anything about what is on screen comes from write-ups. Unverified items
are marked. Sources are at the end.

## 1. Hard requirements for this hackathon

- Public YouTube video, **under 3 minutes**. Target 2:40-2:50.
- It must show Leaf working and how Nebius Token Factory and the NVIDIA models are used.
- No copyrighted music, logos or third-party material without permission. Use our own plant
  footage; do not show Wikimedia eval photos without on-screen credit.
- English. Judges (NVIDIA, Nebius and Tavily people) may score from the video and text alone.
- Set YouTube "Not made for kids". Upload at least a day early; uploads and processing fail.

## 2. What wins with hackathon judges

**Structure that works (about 2:45):**

| Time | Beat | Notes |
| --- | --- | --- |
| 0:00-0:15 | Hook | The outcome in the first 5 s, then one concrete person and scene. |
| 0:15-0:30 | The product in one sentence | Leave the "how" for later. |
| 0:30-1:55 | One real end-to-end flow | Not a feature tour. Sources visible on screen. |
| 1:55-2:25 | How it's built | One architecture shot; name the models and Tavily, show them running. |
| 2:25-2:45 | Impact and evidence | Eval numbers, then the URL and repo. |

**Mistakes that lose points:** going over time; a slow intro; slides or talk instead of the running
product; a feature tour instead of one flow; skipping the problem; terminal-only with no user; bad
audio or no captions; sponsor tech buried; buzzwords over proof; the video claiming more than the
code does (judges open the repo); private video or late upload; demoing half-built features.

**Technical depth without losing people:** show the sponsor API running, not a logo; put detail in
the written description; show quantitative evaluation. The Nemotron hackathon grand-prize team
(Nota AI) was praised for evaluation over polish. Judges do open the code.

**Practical rules:** your own voice plus captions (some hackathons ban TTS); music optional, quiet
(15-20% volume) and licence-clear; 1080p minimum; real product, no mockups or hard-coded data; do
not speed up audio; cut dead waits visibly and say so.

**Examples to study (text write-ups only):**
- CrossBeam, Opus 4.6 1st place: https://www.youtube.com/watch?v=jHwBkFSvyk0 (a real bottleneck, a specific user).
- MaestrIA, Opus 4.7 prize: https://www.youtube.com/watch?v=rkH4AjoTL5Q (photo-to-diagnosis for home repair, the closest analogue to Leaf; a personal story).
- Jayu, Gemini API Best Overall: https://www.youtube.com/watch?v=shnW3VerkiM (eight versions with reviewer feedback; the description discloses cuts and credits music).
- Gaze Link: https://www.youtube.com/watch?v=y_U1vkEC9PA (one memorable input-to-output moment).

## 3. What frontier labs do

- **Anthropic:** plain, labelled agent recordings with candid caveats (the computer-use video says
  every action was the model's, in a controlled environment, and that the feature is experimental).
  It publishes failures and bloopers, and shows human-in-the-loop confirmation.
- **Google:** Project Astra was labelled "two continuous takes", each "in a single take, in real
  time". Gemini 2.5 Computer Use captions say "shown here at 3X speed". Antigravity's launch is
  built on "verify with artifacts, not logs". **Cautionary tale:** "Hands-on with Gemini" (Dec 2023)
  implied live voice and video, but used still frames and text prompts; disclosure sat below the
  fold. Lesson: disclose how the input was given, not only speed-ups.
- **OpenAI:** live and slightly messy (the GPT-4o demo retried a bad attempt on stage); "All videos
  at 1x real time"; agent steps shown as a visible computer with confirm-before-acting; launch
  pages end with limitations.
- **xAI:** long livestreams led by benchmark charts. **Do not copy** for a 3-minute judged video.

**Honesty norms to follow:** "Real run, 1x" or "waits cut" labels; show the agent asking or
escalating; show one failure or uncertain case; state how every number was measured.

**Craft rules a solo builder can copy:**
1. Product on screen within 5 s.
2. One story: a sick plant, a photo, an answer. One memorable moment (sources appearing, or the
   frost alert arriving).
3. Record real runs; label cuts.
4. Show agent steps as named stages with the model that ran each, not logs.
5. Zoom 1.5-2.2x for emphasis (3.5-5x for tiny UI), held 2-4 s.
6. Cut anything over 6-8 s that doesn't change the screen.
7. On-screen text under 10 words, high contrast, readable with the sound off.
8. Two colours, one background. Copy the restraint, not anyone's brand.
9. Calm first-person narration; name the models and what each does, and say the vision model
   isn't NVIDIA and why.
10. Quiet room tone or licence-clear music only.

Not to copy: hour-long livestreams, benchmark-first openings, hype language, licensed-music brand
films, multi-product parallel demos, still frames cut to look live.

## 4. Free production toolchain (Mac)

- **Remotion** (best fit for an agent): React-based programmatic video, free for individuals and
  companies of up to 3 people (check the licence page before use). Composition 1920x1080 at 60 fps;
  `npx remotion render src/index.ts Demo out/demo.mp4 --codec=h264 --crf=18`; embed clips with
  `<OffthreadVideo>`. Official agent skills exist (`npx skills add remotion-dev/skills`; the docs
  disagree on the exact command). Motion Canvas (MIT) is harder to render headless.
- **Scripted capture:** Playwright `recordVideo` is weak (VP8, 25 fps, about 1 Mbps; set `size`).
  Newer `page.screencast.start({path, size, fps, onFrame})` (fps from 1.64) and
  `showActions({cursor})` (1.61) are better. Unverified: bitrate and device-pixel size; test 10 s
  and check with `ffprobe`. Fallback: pipe `onFrame` JPEGs to ffmpeg. Use a 1920x1080 viewport,
  `deviceScaleFactor: 2`, `page.mouse.move(x, y, {steps: 30})`, and log each action's timestamp and
  bounding box to `actions.json` so Remotion can animate zoom and cursor.
- **Manual capture:** OBS (free), QuickTime. Cap (AGPL; free tier is personal use; check before
  relying on it). Screen Studio is paid ($9-29/month).
- **Editing fallback:** DaVinci Resolve free (to 4K60). Avoid CapCut (broad content-licence terms).
- **Captions:** whisper.cpp (`brew install whisper-cpp`, model `small.en`, output SRT, about 42
  characters a line). Easiest is to burn captions in Remotion.
- **Voice:** built-in Mac mic in a quiet, soft room with Voice Isolation on; not AirPods (macOS
  drops them to a call codec). Light cleanup: ffmpeg `afftdn=nr=12:nf=-40`.
- **Loudness:** two-pass ffmpeg `loudnorm=I=-14:TP=-1.5:LRA=11`.
- **Music:** YouTube Audio Library tracks, filtered to no attribution, aren't claimed by Content ID.
  Pixabay needs care: some tracks are registered with Content ID, so keep the download proof.
- **YouTube:** H.264 MP4, faststart, AAC 48 kHz, about 12 Mbps for 1080p60. Public. Chapters in the
  description: first at 00:00, at least 3, each 10 s or longer. Upload the SRT. 16:9 thumbnail at
  1280x720 or larger.

## Sources

Hackathon judging: https://info.devpost.com/blog/6-tips-for-making-a-hackathon-demo-video ·
https://info.devpost.com/blog/hackathon-judging-tips ·
https://info.devpost.com/blog/understanding-hackathon-submission-and-judging-criteria ·
https://dev.to/jacklyn/how-to-make-your-devpost-submissions-not-suck-37i0 ·
https://blog.jetbrains.com/ai/2026/06/how-to-win-a-hackathon-notes-from-the-judging-table/ ·
https://hacks.elevenlabs.io/guide · https://blog.colosseum.com/perfecting-your-hackathon-submission/ ·
https://www.revenuecat.com/blog/engineering/how-to-win-shipaton-part-4-pitching-your-app ·
https://aitinkerers.org/docs/public/video_production_guide.md ·
https://ethglobal.com/events/agents/info/details ·
https://guide.devfolio.co/docs/guide/participating-in-hackathons/judging-1 ·
https://claude.com/blog/meet-the-winners-of-our-built-with-opus-4-6-claude-code-hackathon ·
https://developers.googleblog.com/en/announcing-the-winners-of-the-gemini-api-developer-competition/ ·
https://www.nota.ai/community/nvidia-nemotron-hackathon-grand-prize-among-20-teams-behind-two-sleepless-days

Lab demo craft: https://www.anthropic.com/news/developing-computer-use ·
https://www.youtube.com/watch?v=ODaHJzOyVCQ · https://www.youtube.com/watch?v=nXVvvRhiGjI ·
https://techcrunch.com/2023/12/07/googles-best-Gemini-demo-was-faked/ ·
https://developers.googleblog.com/how-its-made-interacting-with-gemini-through-multimodal-prompting/ ·
https://blog.google/technology/google-deepmind/gemini-computer-use-model/ ·
https://developers.googleblog.com/en/build-with-google-antigravity-our-new-agentic-development-platform/ ·
https://simonwillison.net/2025/Nov/18/google-antigravity/ · https://openai.com/index/hello-gpt-4o/ ·
https://openai.com/index/introducing-chatgpt-agent/ · https://openai.com/index/introducing-codex/ ·
https://techcrunch.com/2025/02/04/openais-operator-agent-helped-me-move-but-i-had-to-help-it-too ·
https://techcrunch.com/2025/02/22/did-xai-lie-about-grok-3s-benchmarks

Toolchain: https://www.remotion.pro/license · https://www.remotion.dev/docs/cli/render ·
https://www.remotion.dev/docs/ai/claude-code · https://playwright.dev/docs/api/class-screencast ·
https://playwright.dev/docs/release-notes · https://github.com/ggml-org/whisper.cpp ·
https://ffmpeg.org/ffmpeg-filters.html · https://support.google.com/youtube/answer/3376882 ·
https://support.google.com/youtube/answer/9884579 · https://pixabay.com/service/license-summary/ ·
https://www.techradar.com/pro/popular-video-editing-app-capcut-wants-to-use-any-content-you-produce-for-free-forever-heres-what-you-should-know
