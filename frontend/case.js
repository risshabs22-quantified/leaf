// Leaf report page: read-only view of a saved report, plus check-ins. Everything is escaped.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => `${Math.round((p || 0) * 100)}%`;
const caseId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
const STATUS = { spreading: "Spreading", stable: "Stable", recovering: "Recovering", unclear: "Unclear" };
let photo = null;
// The owner token (from the creator's link fragment, or saved earlier) allows check-ins.
const fromHash = new URLSearchParams(location.hash.slice(1)).get("k");
let owner = fromHash;
try {
  if (fromHash) localStorage.setItem(`leaf-owner-${caseId}`, fromHash);
  owner = owner || localStorage.getItem(`leaf-owner-${caseId}`);
} catch { /* private mode: only the fragment works */ }
if (fromHash) history.replaceState(null, "", location.pathname);  // keep the token off screen

async function load() {
  const r = await fetch(`/api/cases/${encodeURIComponent(caseId)}`);
  if (!r.ok) { $("loading").textContent = "This report has expired or does not exist."; return; }
  draw(await r.json());
}

const PET = { low: "Low risk to pets", caution: "Keep pets off until dry", high: "Dangerous to pets" };
function stepHtml(s) {
  const tags = [];
  if (s.organic === true) tags.push(["ok", "Organic"]);
  if (s.organic === false) tags.push(["", "Not organic"]);
  if (s.pet_risk) tags.push([s.pet_risk === "high" ? "bad" : s.pet_risk === "caution" ? "warn" : "ok", PET[s.pet_risk]]);
  if (s.bee_risk) tags.push(["warn", "Harms bees: spray at dusk"]);
  const tagHtml = tags.length ? `<span class="tags">${(s.actives || []).length ? `<span class="tag plain">${esc(s.actives.join(", "))}</span>` : ""}${tags.map(([c, t]) => `<span class="tag ${c}">${esc(t)}</span>`).join("")}</span>` : "";
  return `<li>${esc(s.action)}${s.source_ids.map((id) => `<a class="cite" href="#src${id}">[${id}]</a>`).join("")}${s.why ? `<span class="why">${esc(s.why)}</span>` : ""}${tagHtml}${(s.precautions || []).map((p) => `<span class="why caution-line">${esc(p)}</span>`).join("")}</li>`;
}

function draw(v) {
  const d = v.report;
  $("loading").hidden = true;
  $("report").hidden = false;
  const diff = d.differential && d.differential.length ? d.differential.map((h) => ({ name: h.name, p: h.probability }))
    : d.diagnosis.candidates.map((c) => ({ name: c.name, p: c.confidence }));
  $("title").textContent = diff.length ? `Most likely ${diff[0].name}` : "Plant report";
  const created = new Date(v.created * 1000);
  $("meta").textContent = `${v.mode === "deep" ? "Deep" : "Quick"} diagnosis from ${created.toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" })}.`;
  $("expires").textContent = `on ${new Date(v.expires * 1000).toLocaleDateString(undefined, { month: "long", day: "numeric" })}`;
  $("photos").innerHTML = v.photos.map((p, i) => `<figure class="slot"><img src="data:image/jpeg;base64,${p}" alt="${esc(v.labels[i] || "photo")}"><figcaption class="slot-text">${esc(v.labels[i] || "photo")}</figcaption></figure>`).join("");
  const verdict = d.verdict || (d.plan.escalate ? "lab" : "likely");
  const word = { confident: "Confident", likely: "Likely", lab: "Send a sample to a lab" }[verdict];
  const banner = $("verdict");
  banner.className = verdict === "lab" ? "banner warn" : "banner ok";
  banner.textContent = `${word}. ${d.verdict_reason || d.plan.escalate_reason || ""} ${d.notice || ""}`.trim();
  $("summary").textContent = d.summary || d.diagnosis.reasoning_summary || "";
  $("candidates").innerHTML = diff.map((h) => `<li class="cand"><div class="cand-head"><span class="cand-name">${esc(h.name)}</span><span>${pct(h.p)}</span></div><div class="bar"><span style="width:${Math.round(h.p * 100)}%"></span></div></li>`).join("");
  $("plan-summary").textContent = d.plan.summary;
  $("steps").innerHTML = d.plan.steps.map(stepHtml).join("");
  $("cautions").innerHTML = (d.plan.cautions || []).map((c) => `<li>${esc(c)}</li>`).join("");
  $("cautions").hidden = !(d.plan.cautions || []).length;
  $("removed").innerHTML = (d.plan.removed || []).length
    ? `<summary>Leaf removed ${d.plan.removed.length} step${d.plan.removed.length === 1 ? "" : "s"} for this garden's preferences</summary><ul class="small">${d.plan.removed.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
  $("removed").hidden = !(d.plan.removed || []).length;
  $("sources").innerHTML = d.sources.map((s) => `<li id="src${s.id}"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a></li>`).join("") || `<li class="muted">No web sources.</li>`;
  $("no-checkins").hidden = v.checkins.length > 0;
  $("checkin-form").hidden = !owner;
  if (!owner) $("no-checkins").textContent = v.checkins.length ? "" : "No check-ins yet. Only the person who created this report can add them.";
  $("checkins").innerHTML = v.checkins.map((c) => {
    const when = new Date(c.at * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const steps = c.steps.map(stepHtml).join("");
    const notes = [...(c.cautions || []), ...(c.removed || []).map((r) => `Removed for this garden: ${r}`)];
    return `<li class="${c.escalate ? "note" : "done"}"><span class="l-what">${esc(when)}: ${esc(STATUS[c.status] || c.status)}</span><span class="l-got">${esc(c.change)}</span><span class="l-got">${esc(c.advice)}</span>${steps ? `<ol class="steps small">${steps}</ol>` : ""}${notes.map((n) => `<span class="why caution-line">${esc(n)}</span>`).join("")}${c.escalate ? `<span class="step-flag">${esc(c.escalate_reason)}</span>` : ""}</li>`;
  }).join("");
}

const slot = document.querySelector("#checkin-form .slot");
$("checkin-photo").addEventListener("change", (e) => {
  photo = e.target.files[0] || null;
  if (!photo) return;
  const img = slot.querySelector("img");
  img.src = URL.createObjectURL(photo); img.alt = "Your check-in photo"; img.hidden = false;
});

$("checkin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!photo) { $("checkin-status").textContent = "Choose a new photo first."; return; }
  const fd = new FormData();
  fd.append("image", photo);
  fd.append("notes", $("checkin-notes").value.trim());
  $("checkin-status").textContent = "Comparing with your last photo…";
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try {
    const r = await fetch(`/api/cases/${encodeURIComponent(caseId)}/checkin`, { method: "POST", body: fd, headers: { "X-Leaf-Owner": owner || "" } });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `Something went wrong (${r.status}).`);
    draw(body);
    $("checkin-status").textContent = "Check-in saved.";
  } catch (err) {
    $("checkin-status").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

load();
