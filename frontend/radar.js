// Leaf Radar: a state's current pest and disease reports from extension services. Every report
// shows the page's own sentence and link. Server strings go through textContent or esc().
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (id) => document.getElementById(id);
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
const day = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
const NAMES = {
  AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
};
let garden = null;
try { garden = JSON.parse(localStorage.getItem("leaf.garden") || "null"); } catch {}

async function getJson(path, withKey) {
  const r = await fetch(path, withKey && garden ? { headers: { "X-Leaf-Key": garden.key } } : {});
  let d = null;
  try { d = await r.json(); } catch {}
  if (!r.ok) throw new Error((d && d.detail) || "Something went wrong. Please try again.");
  return d;
}

function show(v) {
  const name = NAMES[v.region] || v.region;
  const run = v.last_run;
  $("r-run").textContent = run
    ? `${run.status === "done" ? "Checked" : run.status === "skipped" ? "Skipped" : "Tried"} ${new Date(run.checked_at * 1000).toLocaleDateString([], { day: "numeric", month: "short" })} (week ${run.week.split("-W")[1]}): ${run.searched} pages found, ${run.read} read, ${run.relevant} reported something current, ${run.reports} report${run.reports === 1 ? "" : "s"} kept${run.undated ? `, ${run.undated} with no date skipped` : ""}.${run.note ? ` ${run.note}` : ""}`
    : `No current report for ${name}. Radar checks states each week and keeps a report only when a university page dates a sighting; it shows nothing rather than guess.`;
  $("r-list").innerHTML = v.reports.map((x) => {
    const url = safeUrl(x.source_url);
    return `<li><strong>${esc(x.pest)}</strong>${x.hosts.length ? ` on ${esc(x.hosts.join(", "))}` : ""}${x.place ? `, ${esc(x.place)}` : ""}
      <span class="small">· ${esc(x.status)} · ${esc(day(x.reported_on))}</span>
      <blockquote>${esc(x.quote)}</blockquote>
      ${url ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(x.source_title)}</a>` : `<span class="src">${esc(x.source_title)}</span>`}</li>`;
  }).join("");
  const none = !v.reports.length;
  $("r-empty").hidden = !none;
  if (none) $("r-empty").textContent = run
    ? `Nothing current for ${name}. Leaf looked and found no recent page that reports a specific pest or disease being seen; it shows nothing rather than guess.`
    : "";
  $("r-how").textContent = v.how;
  $("r-unverified").textContent = v.verified_sources === false ? `Leaf could not open ${name}'s extension site to check it when Radar was built; if it is empty, that may be why.` : "";
}

async function load(region) {
  $("r-status").textContent = "Loading…";
  try { show(await getJson(`/api/radar?region=${encodeURIComponent(region)}`)); $("r-status").textContent = ""; }
  catch (err) { $("r-status").textContent = err.message; }
}

async function start() {
  let list = { regions: [], covered: Object.keys(NAMES) };
  try { list = await getJson("/api/radar/regions"); } catch (err) { $("r-status").textContent = err.message; return; }
  const ran = new Map(list.regions.map((r) => [r.region, r]));
  $("r-state").innerHTML = list.covered.map((c) => `<option value="${esc(c)}">${esc(NAMES[c] || c)}${ran.has(c) ? ` (${ran.get(c).reports} report${ran.get(c).reports === 1 ? "" : "s"})` : ""}</option>`).join("");
  let pick = null;
  if (garden && garden.id) {
    try { const g = await getJson(`/api/gardens/${encodeURIComponent(garden.id)}/radar`, true); if (g.region) pick = g.region; } catch {}
  }
  if (!pick) pick = [...list.regions].sort((a, b) => b.reports - a.reports)[0]?.region || "MN";
  $("r-state").value = list.covered.includes(pick) ? pick : list.covered[0];
  $("r-state").addEventListener("change", () => load($("r-state").value));
  await load($("r-state").value);
}
start();
