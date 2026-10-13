// Live satellites: CelesTrak orbital elements (tle.json, refreshed by the
// poll job) propagated in the browser with SGP4 (satellite.js), drawn on the
// map's overlay (overlay.js) at their real altitude (~690-750 km), hidden
// when the Earth is in the way. For each satellite:
//   - its orbit, at altitude: the last 45 min solid and dulled, the next 45
//     min dashed;
//   - a line down to the point beneath it (nadir);
//   - the strip of ground its instrument sees, from its own viewing
//     geometry (Sentinel-1 and NISAR are side-looking radars, Sentinel-2 and
//     Landsat look straight down), the last few minutes of it trailing behind:
//     filled strongly where the agencies' plans have it imaging, faintly where
//     they do not (or where this page has not loaded that plan), fading with age;
//   - the "curtain" from the satellite to the near and far edges of that strip.
// Positions are good to a few km for elements a day old; the pass times in
// the lists come from the acquisition plans, timed with the same elements.

import { fmtHours, fmtStamp } from "./common.js";
import { spherical } from "./overlay.js";
import { TRACK_STEP_S, crossTrack, geometryOf, groundTrack, headingAt, position, sensorOf, swathRange } from "./swathgeom.js";

export { sensorOf };

const TRACK_BACK_MIN = 45;
const TRACK_AHEAD_MIN = 45;
const SWATH_BACK_S = 240; // the trailing swath strip
const SWATH_STEP_S = 6;

// Unwraps a ring's longitudes to be continuous.
function continuous(pts) {
  let prev = null;
  return pts.map(([x, y]) => {
    let lon = x;
    if (prev != null) lon += Math.round((prev - lon) / 360) * 360;
    prev = lon;
    return [lon, y];
  });
}

const rgba = (hex, a) => {
  const n = parseInt(hex.replace("#", "").slice(0, 6), 16);
  return Number.isFinite(n) ? `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})` : hex;
};

export class LiveSatellites {
  // imaging(platform, t) -> {mode, label} while the plan has it imaging,
  // false when it has it idle, null when this page has no plan for it loaded.
  constructor(view, overlay, { colorOf, labelOf, imaging = () => null }) {
    Object.assign(this, { view, overlay, colorOf, labelOf, imaging });
    this.sats = [];
    this.timer = null;
    this.shown = null; // platforms to show; null = all
    this.lastTrack = 0;
    this.meta = null;
    this.on = false;
    this.painter = overlay.add({ visible: false, paint: (ctx, proj) => this.paint(ctx, proj) });
  }

  // Only platforms that appear in the schedule (i.e. are acquiring).
  async load(url, platforms) {
    const r = await fetch(url, { cache: "no-cache" });
    if (!r.ok) throw new Error(`orbital elements unavailable (${r.status})`);
    this.meta = await r.json();
    this.sats = Object.entries(this.meta.satellites || {})
      .filter(([p]) => platforms.has(p))
      .map(([platform, s]) => {
        const satrec = satellite.json2satrec(s.omm);
        const el = Object.assign(document.createElement("button"), { type: "button", className: "sat" });
        el.hidden = true;
        el.innerHTML = `<i style="background:${this.colorOf(sensorOf(platform))}"></i><span>${platform}</span>`;
        el.title = `${this.labelOf(platform)}: click for where it is and what it sees`;
        el.setAttribute("aria-label", `${this.labelOf(platform)}: where it is and what it sees`);
        this.overlay.labels.append(el);
        const sat = { platform, sensor: sensorOf(platform), satrec, el, epoch: new Date(`${s.omm.EPOCH}Z`), fetchedAt: s.fetched_at, track: null, now: null };
        el.addEventListener("click", (e) => { e.stopPropagation(); this.popup(sat); });
        return sat;
      });
    return this.sats.length;
  }

  popup(sat) {
    const now = Date.now();
    const p = position(sat.satrec, new Date(now));
    if (!p) return;
    const age = (now - sat.epoch) / 3.6e6;
    const ns = p.lat >= 0 ? "N" : "S", ew = p.lon >= 0 ? "E" : "W";
    const h = headingAt(sat.satrec, now);
    const dir = h != null && Math.abs(h) < 90 ? "north (ascending)" : "south (descending)";
    const g = geometryOf(sat.sensor);
    const im = this.imaging(sat.platform, now);
    const range = swathRange(sat.sensor, im && im.mode);
    const what = !g ? "" : im
      ? `<b>Imaging now</b> per the plan${im.label ? ` (${im.label})` : ""}: the strongly filled strip is what it is seeing, ${describeRange(range)}.`
      : im === false
        ? `Not imaging now per the plan; the faint strip is where its ${g.look === "looks straight down" ? "instrument" : "radar"} would look, ${describeRange(range)}.`
        : `The faint strip is where its instrument looks, ${describeRange(range)} (plan not loaded here).`;
    const html = `<div class="headline">${this.labelOf(sat.platform)}</div>`
      + `<div>Now ${Math.round(p.km)} km above ${Math.abs(p.lat).toFixed(1)}° ${ns}, ${Math.abs(p.lon).toFixed(1)}° ${ew}, heading ${dir}${g ? `; ${g.look}` : ""}.</div>`
      + (what ? `<div>${what}</div>` : "")
      + `<div class="nm">SGP4 from CelesTrak elements, epoch ${fmtStamp(sat.epoch.toISOString())} (${fmtHours(age)} old)${age > 72 ? "; old elements, the position may be off by tens of km" : ""}. Its orbit is drawn at its altitude: the trail of the last ${TRACK_BACK_MIN} min tapers and fades with age, brightest at the satellite; the thin dashed line is the next ${TRACK_AHEAD_MIN} min; the strip on the ground trails the last ${SWATH_BACK_S / 60} min.</div>`;
    this.view.popup([p.lon, p.lat], html, { offset: 10 });
  }

  visible() {
    return this.sats.filter((s) => !this.shown || this.shown.has(s.platform));
  }

  // Show only these platforms (a Set), e.g. the sensors selected on the page.
  setPlatforms(platforms) {
    this.shown = platforms;
    this.lastTrack = 0;
    if (this.on) this.tick();
  }

  tick() {
    const now = Date.now();
    const vis = new Set(this.visible());
    const retrack = now - this.lastTrack > 30e3;
    if (retrack) this.lastTrack = now;
    this.sats.forEach((s) => {
      if (!vis.has(s)) { s.el.hidden = true; s.now = null; return; }
      const p = position(s.satrec, new Date(now));
      s.now = p;
      if (retrack || !s.track) {
        // one track (continuous longitudes), split at the satellite when drawn
        s.track = groundTrack(s.satrec, now - TRACK_BACK_MIN * 60e3 - 60e3, now + TRACK_AHEAD_MIN * 60e3 + 60e3, TRACK_STEP_S);
      }
      s.swath = this.swath(s, now);
    });
    this.overlay.redraw();
  }

  // The strip seen over the last SWATH_BACK_S: one sample every SWATH_STEP_S
  // (its line of sight across the track, and whether the plan has it
  // imaging then), plus the line of sight now.
  swath(s, now) {
    const im = this.imaging(s.platform, now);
    const range = swathRange(s.sensor, im && im.mode);
    if (!range) return null;
    const samples = [];
    for (let dt = -SWATH_BACK_S; dt <= 0; dt += SWATH_STEP_S) {
      const t = now + dt * 1000;
      const st = this.imaging(s.platform, t);
      const x = crossTrack(s.satrec, t, swathRange(s.sensor, st && st.mode));
      if (x) samples.push({ t, a: x.a, b: x.b, on: Boolean(st) });
    }
    // continuous longitudes along both edges
    let prev = null;
    samples.forEach((p) => {
      [p.a, p.b] = [p.a, p.b].map(([lon, lat]) => {
        let l = lon;
        if (prev != null) l += 360 * Math.round((prev - l) / 360);
        return [l, lat];
      });
      prev = p.a[0];
    });
    return { samples, cur: crossTrack(s.satrec, now, range), imaging: im };
  }

  paint(ctx, proj) {
    if (!this.on) return;
    const path = proj.path(ctx);
    const vis = this.visible();
    // ground first: the strips, then the curtains and the orbits
    vis.forEach((s) => {
      if (!s.swath) return;
      const color = this.colorOf(s.sensor);
      paintStrip(ctx, proj, path, s.swath.samples, Date.now(), color);
      ctx.setLineDash([]);
    });
    vis.forEach((s) => {
      if (!s.track || !s.now) return;
      const color = this.colorOf(s.sensor);
      // the orbit split at the satellite: its trail fades with age, the path
      // ahead dims with time to come, both brightest (and glowing) at it
      const t = Date.now(), tr = s.track;
      const ref = tr.reduce((a, p) => (Math.abs(p[3] - t) < Math.abs(a[3] - t) ? p : a), tr[0]);
      const here = [s.now.lon + 360 * Math.round((ref[0] - s.now.lon) / 360), s.now.lat, s.now.km, t];
      const shift = proj.near(here[0]) - here[0]; // flat map: one world copy, the satellite's
      const back = [...tr.filter((p) => p[3] < t && p[3] >= t - TRACK_BACK_MIN * 60e3), here];
      const ahead = [here, ...tr.filter((p) => p[3] > t && p[3] <= t + TRACK_AHEAD_MIN * 60e3)];
      fadingPath(ctx, proj, back, t, { color, span: TRACK_BACK_MIN * 60e3, shift, trail: true });
      fadingPath(ctx, proj, ahead, t, { color, span: TRACK_AHEAD_MIN * 60e3, shift, trail: false });
      const sat = proj.hidden(s.now.lon, s.now.lat, s.now.km) ? null : proj.px(s.now.lon, s.now.lat, s.now.km);
      const cur = s.swath && s.swath.cur;
      if (sat && cur) {
        const on = Boolean(s.swath.imaging);
        const sub = proj.hidden(cur.sub.lon, cur.sub.lat) ? null : proj.px(cur.sub.lon, cur.sub.lat);
        const a = proj.hidden(...cur.a) ? null : proj.px(...cur.a), b = proj.hidden(...cur.b) ? null : proj.px(...cur.b);
        if (a && b) {
          ctx.beginPath(); ctx.moveTo(...sat); ctx.lineTo(...a); ctx.lineTo(...b); ctx.closePath();
          ctx.fillStyle = rgba(color, on ? 0.22 : 0.07); ctx.fill();
          ctx.strokeStyle = rgba(color, on ? 0.8 : 0.4); ctx.lineWidth = 1; ctx.stroke();
          ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b);
          ctx.strokeStyle = rgba(color, 1); ctx.lineWidth = on ? 3 : 1.5; ctx.stroke();
        }
        if (sub) {
          ctx.beginPath(); ctx.moveTo(...sat); ctx.lineTo(...sub);
          ctx.strokeStyle = "rgba(255,255,255,0.75)"; ctx.lineWidth = 1; ctx.setLineDash([2, 3]); ctx.stroke(); ctx.setLineDash([]);
          ctx.beginPath(); ctx.arc(sub[0], sub[1], 2, 0, 2 * Math.PI); ctx.fillStyle = rgba(color, 0.9); ctx.fill();
        }
      }
      if (sat) {
        // a soft glow around the satellite
        // the brightest point: a glow around the satellite, lighter at its core
        const g = ctx.createRadialGradient(sat[0], sat[1], 0, sat[0], sat[1], 20);
        g.addColorStop(0, rgba(mixWhite(color, 0.5), 0.75)); g.addColorStop(0.35, rgba(color, 0.45)); g.addColorStop(1, rgba(color, 0));
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(sat[0], sat[1], 20, 0, 2 * Math.PI); ctx.fill();
        s.el.hidden = false;
        s.el.style.transform = `translate(${sat[0] - 7}px, ${sat[1] - 7}px)`;
        s.el.classList.toggle("imaging", Boolean(s.swath && s.swath.imaging));
      } else s.el.hidden = true;
    });
    this.sats.filter((s) => !vis.includes(s)).forEach((s) => { s.el.hidden = true; });
  }

  start() {
    if (this.timer || !this.sats.length) return;
    this.on = true;
    this.painter.visible = true;
    this.lastTrack = 0;
    this.timer = setInterval(() => this.tick(), 1000);
    this.tick();
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
    this.on = false;
    this.painter.visible = false;
    this.sats.forEach((s) => { s.el.hidden = true; });
    this.overlay.redraw();
  }
}

function describeRange([a, b]) {
  if (a < 0 && b > 0) return `${Math.round(b - a)} km wide, centred beneath it`;
  const side = a < 0 ? "left" : "right";
  const [n, f] = [Math.abs(a), Math.abs(b)].sort((x, y) => x - y);
  return `${Math.round(f - n)} km wide, ${Math.round(n)}-${Math.round(f)} km to its ${side}`;
}

// The orbit on one side of the satellite, as [lon, lat, km, t] points
// (continuous longitudes); f = 1 at the satellite, 0 at `span` away.
// Behind it (trail): a solid line that tapers and fades with age, widest and
// brightest at the satellite, with a soft glow over its last few minutes.
// Ahead: a thin dashed line of constant width that only dims with time to
// come. Segments with an end hidden by the Earth are skipped; on the flat
// map the line is shifted by `shift` degrees as one piece.
function fadingPath(ctx, proj, pts, now, { color, span, shift = 0, trail }) {
  const xy = pts.map(([lon, lat, km]) => (proj.hidden(lon, lat, km) ? null : proj.globe ? proj.px(lon, lat, km) : proj.px(lon + shift, lat, km, false)));
  const fOf = (i) => Math.max(0, 1 - Math.abs((pts[i][3] + pts[i + 1][3]) / 2 - now) / span);
  ctx.lineJoin = "round";
  if (trail) {
    // a ribbon, tapering and fading toward the oldest end: f per point
    const fp = pts.map((p) => Math.max(0, 1 - Math.abs(p[3] - now) / span));
    ribbon(ctx, xy, fp, (f) => 3.2 * f ** 1.6 + 0.9 * f, (f) => rgba(mixWhite(color, 0.4), 0.2 * f ** 2.2)); // glow
    ribbon(ctx, xy, fp, (f) => 0.35 + 1.9 * f ** 1.3, (f) => rgba(color, 0.95 * f ** 1.25));
    return;
  }
  ctx.lineCap = "butt"; ctx.lineWidth = 1.3; ctx.setLineDash([5, 5]);
  let dash = 0;
  for (let i = 0; i + 1 < pts.length; i++) {
    const a = xy[i], b = xy[i + 1];
    if (a && b) {
      ctx.strokeStyle = rgba(color, 0.2 + 0.55 * fOf(i));
      ctx.lineDashOffset = -dash;
      ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke();
      dash += Math.hypot(b[0] - a[0], b[1] - a[1]);
    }
  }
  ctx.setLineDash([]); ctx.lineDashOffset = 0;
}

// A line drawn as a filled ribbon of quads, one per segment, sharing their
// edges (no overlaps or caps to stack up), each filled with a gradient from
// its start to its end: widths and colours vary smoothly along it. xy: screen
// points (null where hidden), fp: a value per point, half(f) the half-width,
// col(f) the colour.
function ribbon(ctx, xy, fp, half, col) {
  const n = xy.length;
  const normal = (i) => {
    const a = xy[Math.max(0, i - 1)] && i > 0 ? xy[i - 1] : xy[i], b = xy[Math.min(n - 1, i + 1)] && i < n - 1 ? xy[i + 1] : xy[i];
    const dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1;
    return [-dy / l, dx / l];
  };
  for (let i = 0; i + 1 < n; i++) {
    const p = xy[i], q = xy[i + 1];
    if (!p || !q || (fp[i] <= 0.005 && fp[i + 1] <= 0.005)) continue;
    const [nx0, ny0] = normal(i), [nx1, ny1] = normal(i + 1), w0 = half(fp[i]), w1 = half(fp[i + 1]);
    const g = ctx.createLinearGradient(p[0], p[1], q[0], q[1]);
    g.addColorStop(0, col(fp[i])); g.addColorStop(1, col(fp[i + 1]));
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.moveTo(p[0] + nx0 * w0, p[1] + ny0 * w0); ctx.lineTo(q[0] + nx1 * w1, q[1] + ny1 * w1);
    ctx.lineTo(q[0] - nx1 * w1, q[1] - ny1 * w1); ctx.lineTo(p[0] - nx0 * w0, p[1] - ny0 * w0);
    ctx.closePath(); ctx.fill();
  }
}

// The strip of ground seen over the last minutes, as one quad per sample
// interval (great-circle edges and the horizon handled by d3's path): always
// filled, stronger with a solid edge where the plan has the satellite
// imaging, faint where it does not, and fading with age (w = 1 now, 0 at
// SWATH_BACK_S ago).
function paintStrip(ctx, proj, path, samples, now, color) {
  if (!samples || samples.length < 2) return;
  const w = (t) => Math.max(0, 1 - (now - t) / (SWATH_BACK_S * 1000)) ** 0.8;
  for (let i = 0; i + 1 < samples.length; i++) {
    const p = samples[i], q = samples[i + 1], k = w((p.t + q.t) / 2);
    const ring = [p.a, q.a, q.b, p.b, p.a];
    ctx.beginPath();
    path(spherical({ type: "Polygon", coordinates: [ring] }));
    ctx.fillStyle = rgba(color, (q.on ? 0.4 : 0.1) * k);
    ctx.fill();
  }
  // both edges, per segment: solid, as strong as the fill beside them
  ctx.setLineDash([]); ctx.lineCap = "round";
  ["a", "b"].forEach((side) => {
    for (let i = 0; i + 1 < samples.length; i++) {
      const p = samples[i], q = samples[i + 1];
      const pa = proj.hidden(...p[side]) ? null : proj.px(...p[side]), qa = proj.hidden(...q[side]) ? null : proj.px(...q[side]);
      if (!pa || !qa) continue;
      ctx.strokeStyle = rgba(color, (q.on ? 0.95 : 0.4) * w((p.t + q.t) / 2));
      ctx.lineWidth = q.on ? 1.5 : 1;
      ctx.beginPath(); ctx.moveTo(...pa); ctx.lineTo(...qa); ctx.stroke();
    }
  });
}

// A colour mixed toward white (k = 0..1), as hex.
function mixWhite(hex, k) {
  const n = parseInt(hex.replace("#", "").slice(0, 6), 16);
  if (!Number.isFinite(n)) return hex;
  const ch = (v) => Math.round(v + (255 - v) * k).toString(16).padStart(2, "0");
  return `#${ch((n >> 16) & 255)}${ch((n >> 8) & 255)}${ch(n & 255)}`;
}
