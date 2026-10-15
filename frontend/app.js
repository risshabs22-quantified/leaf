// Leaf frontend: no framework, no build step. Everything model- or web-generated is escaped
// (esc() or textContent), never inserted as raw HTML.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (p) => `${Math.round((p || 0) * 100)}%`;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

let last = null;        // last result, for chat and answers
let history = [];       // chat turns
let caseId = null;      // Deep case waiting for answers
let coords = null;      // browser location, rounded to 1 decimal before it leaves the page
const slots = [...document.querySelectorAll(".slot")];
const photoFiles = slots.map(() => null);

// ------------------------------------------------------------------ inputs

slots.forEach((slot, i) => {
  const input = slot.querySelector("input");
  const img = slot.querySelector("img");
  const set = (file) => {
    if (!file || !file.type.startsWith("image/")) return;
    photoFiles[i] = file;
    img.src = URL.createObjectURL(file);
    img.alt = `Your ${slot.dataset.label} photo`;
    img.hidden = false;
    slot.querySelector(".slot-text").innerHTML = `<span class="link">Change</span> ${esc(slot.dataset.label)}`;
  };
  input.addEventListener("change", (e) => set(e.target.files[0]));
  ["dragenter", "dragover"].forEach((ev) => slot.addEventListener(ev, (e) => { e.preventDefault(); slot.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => slot.addEventListener(ev, (e) => { e.preventDefault(); slot.classList.remove("over"); }));
  slot.addEventListener("drop", (e) => set(e.dataTransfer.files[0]));
});

document.querySelectorAll(".try").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  const n = $("notes");
  n.value = a.dataset.try;
  n.focus();
  n.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
}));

$("locate").addEventListener("click", () => {
  if (!navigator.geolocation) { $("locate-status").textContent = "This browser cannot share a location."; return; }
  $("locate").disabled = true;
  $("locate-status").textContent = "Asking your browser…";
  navigator.geolocation.getCurrentPosition((pos) => {
    // Rounded to about 10 km before it is ever sent.
    coords = { lat: Math.round(pos.coords.latitude * 10) / 10, lon: Math.round(pos.coords.longitude * 10) / 10 };
    $("locate-status").textContent = "Using your approximate location.";
    $("locate").disabled = false;
  }, () => {
    $("locate-status").textContent = "Location not shared. Type a city instead.";
    $("locate").disabled = false;
  }, { maximumAge: 600000, timeout: 10000 });
});

const mode = () => document.querySelector('input[name="mode"]:checked').value;

// ------------------------------------------------------------------ Quick progress (four steps)

const NODES = ["vision", "diagnose", "search", "plan"];
const STEPS = ["Looking closely at the leaf…", "Nemotron Ultra is weighing likely causes…",
  "Searching extension guides…", "Nemotron Lightning is writing your plan…"];
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

fetch("/api/health").then((r) => r.json()).then((h) => {
  const m = h.models || {};
  const set = (n, text) => { if (text) document.querySelector(`[data-model="${n}"]`).textContent = text; };
  set("vision", m.vision); set("diagnose", m.reason); set("plan", m.fast);
  set("search", h.search === "TavilySearcher" ? "Tavily search" : "Search unavailable");
  if (h.deep) {
    const left = h.deep.left_today;
    $("deep-left").textContent = left > 0 ? `Investigations left today (Deep, and Auto when it looks further): ${left}.` : "Today's investigations are used up; Auto and Quick still give a quick diagnosis.";
    $("deep-left").hidden = false;
  }
}).catch(() => {});

// ------------------------------------------------------------------ run a case

function formData() {
  const fd = new FormData();
  if ($("organic").checked) fd.append("organic", "true");
  if ($("pets").checked) fd.append("pets", "true");
  const chosen = photoFiles.map((f, i) => [f, slots[i].dataset.label]).filter(([f]) => f);
  return { fd, chosen };
}

function busy(on) {
  $("go").disabled = on; $("rerun").disabled = on;
}

async function diagnose() {
  const notes = $("notes").value.trim();
  const { fd, chosen } = formData();
  if (!chosen.length && !notes) { $("status").textContent = "Add a photo or describe what you see."; return; }
  busy(true);
  $("results").hidden = true;
  try {
    if (mode() === "deep" || mode() === "auto") {
      chosen.forEach(([f, label]) => { fd.append("images", f); fd.append("labels", label); });
      fd.append("notes", notes);
      fd.append("region", $("region").value.trim());
      if (coords) { fd.append("lat", coords.lat); fd.append("lon", coords.lon); }
      await runDeep(`/api/${mode()}/stream`, { method: "POST", body: fd });
    } else {
      if (chosen.length) fd.append("image", chosen[0][0]);
      fd.append("notes", notes);
      fd.append("region", $("region").value.trim());
      await runQuick(fd, !!chosen.length);
    }
  } catch (err) {
    $("status").textContent = err.message;
  } finally {
    busy(false);
  }
}

async function runQuick(fd, hasPhoto, answers = "") {
  $("live").hidden = true;
  if (answers) fd.append("answers", answers);
  let i = hasPhoto ? 0 : 1;
  resetGraph(); setGraphState("running", "Running"); runGraph(i, hasPhoto);
  $("status").textContent = STEPS[i];
  const timer = setInterval(() => { i = Math.min(i + 1, STEPS.length - 1); $("status").textContent = STEPS[i]; runGraph(i, hasPhoto); }, 6000);
  try {
    const r = await fetch("/api/diagnose", { method: "POST", body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `Something went wrong (${r.status}).`);
    finishGraph(body);
    $("status").textContent = "";
    render(body);
  } catch (err) {
    $("run").hidden = true;
    throw err;
  } finally {
    clearInterval(timer);
  }
}

// ------------------------------------------------------------------ Deep: streamed events

const TOOL_LABEL = {
  look_closer: "Looking closer at a photo",
  get_conditions: "Checking your last 14 days of weather",
  search_sources: "Searching extension guides",
  read_source: "Reading a source",
  check_alerts: "Checking recent outbreak news",
  ask_user: "Asking you",
  finish: "Reaching a diagnosis",
};

async function runDeep(url, init) {
  $("run").hidden = true;
  $("live").hidden = false;
  $("log").innerHTML = "";
  delete pending.quick;
  $("differential").innerHTML = "";
  $("status").textContent = "Leaf is taking a first look…";
  const r = await fetch(url, init);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `Something went wrong (${r.status}).`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", result = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, cut); buf = buf.slice(cut + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue; // keep-alive comments
      const ev = JSON.parse(line.slice(6));
      if (ev.type === "result") result = ev.response;
      else if (ev.type === "error") throw new Error(ev.detail || "Something went wrong.");
      else onEvent(ev);
    }
  }
  if (!result) throw new Error("The connection closed before Leaf finished. Please try again.");
  $("status").textContent = "";
  render(result);
}

function logItem(cls, what, why = "", got = "") {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<span class="l-what">${esc(what)}</span>${why ? `<span class="l-why">${esc(why)}</span>` : ""}<span class="l-got">${esc(got)}</span>`;
  $("log").appendChild(li);
  return li;
}

const pending = {};
function onEvent(ev) {
  switch (ev.type) {
    case "quick_start":
      pending.quick = logItem("running", "Quick diagnosis", "One careful look at the first photo.");
      $("status").textContent = "Leaf is taking a quick look…";
      break;
    case "auto":
      if (pending.quick) { pending.quick.className = "done"; pending.quick.querySelector(".l-got").textContent = "Done."; }
      logItem(ev.investigate ? "note" : "done", ev.investigate ? "Leaf decided to investigate" : "No investigation needed", "", ev.reason);
      $("status").textContent = ev.investigate ? "Leaf is working the case…" : "";
      break;
    case "first_look":
      if (ev.photo_id === "P1" && pending.quick) break; // Quick already looked at this photo
      logItem("done", "Took a first look at the photo", "", (ev.observation.symptoms || []).slice(0, 2).join("; "));
      $("status").textContent = "Leaf is working the case…";
      break;
    case "differential":
      drawDifferential(ev.hypotheses || []);
      break;
    case "step":
      if (ev.tool === "update_differential") break;
      pending[ev.n] = logItem("running", TOOL_LABEL[ev.tool] || ev.tool, ev.reason);
      break;
    case "step_done": {
      const li = pending[ev.n];
      if (!li) break;
      li.className = ev.ok ? "done" : "skipped";
      li.querySelector(".l-got").textContent = ev.ok ? ev.summary : `Skipped: ${ev.skipped}`;
      break;
    }
    case "limit":
      logItem("note", "Wrapping up", "", `The ${ev.limit} is used, so Leaf finishes with what it has.`);
      break;
    case "questions":
      logItem("note", "Leaf has a question for you");
      break;
    case "critic":
      logItem("done", "Got a second opinion", "", ev.opinion ? (ev.opinion.agrees ? "It agrees." : `It argues for ${ev.opinion.alternative}.`) : "Unavailable this time.");
      $("status").textContent = "Writing your care plan and checking it against the sources…";
      break;
    case "fallback":
      logItem("note", "Switching to a Quick diagnosis", "", ev.reason);
      break;
    default:
      break;
  }
}

// The differential re-ranks live: FLIP (first, last, invert, play) so rows slide into place.
function drawDifferential(hyps) {
  const list = $("differential");
  const before = new Map([...list.children].map((li) => [li.dataset.key, li.getBoundingClientRect().top]));
  list.innerHTML = hyps.slice(0, 5).map((h) => {
    const second = /\(second opinion\)$/.test(h.name);
    return `<li data-key="${esc(h.name.toLowerCase())}"><div class="d-head"><span class="d-name${second ? " second" : ""}">${esc(h.name)}</span><span class="d-pct">${pct(h.probability)}</span></div><div class="d-bar"><span style="width:${Math.round(h.probability * 100)}%"></span></div></li>`;
  }).join("");
  if (reduceMotion) return;
  [...list.children].forEach((li) => {
    const was = before.get(li.dataset.key);
    if (was === undefined) return;
    const dy = was - li.getBoundingClientRect().top;
    if (!dy) return;
    li.style.transition = "none";
    li.style.transform = `translateY(${dy}px)`;
    requestAnimationFrame(() => { li.style.transition = ""; li.style.transform = ""; });
  });
}

// ------------------------------------------------------------------ Leaf Doctor -> garden

// A garden on this device (from Today) can take the diagnosis: its steps become today's
// tasks, a check-in is scheduled, and the season plan's next weeks are replanned.
async function offerGarden(d) {
  const box = $("to-garden");
  box.hidden = true;
  $("tg-state").textContent = "";
  let g = null;
  try { g = JSON.parse(localStorage.getItem("leaf.garden") || "null"); } catch {}
  if (!d || !g || !g.id || !g.key) return;
  let plants = [];
  try {
    const r = await fetch(`/api/gardens/${encodeURIComponent(g.id)}`, { headers: { "X-Leaf-Key": g.key } });
    if (!r.ok) return;
    plants = ((await r.json()).plants || []).filter((p) => p.status === "active");
  } catch { return; }
  if (!plants.length) return;
  $("tg-plant").innerHTML = plants.map((p) => `<option value="${esc(p.id)}">${esc(p.nickname || p.common_name || p.species || "Plant")}</option>`).join("");
  box.hidden = false;
  $("tg-add").onclick = async () => {
    $("tg-state").textContent = "Adding…";
    try {
      const r = await fetch(`/api/gardens/${encodeURIComponent(g.id)}/plants/${encodeURIComponent($("tg-plant").value)}/diagnosis`, {
        method: "POST",
        headers: { "content-type": "application/json", "X-Leaf-Key": g.key },
        body: JSON.stringify({ case_id: d.case_id, owner_token: d.owner_token }),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out.detail || "Leaf could not add it. Try again.");
      const when = new Date(`${out.checkin_on}T12:00:00`).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
      $("tg-state").textContent = `Added to Today. Check-in photo due ${when}.` + (out.replan ? " Leaf is updating the next weeks of your season plan." : "");
    } catch (err) { $("tg-state").textContent = err.message; }
  };
}

// ------------------------------------------------------------------ render a result

function render(d) {
  last = d; history = [];
  if (window.LeafRegional) window.LeafRegional.render(d);  // every mode: clears a previous result's box
  caseId = d.status === "needs_answers" ? d.case_id : null;
  $("results").hidden = false;
  const deep = d.mode === "deep";
  if (deep && d.differential && d.differential.length) drawDifferential(d.differential);

  const top = deep && d.differential && d.differential.length
    ? { name: d.differential[0].name } : d.diagnosis.candidates[0];
  renderVerdict(d, top);

  renderConfidence(d);
  const saved = d.case_id && d.owner_token && d.status !== "needs_answers";
  $("share").hidden = !saved;
  if (saved) {
    // The owner token rides in the fragment, which browsers never send to the server.
    const base = `${location.origin}/c/${encodeURIComponent(d.case_id)}`;
    $("share-link").href = d.owner_token ? `${base}#k=${encodeURIComponent(d.owner_token)}` : base;
    $("share-link").dataset.readonly = base;
    const name = top ? top.name : "Your plant";
    LeafRemind.remember({
      id: d.case_id, token: d.owner_token, title: name, verdict: d.verdict, created: Date.now(),
      checkin_days: d.plan.check_in_days, plant: (d.observation && d.observation.plant_guess) || "",
    });
  }
  offerGarden(saved ? d : null);
  $("summary").textContent = d.summary || d.diagnosis.reasoning_summary;
  const byId = Object.fromEntries((d.evidence || []).map((e) => [e.id, e]));
  if (deep && d.differential && d.differential.length) {
    $("candidates").innerHTML = d.differential.map((h) => `
      <li class="cand">
        <div class="cand-head"><span class="cand-name">${esc(h.name)}</span><span>${pct(h.probability)}</span></div>
        <div class="bar" role="img" aria-label="${pct(h.probability)}"><span style="width:${Math.round(h.probability * 100)}%"></span></div>
        ${h.evidence_for.length ? `<ul class="evidence">${h.evidence_for.map((e) => byId[e] ? `<li><span class="ev-id">${esc(e)}</span>${esc(byId[e].summary)}</li>` : "").join("")}</ul>` : ""}
      </li>`).join("");
  } else {
    $("candidates").innerHTML = d.diagnosis.candidates.map((c) => `
      <li class="cand">
        <div class="cand-head"><span class="cand-name">${esc(c.name)}<span class="chip">${esc(c.category)}</span></span><span>${pct(c.confidence)}</span></div>
        <div class="bar" role="img" aria-label="${pct(c.confidence)}"><span style="width:${Math.round(c.confidence * 100)}%"></span></div>
        ${c.evidence_for.length ? `<ul class="evidence">${c.evidence_for.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
      </li>`).join("");
  }

  const critic = d.critic;
  $("second").hidden = !critic;
  if (critic) {
    $("second-text").textContent = (critic.agrees ? "Agrees with the leading diagnosis. " : `Disagrees: the strongest alternative is ${critic.alternative}. `) + (critic.argument || "");
  }

  const qs = d.questions && d.questions.length ? d.questions : (d.diagnosis.follow_up_questions || []);
  $("followups").hidden = qs.length === 0;
  $("questions").innerHTML = qs.map((q, n) => `<label class="q field">${esc(q)}<input data-q="${esc(q)}" id="ans${n}" maxlength="400"></label>`).join("");
  $("rerun").querySelector(".cta-text").textContent = caseId ? "Send answers" : "Update diagnosis";

  renderWeather(d);
  renderPlan(d);
  renderEvidence(d);
  $("sources").innerHTML = d.sources.length
    ? d.sources.map((s) => `<li id="src${s.id}"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a></li>`).join("")
    : `<li class="muted">No web sources this time.</li>`;
  renderHandoff(d);

  $("tracebody").innerHTML = d.trace.map((t) => `
    <tr><td>${esc(t.step)}${t.note ? ` <span class="muted small">(${esc(t.note)})</span>` : ""}</td>
    <td class="model">${esc(t.model)}</td><td>${t.latency_ms ? (t.latency_ms / 1000).toFixed(1) + "s" : "–"}</td>
    <td>${t.prompt_tokens ?? "–"} / ${t.completion_tokens ?? "–"}</td></tr>`).join("");
  const cost = d.cost_usd ?? d.trace.reduce((s, t) => s + (t.cost_usd || 0), 0);
  $("cost").textContent = `Model cost for this case: $${cost.toFixed(3)}` + (d.tavily_credits ? `, plus ${d.tavily_credits} Tavily search credits.` : ".");
  const o = d.observation;
  $("obs").textContent = `Seen in the photo: ${o.plant_guess}; ${(o.symptoms || []).join("; ") || "no symptoms listed"}. Image quality: ${o.image_quality}.`;
  $("chatlog").innerHTML = "";
  $("results").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
}

const VERDICT = { confident: "Confident", likely: "Likely", lab: "Send a sample to a lab" };

function renderVerdict(d, top) {
  const waiting = d.status === "needs_answers";
  const v = waiting ? "" : d.verdict || (d.plan.escalate ? "lab" : "likely");
  $("verdict").className = `verdict ${v}`;
  $("verdict-word").textContent = waiting ? "A few questions first"
    : v === "lab" ? VERDICT.lab : `${VERDICT[v]}${top ? `: ${top.name}` : ""}`;
  $("banner").textContent = waiting
    ? "Leaf needs a little more from you to tell the likely causes apart."
    : d.verdict_reason || d.plan.escalate_reason || "";
  $("notice").hidden = !d.notice;
  $("notice").textContent = d.notice || "";
  $("lab-link").hidden = v !== "lab" || !!d.handoff;
  const a = d.auto;
  $("auto-why").hidden = !a;
  if (a) $("auto-why").textContent = `${a.investigated ? "Leaf investigated further." : "Leaf stopped after its quick look."} ${a.reason}`;
}

function renderConfidence(d) {
  const c = d.confidence;
  $("confidence").hidden = !c;
  $("confidence-why").hidden = !c;
  if (!c) return;
  $("confidence").textContent = `Leaf is ${pct(c.value)} confident in this diagnosis.`;
  const parts = [`The investigation put it at ${pct(c.investigator)}.`];
  parts.push(c.critic == null
    ? "No second opinion was available, so Leaf is a little less sure."
    : `The second opinion put it at ${pct(c.critic)}${c.disagreement ? " and argued for something else, so the lower figure counts" : ""}.`);
  parts.push(`Photo quality: ${c.quality === "none" ? "no photo, notes only" : c.quality}.`);
  parts.push(`${c.coverage} piece${c.coverage === 1 ? "" : "s"} of evidence support it.`);
  $("confidence-parts").innerHTML = parts.map((p) => `<li>${esc(p)}</li>`).join("");
}

const PET = { low: "Low risk to pets", caution: "Keep pets off until dry", high: "Dangerous to pets" };

function stepTags(s) {
  const tags = [];
  if (s.organic === true) tags.push(["ok", "Organic"]);
  if (s.organic === false) tags.push(["", "Not organic"]);
  if (s.pet_risk) tags.push([s.pet_risk === "high" ? "bad" : s.pet_risk === "caution" ? "warn" : "ok", PET[s.pet_risk]]);
  if (s.bee_risk) tags.push(["warn", "Harms bees: spray at dusk"]);
  if (!tags.length) return "";
  const names = (s.actives || []).length ? `<span class="tag plain">${esc(s.actives.join(", "))}</span>` : "";
  return `<span class="tags">${names}${tags.map(([c, t]) => `<span class="tag ${c}">${esc(t)}</span>`).join("")}</span>`;
}

function renderPlan(d) {
  $("plan-summary").textContent = d.plan.summary;
  $("steps").innerHTML = d.plan.steps.map((s) => `
    <li>${esc(s.action)}${s.source_ids.map((id) => `<a class="cite" href="#src${id}">[${id}]</a>`).join("")}
    ${s.why ? `<span class="why">${esc(s.why)}</span>` : ""}${s.flag ? `<span class="step-flag">Only partly supported: ${esc(s.flag)}</span>` : ""}
    ${stepTags(s)}${(s.precautions || []).map((p) => `<span class="why caution-line">${esc(p)}</span>`).join("")}</li>`).join("");
  const cautions = d.plan.cautions || [];
  $("cautions").hidden = !cautions.length;
  $("cautions").innerHTML = cautions.map((c) => `<li>${esc(c)}</li>`).join("");
  const removed = d.plan.removed || [];
  $("removed").hidden = !removed.length;
  const why = d.garden && d.garden.organic && d.garden.pets ? "organic, pet-safe" : d.garden && d.garden.organic ? "organic" : "pet-safe";
  $("removed-sum").textContent = `Leaf removed ${removed.length} step${removed.length === 1 ? "" : "s"} to keep your plan ${why}`;
  $("removed-list").innerHTML = removed.map((r) => `<li>${esc(r)}</li>`).join("");
  const dropped = (d.claim_flags || []).filter((f) => f.verdict === "dropped");
  $("claims").hidden = !dropped.length;
  $("claims").textContent = dropped.length ? `Leaf removed ${dropped.length} step${dropped.length === 1 ? "" : "s"} its sources did not support.` : "";
  const watch = d.plan.watch_for || [];
  $("watch").hidden = d.verdict === "confident" || d.status === "needs_answers" || (!watch.length && !d.plan.sample_if);
  $("watch-list").innerHTML = watch.map((w) => `<li>${esc(w)}</li>`).join("");
  $("sample-if").textContent = d.plan.sample_if ? `When to send a sample: ${d.plan.sample_if}` : "";
  const due = LeafRemind.checkInDate(Date.now(), d.plan.check_in_days);
  const waiting = d.status === "needs_answers";
  $("checkin").textContent = waiting ? "" : `Check in: send a new photo around ${due.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}.`;
  const saved = d.case_id && d.owner_token && !waiting;
  $("checkin-remind").hidden = !saved;
  if (saved) {
    const top = (d.differential && d.differential[0]) || d.diagnosis.candidates[0];
    checkinReminder = {
      title: `Leaf check-in: ${top ? top.name : "your plant"}`,
      start: due, end: new Date(due.getTime() + 15 * 60000),
      details: "Take a new photo of the same leaves and add it to your report. Leaf will compare it with the last one and update the advice.",
      path: `/c/${encodeURIComponent(d.case_id)}`,
    };
    $("checkin-google").href = LeafRemind.google(checkinReminder);
  }
  renderSprayOffer(d);
}

// ------------------------------------------------------------------ reminders and the spray window

let checkinReminder = null;
let sprayReminder = null;

$("checkin-ics").addEventListener("click", async () => {
  if (!checkinReminder) return;
  try { await LeafRemind.ics(checkinReminder); } catch (err) { $("checkin-ics").textContent = err.message; }
});
$("spray-ics").addEventListener("click", async () => {
  if (!sprayReminder) return;
  try { await LeafRemind.ics(sprayReminder); } catch (err) { $("spray-ics").textContent = err.message; }
});

function renderSprayOffer(d) {
  const show = !!(d.plan.spray && d.status !== "needs_answers");
  $("spray").hidden = !show;
  $("spray-out").hidden = true;
  $("spray-status").textContent = "";
  if (!show) return;
  const place = $("region").value.trim() || coords;
  $("spray-ask").hidden = !!place;
  if (place) findSprayWindow(d);
}

$("spray-go").addEventListener("click", () => last && findSprayWindow(last));
$("spray-locate").addEventListener("click", () => { $("locate").click(); });

const fmtTemp = (c) => (navigator.language || "").startsWith("en-US") ? `${Math.round(c * 9 / 5 + 32)}°F` : `${Math.round(c)}°C`;
const fmtWind = (k) => (navigator.language || "").startsWith("en-US") ? `${Math.round(k / 1.609)} mph` : `${Math.round(k)} km/h`;
// Window times are the garden's wall-clock time (the forecast's own clock, no zone attached),
// so a browser in another time zone never shifts them. Calendar entries use the UTC times.
const localTime = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const localDay = (iso) => new Date(iso).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
const sentence = (s) => s.charAt(0).toUpperCase() + s.slice(1);

async function findSprayWindow(d) {
  const body = { heat_limit_c: d.plan.heat_limit_c || 32, bees: !!d.plan.bee_safe_hours };
  if (coords) Object.assign(body, coords); else body.region = $("region").value.trim();
  if (!coords && !body.region) { $("spray-ask").hidden = false; $("spray-status").textContent = "Leaf needs your town or location for the forecast."; return; }
  $("spray-go").disabled = true;
  $("spray-status").textContent = "Checking the next five days of forecast…";
  try {
    const r = await fetch("/api/spray-window", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const f = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(f.detail || "The forecast is unavailable right now.");
    renderSpray(f, d);
    $("spray-status").textContent = "";
  } catch (err) {
    $("spray-status").textContent = err.message;
  } finally {
    $("spray-go").disabled = false;
  }
}

function renderSpray(f, d) {
  $("spray-out").hidden = false;
  $("spray-rules").innerHTML = (f.rules || []).map((r) => `<li>${esc(r)}</li>`).join("")
    + `<li>Heat limit for this plan: ${esc(fmtTemp(f.heat_limit_c))}${f.heat_limit_c < 30 ? " (it uses oil or sulfur, which can burn leaves in heat)" : ""}.</li>`
    + (f.bee_safe_hours ? "<li>A product in this plan harms bees, so only early morning or evening hours count.</li>" : "");
  const best = (f.windows || []).find((w) => w.best);
  $("spray-remind").hidden = !best;
  if (best) {
    const dry = best.dry_hours_after >= 48 ? "2 days or more" : `about ${best.dry_hours_after} hours`;
    $("spray-best").innerHTML = `<strong>${esc(localDay(best.start))}, ${esc(localTime(best.start))} to ${esc(localTime(best.end))}</strong>`
      + ` <span class="muted">${esc(fmtTemp(best.temp_c[0]))} to ${esc(fmtTemp(best.temp_c[1]))}, wind up to ${esc(fmtWind(best.wind_kmh))}, dry for ${esc(dry)} after.</span>`
      + (best.notes.length ? ` <span class="muted">${esc(sentence(best.notes.join("; ")))}.</span>` : "");
    const products = [...new Set(d.plan.steps.flatMap((s) => s.actives || []))].join(", ");
    sprayReminder = {
      title: `Spray ${products || "your plants"} (Leaf)`,
      start: best.start_utc, end: best.end_utc,
      details: `Leaf's best window for your care plan in ${f.place}. Follow the product label. Check the forecast again on the day.`,
      path: d.case_id && d.owner_token ? `/c/${encodeURIComponent(d.case_id)}` : "",
    };
    $("spray-google").href = LeafRemind.google(sprayReminder);
  } else {
    $("spray-best").textContent = f.message;
  }
  // Day rows x hour columns, coloured by score; Leaf's pick outlined.
  const pickDay = best ? best.start.slice(0, 10) : "";
  const pickFrom = best ? +best.start.slice(11, 13) : -1, pickTo = best ? +best.end.slice(11, 13) : -1;
  const hours = Array.from({ length: 14 }, (_, i) => i + 6);
  const head = `<span></span>${hours.map((h) => `<span class="gh">${h % 3 === 0 ? h : ""}</span>`).join("")}`;
  const rows = (f.days || []).map((day) => {
    const byHour = Object.fromEntries(day.hours.map((c) => [c.hour, c]));
    const label = new Date(`${day.date}T12:00`).toLocaleDateString(undefined, { weekday: "short" });
    return `<span class="gd">${esc(label)}</span>` + hours.map((h) => {
      const c = byHour[h] || { score: -1, why: "" };
      const cls = c.score < 0 ? "no" : c.score >= 75 ? "good" : "ok";
      const pick = day.date === pickDay && h >= pickFrom && h < pickTo ? " pick" : "";
      const tip = c.score < 0 ? (c.why || "not suitable") : `score ${c.score}`;
      return `<span class="cell ${cls}${pick}" title="${esc(`${h}:00 ${tip}`)}"></span>`;
    }).join("");
  }).join("");
  $("spray-grid").innerHTML = head + rows;
  $("spray-grid").setAttribute("aria-label", best ? `Best window ${localDay(best.start)} ${localTime(best.start)}` : "No suitable window");
  $("spray-others").innerHTML = (f.windows || []).filter((w) => !w.best).slice(0, 4).map((w) =>
    `<li>${esc(localDay(w.start))}: ${esc(localTime(w.start))} to ${esc(localTime(w.end))} <span class="muted">(score ${w.score})</span></li>`).join("");
}

function renderEvidence(d) {
  const ev = d.evidence || [];
  $("evidence-block").hidden = !ev.length;
  const label = { look_closer: "close look", get_conditions: "weather", search_sources: "search", read_source: "source", check_alerts: "news" };
  const cite = (s) => { const n = String(s).replace(/^S/, ""); return ` <a class="cite" href="#src${esc(n)}">[${esc(n)}]</a>`; };
  $("evidence").innerHTML = ev.map((e) => `<li><span class="ev-id">${esc(e.id)}</span><span class="ev-tool">${esc(label[e.tool] || e.tool)}</span>${esc(e.summary)}${(e.source_ids || []).map(cite).join("")}</li>`).join("");
}

function renderWeather(d) {
  const w = (d.evidence || []).find((e) => e.tool === "get_conditions");
  $("weather").hidden = !w;
  if (!w) return;
  const x = w.data;
  $("weather-line").textContent = `${x.place}, ${x.from} to ${x.to}: ${x.rain_mm_total} mm of rain on ${x.rainy_days} days, ${x.wet_hours_total} humid hours, ${x.temp_c.min} to ${x.temp_c.max} °C.`;
  const wet = x.daily_wet_hours || [], rain = x.daily_rain_mm || [];
  const W = 14 * 22, H = 64, maxRain = Math.max(5, ...rain);
  const bars = wet.map((h, i) => `<rect class="wet" x="${i * 22 + 3}" y="${H - 12 - (h / 24) * (H - 18)}" width="16" height="${(h / 24) * (H - 18)}"><title>${esc(h)} humid hours</title></rect>`).join("");
  const dots = rain.map((mm, i) => mm > 0 ? `<circle class="rain" cx="${i * 22 + 11}" cy="${H - 5}" r="${1.5 + 3.5 * Math.min(1, mm / maxRain)}"><title>${esc(mm)} mm rain</title></circle>` : "").join("");
  $("weather-strip").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line class="axis" x1="0" x2="${W}" y1="${H - 12}" y2="${H - 12}"/>${bars}${dots}</svg>`;
  $("weather-strip").setAttribute("aria-label", "Bars show humid hours per day, dots show rain");
  $("weather-indices").innerHTML = (x.indices || []).map((i) => {
    const value = i.last_7_days !== undefined ? `${i.last_7_days} in the last 7 days, ${i.last_14_days} in 14` : `${i.count} in 14 days`;
    const links = (i.sources || []).map((u, n) => `<a href="${esc(u)}" target="_blank" rel="noopener">source${i.sources.length > 1 ? " " + (n + 1) : ""}</a>`).join(" ");
    return `<li><strong>${esc(i.name)}</strong> (${esc(i.applies_to)}): ${esc(value)}. ${esc(i.meaning)} ${links}</li>`;
  }).join("") || (x.note ? `<li>${esc(x.note)}</li>` : "");
  $("weather-caveat").textContent = x.caveat || "";
}

function renderHandoff(d) {
  const h = d.handoff;
  $("handoff").hidden = !h;
  if (!h) return;
  $("handoff-reason").textContent = h.reason;
  $("handoff-lab").href = h.lab_url;
  $("handoff-lab").textContent = h.lab_state ? `Find a plant diagnostic lab in ${h.lab_state}` : "Find your state's plant diagnostic lab";
  $("handoff-notice").textContent = h.lab_notice;
  $("handoff-steps").innerHTML = h.sampling.map((s) => `<li>${esc(s)}</li>`).join("");
  $("handoff-sources").innerHTML = h.sampling_sources.map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(new URL(u).hostname.replace(/^www\./, ""))}</a>`).join(", ");
  // Photos for the printout come from this page, never from the server.
  $("handoff-photos").innerHTML = "";
  photoFiles.forEach((f, i) => {
    if (!f) return;
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.alt = slots[i].dataset.label;
    $("handoff-photos").appendChild(img);
  });
  const facts = [["Case", h.case_id], ["Date", h.created], ["Plant", h.crop || "not given"], ["Place", h.place || "not given"],
    ["Notes", h.history.notes || "none"], ["Leaf's differential", h.differential.map((x) => `${x.name} (${pct(x.probability)})`).join("; ")],
    ["Second opinion", h.second_opinion ? `${h.second_opinion.alternative}: ${h.second_opinion.argument}` : "none"],
    ["Weather", h.weather ? `${h.weather.rain_mm_total} mm rain on ${h.weather.rainy_days} days, ${h.weather.wet_hours_total} humid hours (${h.weather.from} to ${h.weather.to})` : "not checked"]];
  $("handoff-facts").innerHTML = facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

$("print").addEventListener("click", () => window.print());
$("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("share-link").dataset.readonly); $("copy").textContent = "Copied"; }
  catch { $("copy").textContent = "Copy failed: select the link instead"; }
});

// ------------------------------------------------------------------ answers and chat

$("go").addEventListener("click", () => diagnose());
$("rerun").addEventListener("click", async () => {
  const inputs = [...document.querySelectorAll("#questions input")];
  busy(true);
  try {
    if (caseId) {
      const answers = inputs.map((i) => i.value.trim());
      await runDeep(`/api/deep/${encodeURIComponent(caseId)}/answers/stream`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answers }),
      });
    } else {
      const answers = inputs.filter((i) => i.value.trim()).map((i) => `Q: ${i.dataset.q} A: ${i.value.trim()}`).join("\n");
      const { fd, chosen } = formData();
      if (chosen.length) fd.append("image", chosen[0][0]);
      fd.append("notes", $("notes").value.trim());
      fd.append("region", $("region").value.trim());
      await runQuick(fd, !!chosen.length, answers);
    }
  } catch (err) {
    $("status").textContent = err.message;
  } finally {
    busy(false);
  }
});

$("chatform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("chatin").value.trim();
  if (!msg || !last) return;
  $("chatin").value = "";
  addMsg("user", msg);
  // Send only what the chat needs, not the whole case.
  const context = { observation: last.observation, diagnosis: last.diagnosis, plan: last.plan, sources: last.sources, trace: [], low_confidence: last.low_confidence };
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ context, history, message: msg }),
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
