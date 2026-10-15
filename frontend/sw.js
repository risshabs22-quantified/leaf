// Leaf's service worker: Web Push notifications only (no offline caching).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data ? event.data.text() : "" }; }
  const title = String(data.title || "Leaf").slice(0, 120);
  event.waitUntil(self.registration.showNotification(title, {
    body: String(data.body || "").slice(0, 400),
    tag: String(data.tag || "leaf"),
    renotify: Boolean(data.urgent),
    requireInteraction: Boolean(data.urgent),
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: { url: String(data.url || "/today") },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const path = event.notification.data && event.notification.data.url || "/today";
  // Only pages of this site ("//evil.com" also starts with "/").
  let url = new URL("/today", self.location.origin).href;
  try {
    const u = new URL(path, self.location.origin);
    if (u.origin === self.location.origin) url = u.href;
  } catch {}
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const want = new URL(url).pathname;  // e.g. /today, or /season for a plan update
    for (const w of wins) {
      const at = new URL(w.url).pathname;
      if (at === want || (want === "/today" && at.startsWith("/g/"))) { await w.focus(); return; }
    }
    await self.clients.openWindow(url);
  })());
});
