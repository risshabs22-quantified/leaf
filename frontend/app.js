// Leaf frontend: no framework, no build step. Everything model-generated is escaped.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let photo = null;      // File the user picked
let last = null;       // last DiagnoseResponse, used for chat and re-runs
let history = [];      // chat turns

const STEPS = ["Looking closely at the leaf…", "Nemotron Ultra is weighing likely causes…",
  "Searching extension guides…", "Nemotron Lightning is writing your plan…"];

// Hero graph: one node per pipeline step, in the same order as STEPS.
const NODES = ["vision", "diagnose", "search", "plan"];
const nodeEl = (n) => document.querySelector(`[data-node="${n}"]`);
const timeEl = (n) => document.querySelector(`[data-time="${n}"]`);

function setGraphState(kind, label) {
  const g = $("graph-state");
  g.className = `run-state ${kind}`.trim();
  g.textContent = label;
}

function runGraph(active, hasPhoto) {
  NODES.forEach((n, k) => { nodeEl(n).className = "node" + (k < active ? " done" : k === active ? " active" : ""); });
  if (!hasPhoto) nodeEl("vision").className = "node skipped";
}

function resetGraph() {
  $("run").hidden = false;
  NODES.forEach((n) => { nodeEl(n).className = "node"; timeEl(n).textContent = ""; });
}

// Settle the graph from the real trace: model, latency and failures come from the backend.
function finishGraph(d) {
  const byStep = Object.fromEntries(d.trace.map((t) => [t.step, t]));
  NODES.forEach((n) => {
    const el = nodeEl(n), t = byStep[n];
    timeEl(n).textContent = t && t.latency_ms ? (t.latency_ms / 1000).toFixed(1) + "s" : "";
    if (n === "search") el.className = "node " + (d.sources.length ? "done" : "skipped");
    else if (!t) el.className = "node skipped";
    else el.className = "node " + (t.note && t.note.startsWith("failed") ? "failed" : "done");
    if (t && n !== "search") el.querySelector(".node-model").textContent = t.model;
  });
  setGraphState("done", "Done");
}

// Show the real model IDs from the backend instead of hard-coding them in the page.
fetch("/api/health").then((r) => r.json()).then((h) => {
  const m = h.models || {};
  const set = (n, text) => { if (text) document.querySelector(`[data-model="${n}"]`).textContent = text; };
  set("vision", m.vision); set("diagnose", m.reason); set("plan", m.fast);
  set("search", h.search === "TavilySearcher" ? "Tavily search" : "Search unavailable");
}).catch(() => {});

function setPhoto(file) {
  if (!file || !file.type.startsWith("image/")) return;
  photo = file;
  const img = $("preview");
  img.src = URL.createObjectURL(file);
  img.hidden = false;
  $("drop-text").innerHTML = '<span class="link">Choose a different photo</span>'; // static markup, no user text
}

// Example phrases in the intro fill the notes field so a first-time visitor can try Leaf without a photo.
document.querySelectorAll(".try").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  const n = $("notes");
  n.value = a.dataset.try;
  n.focus();
  n.scrollIntoView({ behavior: "smooth", block: "center" });
}));

$("file").addEventListener("change", (e) => setPhoto(e.target.files[0]));
const drop = $("drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => setPhoto(e.dataTransfer.files[0]));

async function diagnose(answers = "") {
  const notes = $("notes").value.trim();
  if (!photo && !notes) { $("status").textContent = "Add a photo or describe what you see."; return; }
  const fd = new FormData();
  if (photo) fd.append("image", photo);
  fd.append("notes", notes);
  fd.append("region", $("region").value.trim());
  fd.append("answers", answers);

  $("go").disabled = true; $("rerun").disabled = true;
  let i = photo ? 0 : 1; // no photo: the vision step is skipped
  resetGraph(); setGraphState("running", "Running"); runGraph(i, !!photo);
  $("status").textContent = STEPS[i];
  const timer = setInterval(() => {
    i = Math.min(i + 1, STEPS.length - 1);
    $("status").textContent = STEPS[i]; runGraph(i, !!photo);
  }, 6000);
  try {
    const r = await fetch("/api/diagnose", { method: "POST", body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `Something went wrong (${r.status}).`);
    last = body; history = [];
    render(body);
    finishGraph(body);
    $("status").textContent = "";
  } catch (err) {
    $("status").textContent = err.message;
    $("run").hidden = true;
  } finally {
    clearInterval(timer);
    $("go").disabled = false; $("rerun").disabled = false;
  }
}

function render(d) {
  $("results").hidden = false;
  const top = d.diagnosis.candidates[0];
  const banner = $("banner");
  if (d.plan.escalate) {
    banner.className = "banner warn";
    banner.textContent = d.plan.escalate_reason || "Leaf isn't sure. Please check with your local extension office.";
  } else {
    banner.className = "banner ok";
    banner.textContent = top ? `Most likely: ${top.name} (${Math.round(top.confidence * 100)}% confidence)` : "Diagnosis ready.";
  }

  $("summary").textContent = d.diagnosis.reasoning_summary;
  $("candidates").innerHTML = d.diagnosis.candidates.map((c) => `
    <li class="cand">
      <div class="cand-head"><span class="cand-name">${esc(c.name)}<span class="chip">${esc(c.category)}</span></span>
      <span>${Math.round(c.confidence * 100)}%</span></div>
      <div class="bar" role="img" aria-label="${Math.round(c.confidence * 100)} percent"><span style="width:${Math.round(c.confidence * 100)}%"></span></div>
      ${c.evidence_for.length ? `<ul class="evidence">${c.evidence_for.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    </li>`).join("");

  const qs = d.diagnosis.follow_up_questions || [];
  $("followups").hidden = qs.length === 0;
  $("questions").innerHTML = qs.map((q, n) => `
    <label class="q field">${esc(q)}<input data-q="${esc(q)}" id="ans${n}" maxlength="400"></label>`).join("");

  $("plan-summary").textContent = d.plan.summary;
  $("steps").innerHTML = d.plan.steps.map((s) => `
    <li>${esc(s.action)}${s.source_ids.map((id) => `<a class="cite" href="#src${id}">[${id}]</a>`).join("")}
    ${s.why ? `<span class="why">${esc(s.why)}</span>` : ""}</li>`).join("");
  const due = new Date(Date.now() + d.plan.check_in_days * 864e5);
  $("checkin").textContent = `Check in: send a new photo around ${due.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}.`;

  $("sources").innerHTML = d.sources.length
    ? d.sources.map((s) => `<li id="src${s.id}"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a></li>`).join("")
    : `<li class="muted">No web sources this time.</li>`;

  $("tracebody").innerHTML = d.trace.map((t) => `
    <tr><td>${esc(t.step)}${t.note ? ` <span class="muted small">(${esc(t.note)})</span>` : ""}</td>
    <td class="model">${esc(t.model)}</td><td>${t.latency_ms ? (t.latency_ms / 1000).toFixed(1) + "s" : "–"}</td>
    <td>${t.prompt_tokens ?? "–"} / ${t.completion_tokens ?? "–"}</td></tr>`).join("");
  const o = d.observation;
  $("obs").textContent = `Seen in the photo: ${o.plant_guess}; ${o.symptoms.join("; ") || "no symptoms listed"}. Image quality: ${o.image_quality}.`;
  $("chatlog").innerHTML = "";
  $("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("go").addEventListener("click", () => diagnose());
$("rerun").addEventListener("click", () => {
  const answers = [...document.querySelectorAll("#questions input")]
    .filter((i) => i.value.trim()).map((i) => `Q: ${i.dataset.q} A: ${i.value.trim()}`).join("\n");
  diagnose(answers);
});

$("chatform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("chatin").value.trim();
  if (!msg || !last) return;
  $("chatin").value = "";
  addMsg("user", msg);
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ context: last, history, message: msg }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || "Leaf couldn't answer just now.");
    history.push({ role: "user", content: msg }, { role: "assistant", content: body.reply });
    addMsg("bot", body.reply);
  } catch (err) { addMsg("bot", err.message); }
});

function addMsg(who, text) {
  const div = document.createElement("div");
  div.className = `msg ${who}`;
  div.textContent = text;
  $("chatlog").appendChild(div);
}
