// Shown instead of a blank wait while the container starts after a sleep (5-10 s measured).
// The page waits on /__leaf/ready, which returns once Leaf answers, then reloads itself.
export const WAKING_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Leaf · plant doctor</title>
<noscript><meta http-equiv="refresh" content="8"></noscript>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M6 26C6 14 13 7 26 6c0 13-7 20-18 20z' fill='none' stroke='%232f6b3a' stroke-width='2.4' stroke-linejoin='round'/%3E%3Cpath d='M6 26C12 19 17 14 22 10' fill='none' stroke='%232f6b3a' stroke-width='2.4' stroke-linecap='round'/%3E%3C/svg%3E">
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
  svg { width: 40px; height: 40px; display: block; margin-bottom: 20px; transform-origin: 20% 80%; animation: sway 2.4s ease-in-out infinite; }
  h1 { font: 500 1.6rem/1.2 Newsreader, "Iowan Old Style", Georgia, serif; margin: 0 0 8px; }
  p { margin: 0; color: var(--muted); }
  @keyframes sway { 50% { transform: rotate(-8deg); } }
  @media (prefers-reduced-motion: reduce) { svg { animation: none; } }
</style>
</head>
<body>
<main role="status" aria-live="polite">
  <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M6 26C6 14 13 7 26 6c0 13-7 20-18 20z" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round" style="color: var(--green)"/><path d="M6 26C12 19 17 14 22 10" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" style="color: var(--green)"/></svg>
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
