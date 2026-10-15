// Today: the garden's day. Setup, tasks (urgent first), water, plants, notifications.
// Every model or web string goes through esc() or textContent.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (id) => document.getElementById(id);
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
const STORE = "leaf.garden";

function loadGarden() {
  // A private link (/g/<id>#k=<key>) wins; it is then remembered on this device.
  const m = location.pathname.match(/^\/g\/([\w-]+)$/);
  const k = (location.hash.match(/^#k=([\w-]+)$/) || [])[1];
  if (m && k) {
    const g = { id: m[1], key: k };
    try { localStorage.setItem(STORE, JSON.stringify(g)); } catch {}
    return g;
  }
  try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch { return null; }
}

let garden = loadGarden();
let place = null; // {lat, lon, label} chosen during setup

async function api(path, opts = {}) {
  const headers = { ...(opts.body ? { "content-type": "application/json" } : {}), ...(garden ? { "X-Leaf-Key": garden.key } : {}) };
  const r = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  let d = null;
  try { d = await r.json(); } catch {}
  if (!r.ok) throw new Error((d && d.detail) || "Something went wrong. Please try again.");
  return d;
}

function show(id, on = true) { $(id).hidden = !on; }
function status(text) { $("t-status").textContent = text || ""; }
function hhmm(ts, tz) {
  try { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", timeZone: tz }); } catch { return ""; }
}
function sourceHtml(src) {
  if (!src) return "";
  if (src.heuristic) return `<span class="src">Leaf's own rule</span>`;
  const url = safeUrl(src.url) || (/^\/c\/[\w-]+$/.test(String(src.url || "")) ? src.url : "");
  return url ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(src.title || "Source")}</a>` : `<span class="src">${esc(src.title || "")}</span>`;
}

// ---------- setup ----------

$("g-locate").addEventListener("click", () => {
  if (!navigator.geolocation) { $("g-where").textContent = "Location is not available in this browser."; return; }
  $("g-where").textContent = "Finding you…";
  navigator.geolocation.getCurrentPosition(
    (p) => { place = { lat: p.coords.latitude, lon: p.coords.longitude, label: "your location" }; $("g-where").textContent = "Using your location."; },
    () => { $("g-where").textContent = "Leaf could not get your location; type a town instead."; },
    { maximumAge: 600000, timeout: 10000 },
  );
});

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  status("Creating your garden…");
  try {
    if (!place && $("g-place").value.trim()) {
      const g = await api(`/api/geocode?q=${encodeURIComponent($("g-place").value.trim())}`);
      place = { lat: g.lat, lon: g.lon, label: g.label };
    }
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const made = await api("/api/gardens", { method: "POST", body: JSON.stringify({
      name: $("g-name").value.trim() || "My garden", timezone: tz,
      ...(place ? { lat: place.lat, lon: place.lon } : {}),
    }) });
    garden = { id: made.garden.id, key: made.key };
    try { localStorage.setItem(STORE, JSON.stringify(garden)); } catch {}
    history.replaceState(null, "", `/g/${garden.id}#k=${garden.key}`);
    status("");
    await refresh();
  } catch (err) { status(err.message); }
});

// ---------- the day ----------

async function refresh() {
  if (!garden) { show("setup"); return; }
  show("setup", false);
  status("Checking your garden's weather…");
  let d;
  try { d = await api(`/api/gardens/${encodeURIComponent(garden.id)}/today`); }
  catch (err) {
    status(err.message);
    if (/No garden at this link/.test(err.message)) { try { localStorage.removeItem(STORE); } catch {} garden = null; show("setup"); }
    return;
  }
  status("");
  const tz = d.garden.timezone;
  $("t-title").textContent = d.garden.name || "Today";
  const when = new Date(`${d.date}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
  $("t-sub").textContent = when;
  show("where-block", !d.garden.cell);

  const urgent = [...(d.urgent || [])];
  const shown = new Set(urgent.map((u) => u.kind));
  const same = { freeze: "frost", frost: "frost", heat: "heat", flood: "flood", wind: "wind" };
  for (const a of d.alerts || []) if (!shown.has(same[a.kind] || a.kind)) urgent.push({ kind: a.kind, title: `NWS ${a.event}`, text: a.headline, source: { title: "National Weather Service", url: "https://www.weather.gov/" } });
  $("urgent").innerHTML = urgent.map((u) => `<div class="alert ${esc(u.kind)}"><strong>${esc(u.title)}</strong><p>${esc(u.text)}</p>${sourceHtml(u.source)}</div>`).join("");
  show("urgent", urgent.length > 0);

  $("tasks").innerHTML = (d.tasks || []).map((t) => `
    <li class="task ${esc(t.urgency)} ${esc(t.status)}" data-id="${esc(t.id)}">
      <div class="task-head"><span class="task-time">${esc(hhmm(t.due_at, tz))}</span> <strong>${esc(t.title)}</strong></div>
      <p class="why">${esc(t.reason)}</p>
      <div class="task-foot">${sourceHtml(t.source)}
        ${t.status === "open" ? `<button type="button" class="textbtn act" data-s="done">Done</button> <button type="button" class="textbtn act" data-s="skipped">Skip</button>`
          : `<span class="small">${t.status === "done" ? "Done" : "Skipped"}</span> <button type="button" class="textbtn act" data-s="open">Undo</button>`}</div>
    </li>`).join("");
  show("tasks-block");
  show("tasks-empty", !(d.tasks || []).length);

  const week = d.season_week || [];
  $("season-week").innerHTML = week.map((it) => `
    <li class="task"><div class="task-head"><strong>${esc(it.action)}</strong> <span class="small">· ${esc(it.plant)}</span></div>
      ${it.why ? `<p class="why">${esc(it.why)}</p>` : ""}
      ${(it.cautions || []).length ? `<ul class="cautions caution-line">${it.cautions.map((c) => `<li>${esc(c.text)}${c.source && c.source.title ? ` ${sourceHtml(c.source)}` : ""}</li>`).join("")}</ul>` : ""}
      <div class="task-foot">${(it.sources || []).map((s) => sourceHtml(s)).join(" · ") || `<span class="src">General practice; no source cited</span>`}</div></li>`).join("");
  $("season-empty").textContent = week.length ? "" : "Nothing from the season plan this week. If you have no plan yet, Leaf can write one from your frost dates and plants.";
  show("season-block");

  $("water").innerHTML = (d.water || []).map((w) => `
    <li data-plant="${esc(w.plant_id)}"><strong>${esc(w.name)}</strong>: ${esc(w.action === "skip" ? "no water needed" : w.action === "water" ? "water today" : "check the soil")}
      <span class="why">${esc(w.reason)}</span>
      <button type="button" class="textbtn log" data-k="watered">I watered it</button>
      ${w.model === "indoor" ? `<button type="button" class="textbtn log" data-k="too_wet">Soil was still wet</button> <button type="button" class="textbtn log" data-k="too_dry">Soil was very dry</button>` : ""}
    </li>`).join("");
  show("water-block", (d.water || []).length > 0);

  await loadPlants();
  show("plants-block");
  $("p-identify").href = `/identify?garden=${encodeURIComponent(garden.id)}#k=${garden.key}`;
  show("notify-block");
  show("link-block");
  $("g-link").value = `${location.origin}/g/${garden.id}#k=${garden.key}`;
  loadNotify();
}

$("tasks").addEventListener("click", async (e) => {
  const b = e.target.closest("button.act");
  if (!b) return;
  const id = b.closest("li").dataset.id;
  b.disabled = true;
  try { await api(`/api/gardens/${encodeURIComponent(garden.id)}/tasks/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ status: b.dataset.s }) }); await refresh(); }
  catch (err) { status(err.message); b.disabled = false; }
});

$("water").addEventListener("click", async (e) => {
  const b = e.target.closest("button.log");
  if (!b) return;
  const pid = b.closest("li").dataset.plant;
  b.disabled = true;
  try { await api(`/api/gardens/${encodeURIComponent(garden.id)}/plants/${encodeURIComponent(pid)}/log`, { method: "POST", body: JSON.stringify({ kind: b.dataset.k }) }); await refresh(); }
  catch (err) { status(err.message); b.disabled = false; }
});

// ---------- plants ----------

async function loadPlants() {
  const g = await api(`/api/gardens/${encodeURIComponent(garden.id)}`);
  $("plant-list").innerHTML = g.plants.map((p) => `<li data-id="${esc(p.id)}"><strong>${esc(p.nickname || p.common_name || p.species)}</strong>${p.species ? ` <em>${esc(p.species)}</em>` : ""} <span class="small">${esc(p.setting)}${p.container && p.container.pot_litres ? `, ${esc(p.container.pot_litres)} L pot` : ""}</span> <button type="button" class="textbtn rm">Remove</button></li>`).join("");
}

$("plant-list").addEventListener("click", async (e) => {
  const b = e.target.closest("button.rm");
  if (!b) return;
  try { await api(`/api/gardens/${encodeURIComponent(garden.id)}/plants/${encodeURIComponent(b.closest("li").dataset.id)}`, { method: "DELETE" }); await refresh(); }
  catch (err) { status(err.message); }
});

$("plant-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pot = parseFloat($("p-pot").value);
  const body = { common_name: $("p-name").value.trim(), setting: $("p-setting").value, stage: $("p-stage").value,
    container: pot > 0 ? { pot_litres: pot } : {} };
  try { await api(`/api/gardens/${encodeURIComponent(garden.id)}/plants`, { method: "POST", body: JSON.stringify(body) }); $("p-name").value = ""; $("p-pot").value = ""; await refresh(); }
  catch (err) { status(err.message); }
});

// ---------- notifications ----------

const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

function keyBytes(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

let vapid = { public_key: "", enabled: false };

async function loadNotify() {
  try { vapid = await api("/api/push/key"); } catch {}
  const s = await api(`/api/gardens/${encodeURIComponent(garden.id)}/notifications`);
  $("q-start").value = s.quiet_start; $("q-end").value = s.quiet_end;
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  show("ios-hint", isIOS && !standalone);
  if (!supported) { $("n-state").textContent = isIOS && !standalone ? "" : "This browser cannot show notifications."; $("n-enable").hidden = true; return; }
  const reg = await navigator.serviceWorker.getRegistration("/");
  const sub = reg && await reg.pushManager.getSubscription();
  const on = Boolean(sub) && s.channels.includes("push") && Notification.permission === "granted";
  $("n-enable").hidden = on;
  $("n-test").hidden = !on;
  $("n-state").textContent = on ? "Notifications are on for this device." : Notification.permission === "denied" ? "Notifications are blocked in this browser's settings." : "";
}

$("n-enable").addEventListener("click", async () => {
  // Called from a tap: iPhone only allows the permission prompt from a user gesture, so it is
  // asked first, before any network call (the push key was fetched when the page loaded).
  try {
    const perm = await Notification.requestPermission();
    const key = vapid.public_key;
    if (!vapid.enabled || !key) { $("n-state").textContent = "Notifications are not available on this server yet."; return; }
    if (perm !== "granted") { $("n-state").textContent = "Leaf needs your permission to send notifications."; return; }
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
    await api(`/api/gardens/${encodeURIComponent(garden.id)}/push`, { method: "POST", body: JSON.stringify(sub.toJSON()) });
    await loadNotify();
  } catch (err) { $("n-state").textContent = err.message || "Could not turn on notifications."; }
});

$("n-test").addEventListener("click", async () => {
  try { const r = await api(`/api/gardens/${encodeURIComponent(garden.id)}/push/test`, { method: "POST" }); $("n-state").textContent = r.status === "sent" ? "Sent. It should arrive in a few seconds." : `Not sent (${r.status.replace(/_/g, " ")}).`; }
  catch (err) { $("n-state").textContent = err.message; }
});

$("quiet-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const s = await api(`/api/gardens/${encodeURIComponent(garden.id)}/notifications`);
    await api(`/api/gardens/${encodeURIComponent(garden.id)}/notifications`, { method: "PUT", body: JSON.stringify({ ...s, quiet_start: $("q-start").value, quiet_end: $("q-end").value }) });
    $("n-state").textContent = "Quiet hours saved.";
  } catch (err) { $("n-state").textContent = err.message; }
});

$("where-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const g = await api(`/api/geocode?q=${encodeURIComponent($("w-place").value.trim())}`);
    await api(`/api/gardens/${encodeURIComponent(garden.id)}/location`, { method: "POST", body: JSON.stringify({ lat: g.lat, lon: g.lon }) });
    await refresh();
  } catch (err) { status(err.message); }
});

$("g-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("g-link").value); $("g-copy").textContent = "Copied"; } catch { $("g-link").select(); }
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
refresh();
