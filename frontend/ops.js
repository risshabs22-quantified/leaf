// /ops: what the platform is doing, from two public aggregate endpoints. Counts and sums only.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (id) => document.getElementById(id);
const usd = (n) => (n < 0.01 ? `$${Number(n).toFixed(4)}` : `$${Number(n).toFixed(2)}`);
const num = (n) => Number(n || 0).toLocaleString();
const short = (m) => String(m || "").split("/").pop();
const MODE = { normal: "Normal", brownout: "Brownout: optional features are on rules", rules_only: "Rules only: the daily cap is reached" };

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error("The numbers are not available right now.");
  return r.json();
}

function render(u, p) {
  $("o-mode").textContent = MODE[u.guard.mode] || u.guard.mode;
  const pct = Math.min(100, (u.guard.spent_usd / u.guard.cap_usd) * 100);
  $("o-bar-fill").style.width = `${pct}%`;
  $("o-bar").setAttribute("aria-label", `${Math.round(pct)}% of the daily cap`);
  $("o-spend").textContent = `Spent today: ${usd(u.guard.spent_usd)} of ${usd(u.guard.cap_usd)} (brownout at ${usd(u.guard.brownout_at_usd)}). ${num(u.guard.calls)} model calls today.`;
  const MIN_USER_DAYS = 20;  // below this an average says nothing
  $("o-cpu").textContent = u.cost_per_active_user_day == null || u.active_user_days < MIN_USER_DAYS
    ? `Cost per active user per day (target under ${usd(u.target_per_active_user_day)}): not meaningful yet. Only ${num(u.active_user_days)} active user-day${u.active_user_days === 1 ? "" : "s"} so far, and the spend is mostly development and testing.`
    : `Cost per active user per day (last 7 days): ${usd(u.cost_per_active_user_day)} (target under ${usd(u.target_per_active_user_day)}); ${num(u.active_user_days)} active user-days.`;
  const tasks = Object.entries(u.tasks).sort((a, b) => b[1].cost_usd - a[1].cost_usd);
  $("o-tasks").innerHTML = tasks.length ? tasks.map(([name, t]) => `<tr><td><code>${esc(name)}</code></td><td>${esc(num(t.calls))}${t.failed ? ` <span class="caution-line">(${esc(t.failed)} failed)</span>` : ""}</td><td>${esc(t.avg_latency_ms)} ms</td><td>${esc(num(t.prompt_tokens))} / ${esc(num(t.completion_tokens))}</td><td>${esc(usd(t.cost_usd))}</td><td>${esc(Object.keys(t.models).map(short).join(", "))}</td></tr>`).join("") : `<tr><td colspan="6">No model calls in this window yet.</td></tr>`;
  const caches = Object.entries(u.caches).map(([ns, c]) => `${ns}: ${c.hits} hit${c.hits === 1 ? "" : "s"}, ${c.misses} miss${c.misses === 1 ? "" : "es"}`);
  $("o-cache").textContent = u.cache_hit_rate == null ? "No cached lookups yet." : `Cache hit rate ${Math.round(u.cache_hit_rate * 100)} % (${caches.join("; ")}).`;

  const jobs = Object.entries(p.jobs_24h);
  $("o-jobs").innerHTML = jobs.length ? jobs.map(([k, s]) => `<tr><td><code>${esc(k)}</code></td><td>${esc(s.done || 0)}</td><td>${esc(s.failed || 0)}</td><td>${esc((s.queued || 0) + (s.running || 0))}</td></tr>`).join("") : `<tr><td colspan="4">No jobs ran in the last 24 hours.</td></tr>`;

  const free = Object.entries(p.allowances).map(([svc, cap]) => {
    const used = (p.free_services[svc] || { units: 0 }).units;
    const label = svc === "open-meteo" ? "Open-Meteo forecast and archive (units; Leaf keeps under 8,000 of the free 10,000 a day)" : svc === "tavily:knowledge" ? "Tavily credits for plant profiles and identification" : "Tavily credits for Radar";
    return `<li><span>${esc(label)}</span> <strong>${esc(num(used))} of ${esc(num(cap))}</strong><div class="meter small-meter"><span style="width:${Math.min(100, (used / cap) * 100)}%"></span></div></li>`;
  });
  $("o-free").innerHTML = free.join("");
  const d = p.data;
  const rows = [["Gardens", d.gardens], ["Plants", d.plants], ["Places with 30-year climate data", d.climatology], ["Plant profiles built", d.species], ["Verified plant facts", d.facts], ["Season plans", d.season_plans], ["Radar reports held", d.radar_reports], ["States Radar has run for", d.radar_regions], ["API keys in use", d.api_keys]];
  $("o-data").innerHTML = rows.map(([k, v]) => `<li><span>${esc(k)}</span> <strong>${esc(num(v))}</strong></li>`).join("");
}

async function refresh() {
  try { const [u, p] = await Promise.all([get("/api/ops/usage?days=7"), get("/api/ops/platform")]); render(u, p); $("o-status").textContent = `Updated ${new Date().toLocaleTimeString()}.`; }
  catch (err) { $("o-status").textContent = err.message; }
}
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 60000);
