// Connect: API keys, Home Assistant, MCP and the REST API for this garden.
// Every string from the server goes through textContent or esc().
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (id) => document.getElementById(id);
const STORE = "leaf.garden";

let garden = null;
try { garden = JSON.parse(localStorage.getItem(STORE) || "null"); } catch {}
let plants = [];
let origin = location.origin;

async function api(path, opts = {}) {
  const headers = { "X-Leaf-Key": garden.key, ...(opts.body ? { "content-type": "application/json" } : {}) };
  const r = await fetch(path, { ...opts, headers });
  let d = null;
  try { d = await r.json(); } catch {}
  if (!r.ok) throw new Error((d && d.detail) || "Something went wrong. Please try again.");
  return d;
}
const gpath = (p) => `/api/gardens/${encodeURIComponent(garden.id)}${p}`;
const status = (t) => { $("c-status").textContent = t || ""; };
const show = (id, on = true) => { $(id).hidden = !on; };

function when(ts) { return ts ? new Date(ts * 1000).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : "never"; }

// ---------- keys ----------

async function loadKeys() {
  const keys = await api(gpath("/api-keys"));
  $("key-list").innerHTML = keys.map((k) => `
    <li data-id="${esc(k.id)}"><strong>${esc(k.label || "Unnamed key")}</strong>
      <span class="why">${esc(k.prefix)}… · ${esc(k.scopes.join(", "))} · ${esc(k.per_hour)}/hour, ${esc(k.per_day)}/day, ${esc(k.model_per_day)} model calls/day · made ${esc(when(k.created_at))} · last used ${esc(when(k.last_used))}</span>
      <button type="button" class="textbtn revoke">Revoke</button></li>`).join("");
}

$("key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const scopes = [...document.querySelectorAll('input[name="scope"]:checked')].map((i) => i.value);
  if (!scopes.length) { status("Choose at least one scope."); return; }
  status("Making your key…");
  try {
    const k = await api(gpath("/api-keys"), { method: "POST", body: JSON.stringify({ label: $("k-label").value.trim(), scopes }) });
    $("key-secret").value = k.key;
    show("key-new");
    $("k-label").value = "";
    status("");
    window.leafNewKey = k.key;  // for the snippets below, until the page is closed
    renderSnippets(k.key);
    await loadKeys();
  } catch (err) { status(err.message); }
});

$("key-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("key-secret").value); status("Copied."); }
  catch { $("key-secret").select(); status("Press Ctrl+C or Cmd+C to copy."); }
});

$("key-list").addEventListener("click", async (e) => {
  const btn = e.target.closest(".revoke");
  if (!btn) return;
  if (!confirm("Revoke this key? Anything using it stops working.")) return;
  try { await api(gpath(`/api-keys/${encodeURIComponent(btn.closest("li").dataset.id)}`), { method: "DELETE" }); await loadKeys(); }
  catch (err) { status(err.message); }
});

// ---------- snippets ----------

function renderSnippets(key) {
  const K = key || "YOUR_LEAF_KEY";
  const plant = plants[0];
  const pid = plant ? plant.id : "PLANT_ID";
  $("ha-yaml").textContent = `# configuration.yaml
rest_command:
  leaf_soil_moisture:
    url: "${origin}/api/v1/readings"
    method: POST
    headers:
      Authorization: "Bearer ${K}"
      Content-Type: "application/json"
    payload: '{"plant_id": "{{ plant_id }}", "value": {{ value }}, "source": "{{ source }}"}'

# automations.yaml: send ${plant ? (plant.nickname || plant.common_name || "the plant") : "a plant"}'s moisture whenever it changes
- alias: Send soil moisture to Leaf
  trigger:
    - platform: state
      entity_id: sensor.YOUR_SOIL_MOISTURE_SENSOR
  condition:
    - condition: template
      value_template: "{{ trigger.to_state.state is number }}"
  action:
    - service: rest_command.leaf_soil_moisture
      data:
        plant_id: "${pid}"
        value: "{{ trigger.to_state.state | float }}"
        source: "{{ trigger.entity_id }}"`;
  $("mcp-code").textContent = `# Claude Code
claude mcp add --transport http leaf ${origin}/mcp --header "Authorization: Bearer ${K}"

# Any MCP client that takes a URL and headers
{
  "mcpServers": {
    "leaf": {
      "type": "http",
      "url": "${origin}/mcp",
      "headers": { "Authorization": "Bearer ${K}" }
    }
  }
}`;
  $("api-curl").textContent = `curl ${origin}/api/v1/today \\
  -H "Authorization: Bearer ${K}"

curl ${origin}/api/v1/diagnose \\
  -H "Authorization: Bearer ${K}" \\
  -F image=@leaf.jpg -F notes="brown spots with yellow rings"`;
}

async function loadApiTable() {
  try {
    const spec = await (await fetch("/api/v1/openapi.json")).json();
    const rows = [];
    for (const [path, ops] of Object.entries(spec.paths || {})) {
      for (const [method, op] of Object.entries(ops)) rows.push(`<tr><td><code>${esc(method.toUpperCase())} ${esc(path)}</code></td><td>${esc(op.summary || "")}</td></tr>`);
    }
    $("api-rows").innerHTML = rows.join("");
  } catch { $("api-rows").innerHTML = ""; }
}

// ---------- test reading ----------

$("test-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("test-result").textContent = "Sending…";
  try {
    const r = await api(gpath("/readings"), { method: "POST", body: JSON.stringify({ plant_id: $("t-plant").value, value: Number($("t-value").value), test: true, source: "Connect page test" }) });
    $("test-result").textContent = `Stored. ${r.water.reason} Open Today to see the watering task change.`;
  } catch (err) { $("test-result").textContent = err.message; }
});

// ---------- start ----------

async function start() {
  if (!garden || !garden.id || !garden.key) { show("c-none"); return; }
  try {
    const [view, conn] = await Promise.all([api(gpath("")), fetch("/api/connect/status").then((r) => r.json())]);
    plants = (view.plants || []).filter((p) => p.status === "active");
    origin = (conn.api && conn.api.base) ? conn.api.base.replace(/\/api\/v1$/, "") : location.origin;
    $("c-title").textContent = `Connect: ${view.garden.name || "your garden"}`;
    $("t-plant").innerHTML = plants.map((p) => `<option value="${esc(p.id)}">${esc(p.nickname || p.common_name || p.species || "Plant")}</option>`).join("");
    ["c-keys", "c-ha", "c-mcp", "c-api"].forEach((id) => show(id));
    show("test-form", plants.length > 0);
    renderSnippets();
    await Promise.all([loadKeys(), loadApiTable()]);
  } catch (err) {
    status(err.message);
    if (/No garden at this link/.test(err.message)) show("c-none");
  }
}
start();
