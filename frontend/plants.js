// My plants: the reports saved on this device, newest first, with check-in status and
// reminders. Report data comes from /api/cases/<id>; everything shown is escaped.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const DAY = 864e5;
const VERDICT = { confident: "Confident", likely: "Likely", lab: "Send a sample to a lab" };
const STATUS = { spreading: "Spreading", stable: "Stable", recovering: "Recovering", unclear: "Unclear" };
const day = (t) => new Date(t).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

// A link from another device: /plants#import=<base64url of [{id, token}]>. Read, then removed
// from the address bar so it is not left in the history.
function importFromHash() {
  const m = location.hash.match(/^#import=([\w-]+)$/);
  if (!m) return 0;
  history.replaceState(null, "", location.pathname);
  try {
    const json = atob(m[1].replace(/-/g, "+").replace(/_/g, "/"));
    const items = JSON.parse(json);
    const have = new Set(LeafRemind.load().map((p) => p.id));
    const fresh = (Array.isArray(items) ? items.slice(0, 60) : [])
      .filter((p) => p && typeof p.i === "string" && /^[\w-]{1,64}$/.test(p.i) && !have.has(p.i));
    if (!fresh.length) return 0;
    if (!confirm(`Add ${fresh.length} plant${fresh.length === 1 ? "" : "s"} from a link to this device?`)) return 0;
    fresh.forEach((p) => LeafRemind.remember({ id: p.i, token: typeof p.k === "string" ? p.k : "", created: 0 }));
    return fresh.length;
  } catch { return 0; }
}

function moveLink(list) {
  const data = JSON.stringify(list.map((p) => ({ i: p.id, k: p.token || "" })));
  const b64 = btoa(data).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${location.origin}/plants#import=${b64}`;
}

function due(created, days) {
  const at = LeafRemind.checkInDate(created, days);
  const left = Math.ceil((at - Date.now()) / DAY);
  const text = left > 1 ? `Check in ${day(at)} (in ${left} days)` : left === 1 ? "Check in tomorrow" : left === 0 ? "Check in today" : `Check-in was due ${day(at)}`;
  return { at, text, overdue: left < 0, soon: left <= 0 };
}

function card(p, view) {
  const r = view.report || {};
  const top = (r.differential && r.differential[0]) || (r.diagnosis && r.diagnosis.candidates && r.diagnosis.candidates[0]) || {};
  const name = top.name || p.title || "Your plant";
  const last = (view.checkins || []).slice(-1)[0];
  const photo = last && last.photo ? last.photo : (view.photos || [])[0];
  const created = (view.created || 0) * 1000;
  const lastAt = last ? last.at * 1000 : created;
  const days = (r.plan && r.plan.check_in_days) || p.checkin_days || 5;
  const d = due(lastAt, days);
  const expires = Math.max(0, Math.ceil(((view.expires || 0) * 1000 - Date.now()) / DAY));
  const path = `/c/${encodeURIComponent(p.id)}`;
  const open = p.token ? `${path}#k=${encodeURIComponent(p.token)}` : path;
  const reminder = {
    title: `Leaf check-in: ${name}`, start: d.at, end: new Date(d.at.getTime() + 15 * 60000),
    details: "Take a new photo of the same leaves and add it to your report.", path,
  };
  const li = document.createElement("li");
  li.className = `plant${d.soon ? " due" : ""}`;
  li.innerHTML = `
    ${photo ? `<img class="plant-photo" alt="" src="data:image/jpeg;base64,${esc(photo)}">` : `<span class="plant-photo empty" aria-hidden="true"></span>`}
    <div class="plant-body">
      <h2 class="plant-name">${esc(name)}</h2>
      <p class="plant-meta small">${esc(VERDICT[r.verdict] || "Saved")}${last ? `. Last check-in: ${esc(STATUS[last.status] || "Unclear")}` : ""}. Saved ${esc(day(created))}, kept ${expires} more day${expires === 1 ? "" : "s"}.</p>
      <p class="plant-due${d.overdue ? " late" : ""}">${esc(d.text)}</p>
      <p class="plant-actions small">
        <a href="${esc(open)}">${p.token ? "Open and check in" : "Open the report"}</a>
        <a href="${esc(LeafRemind.google(reminder))}" target="_blank" rel="noopener">Google Calendar</a>
        <button type="button" class="textbtn ics">Apple or Outlook (.ics)</button>
        <button type="button" class="textbtn forget">Remove</button>
      </p>
    </div>`;
  li.querySelector(".ics").addEventListener("click", (e) => LeafRemind.ics(reminder).catch((err) => { e.target.textContent = err.message; }));
  li.querySelector(".forget").addEventListener("click", () => { LeafRemind.forget(p.id); li.remove(); refreshEmpty(); });
  return { li, soon: d.soon, at: d.at };
}

function refreshEmpty() {
  const n = $("plants").children.length;
  $("plants-empty").hidden = n > 0;
  $("move").hidden = n === 0;
}

async function main() {
  const imported = importFromHash();
  const list = LeafRemind.load();
  if (imported) $("plants-status").textContent = `Added ${imported} plant${imported === 1 ? "" : "s"} from your other device.`;
  if (!list.length) { refreshEmpty(); return; }
  $("plants-status").textContent ||= "Loading your plants…";
  const results = await Promise.all(list.map(async (p) => {
    try {
      const r = await fetch(`/api/cases/${encodeURIComponent(p.id)}`);
      if (r.status === 404) return { p, gone: true };
      if (!r.ok) return { p, error: true };
      return { p, view: await r.json() };
    } catch { return { p, error: true }; }
  }));
  results.filter((x) => x.gone).forEach((x) => LeafRemind.forget(x.p.id)); // expired: gone for good
  const cards = results.filter((x) => x.view).map((x) => card(x.p, x.view));
  cards.sort((a, b) => (b.soon - a.soon) || (a.at - b.at)); // due first, then soonest
  cards.forEach((c) => $("plants").appendChild(c.li));
  const failed = results.filter((x) => x.error).length;
  const gone = results.filter((x) => x.gone).length;
  const notes = [];
  if (imported) notes.push(`Added ${imported} from your other device.`);
  if (gone) notes.push(`${gone} report${gone === 1 ? " has" : "s have"} expired and ${gone === 1 ? "was" : "were"} removed.`);
  if (failed) notes.push(`${failed} could not load right now; try again in a minute.`);
  $("plants-status").textContent = notes.join(" ");
  refreshEmpty();
  $("move-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(moveLink(LeafRemind.load())); $("move-copy").textContent = "Copied. Open it on your other device."; }
    catch { $("move-copy").textContent = "Copy failed in this browser."; }
  });
}

// A move link opened in a tab that is already on this page only changes the hash.
window.addEventListener("hashchange", () => { if (location.hash.startsWith("#import=")) location.reload(); });
main();
