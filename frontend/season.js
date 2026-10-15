// Season plan: the garden's next twelve months, week by week. Printable, and exportable to a
// calendar. Every model or web string goes through esc() or textContent.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (id) => document.getElementById(id);
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
const STORE = "leaf.garden";
const KIND = { sow_indoors: "Sow indoors", sow: "Sow", transplant: "Plant out", feed: "Feed", prune: "Prune", protect: "Protect", harvest: "Harvest", pest_check: "Pest check", other: "Other" };
const CLASS = { hardy: "hardy", half_hardy: "semi-hardy", tender: "tender", very_tender: "very tender" };

let garden = null;
try { garden = JSON.parse(localStorage.getItem(STORE) || "null"); } catch {}
let polls = 0;
let timer = null;  // one polling loop at most

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Leaf-Key": garden.key, ...(opts.headers || {}) } });
  if (path.endsWith(".ics")) {
    if (!r.ok) throw new Error("The calendar file is not ready yet.");
    return r.blob();
  }
  let d = null;
  try { d = await r.json(); } catch {}
  if (!r.ok) throw new Error((d && d.detail) || "Something went wrong. Please try again.");
  return d;
}

function status(text) { $("s-status").textContent = text || ""; }
function show(id, on = true) { $(id).hidden = !on; }
function day(iso) { return new Date(`${iso}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" }); }
function mmdd(md) { return md ? day(`2001-${md}`) : ""; }
function srcHtml(s) {
  const url = safeUrl(s && s.url);
  return url ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(s.title || "Source")}</a>` : `<span class="src">${esc((s && s.title) || "")}</span>`;
}

function itemHtml(it) {
  const sources = (it.sources || []).length
    ? it.sources.map(srcHtml).join(" · ")
    : `<span class="src">General practice; no source cited</span>`;
  const cautions = (it.cautions || []).map((c) => `<li>${esc(c.text)}${c.source && c.source.title ? ` ${srcHtml(c.source)}` : ""}</li>`).join("");
  return `<li class="task season-item">
    <div class="task-head"><span class="kind">${esc(KIND[it.kind] || "Other")}</span> <strong>${esc(it.action)}</strong> <span class="small">· ${esc(it.plant)}</span></div>
    ${it.why ? `<p class="why">${esc(it.why)}</p>` : ""}
    ${cautions ? `<ul class="cautions caution-line">${cautions}</ul>` : ""}
    <div class="task-foot">${sources}</div>
  </li>`;
}

function render(v) {
  const p = v.plan, pend = v.pending;
  $("s-title").textContent = `Season plan${v.garden.name ? `: ${v.garden.name}` : ""}`;
  show("s-start", !p && (!pend || pend.status === "failed"));
  if (pend && (pend.status === "writing" || pend.status === "unverified")) {
    const what = pend.status === "writing" ? "Nemotron Ultra is writing your plan: first the planting dates, then a month at a time. This takes about a minute and a half, and you can leave this page; the plan will be here when you return…" : "Waiting for Nemotron Super to check your plan…";
    status(p ? `${what} Your current plan stays below until the new one is ready.` : what);
    clearTimeout(timer);
    if (polls++ < 120) timer = setTimeout(load, 5000);
    else status("This is taking longer than usual. Leaf keeps working; check back in a few minutes.");
  } else if (pend && pend.status === "failed") status(pend.note || "Leaf could not write the plan. Try again.");
  else status("");
  const ready = !!p;
  $("s-rewrite").hidden = !!(pend && pend.status !== "failed");  // no second write meanwhile
  show("s-basis", !!ready);
  show("s-changes", !!ready && (v.changes || []).length > 0);
  show("s-removed", !!ready && (p.checks.removed || []).length > 0);
  if (!ready) { $("s-months").innerHTML = ""; return; }

  const f = (p.basis.frost || {});
  const last = f.last_spring_frost || {}, first = f.first_autumn_frost || {};
  const basis = [];
  if (f.enough_data && last.risk_50) basis.push(`Average last spring frost ${esc(mmdd(last.risk_50))}${last.risk_10 ? ` (1 year in 10 after ${esc(mmdd(last.risk_10))})` : ""}${first.risk_50 ? `; average first autumn frost ${esc(mmdd(first.risk_50))}` : ""}. 1991-2020 reanalysis for your area.`);
  else if (f.enough_data) basis.push("Your area has no frost in most years (1991-2020 reanalysis).");
  if (p.basis.zone) basis.push(`USDA hardiness zone ${esc(p.basis.zone)} (estimated).`);
  for (const pl of p.basis.plants || []) basis.push(`${esc(pl.name)}: ${esc(CLASS[pl.class] || pl.class)}${pl.assumed ? " (no source; treated as tender)" : ""}.`);
  const by = (p.checks.written_by || []).join(", ");
  basis.push(`Written by ${esc(by || "Nemotron")}; ${p.checks.verified_by ? `checked by code rules and ${esc(p.checks.verified_by)}` : "checked by code rules"}. Made ${esc(p.basis.made_on || "")}${p.revision ? `, updated ${esc(p.revision)} time${p.revision > 1 ? "s" : ""} since` : ""}.`);
  if ((p.checks.unwritten || []).length) basis.push(`<span class="caution-line">Leaf could not write ${esc(p.checks.unwritten.join(", "))}. Rewrite the plan to try again.</span>`);
  $("s-basis-list").innerHTML = basis.map((b) => `<li>${b}</li>`).join("");

  $("s-changes-list").innerHTML = (v.changes || []).map((c) => {
    const t = c.trigger || {};
    const lines = (c.changes || []).map((w) => `<li>Week of ${esc(day(w.week))}: ${w.added.length ? `added ${esc(w.added.join("; "))}` : ""}${w.added.length && w.removed.length ? "; " : ""}${w.removed.length ? `removed ${esc(w.removed.join("; "))}` : ""}</li>`).join("");
    return `<li><strong>${esc(new Date(c.at * 1000).toLocaleDateString())}</strong>: ${esc(t.text || "")} ${esc(c.summary || "")}${t.source && t.source.title ? ` ${srcHtml(t.source)}` : ""}<ul>${lines}</ul></li>`;
  }).join("");

  const removed = p.checks.removed || [];
  $("s-removed-sum").textContent = `What the checks removed (${removed.length})`;
  $("s-removed-list").innerHTML = removed.map((r) => `<li>${esc(day(r.week))}, ${esc(r.plant)}: ${esc(r.action)}. <span class="why">${esc(r.reason)} (${esc(r.by)})</span></li>`).join("");

  const months = new Map();
  for (const w of v.weeks) {
    const key = w.week.slice(0, 7);
    if (!months.has(key)) months.set(key, []);
    months.get(key).push(w);
  }
  const dormant = new Set(p.checks.dormant || []);
  $("s-months").innerHTML = [...months.entries()].map(([key, weeks]) => {
    const label = new Date(`${key}-15T12:00:00`).toLocaleDateString([], { month: "long", year: "numeric" });
    const enLabel = new Date(`${key}-15T12:00:00`).toLocaleDateString("en-US", { month: "long", year: "numeric" });
    const items = weeks.reduce((n, w) => n + w.items.length, 0);
    const body = items ? weeks.map((w) => `
      <div class="week${w.week === v.this_week ? " this-week" : ""}" id="w-${esc(w.week)}">
        <h3>Week of ${esc(day(w.week))}${w.week === v.this_week ? " <span class=\"chip\">this week</span>" : ""}</h3>
        ${w.changed ? `<p class="small changed">Changed: ${esc(w.changed)}</p>` : ""}
        ${w.items.length ? `<ol class="tasks">${w.items.map(itemHtml).join("")}</ol>` : `<p class="small muted">Nothing planned.</p>`}
      </div>`).join("")
      : `<p class="small muted">${dormant.has(enLabel) ? "A dormant month here: nothing to plan. Leaf's daily care still watches for frost and storms." : "Nothing planned this month."}</p>`;
    return `<section class="block month"><h2>${esc(label)}</h2>${body}</section>`;
  }).join("");
}

async function load() {
  try { render(await api(`/api/gardens/${encodeURIComponent(garden.id)}/season`)); }
  catch (err) { status(err.message); }
}

async function write() {
  status("Starting…");
  polls = 0;
  try { render(await api(`/api/gardens/${encodeURIComponent(garden.id)}/season`, { method: "POST" })); }
  catch (err) { status(err.message); }
}

$("s-write").addEventListener("click", write);
$("s-rewrite").addEventListener("click", () => { if (confirm("Rewrite the whole plan? Leaf writes a garden's plan at most 3 times a day.")) write(); });
$("s-print").addEventListener("click", () => window.print());
$("s-ics").addEventListener("click", async () => {
  try {
    const blob = await api(`/api/gardens/${encodeURIComponent(garden.id)}/season.ics`);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "leaf-season-plan.ics";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (err) { status(err.message); }
});

if (!garden || !garden.id || !garden.key) show("s-none");
else load();
