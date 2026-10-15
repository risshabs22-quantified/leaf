// Identify a plant: photo -> candidates checked against sources -> one tap -> species profile.
// Every model-written string goes through esc() or textContent.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (id) => document.getElementById(id);
// Links come from web search results: only http(s) becomes a link, anything else plain text.
const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
const link = (url, text) => (safeUrl(url)
  ? `<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text));
// A garden's private link (/identify?garden=<id>#k=<key>) adds the confirmed plant to it.
const gardenId = new URLSearchParams(location.search).get("garden");
const gardenKey = (location.hash.match(/^#k=([\w-]+)$/) || [])[1] || "";

const FIELD_LABELS = {
  light: "Light", water: "Water", temp_min_c: "Lowest temperature", temp_max_c: "Suffers above",
  frost_tolerance: "Frost", hardiness_zones: "Hardiness zones", soil: "Soil", soil_ph: "Soil pH",
  feeding: "Feeding", toxic_to_pets: "Pets", toxic_to_children: "Children", pests: "Common pests",
  diseases: "Common diseases", timing: "Timing", ideal_temp_c: "Ideal temperature (°C)",
  kc: "Water use (FAO-56 Kc)",
  root_depth_m: "Root depth (m)", germination_soil_c: "Germination soil temperature",
};
const TOXIC_WORDS = { toxic: "Toxic", mildly_toxic: "Mildly toxic", non_toxic: "Not toxic", unknown: "Unknown: no source found" };

let photo = null;
let ident = null;

$("id-photo").addEventListener("change", (e) => {
  photo = e.target.files[0] || null;
  $("id-go").disabled = !photo;
  const label = $("id-slot").querySelector(".slot-label");
  label.textContent = photo ? photo.name : "Choose a photo";
});

$("id-go").addEventListener("click", async () => {
  if (!photo) return;
  $("id-go").disabled = true;
  $("id-status").textContent = "Looking at the photo and checking sources (about 15 seconds)…";
  $("id-result").hidden = true;
  $("id-profile").hidden = true;
  const form = new FormData();
  form.append("image", photo);
  form.append("notes", $("id-notes").value);
  try {
    const r = await fetch("/api/identify", { method: "POST", body: form });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Leaf could not identify this photo.");
    ident = d;
    showCandidates(d);
    $("id-status").textContent = "";
  } catch (err) {
    $("id-status").textContent = err.message;
  } finally {
    $("id-go").disabled = !photo;
  }
});

function pct(p) { return `${Math.round((p || 0) * 100)}%`; }

function showCandidates(d) {
  $("id-result").hidden = false;
  if (!d.plant_visible || !d.candidates.length) {
    $("id-summary").textContent = "Leaf could not see a plant clearly. Try a closer photo of a leaf or flower, in daylight.";
    $("id-cands").innerHTML = "";
  } else {
    $("id-summary").textContent = d.checked
      ? (d.confident ? "Leaf is fairly sure, but you decide." : "Leaf is not sure. Pick the one that matches, or type the name.")
      : "Leaf could not check its guesses against sources this time; treat them as guesses.";
    const sources = Object.fromEntries((d.sources || []).map((s) => [s.id, s]));
    $("id-cands").innerHTML = d.candidates.map((c, i) => {
      const cites = (c.source_ids || []).map((id) => sources[id]).filter(Boolean)
        .map((s) => link(s.url, s.title)).join(", ");
      const flag = c.consistent === false ? `<span class="why caution-line">The sources describe it differently from this photo.</span>` : "";
      return `<li class="cand${i === 0 && d.confident ? " lead" : ""}">
        <div><strong>${esc(c.common_name || c.scientific_name)}</strong> <em>${esc(c.scientific_name)}</em> <span class="small">${pct(c.probability)}</span></div>
        ${c.reason ? `<span class="why">${esc(c.reason)}</span>` : ""}${flag}
        ${cites ? `<span class="why small">Sources: ${cites}</span>` : ""}
        <button type="button" class="cta pick" data-i="${i}"><span class="cta-text">This is it</span></button>
      </li>`;
    }).join("");
  }
  $("id-trace").innerHTML = (d.trace || []).map((t) =>
    `<li>${esc(t.step)}: ${esc(t.model)}${t.note ? ` (${esc(t.note)})` : ""}${t.latency_ms ? `, ${(t.latency_ms / 1000).toFixed(1)} s` : ""}</li>`).join("");
}

$("id-cands").addEventListener("click", (e) => {
  const btn = e.target.closest("button.pick");
  if (!btn || !ident) return;
  const c = ident.candidates[Number(btn.dataset.i)];
  confirmChoice(c.scientific_name, c.common_name);
});

$("id-other").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("id-other-name").value.trim();
  if (name) confirmChoice(name, "");
});

async function confirmChoice(scientific, common) {
  $("id-status").textContent = "Saved. Building what this plant needs from sources…";
  try {
    const headers = { "content-type": "application/json" };
    if (gardenId && gardenKey) headers["X-Leaf-Key"] = gardenKey;
    const r = await fetch(`/api/identify/${encodeURIComponent(ident.id)}/confirm`, {
      method: "POST", headers,
      body: JSON.stringify({ scientific_name: scientific, common_name: common, garden_id: gardenId && gardenKey ? gardenId : null }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not save your choice.");
    $("id-result").hidden = true;
    $("id-status").textContent = d.plant ? "Added to your garden." : "";
    if (d.profile_status === "not_queued") {
      $("id-status").textContent = "Saved. Leaf has reached today's limit for new plant profiles; this one will be built when you add it again tomorrow.";
      return;
    }
    pollProfile(d.species, common || scientific);
  } catch (err) {
    $("id-status").textContent = err.message;
  }
}

async function pollProfile(key, title) {
  $("id-profile").hidden = false;
  $("prof-title").textContent = title;
  $("prof-facts").innerHTML = "";
  for (let i = 0; i < 24; i++) {
    const r = await fetch(`/api/species/${encodeURIComponent(key)}`);
    const d = await r.json();
    if (r.status === 404) { $("prof-status").textContent = "Leaf has no profile for this plant yet."; return; }
    if (d.status === "ready") return showProfile(d);
    if (d.status === "failed") { $("prof-status").textContent = "Leaf could not build this plant's profile. Try again later."; return; }
    $("prof-status").textContent = "Reading sources and checking every fact (usually under a minute)…";
    await new Promise((res) => setTimeout(res, 5000));
  }
  $("prof-status").textContent = "Still working on it. Come back in a few minutes.";
}

function showValue(field, v) {
  if (field === "toxic_to_pets" || field === "toxic_to_children") return TOXIC_WORDS[v] || v;
  if (field === "kc") return `start ${v.initial}, peak ${v.mid}, end ${v.end}`;
  if (field === "germination_soil_c") return `at least ${v.min} °C, best ${v.optimum.join("–")} °C`;
  if (field === "temp_min_c" || field === "temp_max_c") return `${v} °C`;
  if (Array.isArray(v)) return v.join(", ");
  return v;
}

function showProfile(d) {
  $("prof-title").textContent = `${(d.common_names || [])[0] || d.scientific_name || d.key}${d.scientific_name ? ` (${d.scientific_name})` : ""}`;
  $("prof-status").textContent = `Pets: ${TOXIC_WORDS[d.toxicity.toxic_to_pets]}. Children: ${TOXIC_WORDS[d.toxicity.toxic_to_children]}.`;
  $("prof-facts").innerHTML = Object.entries(d.facts).map(([field, items]) => `
    <dt>${esc(FIELD_LABELS[field] || field)}</dt>
    ${items.map((x) => `<dd>${esc(showValue(field, x.value))} <span class="cite">${link(x.source.url, x.source.title)}</span>${x.quote ? `<q class="quote">${esc(x.quote)}</q>` : ""}</dd>`).join("")}`).join("");
  const missing = (d.unsourced || []).map((f) => FIELD_LABELS[f] || f);
  $("prof-unsourced").textContent = missing.length ? `No source found yet for: ${missing.join(", ")}. Leaf leaves these blank rather than guess.` : "";
}
