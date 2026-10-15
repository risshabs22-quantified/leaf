// "Reported nearby": the Leaf Radar reports that matched a diagnosis, each with the page's own
// sentence and link. Every string from the server goes through textContent or esc().
window.LeafRegional = (() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u || "")) ? String(u) : "");
  const day = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
  function render(d) {
    const box = document.getElementById("regional");
    if (!box) return;
    const r = d && d.regional;
    box.hidden = !(r && r.reports && r.reports.length);
    if (box.hidden) return;
    const pts = (r.boosted || []).map((b) => `${esc(b.name)} +${Math.round(b.points * 100)} point${Math.round(b.points * 100) === 1 ? "" : "s"}`).join("; ");
    document.getElementById("regional-intro").innerHTML = `Extension services in ${esc(r.region_name)} reported this in the last ${esc(r.window_days)} days. It raised ${pts} (never more than 5 points, and never enough to make Leaf confident on its own).`;
    document.getElementById("regional-list").innerHTML = r.reports.map((x) => {
      const url = safeUrl(x.source_url);
      return `<li><strong>${esc(x.pest)}</strong>${x.hosts.length ? ` on ${esc(x.hosts.join(", "))}` : ""}${x.place ? `, ${esc(x.place)}` : ""} <span class="small">(${esc(day(x.reported_on))})</span>
        <blockquote>${esc(x.quote)}</blockquote>
        ${url ? `<a class="src" href="${esc(url)}" target="_blank" rel="noopener">${esc(x.source_title)}</a>` : `<span class="src">${esc(x.source_title)}</span>`}</li>`;
    }).join("");
  }
  return { render };
})();
