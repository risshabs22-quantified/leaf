// Shown instead of a blank wait while the container starts after a sleep (5-10 s measured).
// The page waits on /__leaf/ready, which returns once Leaf answers, then reloads itself.
export const WAKING_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Leaf · plant doctor</title>
<noscript><meta http-equiv="refresh" content="8"></noscript>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%2212 0 96 200%22 role=%22img%22 aria-label=%22Leaf%22%3E %3Cdefs%3E %3ClinearGradient id=%22leaf-g%22 x1=%220%22 y1=%220%22 x2=%220%22 y2=%221%22%3E %3Cstop offset=%220%22 stop-color=%22%23a8ea60%22/%3E %3Cstop offset=%220.55%22 stop-color=%22%234fbd5a%22/%3E %3Cstop offset=%221%22 stop-color=%22%230e8a45%22/%3E %3C/linearGradient%3E %3C/defs%3E %3Cpath d=%22M60 160 C61 172 58 182 54 195%22 fill=%22none%22 stroke=%22%231b9a4c%22 stroke-width=%226%22 stroke-linecap=%22round%22/%3E %3Cpath d=%22M60 8 C76 22 97 48 97 80 C97 118 80 148 60 163 C40 148 23 118 23 80 C23 48 44 22 60 8 Z%22 fill=%22url%28%23leaf-g%29%22/%3E %3C/svg%3E ">
<style>
  :root { --paper: #fbfcf9; --ink: #16201a; --muted: #5a675e; --green: #2f6b3a; }
  @media (prefers-color-scheme: dark) {
    :root { --paper: #0f1511; --ink: #e8efe6; --muted: #9aa99d; --green: #8fcf8f; }
  }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--paper); color: var(--ink);
    font: 17px/1.5 "Instrument Sans", "Helvetica Neue", Arial, sans-serif;
    display: grid; place-items: center; padding: 0 16px;
  }
  main { max-width: 30rem; }
  svg { width: 24px; height: 50px; display: block; margin-bottom: 20px; transform-origin: 50% 100%; animation: sway 2.4s ease-in-out infinite; }
  h1 { font: 500 1.6rem/1.2 Newsreader, "Iowan Old Style", Georgia, serif; margin: 0 0 8px; }
  p { margin: 0; color: var(--muted); }
  @keyframes sway { 50% { transform: rotate(-8deg); } }
  @media (prefers-reduced-motion: reduce) { svg { animation: none; } }
</style>
</head>
<body>
<main role="status" aria-live="polite">
  <svg viewBox="12 0 96 200" aria-hidden="true"><defs><linearGradient id="wg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#a8ea60"/><stop offset=".55" stop-color="#4fbd5a"/><stop offset="1" stop-color="#0e8a45"/></linearGradient></defs><path d="M60 160 C61 172 58 182 54 195" fill="none" stroke="#1b9a4c" stroke-width="6" stroke-linecap="round"/><path d="M60 8 C76 22 97 48 97 80 C97 118 80 148 60 163 C40 148 23 118 23 80 C23 48 44 22 60 8 Z" fill="url(#wg)"/></svg>
  <h1>Leaf is waking up</h1>
  <p id="msg">It rests when nobody is using it. This takes about ten seconds, and the page opens by itself.</p>
</main>
<script>
  (async () => {
    const started = Date.now();
    while (Date.now() - started < 90000) {
      try {
        const r = await fetch("/__leaf/ready", { cache: "no-store" });
        if (r.ok && (await r.json()).ready) return location.reload();
      } catch {}
      await new Promise((ok) => setTimeout(ok, 1500));
    }
    document.getElementById("msg").textContent = "This is taking longer than usual. Reload the page to try again.";
  })();
</script>
</body>
</html>`;
