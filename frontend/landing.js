// Landing page motion. Every animation explains something and settles into a readable final
// state; with reduced motion the final state is shown straight away. No model output here.
(() => {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (id) => document.getElementById(id);
  const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));

  // ------------------------------------------------------------ the specimen (hero)
  // An example case: the scan, what the vision step notices, the differential re-ranking as
  // evidence arrives, then the verdict. Loops while visible.
  const specimen = document.querySelector(".specimen");
  const diff = $("cc-diff");
  const log = $("cc-log");
  const STATES = [
    [["Septoria leaf spot", 38], ["Early blight", 34], ["Nitrogen deficiency", 18]],
    [["Early blight", 49], ["Septoria leaf spot", 29], ["Nitrogen deficiency", 11]],
    [["Early blight", 58], ["Septoria leaf spot", 24], ["Nitrogen deficiency", 7]],
    [["Early blight", 62], ["Septoria leaf spot", 21], ["Nitrogen deficiency", 6]],
  ];
  const EVIDENCE = [
    "Looked closer: target rings, not tiny dots",
    "Weather: 9 humid days in 14",
    "Read an extension guide",
    "Second opinion agrees",
  ];

  function drawDiff(rows) {
    const before = new Map([...diff.children].map((li) => [li.dataset.k, li.getBoundingClientRect().top]));
    diff.innerHTML = rows.map(([n, p]) => `<li data-k="${n}"><div class="cc-row"><span>${n}</span><span>${p}%</span></div><div class="cc-bar"><span style="width:${p}%"></span></div></li>`).join("");
    if (reduce) return;
    [...diff.children].forEach((li) => {
      const was = before.get(li.dataset.k);
      if (was === undefined) return;
      const dy = was - li.getBoundingClientRect().top;
      if (!dy) return;
      li.style.transition = "none";
      li.style.transform = `translateY(${dy}px)`;
      requestAnimationFrame(() => { li.style.transition = ""; li.style.transform = ""; });
    });
  }

  function addLog(text) {
    const li = document.createElement("li");
    li.textContent = text;
    log.appendChild(li);
  }

  const STEPS = ["s1", "s2", "s3", "s5"];
  const setStep = (...on) => STEPS.forEach((c) => specimen.classList.toggle(c, on.includes(c)));

  function finalState() {
    setStep("s2", "s3", "s5");
    drawDiff(STATES[3]);
    log.innerHTML = "";
    EVIDENCE.forEach(addLog);
  }

  let visible = true;
  let running = false;
  async function play() {
    if (running) return;
    running = true;
    while (visible) {
      setStep();
      log.innerHTML = "";
      drawDiff(STATES[0]);
      await wait(700);
      specimen.classList.add("s1");            // scan
      await wait(2000);
      specimen.classList.add("s2");            // first callout
      addLog(EVIDENCE[0]);
      drawDiff(STATES[1]);
      await wait(1500);
      specimen.classList.add("s3");            // the other callouts
      addLog(EVIDENCE[1]);
      drawDiff(STATES[2]);
      await wait(1600);
      addLog(EVIDENCE[2]);
      await wait(1200);
      addLog(EVIDENCE[3]);
      drawDiff(STATES[3]);
      await wait(900);
      specimen.classList.add("s5");            // verdict
      await wait(5200);
    }
    running = false;
  }

  if (specimen) {
    if (reduce) finalState();
    else {
      drawDiff(STATES[0]);
      new IntersectionObserver(([e]) => {
        visible = e.isIntersecting;
        if (visible) play();
      }, { threshold: 0.25 }).observe(specimen);
    }
  }

  // ------------------------------------------------------------ "How Leaf works": the line draws as you read
  const line = $("steps-line");
  if (line && !reduce) {
    const items = [...line.children];
    const update = () => {
      const r = line.getBoundingClientRect();
      const mark = window.innerHeight * 0.6;
      const p = Math.min(1, Math.max(0, (mark - r.top) / r.height));
      line.style.setProperty("--p", p.toFixed(3));
      items.forEach((li) => li.classList.toggle("on", li.getBoundingClientRect().top < mark));
    };
    let queued = false;
    const onScroll = () => { if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; update(); }); } };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    update();
  } else if (line) {
    [...line.children].forEach((li) => li.classList.add("on"));
  }

  // ------------------------------------------------------------ reveal-on-view helpers
  const onView = (el, fn, threshold = 0.35) => {
    if (!el) return;
    if (reduce) { fn(); return; }
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { fn(); io.disconnect(); } }, { threshold });
    io.observe(el);
  };

  // Spray window miniature: five days of forecast, scored hour by hour (example data).
  const grid = $("demo-grid");
  if (grid) {
    const days = ["Tue", "Wed", "Thu", "Fri", "Sat"];
    // n = rain, wind or heat; o = possible; g = good; one string per day, 6:00 to 19:00
    const plan = ["nnnnnnnoonnnnn", "nnnnnnnnnnnnnn", "ogggggooooggoo", "oooonnnnnnnnoo", "ggooooooonnnnn"];
    const pick = { day: 2, from: 1, to: 4 };
    let i = 0;
    let html = `<span></span>${Array.from({ length: 14 }, (_, h) => `<span>${(h + 6) % 3 === 0 ? h + 6 : ""}</span>`).join("")}`;
    plan.forEach((row, d) => {
      html += `<span>${days[d]}</span>`;
      [...row].forEach((c, h) => {
        const cls = c === "g" ? "good" : c === "o" ? "ok" : "";
        const p = d === pick.day && h >= pick.from && h < pick.to ? " pick" : "";
        html += `<span class="cell ${cls}${p}" style="--i:${i++}"></span>`;
      });
    });
    grid.innerHTML = html;
    onView(grid, () => grid.classList.add("in"));
  }

  // Organic miniature: flips on once when seen; the visitor can toggle it.
  const demo = $("plan-demo");
  const toggle = $("demo-organic");
  if (demo && toggle) {
    toggle.addEventListener("change", () => demo.classList.toggle("organic", toggle.checked));
    onView(demo, () => setTimeout(() => { toggle.checked = true; demo.classList.add("organic"); }, reduce ? 0 : 900), 0.6);
  }

  onView($("demo-cal"), () => $("demo-cal").classList.add("in"), 0.6);
  onView($("acc-bars"), () => $("acc-bars").classList.add("in"), 0.3);
})();
