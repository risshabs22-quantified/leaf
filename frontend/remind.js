// Calendar reminders and the "My plants" list, shared by the app and the plants page.
// Plants live only in this browser (localStorage). The owner token that allows check-ins
// stays in the browser and in URL fragments, which are never sent to the server.
(() => {
  const KEY = "leaf-plants";
  const MAX = 60;
  const stamp = (d) => new Date(d).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");

  function load() {
    let list = [];
    try { list = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { list = []; }
    if (!Array.isArray(list)) list = [];
    // Reports saved before My plants existed: pick up their owner tokens too.
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        const m = k && k.match(/^leaf-owner-(.+)$/);
        if (m && !list.some((p) => p.id === m[1])) list.push({ id: m[1], token: localStorage.getItem(k), created: 0 });
      }
    } catch { /* storage blocked */ }
    return list.filter((p) => p && typeof p.id === "string" && /^[\w-]{1,64}$/.test(p.id));
  }

  function save(list) {
    try {
      localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)));
      // Plants past the limit go entirely, or load() would bring them back from their keys.
      list.slice(MAX).forEach((p) => localStorage.removeItem(`leaf-owner-${p.id}`));
    } catch { /* private mode */ }
  }

  function remember(plant) {
    const list = load().filter((p) => p.id !== plant.id);
    list.unshift(plant);
    save(list);
    try { if (plant.token) localStorage.setItem(`leaf-owner-${plant.id}`, plant.token); } catch { /* ignore */ }
  }

  function forget(id) {
    save(load().filter((p) => p.id !== id));
    try { localStorage.removeItem(`leaf-owner-${id}`); } catch { /* ignore */ }
  }

  // Google Calendar's "add event" link: no account connection, the person confirms it there.
  function google({ title, start, end, details = "", path = "" }) {
    const link = path ? `${location.origin}${path}` : "";
    const q = new URLSearchParams({
      action: "TEMPLATE",
      text: title,
      dates: `${stamp(start)}/${stamp(end)}`,
      details: link ? `${details}\n\n${link}` : details,
    });
    return `https://calendar.google.com/calendar/render?${q}`;
  }

  // An .ics file built by the server (Apple Calendar, Outlook, anything else).
  async function ics({ title, start, end, details = "", path = "" }) {
    const r = await fetch("/api/calendar.ics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, start: new Date(start).toISOString(), end: new Date(end).toISOString(), details, path }),
    });
    if (!r.ok) throw new Error("Leaf could not make the calendar file.");
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = "leaf-reminder.ics";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  // The check-in date: the plan's check-in days after saving, at 9:00 local time.
  function checkInDate(created, days) {
    const d = new Date(created || Date.now());
    d.setDate(d.getDate() + (days || 5));
    d.setHours(9, 0, 0, 0);
    return d;
  }

  window.LeafRemind = { load, save, remember, forget, google, ics, checkInDate };
})();
