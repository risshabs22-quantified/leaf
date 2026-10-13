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
    $("deep-left").textContent = left > 0 ? `Deep cases left today: ${left}.` : "Deep mode has reached today's limit; Quick still works.";
    $("deep-left").hidden = false;
  }
}).catch(() => {});

// ------------------------------------------------------------------ run a case

function formData() {
  const fd = new FormData();
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
    if (mode() === "deep") {
      chosen.forEach(([f, label]) => { fd.append("images", f); fd.append("labels", label); });
      fd.append("notes", notes);
      fd.append("region", $("region").value.trim());
      if (coords) { fd.append("lat", coords.lat); fd.append("lon", coords.lon); }
      await runDeep("/api/deep/stream", { method: "POST", body: fd });
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
    case "first_look":
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

// ------------------------------------------------------------------ render a result

function render(d) {
  last = d; history = [];
  caseId = d.status === "needs_answers" ? d.case_id : null;
  $("results").hidden = false;
  const deep = d.mode === "deep";
  if (deep && d.differential && d.differential.length) drawDifferential(d.differential);

  const top = deep && d.differential && d.differential.length
    ? { name: d.differential[0].name } : d.diagnosis.candidates[0];
  const banner = $("banner");
  if (d.notice) {
    banner.className = "banner warn";
    banner.textContent = d.notice;
  } else if (d.status === "needs_answers") {
    banner.className = "banner";
    banner.textContent = "Leaf needs a little more from you to tell the likely causes apart.";
  } else if (d.plan.escalate) {
    banner.className = "banner warn";
    banner.textContent = d.plan.escalate_reason || "Leaf isn't sure. Please check with your local extension office.";
  } else {
    banner.className = "banner ok";
    banner.textContent = top ? `Most likely: ${top.name}` : "Diagnosis ready.";
  }

  renderConfidence(d);
  const saved = d.case_id && d.status !== "needs_answers";
  $("share").hidden = !saved;
  if (saved) {
    // The owner token rides in the fragment, which browsers never send to the server.
    const base = `${location.origin}/c/${encodeURIComponent(d.case_id)}`;
    $("share-link").href = d.owner_token ? `${base}#k=${encodeURIComponent(d.owner_token)}` : base;
    $("share-link").dataset.readonly = base;
    try { if (d.owner_token) localStorage.setItem(`leaf-owner-${d.case_id}`, d.owner_token); } catch { /* private mode */ }
  }
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

function renderPlan(d) {
  $("plan-summary").textContent = d.plan.summary;
  $("steps").innerHTML = d.plan.steps.map((s) => `
    <li>${esc(s.action)}${s.source_ids.map((id) => `<a class="cite" href="#src${id}">[${id}]</a>`).join("")}
    ${s.why ? `<span class="why">${esc(s.why)}</span>` : ""}${s.flag ? `<span class="step-flag">Only partly supported: ${esc(s.flag)}</span>` : ""}</li>`).join("");
  const dropped = (d.claim_flags || []).filter((f) => f.verdict === "dropped");
  $("claims").hidden = !dropped.length;
  $("claims").textContent = dropped.length ? `Leaf removed ${dropped.length} step${dropped.length === 1 ? "" : "s"} its sources did not support.` : "";
  const due = new Date(Date.now() + d.plan.check_in_days * 864e5);
  $("checkin").textContent = d.status === "needs_answers" ? "" : `Check in: send a new photo around ${due.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}.`;
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
