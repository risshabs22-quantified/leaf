// The rainforest behind every page: an original scene drawn in code (no images, nothing to
// license), built the way layered 2D game and wallpaper scenes are. Far layers are painted
// once per resize; near layers are redrawn every frame so they can move.
//
//   sky, sun glow                         static
//   far and near canopy ridges            static, stippled foliage, slow parallax
//   emergent giants above the canopy      static
//   a waterfall over a mossy rock wall    falling ribbons, plume, spray
//   drifting mist at two depths           scrolling soft clouds
//   buttress trees, canopy roof, lianas   trunks static, vines sway
//   light shafts through the canopy       breathing, additive
//   understory and forest floor           static
//   banana leaves, palms, tree ferns      bend in gusty wind, tips more than bases
//   motes, falling leaves, butterflies    particles (fireflies at dusk)
//   macaws crossing the far canopy        now and then
//   monstera framing the corners          pre-cut slits and holes, sway
//
// Atmospheric perspective: each layer is mixed toward the haze colour by its distance, and
// foliage gets finer and paler with distance. Budget: device pixel ratio capped, fewer
// elements on small screens, 30 fps on phones, paused when the tab is hidden, quality drops
// if frames run long, one still frame with prefers-reduced-motion.
(() => {
  const canvas = document.getElementById("forest");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d", { alpha: false });
  const reduceQ = window.matchMedia("(prefers-reduced-motion: reduce)");
  const darkQ = window.matchMedia("(prefers-color-scheme: dark)");
  const calm = document.body.classList.contains("calm"); // app pages: the same forest, gentler

  // ------------------------------------------------------------ helpers
  const TAU = Math.PI * 2;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  function rng(seed) {
    let s = (seed * 2654435761) >>> 0 || 1;
    return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296);
  }
  const hash = (n) => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };
  function noise(x) { // smooth 1-D value noise in [0, 1]
    const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
    return lerp(hash(i), hash(i + 1), u);
  }
  const fbm = (x) => noise(x) * 0.55 + noise(x * 2.03 + 7) * 0.28 + noise(x * 4.1 + 13) * 0.12 + noise(x * 8.3 + 29) * 0.05;
  const hex = (h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  const toHex = (c) => "#" + c.map((v) => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, "0")).join("");
  const mix = (a, b, t) => { const A = hex(a), B = hex(b); return toHex(A.map((v, i) => lerp(v, B[i], t))); };
  const rgba = (h, a) => `rgba(${hex(h).join(",")},${a})`;

  // A lens-shaped leaf added to the current path (batched: fill many with one call).
  function leafPath(g, x, y, len, wid, ang) {
    const c = Math.cos(ang), s = Math.sin(ang);
    const mx = x + c * len * 0.5, my = y + s * len * 0.5, tx = x + c * len, ty = y + s * len;
    const nx = -s * wid, ny = c * wid;
    g.moveTo(x, y);
    g.quadraticCurveTo(mx + nx, my + ny, tx, ty);
    g.quadraticCurveTo(mx - nx, my - ny, x, y);
  }

  // A clump of foliage: many small leaves in a rough dome, lit from above-right.
  function clump(g, r, x, y, rad, tones, n, leafScale = 1) {
    const paths = tones.map(() => new Path2D());
    for (let i = 0; i < n; i++) {
      // a dome: most leaves in the upper half, a ragged fringe below
      const a = r() < 0.78 ? Math.PI + r() * Math.PI : r() * Math.PI, d = Math.pow(r(), 0.6);
      const lx = x + Math.cos(a) * rad * d * (1 + 0.25 * noise(i * 0.7)), ly = y + Math.sin(a) * rad * d * (a > Math.PI ? 0.85 : 0.4);
      const up = clamp(0.55 - (ly - y) / (rad * 1.3) + (lx - x) / (rad * 4) + (r() - 0.5) * 0.45 - (1 - d) * 0.35, 0, 0.999);
      const p = paths[Math.floor(up * tones.length)];
      const len = rad * (0.16 + r() * 0.16) * leafScale;
      leafPath(p, lx, ly, len, len * 0.32, r() * TAU);
    }
    tones.forEach((t, i) => { g.fillStyle = t; g.fill(paths[i]); });
  }

  // ------------------------------------------------------------ palettes (morning and dusk)
  const DAY = {
    skyTop: "#d6ecdc", skyLow: "#f6fbef", sun: "#fff1c2", haze: "#e9f4ea",
    ridgeFar: "#b9d8c1", ridgeNear: "#86bb93", emergent: "#9fc8aa",
    rock: "#8e9c8f", rockLit: "#b4bfb2", rockDark: "#6a7a6c", moss: "#5d9662", water: "#f5fbff", pool: "#d3ece6",
    trunk: "#4b5a45", trunkLit: "#77866a", crown: "#2f6a3e", crownMid: "#418049", crownLit: "#6aa55e",
    vine: "#355f3b", palm: "#2a6a3b", palmLit: "#5c9c52", fern: "#3a874a", fernLit: "#86bd6c",
    under: "#1f5a32", underLit: "#3c7f46", banana: "#3a8a46", bananaLit: "#86c26c", fore: "#174d2a", foreLit: "#2f7a43",
    shaft: "#fff4d2", mote: "#fffbe6", leafFall: "#a6bf6a", mist: "#ffffff",
    wing: "#1f64e0", wingLit: "#85d6ff", macaw: "#c8432f", night: false,
  };
  const DUSK = {
    skyTop: "#0a1712", skyLow: "#1b3026", sun: "#3a4d38", haze: "#15241c",
    ridgeFar: "#1a2e24", ridgeNear: "#15281e", emergent: "#192c22",
    rock: "#25332b", rockLit: "#33443a", rockDark: "#18231c", moss: "#1c3826", water: "#9cb6b2", pool: "#1d3833",
    trunk: "#0e1912", trunkLit: "#1b2a1f", crown: "#0d2016", crownMid: "#122a1c", crownLit: "#1a3a25",
    vine: "#0f2318", palm: "#0c2215", palmLit: "#193a24", fern: "#0f2819", fernLit: "#1e482b",
    under: "#07140c", underLit: "#0f2416", banana: "#0e2818", bananaLit: "#1d482a", fore: "#05100a", foreLit: "#0d2014",
    shaft: "#cfe8d0", mote: "#d6ff96", leafFall: "#2b4628", mist: "#cfe0d6",
    wing: "#1f64e0", wingLit: "#85d6ff", macaw: "#7a281d", night: true,
  };
  let P = darkQ.matches ? DUSK : DAY;

  // ------------------------------------------------------------ size and quality
  let W = 0, H = 0, DPR = 1, small = false, quality = 1;
  const MARGIN = 80; // painted beyond the edges so parallax never shows a seam
  const L = {}; // static layers
  let S = null; // scene state

  function resize() {
    small = innerWidth < 760;
    DPR = Math.min(window.devicePixelRatio || 1, 1.5);
    W = innerWidth; H = innerHeight;
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    build();
  }

  function offscreen(w, h) {
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * DPR)); c.height = Math.max(1, Math.round(h * DPR));
    const g = c.getContext("2d");
    g.scale(DPR, DPR);
    return [c, g];
  }
  const FW = () => W + MARGIN * 2, FH = () => H + MARGIN * 2;

  // ------------------------------------------------------------ static layers
  function paintSky() {
    const [c, g] = offscreen(FW(), FH());
    const w = FW(), h = FH();
    const grad = g.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, P.skyTop); grad.addColorStop(0.5, P.skyLow); grad.addColorStop(1, P.haze);
    g.fillStyle = grad; g.fillRect(0, 0, w, h);
    const sx = w * 0.68, sy = h * 0.14;
    const sun = g.createRadialGradient(sx, sy, 0, sx, sy, Math.max(w, h) * 0.6);
    sun.addColorStop(0, rgba(P.sun, P.night ? 0.3 : 1)); sun.addColorStop(0.18, rgba(P.sun, 0.45)); sun.addColorStop(1, rgba(P.sun, 0));
    g.fillStyle = sun; g.fillRect(0, 0, w, h);
    return c;
  }

  // A canopy ridge: an fbm skyline, crowns stamped along it from small leaves, finer
  // stipple below, then haze rising from the bottom.
  function paintRidge(seed, baseY, amp, base, scale, haze) {
    const r = rng(seed);
    const w = FW(), h = FH();
    const [c, g] = offscreen(w, h);
    const y0 = h * baseY;
    const top = (x) => y0 - amp * (fbm(x / 300 + seed) * 1.15 + fbm(x / 80 + seed * 3) * 0.3);
    g.fillStyle = base;
    g.beginPath(); g.moveTo(0, h);
    for (let x = 0; x <= w; x += 4) g.lineTo(x, top(x) + 6 * scale);
    g.lineTo(w, h); g.closePath(); g.fill();
    const tones = [mix(base, P.haze, 0.1), base, mix(base, P.crownLit, 0.25), mix(base, P.skyLow, 0.35)];
    const crowns = Math.round((w / (26 * scale)) * (small ? 0.6 : 1));
    for (let i = 0; i < crowns; i++) {
      const x = r() * w, rad = (14 + r() * 22) * scale;
      clump(g, r, x, top(x) + rad * 0.4, rad, tones, Math.round(rad * 1.6), 0.9);
    }
    const stip = Math.round((w * (h - y0)) / (900 * scale) * (small ? 0.5 : 1));
    for (let i = 0; i < stip; i++) { // texture inside the ridge, fading with depth into mist
      const x = r() * w, y = top(x) + 10 + Math.pow(r(), 1.4) * (h - top(x)) * 0.5;
      g.fillStyle = r() < 0.5 ? rgba(P.crownLit, 0.12) : rgba(P.crown, 0.12);
      g.beginPath(); leafPath(g, x, y, 3 + r() * 6 * scale, 1.5 + r() * 2, r() * TAU); g.fill();
    }
    const fog = g.createLinearGradient(0, y0 - amp * 1.2, 0, h);
    fog.addColorStop(0, rgba(P.haze, haze * 0.25)); fog.addColorStop(0.5, rgba(P.haze, haze)); fog.addColorStop(1, rgba(P.haze, Math.min(1, haze + 0.3)));
    g.fillStyle = fog; g.fillRect(0, 0, w, h);
    return c;
  }

  // Emergent giants standing above the canopy, umbrella crowns of fine leaves.
  function paintEmergents(seed) {
    const r = rng(seed);
    const w = FW(), h = FH();
    const [c, g] = offscreen(w, h);
    const tones = [mix(P.emergent, P.ridgeFar, 0.4), P.emergent, mix(P.emergent, P.skyLow, 0.3), mix(P.emergent, P.skyLow, 0.55)];
    const count = small ? 3 : 5;
    for (let i = 0; i < count; i++) {
      const x = w * (0.06 + (i + 0.2 + r() * 0.6) / count * 0.9);
      const base = h * (0.47 + r() * 0.05), crownY = h * (0.19 + r() * 0.1);
      g.strokeStyle = mix(P.emergent, P.trunk, 0.25); g.lineCap = "round";
      g.lineWidth = 3 + r() * 3;
      g.beginPath(); g.moveTo(x, base); g.quadraticCurveTo(x + (r() - 0.5) * 24, (base + crownY) / 2, x, crownY); g.stroke();
      const spread = 70 + r() * 80;
      g.lineWidth = 1.6;
      for (let k = 0; k < 5; k++) {
        const ex = x + (k - 2) * spread * 0.32;
        g.beginPath(); g.moveTo(x, crownY + 8); g.quadraticCurveTo((x + ex) / 2, crownY - 4, ex, crownY - 6 + Math.abs(k - 2) * 6); g.stroke();
      }
      for (let k = 0; k < 11; k++) {
        const cx = x + (k - 5) * spread * 0.18 + (r() - 0.5) * 14;
        clump(g, r, cx, crownY - 10 + Math.abs(k - 5) * 3, 26 + r() * 16, tones, 70, 0.75);
      }
    }
    return c;
  }

  // A rock wall with a notch the river spills through; moss on every ledge.
  function paintCliff(fx, top, bottom) {
    const w = FW(), h = FH();
    const [c, g] = offscreen(w, h);
    const r = rng(91);
    const half = Math.min(W * 0.2, 260);
    const left = fx - half, right = fx + half;
    const edge = (x) => top - 18 + fbm(x / 60) * 36 - Math.max(0, 40 - Math.abs(x - fx)) * 0.6; // the notch
    const rock = g.createLinearGradient(left, 0, right, 0);
    rock.addColorStop(0, mix(P.rockDark, P.moss, 0.25)); rock.addColorStop(0.6, mix(P.rock, P.moss, 0.15)); rock.addColorStop(1, P.rockLit);
    g.fillStyle = rock;
    g.beginPath(); g.moveTo(left - 90, bottom + 60);
    for (let y = bottom + 60; y > top; y -= 10) g.lineTo(left + fbm(y / 50 + 3) * 50 - (bottom - y) * 0.25, y);
    for (let x = left; x <= right; x += 6) g.lineTo(x, edge(x));
    for (let y = top; y < bottom + 60; y += 10) g.lineTo(right - fbm(y / 47 + 9) * 50 + (y - top) * 0.25, y);
    g.closePath(); g.fill();
    for (let i = 0; i < 70; i++) { // vertical fluting down the face, dark grooves with lit ribs
      const x = lerp(left - 30, right + 30, r()), y = edge(x) + 6 + r() * 20, len = 40 + r() * (bottom - top);
      g.strokeStyle = r() < 0.6 ? rgba(P.rockDark, 0.45) : rgba(P.rockLit, 0.35); g.lineWidth = 1 + r() * 2.5;
      g.beginPath(); g.moveTo(x, y); g.bezierCurveTo(x + (r() - 0.5) * 10, y + len * 0.3, x + (r() - 0.5) * 14, y + len * 0.7, x + (r() - 0.5) * 12, y + len); g.stroke();
    }
    g.strokeStyle = rgba(P.rockLit, 0.8); g.lineWidth = 2; // the sunlit rim
    g.beginPath(); for (let x = left; x <= right; x += 6) (x === left ? g.moveTo(x, edge(x) + 2) : g.lineTo(x, edge(x) + 2)); g.stroke();
    const moss = [mix(P.moss, P.rockDark, 0.3), P.moss, mix(P.moss, P.crownLit, 0.45), mix(P.moss, P.haze, 0.3)];
    for (let x = left - 60; x < right + 60; x += 14) clump(g, r, x, edge(x) + 6, 20 + r() * 8, moss, 34, 0.85);
    for (let y = top + 10; y < bottom + 40; y += 12) { // vegetation hangs over both sides of the wall
      for (const side of [-1, 1]) {
        const reach = half * (0.45 + 0.5 * fbm(y / 40 + side * 5));
        for (let k = 0; k < 3; k++) {
          const x = fx + side * (36 + r() * Math.max(10, half + (y - top) * 0.25 - 36) * (k === 0 ? 1 : 0.6) + reach * 0.2);
          if (Math.abs(x - fx) < 30 + reach * 0.3) continue; // leave bare rock beside the water
          clump(g, r, x, y, 14 + r() * 12, moss, 26, 0.85);
        }
      }
    }
    const fog = g.createLinearGradient(0, top - 40, 0, bottom + 60);
    fog.addColorStop(0, rgba(P.haze, 0.12)); fog.addColorStop(1, rgba(P.haze, 0.5));
    g.fillStyle = fog; g.fillRect(0, 0, w, h);
    return c;
  }

  // The forest between the hills and the near trees: hazy crowns on thin trunks.
  function paintMidForest(seed) {
    const r = rng(seed);
    const w = FW(), h = FH();
    const [c, g] = offscreen(w, h);
    const base = mix(P.crownMid, P.haze, 0.45);
    const tones = [mix(P.crown, P.haze, 0.5), base, mix(P.crownLit, P.haze, 0.4), mix(P.crownLit, P.skyLow, 0.55)];
    const count = small ? 14 : 26;
    for (let i = 0; i < count; i++) {
      const x = (i + r()) / count * w, cy = h * (0.5 + r() * 0.12), rad = 34 + r() * 40;
      g.strokeStyle = mix(P.trunk, P.haze, 0.5); g.lineWidth = 2 + r() * 2;
      g.beginPath(); g.moveTo(x, cy); g.lineTo(x + (r() - 0.5) * 10, h * 0.8); g.stroke();
      clump(g, r, x, cy, rad * 0.8, [tones[0], tones[0], tones[1]], small ? 70 : 120, 1.3);
      clump(g, r, x, cy - rad * 0.15, rad, tones, small ? 90 : 170, 0.85);
    }
    const fog = g.createLinearGradient(0, h * 0.45, 0, h * 0.85);
    fog.addColorStop(0, rgba(P.haze, 0.05)); fog.addColorStop(1, rgba(P.haze, 0.75));
    g.fillStyle = fog; g.fillRect(0, 0, w, h);
    return c;
  }

  function paintMist(seed, alpha) {
    const w = Math.round(FW()), h = Math.round(H * 0.45);
    const [c, g] = offscreen(w, h);
    const r = rng(seed);
    for (let i = 0; i < (small ? 24 : 44); i++) {
      const x = r() * w, y = h * (0.35 + r() * 0.4), rad = 70 + r() * 170;
      const grd = g.createRadialGradient(x, y, 0, x, y, rad);
      grd.addColorStop(0, rgba(P.mist, alpha * (0.45 + r() * 0.55))); grd.addColorStop(1, rgba(P.mist, 0));
      g.fillStyle = grd;
      for (const ox of [-w, 0, w]) { g.beginPath(); g.ellipse(x + ox, y, rad * 1.6, rad * 0.6, 0, 0, TAU); g.fill(); }
    }
    return c;
  }

  function makeTrunks(seed) {
    const r = rng(seed);
    const xs = small ? [0.03, 0.95] : [0.03, 0.16, 0.82, 0.96];
    return xs.map((fx) => ({
      x: FW() * fx, wid: (small ? 22 : 30) + r() * 22, lean: (r() - 0.5) * 0.06,
      vines: Array.from({ length: 2 + Math.floor(r() * 2) }, () => ({ dx: (r() - 0.5) * 110, len: 0.35 + r() * 0.45, phase: r() * TAU })),
      epi: Array.from({ length: 3 }, () => ({ y: 0.25 + r() * 0.45, side: r() < 0.5 ? -1 : 1 })),
    }));
  }

  // Buttress-rooted trunks and the dense canopy roof across the top of the screen.
  function paintTrunks(trunks) {
    const w = FW(), h = FH();
    const [c, g] = offscreen(w, h);
    const ground = h * 0.92;
    const r = rng(17);
    for (const t of trunks) {
      const topX = t.x + t.lean * h;
      const bark = g.createLinearGradient(t.x - t.wid, 0, t.x + t.wid, 0);
      bark.addColorStop(0, P.trunk); bark.addColorStop(0.7, P.trunkLit); bark.addColorStop(1, P.trunk);
      g.fillStyle = bark;
      g.beginPath();
      g.moveTo(topX - t.wid * 0.45, -20); g.lineTo(topX + t.wid * 0.45, -20);
      g.bezierCurveTo(t.x + t.wid * 0.5, h * 0.5, t.x + t.wid * 0.6, ground - 130, t.x + t.wid * 2.8, ground);
      g.lineTo(t.x - t.wid * 2.8, ground);
      g.bezierCurveTo(t.x - t.wid * 0.6, ground - 130, t.x - t.wid * 0.5, h * 0.5, topX - t.wid * 0.45, -20);
      g.fill();
      g.strokeStyle = rgba(P.trunk, 0.9); g.lineWidth = 2.2;
      for (const s of [-1, -0.4, 0.45, 1]) {
        g.beginPath(); g.moveTo(t.x + s * t.wid * 0.3, ground - 170); g.quadraticCurveTo(t.x + s * t.wid * 0.95, ground - 50, t.x + s * t.wid * 2.5, ground); g.stroke();
      }
      g.strokeStyle = rgba(P.trunkLit, 0.3); g.lineWidth = 1;
      for (let k = 0; k < 8; k++) {
        const off = (k / 7 - 0.5) * t.wid * 0.75;
        g.beginPath(); g.moveTo(topX + off, -20); g.quadraticCurveTo(t.x + off * 1.1, h * 0.55, t.x + off * 1.7, ground - 40); g.stroke();
      }
      g.fillStyle = rgba(P.moss, 0.55); // moss creeping up the bark
      for (let k = 0; k < 40; k++) { const yy = lerp(ground - 40, h * 0.35, Math.pow(r(), 1.5)); g.beginPath(); leafPath(g, lerp(topX, t.x, yy / h) + (r() - 0.5) * t.wid, yy, 4 + r() * 6, 2, r() * TAU); g.fill(); }
      for (const e of t.epi) { // bromeliad rosettes on the bark
        const ey = h * e.y, ex = lerp(topX, t.x, e.y) + e.side * t.wid * 0.45;
        const p = new Path2D();
        for (let k = 0; k < 9; k++) leafPath(p, ex, ey, 18 + (k % 3) * 5, 3, -Math.PI / 2 + (k - 4) * 0.33);
        g.fillStyle = P.crownLit; g.fill(p);
      }
    }
    // canopy roof: dense layered crowns that run off the top edge
    const tones = [P.crown, P.crownMid, P.crownLit, mix(P.crownLit, P.skyLow, 0.35)];
    const roof = small ? 26 : 52;
    for (let i = 0; i < roof; i++) {
      const x = (i / roof) * w + (r() - 0.5) * 60, y = -40 + Math.pow(r(), 1.8) * h * 0.14;
      clump(g, r, x, y - 10, 70 + r() * 40, [P.crown, P.crown, P.crownMid], small ? 50 : 90, 1.3);
      clump(g, r, x, y + 10, 60 + r() * 50, tones, small ? 80 : 130, 1);
    }
    return c;
  }

  // Ground cover in front of the trunks: dark, dense, a few lighter leaves catching light.
  function paintUnder() {
    const w = FW(), h = FH();
    const [c, g] = offscreen(w, h);
    const r = rng(41);
    const tones = [P.under, mix(P.under, P.underLit, 0.5), P.underLit, mix(P.underLit, P.fernLit, 0.4)];
    const floor = g.createLinearGradient(0, h * 0.8, 0, h);
    floor.addColorStop(0, rgba(P.under, 0)); floor.addColorStop(0.5, rgba(P.under, 0.75)); floor.addColorStop(1, P.under);
    g.fillStyle = floor; g.fillRect(0, h * 0.8, w, h * 0.2);
    for (let x = -20; x < w + 20; x += small ? 34 : 26) {
      clump(g, r, x, h * (0.93 + r() * 0.05), 34 + r() * 30, tones, small ? 40 : 60, 1.15);
    }
    return c;
  }

  // Monstera leaf, pre-cut: deep slits from the edge and holes beside the midrib.
  function paintMonstera(size, seed) {
    const r = rng(seed);
    const s = size, [c, g] = offscreen(s * 1.3, s * 1.15);
    const cx = s * 0.65, base = s * 1.1;
    g.translate(cx, base);
    const lg = g.createLinearGradient(-s * 0.5, 0, s * 0.5, -s);
    lg.addColorStop(0, P.fore); lg.addColorStop(1, P.foreLit);
    g.fillStyle = lg;
    g.beginPath();
    g.moveTo(0, -s * 0.05);
    g.bezierCurveTo(s * 0.62, -s * 0.12, s * 0.66, -s * 0.9, 0, -s);
    g.bezierCurveTo(-s * 0.66, -s * 0.9, -s * 0.62, -s * 0.12, 0, -s * 0.05);
    g.fill();
    g.globalCompositeOperation = "destination-out";
    for (const side of [-1, 1]) {
      for (let k = 0; k < 6; k++) { // slits along the side veins
        const q = 0.2 + k * 0.13, my = -s * q, ang = side > 0 ? -0.35 - k * 0.12 : Math.PI + 0.35 + k * 0.12;
        const inner = s * (0.18 + r() * 0.08), outer = s * 0.7;
        g.beginPath();
        g.moveTo(Math.cos(ang) * inner, my + Math.sin(ang) * inner);
        g.lineTo(Math.cos(ang) * outer - side * 4, my + Math.sin(ang) * outer - 5);
        g.lineTo(Math.cos(ang) * outer + side * 4, my + Math.sin(ang) * outer + 5);
        g.closePath(); g.fill();
        if (k > 0 && k < 5) { // a hole between slits
          const ha = ang + (side > 0 ? 0.18 : -0.18), hr = s * 0.11;
          g.beginPath(); g.ellipse(Math.cos(ha) * hr, my + Math.sin(ha) * hr - s * 0.04, s * 0.028, s * 0.016, ha, 0, TAU); g.fill();
        }
      }
    }
    g.globalCompositeOperation = "source-over";
    g.strokeStyle = rgba(P.foreLit, 0.9); g.lineWidth = Math.max(1.5, s * 0.008);
    g.beginPath(); g.moveTo(0, 0); g.quadraticCurveTo(s * 0.03, -s * 0.5, 0, -s * 0.98); g.stroke();
    g.strokeStyle = P.fore; g.lineWidth = Math.max(3, s * 0.02); // petiole
    g.beginPath(); g.moveTo(0, 0); g.lineTo(0, s * 0.1); g.stroke();
    return { c, ax: cx, ay: base };
  }

  // Banana leaf, pre-cut: a long paddle with wind tears running in to the midrib.
  function paintBanana(len, seed) {
    const r = rng(seed);
    const wd = len * 0.22, [c, g] = offscreen(wd * 2.2, len * 1.05);
    const cx = wd * 1.1, base = len;
    g.translate(cx, base);
    const lg = g.createLinearGradient(-wd, 0, wd, 0);
    const shade = r() * 0.35;
    lg.addColorStop(0, mix(P.banana, P.under, shade)); lg.addColorStop(0.5, mix(P.bananaLit, P.banana, shade)); lg.addColorStop(1, mix(P.banana, P.under, shade));
    g.fillStyle = lg;
    g.beginPath(); g.moveTo(0, 0);
    g.bezierCurveTo(wd, -len * 0.18, wd * 0.95, -len * 0.8, 0, -len);
    g.bezierCurveTo(-wd * 0.95, -len * 0.8, -wd, -len * 0.18, 0, 0); g.fill();
    g.globalCompositeOperation = "destination-out";
    g.lineWidth = 1.6;
    for (let k = 0; k < 9; k++) {
      const y = -len * (0.18 + r() * 0.75), side = r() < 0.5 ? -1 : 1;
      g.beginPath(); g.moveTo(side * wd, y - wd * 0.3); g.lineTo(side * wd * (0.15 + r() * 0.3), y); g.stroke();
    }
    g.globalCompositeOperation = "source-over";
    g.strokeStyle = rgba(P.underLit, 0.8); g.lineWidth = Math.max(1.5, len * 0.012);
    g.beginPath(); g.moveTo(0, 0); g.lineTo(0, -len * 0.97); g.stroke();
    g.strokeStyle = rgba(P.underLit, 0.25); g.lineWidth = 0.8;
    for (let k = 1; k < 26; k++) { const y = -len * k / 27; g.beginPath(); g.moveTo(0, y); g.lineTo(wd * 0.9, y - wd * 0.35); g.moveTo(0, y); g.lineTo(-wd * 0.9, y - wd * 0.35); g.stroke(); }
    return { c, ax: cx, ay: base };
  }

  // ------------------------------------------------------------ build the scene
  function build() {
    P = darkQ.matches ? DUSK : DAY;
    const r = rng(7);
    const fx = FW() * (small ? 0.62 : 0.4), fallTop = H * 0.34 + MARGIN, fallBottom = H * 0.6 + MARGIN;
    L.sky = paintSky();
    L.ridgeFar = paintRidge(3, 0.5, H * 0.12, P.ridgeFar, 0.6, 0.38);
    L.emergents = paintEmergents(5);
    L.cliff = paintCliff(fx, fallTop, fallBottom);
    L.ridgeNear = paintRidge(11, 0.67, H * 0.1, P.ridgeNear, 1, 0.35);
    L.midForest = paintMidForest(33);
    L.mistFar = paintMist(21, P.night ? 0.16 : 0.5);
    L.mistNear = paintMist(29, P.night ? 0.12 : 0.35);
    const trunks = makeTrunks(13);
    L.trunks = paintTrunks(trunks);
    L.under = paintUnder();
    const unit = Math.min(W, H * 1.4);
    const n = small ? 0.55 : 1;
    S = {
      fall: { x: fx - MARGIN, top: fallTop - MARGIN, bottom: fallBottom - MARGIN, w: small ? 14 : 22,
        streaks: Array.from({ length: Math.round(30 * n) + 8 }, () => ({ x: r(), speed: 0.55 + r() * 0.5, len: 0.12 + r() * 0.3, phase: r(), a: 0.3 + r() * 0.5 })) },
      trunks,
      shafts: Array.from({ length: small ? 3 : 5 }, (_, i) => ({ x: 0.4 + i * 0.11 + r() * 0.05, w: 36 + r() * 70, tilt: 0.26 + r() * 0.1, phase: r() * 10 })),
      palms: [
        { x: 0.1, base: 0.99, h: 0.62, lean: 0.11, fronds: 12, scale: 1.0, depth: 0.75 },
        { x: 0.9, base: 1.0, h: 0.7, lean: -0.1, fronds: 12, scale: 1.08, depth: 0.8 },
        ...(small ? [] : [{ x: 0.67, base: 0.9, h: 0.4, lean: -0.05, fronds: 10, scale: 0.68, depth: 0.55 }]),
      ].map((p) => ({ ...p, angles: Array.from({ length: p.fronds }, (_, i) => -Math.PI / 2 + (i / (p.fronds - 1) - 0.5) * 3.4 + (r() - 0.5) * 0.25), lens: Array.from({ length: p.fronds }, () => 0.75 + r() * 0.35) })),
      ferns: Array.from({ length: small ? 3 : 6 }, (_, i, a) => ({ x: (i + 0.5) / (small ? 3 : 6) + (r() - 0.5) * 0.08, base: 0.95 + r() * 0.05, size: 0.15 + r() * 0.07, fronds: 8 + Math.floor(r() * 3), seed: r() * 100 })),
      bananas: (small ? [0.3, 0.72] : [0.27, 0.73, 0.47, 0.57]).map((x, i) => ({ x, base: 1.02, leaves: Array.from({ length: 5 }, (_, k) => ({ a: (k - 2) * 0.5 + (r() - 0.5) * 0.2, sprite: paintBanana(unit * (0.2 + r() * 0.08), i * 10 + k), ph: r() * TAU })) })),
      monstera: [
        { x: -0.03, y: 1.04, rot: -0.6, sprite: paintMonstera(unit * (small ? 0.46 : 0.36), 1) },
        { x: 0.09, y: 1.09, rot: 0.12, sprite: paintMonstera(unit * (small ? 0.32 : 0.27), 2) },
        { x: 1.03, y: 1.05, rot: 0.62, sprite: paintMonstera(unit * (small ? 0.44 : 0.34), 3) },
      ],
      motes: Array.from({ length: Math.round((small ? 30 : 70) * n) }, () => ({ x: r(), y: r(), z: r(), sp: 0.2 + r() * 0.8, ph: r() * TAU })),
      falling: Array.from({ length: small ? 4 : 8 }, () => ({ x: r(), y: r(), sp: 0.25 + r() * 0.35, rot: r() * TAU, spin: (r() - 0.5) * 2, size: 5 + r() * 6, ph: r() * TAU })),
      flies: Array.from({ length: P.night ? (small ? 14 : 26) : (small ? 2 : 3) }, () => ({ x: 0.1 + r() * 0.8, y: 0.45 + r() * 0.35, ph: r() * 100, sp: 0.6 + r() * 0.6 })),
      birds: { next: 5, t: -1, y: 0.3, dir: 1 },
    };
    // the vignette never changes, so paint it once too
    const [vc, vg] = offscreen(W, H);
    const v = vg.createRadialGradient(W / 2, H * 0.45, Math.min(W, H) * 0.35, W / 2, H * 0.5, Math.max(W, H) * 0.8);
    v.addColorStop(0, "rgba(0,0,0,0)"); v.addColorStop(1, P.night ? "rgba(0,0,0,0.45)" : "rgba(20,50,30,0.16)");
    vg.fillStyle = v; vg.fillRect(0, 0, W, H);
    L.vignette = vc;
  }

  // ------------------------------------------------------------ the moving parts
  // Wind: a slow breeze plus gusts rolling across the scene from left to right.
  const wind = (t, x) => 0.35 * Math.sin(t * 0.7 + x * 2) + 0.65 * (fbm(t * 0.18 - x * 0.9) - 0.42) * 2;

  function drawFall(t, ox, oy) {
    const f = S.fall, x0 = f.x + ox, top = f.top + oy, bot = f.bottom + oy, len = bot - top;
    const body = ctx.createLinearGradient(0, top, 0, bot);
    body.addColorStop(0, rgba(P.water, 0.6)); body.addColorStop(1, rgba(P.water, 0.9));
    ctx.fillStyle = body;
    ctx.beginPath();
    ctx.moveTo(x0 - f.w * 0.5, top); ctx.quadraticCurveTo(x0, top - 4, x0 + f.w * 0.5, top);
    ctx.lineTo(x0 + f.w * 0.9, bot); ctx.lineTo(x0 - f.w * 0.9, bot); ctx.closePath(); ctx.fill();
    ctx.lineCap = "round";
    for (const s of f.streaks) { // white ribbons racing down
      const p = (t * s.speed * 0.55 + s.phase) % 1;
      const y1 = top + p * len * 1.3 - len * 0.15, y2 = y1 + s.len * len;
      const spread = lerp(0.5, 0.9, clamp((y1 - top) / len, 0, 1));
      const xx = x0 + (s.x - 0.5) * f.w * 2 * spread;
      ctx.strokeStyle = `rgba(255,255,255,${s.a})`; ctx.lineWidth = 1 + s.a * 1.6;
      ctx.beginPath(); ctx.moveTo(xx, clamp(y1, top, bot)); ctx.lineTo(xx + 0.6, clamp(y2, top, bot)); ctx.stroke();
    }
    for (let i = 0; i < 7; i++) { // the plume boils up from the plunge pool
      const p = (t * 0.11 + i / 7) % 1, rad = lerp(f.w * 0.9, f.w * 3.6, p), y = bot - p * 46;
      const g2 = ctx.createRadialGradient(x0, y, 0, x0, y, rad);
      g2.addColorStop(0, `rgba(255,255,255,${0.45 * (1 - p)})`); g2.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = g2; ctx.beginPath(); ctx.arc(x0, y, rad, 0, TAU); ctx.fill();
    }
  }

  function drawVines(t, ox, oy) {
    ctx.lineCap = "round";
    const leaves = new Path2D();
    ctx.strokeStyle = P.vine; ctx.lineWidth = 2;
    for (const tr of S.trunks) {
      for (const v of tr.vines) {
        const x = tr.x - MARGIN + ox + v.dx, endY = H * v.len;
        const sway = (Math.sin(t * 0.6 + v.phase) * 9 + wind(t, x / W) * 13) * (calm ? 0.6 : 1);
        ctx.beginPath(); ctx.moveTo(x, -10 + oy); ctx.quadraticCurveTo(x + sway * 0.5, endY * 0.6 + oy, x + sway, endY + oy); ctx.stroke();
        for (let k = 1; k < 9; k++) {
          const p = k / 9, lx = x + sway * p * p, ly = endY * p + oy;
          leafPath(leaves, lx, ly, 9, 3.5, k % 2 ? 0.9 + sway * 0.01 : Math.PI - 0.9 + sway * 0.01);
        }
      }
    }
    ctx.fillStyle = P.crownMid; ctx.fill(leaves);
  }

  function drawShafts(t) {
    ctx.save();
    ctx.globalCompositeOperation = P.night ? "lighter" : "screen";
    for (const s of S.shafts) {
      const breathe = 0.45 + 0.55 * noise(t * 0.12 + s.phase);
      const x = W * s.x, top = -20, bot = H * 0.95;
      const g = ctx.createLinearGradient(0, top, 0, bot);
      const a = (P.night ? 0.05 : 0.24) * breathe;
      g.addColorStop(0, rgba(P.shaft, a)); g.addColorStop(0.7, rgba(P.shaft, a * 0.4)); g.addColorStop(1, rgba(P.shaft, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.moveTo(x, top); ctx.lineTo(x + s.w, top);
      ctx.lineTo(x + s.w * 2.4 - s.tilt * bot, bot); ctx.lineTo(x - s.tilt * bot, bot); ctx.closePath(); ctx.fill();
    }
    ctx.restore();
  }

  // A pinnate frond: a rachis that arches and bends with the wind (most at the tip), with
  // tapered leaflets that hang under gravity. All leaflets of a tone go into one path.
  function frond(paths, x, y, angle, length, droop, bend, leafLen, width, segs) {
    let a = angle, px = x, py = y;
    const step = length / segs;
    const rachis = new Path2D();
    rachis.moveTo(x, y);
    for (let k = 1; k <= segs; k++) {
      const p = k / segs;
      a += droop / segs + bend * p * 0.16;
      const nx = px + Math.cos(a) * step, ny = py + Math.sin(a) * step;
      rachis.lineTo(nx, ny);
      const ll = leafLen * Math.sin(Math.PI * (0.12 + p * 0.85)) * (1 - p * 0.3);
      for (const h of [0, 0.5]) {
        const bx = lerp(px, nx, h), by = lerp(py, ny, h);
        for (const side of [-1, 1]) {
          const fa = a + side * 1.15;
          const grav = 0.45 + 0.3 * p; // leaflets droop more toward the tip
          let dx = Math.cos(fa) * (1 - grav), dy = Math.sin(fa) * (1 - grav) + grav;
          const m = Math.hypot(dx, dy) || 1; dx /= m; dy /= m;
          leafPath(paths[(k + (side > 0 ? 1 : 0)) % paths.length], bx, by, ll, ll * 0.1, Math.atan2(dy, dx));
        }
      }
      px = nx; py = ny;
    }
    ctx.lineWidth = width; ctx.stroke(rachis);
  }

  function drawPalm(p, t, ox, oy) {
    const s = Math.min(W, H * 1.4) * p.scale;
    const bx = W * p.x + ox * p.depth, by = H * p.base + oy * p.depth;
    const sway = wind(t, p.x) * 0.05 * (calm ? 0.5 : 1);
    const tx = bx + (p.lean + sway) * H * p.h, ty = by - H * p.h;
    const haze = 1 - p.depth;
    ctx.strokeStyle = mix(P.trunk, P.haze, haze * 0.5); ctx.lineCap = "round";
    ctx.lineWidth = 9 * p.scale;
    ctx.beginPath(); ctx.moveTo(bx, by); ctx.quadraticCurveTo(bx + (tx - bx) * 0.2, (by + ty) / 2, tx, ty); ctx.stroke();
    ctx.strokeStyle = rgba(P.trunkLit, 0.6); ctx.lineWidth = 1.2;
    for (let k = 1; k < 16; k++) {
      const q = k / 16, cx = lerp(bx, tx, q * q * 0.8 + q * 0.2), cy = lerp(by, ty, q);
      ctx.beginPath(); ctx.moveTo(cx - 4.5 * p.scale, cy); ctx.lineTo(cx + 4.5 * p.scale, cy - 1); ctx.stroke();
    }
    const tones = [mix(P.palm, P.haze, haze * 0.5), mix(P.palmLit, P.haze, haze * 0.5)];
    const paths = tones.map(() => new Path2D());
    ctx.strokeStyle = tones[0];
    const segs = quality > 0.7 ? 14 : 9;
    p.angles.forEach((a0, i) => {
      const g = wind(t + i * 0.13, p.x + i * 0.02) * (calm ? 0.5 : 1);
      const droop = 0.9 * Math.cos(a0 + Math.PI / 2) + 0.55; // side fronds arch down more
      frond(paths, tx, ty, a0 + g * 0.07, s * 0.25 * p.lens[i], droop, g * 0.8, s * 0.05, 2.6 * p.scale, segs);
    });
    tones.forEach((tone, i) => { ctx.fillStyle = tone; ctx.fill(paths[i]); });
  }

  function drawFern(f, t, ox, oy) {
    const s = Math.min(W, H * 1.4) * f.size;
    const bx = W * f.x + ox * 0.65, by = H * f.base + oy * 0.65;
    const paths = [new Path2D(), new Path2D()];
    ctx.strokeStyle = P.fern;
    for (let i = 0; i < f.fronds; i++) {
      const a0 = -Math.PI / 2 + (i / (f.fronds - 1) - 0.5) * 2.7;
      const g = wind(t * 1.2 + i * 0.2, f.x) * (calm ? 0.5 : 1);
      frond(paths, bx, by, a0, s * (0.7 + hash(f.seed + i) * 0.4), 0.8 * Math.sign(Math.cos(a0)), g * 0.6, s * 0.075, 1.6, quality > 0.7 ? 12 : 8);
    }
    ctx.fillStyle = P.fern; ctx.fill(paths[0]);
    ctx.fillStyle = P.fernLit; ctx.fill(paths[1]);
  }

  function drawSprite(sp, x, y, rot) {
    ctx.save(); ctx.translate(x, y); ctx.rotate(rot);
    ctx.drawImage(sp.c, -sp.ax, -sp.ay, sp.c.width / DPR, sp.c.height / DPR);
    ctx.restore();
  }

  function drawBananas(t, ox, oy) {
    for (const b of S.bananas) {
      const bx = W * b.x + ox * 0.7, by = H * b.base + oy * 0.7;
      for (const lf of b.leaves) drawSprite(lf.sprite, bx, by, lf.a + wind(t * 0.9 + lf.ph, b.x) * 0.07 * (calm ? 0.5 : 1));
    }
  }

  function drawMonstera(t, ox, oy) {
    for (const m of S.monstera) drawSprite(m.sprite, W * m.x + ox * 1.2, H * m.y + oy * 1.1, m.rot + wind(t * 0.8, m.x) * 0.035 * (calm ? 0.5 : 1));
  }

  function drawParticles(t, dt, ox, oy) {
    ctx.fillStyle = rgba(P.mote, P.night ? 0.25 : 0.6);
    for (const m of S.motes) { // motes drifting up through the light
      m.y -= dt * 0.006 * m.sp; m.x += dt * 0.004 * Math.sin(t * 0.3 + m.ph);
      if (m.y < -0.02) { m.y = 1.02; m.x = Math.random(); }
      ctx.globalAlpha = (0.35 + 0.65 * Math.sin(t * 2 + m.ph) ** 2) * (0.4 + m.z * 0.6);
      ctx.beginPath(); ctx.arc(m.x * W + ox * m.z, m.y * H + oy * m.z, 0.8 + m.z * 1.6, 0, TAU); ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = P.leafFall;
    for (const l of S.falling) { // leaves tumbling down
      l.y += dt * 0.025 * l.sp; l.x += dt * 0.01 * (Math.sin(t * 0.9 + l.ph) * 0.6 + wind(t, l.x) * 0.5); l.rot += dt * l.spin * 0.6;
      if (l.y > 1.05) { l.y = -0.05; l.x = Math.random(); }
      ctx.save(); ctx.translate(l.x * W + ox * 0.9, l.y * H + oy * 0.9); ctx.rotate(l.rot); ctx.scale(1, Math.cos(l.rot * 1.7));
      ctx.beginPath(); leafPath(ctx, -l.size, 0, l.size * 2, l.size * 0.45, 0); ctx.fill();
      ctx.restore();
    }
    for (const f of S.flies) { // blue morphos by day, fireflies at dusk
      const x = W * (f.x + 0.2 * (fbm(t * 0.07 * f.sp + f.ph) - 0.5) * 2), y = H * (f.y + 0.12 * (fbm(t * 0.09 * f.sp + f.ph + 50) - 0.5) * 2);
      if (P.night) {
        const glow = Math.max(0, Math.sin(t * 1.4 * f.sp + f.ph)) ** 3;
        if (glow < 0.02) continue;
        const g = ctx.createRadialGradient(x, y, 0, x, y, 9);
        g.addColorStop(0, rgba(P.mote, 0.9 * glow)); g.addColorStop(1, rgba(P.mote, 0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, 9, 0, TAU); ctx.fill();
        continue;
      }
      const flap = Math.abs(Math.sin(t * 9 * f.sp + f.ph)) * 0.85 + 0.15;
      ctx.save(); ctx.translate(x, y); ctx.rotate(Math.sin(t + f.ph) * 0.3);
      for (const side of [-1, 1]) {
        const wg = ctx.createLinearGradient(0, 0, side * 14, 0);
        wg.addColorStop(0, P.wing); wg.addColorStop(1, P.wingLit);
        ctx.fillStyle = wg;
        ctx.beginPath(); ctx.ellipse(side * 6 * flap, -2, 7 * flap, 5, side * 0.4, 0, TAU); ctx.fill();
        ctx.beginPath(); ctx.ellipse(side * 4 * flap, 4, 5 * flap, 3.5, -side * 0.3, 0, TAU); ctx.fill();
      }
      ctx.fillStyle = "#1a2a2a"; ctx.fillRect(-0.8, -5, 1.6, 10);
      ctx.restore();
    }
  }

  // Now and then a pair of macaws crosses the far canopy.
  function drawBirds(t, dt) {
    const b = S.birds;
    if (b.t < 0) {
      b.next -= dt;
      if (b.next <= 0) { b.t = 0; b.y = 0.2 + Math.random() * 0.16; b.dir = Math.random() < 0.5 ? 1 : -1; }
      return;
    }
    b.t += dt / 10;
    if (b.t > 1) { b.t = -1; b.next = 16 + Math.random() * 22; return; }
    ctx.strokeStyle = P.night ? "#0a140e" : mix(P.macaw, P.haze, 0.3); ctx.lineWidth = 2; ctx.lineCap = "round";
    for (let k = 0; k < 2; k++) {
      const p = b.dir > 0 ? b.t : 1 - b.t;
      const x = lerp(-60, W + 60, p) - k * 34 * b.dir, y = H * b.y + Math.sin(b.t * 6 + k) * 8 + k * 14;
      const flap = Math.sin(t * 10 + k * 1.3);
      ctx.beginPath(); ctx.moveTo(x - 8, y - flap * 5); ctx.quadraticCurveTo(x - 3, y - 2, x, y); ctx.quadraticCurveTo(x + 3, y - 2, x + 8, y - flap * 5); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x - b.dir * 11, y + 2); ctx.stroke();
    }
  }

  // ------------------------------------------------------------ one frame
  let mouseX = 0, mouseY = 0, scrollP = 0, last = performance.now(), slow = 0;
  const T0 = performance.now();

  function frame(now, still) {
    const t = still ? 14 : (now - T0) / 1000;
    const dt = still ? 0 : Math.min(0.05, (now - last) / 1000);
    last = now;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    const lift = scrollP * 60;
    const px = (d) => -MARGIN + mouseX * 14 * d, py = (d) => -MARGIN + (mouseY * 8 - lift) * d;
    const layer = (img, d) => ctx.drawImage(img, px(d), py(d), FW(), FH());
    const mist = (img, speed, y, d) => {
      const x = (t * speed) % FW();
      ctx.drawImage(img, x - FW(), y + py(d) + MARGIN, FW(), H * 0.45);
      ctx.drawImage(img, x, y + py(d) + MARGIN, FW(), H * 0.45);
    };
    layer(L.sky, 0.02);
    layer(L.ridgeFar, 0.12);
    layer(L.emergents, 0.18);
    mist(L.mistFar, 6, H * 0.3, 0.2);
    layer(L.cliff, 0.3);
    drawFall(t, px(0.3) + MARGIN, py(0.3) + MARGIN);
    layer(L.ridgeNear, 0.38);
    layer(L.midForest, 0.45);
    mist(L.mistNear, 11, H * 0.5, 0.5);
    drawBirds(t, dt);
    layer(L.trunks, 0.55);
    drawVines(t, px(0.55) + MARGIN, py(0.55) + MARGIN);
    drawShafts(t);
    layer(L.under, 0.7);
    const ox = mouseX * 14, oy = mouseY * 8 - lift;
    drawBananas(t, ox, oy);
    for (const p of S.palms) drawPalm(p, t, ox, oy);
    for (const f of S.ferns) drawFern(f, t, ox, oy);
    drawParticles(t, dt, ox, oy);
    drawMonstera(t, ox, oy);
    ctx.drawImage(L.vignette, 0, 0, W, H);
  }

  // ------------------------------------------------------------ loop and budget
  let rafId = 0, lastDraw = 0;
  function loop(now) {
    rafId = requestAnimationFrame(loop);
    // 60 fps on desktops (120 Hz screens would double the work for nothing), 30 on phones
    const gap = small || quality < 0.7 ? 1000 / 30 : 1000 / 60;
    if (now - lastDraw < gap - 1) return;
    const t0 = performance.now();
    frame(now, false);
    lastDraw = now;
    slow = slow * 0.95 + (performance.now() - t0 > 14 ? 0.05 : 0); // adapt if frames run long
    if (slow > 0.5 && quality > 0.5) { quality = 0.5; slow = 0; }
  }
  const start = () => { if (!rafId && !reduceQ.matches) { last = performance.now(); rafId = requestAnimationFrame(loop); } };
  const stop = () => { cancelAnimationFrame(rafId); rafId = 0; };

  resize();
  frame(performance.now(), true);
  canvas.classList.add("ready");
  start();

  let resizeTimer = 0;
  let lastW = innerWidth;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      // phones fire resize when the address bar slides; only rebuild on a real change
      if (small && Math.abs(innerWidth - lastW) < 2 && Math.abs(innerHeight - H) < 120) return;
      lastW = innerWidth; resize(); frame(performance.now(), reduceQ.matches);
    }, 150);
  });
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  reduceQ.addEventListener?.("change", () => { if (reduceQ.matches) { stop(); frame(performance.now(), true); } else start(); });
  darkQ.addEventListener?.("change", () => { build(); frame(performance.now(), reduceQ.matches); });
  window.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    mouseX = (e.clientX / innerWidth - 0.5) * 2; mouseY = (e.clientY / innerHeight - 0.5) * 2;
  }, { passive: true });
  window.addEventListener("scroll", () => {
    const max = Math.max(1, document.documentElement.scrollHeight - innerHeight);
    scrollP = clamp(scrollY / max, 0, 1);
    if (reduceQ.matches) frame(performance.now(), true);
  }, { passive: true });
  window.LeafForest = { quality: () => quality };
})();
