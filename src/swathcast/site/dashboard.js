import {
  getJSON, forgetSettings, showFreshness, failingSources, fmtHours, fmtStamp, localStamp, fmtAge, ageHours, sourceColor, SOURCE_SLOT,
} from "./common.js";
import { Tour, flash, handOver, pendingStep, sleep } from "./tour.js";

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}, parent) => {
  const node = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (parent) parent.append(node);
  return node;
};
const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(v < 0.1 ? 1 : 0)} %`);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let L = null; // latency.json
const hidden = new Set();

// ------------------------------------------------------------------ chart
// Minimal SVG line chart: one y-axis, recessive grid, 2px lines, crosshair tooltip.
function lineChart(container, { series, xType, xDomain, yDomain, xTicks, yTicks, xFmt, yFmt, tipX, tipY, height = 320 }) {
  container.replaceChildren();
  const width = Math.max(320, container.clientWidth || 800);
  const m = { t: 10, r: 110, b: 34, l: 48 };
  if (width < 520) m.r = 16;
  const W = width - m.l - m.r, H = height - m.t - m.b;
  const lx = (v) => Math.log10(v);
  const sx = xType === "log"
    ? (v) => ((lx(v) - lx(xDomain[0])) / (lx(xDomain[1]) - lx(xDomain[0]))) * W
    : (v) => ((v - xDomain[0]) / (xDomain[1] - xDomain[0] || 1)) * W;
  const sy = (v) => H - ((v - yDomain[0]) / (yDomain[1] - yDomain[0] || 1)) * H;
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" }, container);
  const g = el("g", { transform: `translate(${m.l},${m.t})` }, svg);
  const grid = el("g", { class: "grid" }, g);
  const axis = el("g", { class: "axis" }, g);
  yTicks.forEach((v) => {
    el("line", { x1: 0, x2: W, y1: sy(v), y2: sy(v) }, grid);
    el("text", { x: -8, y: sy(v) + 4, "text-anchor": "end" }, axis).textContent = yFmt(v);
  });
  xTicks.forEach((v) => {
    el("line", { x1: sx(v), x2: sx(v), y1: 0, y2: H }, grid);
    el("text", { x: sx(v), y: H + 18, "text-anchor": "middle" }, axis).textContent = xFmt(v);
  });
  el("line", { x1: 0, x2: W, y1: H, y2: H }, axis);

  const visible = series.filter((s) => !hidden.has(s.key));
  const labels = [];
  visible.forEach((s) => {
    const pts = s.points.filter(([x, y]) => y != null && x >= xDomain[0] && x <= xDomain[1]);
    if (!pts.length) return;
    const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join("");
    el("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, g);
    if (s.dots) pts.forEach(([x, y]) => el("circle", { cx: sx(x), cy: sy(y), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }, g));
    const [lxv, lyv] = pts[pts.length - 1];
    labels.push({ y: sy(lyv), x: sx(lxv), text: s.label });
  });
  // Direct labels at line ends (when there is room), de-collided vertically.
  if (m.r > 50 && visible.length <= 8) {
    labels.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labels.length; i++) if (labels[i].y - labels[i - 1].y < 13) labels[i].y = labels[i - 1].y + 13;
    const overflow = labels.length ? labels[labels.length - 1].y - (H + 4) : 0;
    if (overflow > 0) labels.forEach((lb) => { lb.y -= overflow; });
    labels.forEach((lb) => { el("text", { x: W + 6, y: lb.y + 4, class: "label" }, g).textContent = lb.text; });
  }

  // Crosshair + tooltip.
  const tip = document.createElement("div");
  tip.className = "tip";
  container.append(tip);
  const cross = el("line", { y1: 0, y2: H, stroke: "var(--muted)", "stroke-dasharray": "3 3", visibility: "hidden" }, g);
  const hit = el("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent" }, g);
  const invert = xType === "log"
    ? (px) => 10 ** (lx(xDomain[0]) + (px / W) * (lx(xDomain[1]) - lx(xDomain[0])))
    : (px) => xDomain[0] + (px / W) * (xDomain[1] - xDomain[0]);
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const scale = width / rect.width;
    const px = (ev.clientX - rect.left) * scale - m.l;
    if (px < 0 || px > W) return leave();
    const x = invert(px);
    const rows = visible.map((s) => ({ s, y: tipY(s, x) })).filter((r) => r.y != null);
    if (!rows.length) return leave();
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    tip.innerHTML = `<div><b>${tipX(x)}</b></div>` + rows.map((r) =>
      `<div class="row"><span class="k"><span class="swatch" style="background:${r.s.color};width:10px;height:3px;display:inline-block"></span>${esc(r.s.label)}</span><b>${r.y}</b></div>`).join("");
    tip.style.display = "block";
    const left = (px + m.l) / scale;
    tip.style.left = `${Math.min(left + 12, rect.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${m.t / scale}px`;
  };
  const leave = () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", leave);
}

function legend(container, series, redraw) {
  container.replaceChildren(...series.map((s) => {
    const label = document.createElement("label");
    const cb = Object.assign(document.createElement("input"), { type: "checkbox", checked: !hidden.has(s.key) });
    cb.addEventListener("change", () => { cb.checked ? hidden.delete(s.key) : hidden.add(s.key); redraw(); });
    const sw = Object.assign(document.createElement("span"), { className: "swatch" });
    sw.style.background = s.color;
    label.append(cb, sw, document.createTextNode(s.label));
    return label;
  }));
}

const label = (key) => (L.sources[key] && L.sources[key].label) || key;
const orderedSources = () => Object.keys(L.sources).sort((a, b) => (SOURCE_SLOT[a] || 9) - (SOURCE_SLOT[b] || 9));

// ------------------------------------------------------------------ KM curves
function drawKM() {
  const platform = $("km-platform").value;
  const series = L.curves.filter((c) => c.platform === platform).map((c) => ({
    key: c.source, label: label(c.source), color: sourceColor(c.source),
    points: c.t_h.map((t, i) => [t, c.F[i] == null ? null : c.F[i] * 100]), raw: c,
  })).sort((a, b) => (SOURCE_SLOT[a.key] || 9) - (SOURCE_SLOT[b.key] || 9));
  if (!series.length) { $("km-chart").innerHTML = '<p class="nm">No curves yet: not enough measurements.</p>'; $("km-legend").replaceChildren(); return; }
  const redraw = () => drawKM();
  legend($("km-legend"), series, redraw);
  lineChart($("km-chart"), {
    series, xType: "log", xDomain: [0.25, 504], yDomain: [0, 100],
    xTicks: [0.5, 1, 2, 3, 6, 12, 24, 48, 96, 168, 336], yTicks: [0, 25, 50, 75, 90, 100],
    xFmt: (v) => (v < 48 ? `${v}h` : `${v / 24}d`), yFmt: (v) => `${v}%`,
    tipX: (x) => `${fmtHours(x)} after sensing`,
    tipY: (s, x) => {
      const c = s.raw;
      let idx = -1;
      for (let i = 0; i < c.t_h.length; i++) if (c.t_h[i] <= x) idx = i;
      if (idx < 0 || c.F[idx] == null) return null;
      return `${(c.F[idx] * 100).toFixed(1)} %`;
    },
  });
}

// ------------------------------------------------------------------ trend
function drawTrend() {
  const months = [...new Set(L.trend.map((r) => r.month))].sort();
  const idx = Object.fromEntries(months.map((mo, i) => [mo, i]));
  const bySource = {};
  L.trend.filter((r) => r.median_h != null).forEach((r) => { (bySource[r.source] ||= []).push([idx[r.month], r.median_h, r]); });
  const series = orderedSources().filter((k) => bySource[k]).map((k) => ({
    key: k, label: label(k), color: sourceColor(k), dots: true, points: bySource[k].sort((a, b) => a[0] - b[0]),
  }));
  if (!series.length) { $("trend-chart").innerHTML = '<p class="nm">No month has enough sightings yet.</p>'; $("trend-legend").replaceChildren(); return; }
  legend($("trend-legend"), series, drawTrend);
  const maxY = Math.max(1, ...series.flatMap((s) => s.points.map((p) => p[1])));
  const step = maxY > 48 ? 24 : maxY > 12 ? 6 : maxY > 4 ? 2 : 1;
  const top = Math.ceil(maxY / step) * step;
  lineChart($("trend-chart"), {
    series, xType: "linear", xDomain: months.length > 1 ? [0, months.length - 1] : [-0.5, 0.5], yDomain: [0, top],
    xTicks: months.map((_, i) => i), yTicks: Array.from({ length: top / step + 1 }, (_, i) => i * step),
    xFmt: (i) => months[i] || "", yFmt: (v) => `${v}h`, height: 260,
    tipX: (x) => months[Math.round(x)] || "",
    tipY: (s, x) => { const p = s.points.find((q) => q[0] === Math.round(x)); return p ? `${fmtHours(p[1])} (n=${p[2].n_events})` : null; },
  });
}

// ------------------------------------------------------------------ tables
function table(target, headers, rows) {
  const t = $(target);
  t.innerHTML = `<thead><tr>${headers.map(([h, cls]) => `<th class="${cls || ""}">${h}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map((r) => `<tr>${r.map(([v, cls, title]) => `<td class="${cls || ""}"${title ? ` title="${esc(title)}"` : ""}>${v}</td>`).join("")}</tr>`).join("")}</tbody>`;
}

function summaryTable() {
  const rows = orderedSources().map((k) => {
    const s = L.sources[k], o = L.overall[k] || {};
    const nm = '<span class="nm">not measured</span>';
    const excluded = Object.entries(o.excluded || {}).map(([r, n]) => `${n} ${r}`).join("; ");
    return [
      [`<span class="chip" style="background:${sourceColor(k)}"></span> ${esc(s.label)}`, "", s.distributor],
      [esc(s.product)],
      [o.measured ? fmtHours(o.median_h) : nm, "num"],
      [o.measured ? fmtHours(o.p90_h) : "", "num"],
      [o.measured ? fmtHours(o.p99_h) : "", "num"],
      [o.measured ? pct(o.not_delivered_frac) : "", "num", o.not_delivered_frac == null && o.measured ? `not yet followed for the full ${fmtHours(s.timeout_hours)} timeout (longest follow-up ${fmtHours(o.max_followup_h)})` : ""],
      [`${o.n_events ?? 0} / ${o.n ?? 0}`, "num", excluded ? `excluded: ${excluded}` : ""],
      [String(o.n_pending ?? 0), "num"],
      [s.provider_timestamp_validated ? "validated" : "cross-check"],
    ];
  });
  table("summary-table", [["Source"], ["Product"], ["Median", "num"], ["p90", "num"], ["p99", "num"], ["% not delivered", "num"], ["seen / tracked", "num"], ["pending", "num"], ["Provider time"]], rows);
  const measured = orderedSources().filter((k) => L.overall[k] && L.overall[k].measured);
  $("lede").textContent = measured.length
    ? `Measured over acquisitions sensed in the last ${L.recent_days} days. Fastest median: ${label(measured.reduce((a, b) => (L.overall[b].median_h < L.overall[a].median_h ? b : a)))}.`
    : "Not enough measurements yet: the poller needs a few days of data before any source is reported.";
  $("window-note").textContent = `Window: acquisitions sensed in the last ${L.recent_days} days; at least ${L.min_events} sightings to report a source.`;
  document.querySelectorAll(".min-events").forEach((n) => { n.textContent = L.min_events; });
}

function strataTable() {
  const src = $("strata-source").value;
  const rows = L.table.filter((r) => !src || r.source === src)
    .sort((a, b) => (a.month < b.month ? 1 : a.month > b.month ? -1 : (SOURCE_SLOT[a.source] || 9) - (SOURCE_SLOT[b.source] || 9)))
    .map((r) => [[esc(label(r.source))], [r.platform], [r.month], [r.measured ? fmtHours(r.median_h) : '<span class="nm">not measured</span>', "num"],
      [r.measured ? fmtHours(r.p90_h) : "", "num"], [r.measured ? fmtHours(r.p99_h) : "", "num"], [r.measured ? pct(r.not_delivered_frac) : "", "num"], [`${r.n_events} / ${r.n}`, "num"]]);
  table("strata-table", [["Source"], ["Platform"], ["Month"], ["Median", "num"], ["p90", "num"], ["p99", "num"], ["% not delivered", "num"], ["seen / n", "num"]], rows);
}

let LATEST = null;
function latestTable() {
  if (!LATEST) return;
  const type = $("latest-type").value, plat = $("latest-platform").value, q = $("latest-q").value.trim().toLowerCase();
  const srcs = orderedSources().filter((k) => L.sources[k].reference_product === type);
  const rows = LATEST.granules.filter((g) => g.type === type && (!plat || g.platform === plat) &&
    (!q || g.id.toLowerCase().includes(q) || String(g.track) === q));
  $("latest-count").textContent = `${rows.length} granules`;
  table("latest-table", [["Sensing (UTC)"], ["Plat."], ["Track"], ["Dir"], ["Granule"], ...srcs.map((k) => [esc(label(k)), "num"])],
    rows.slice(0, 1500).map((g) => [[g.sensing.slice(5, 16).replace("T", " "), "", localStamp(g.sensing)], [g.platform], [g.track ?? ""], [g.dir ? g.dir.slice(0, 4) : ""], [`<code>${esc(g.id)}</code>`],
      ...srcs.map((k) => { const v = g.h[k]; return v === undefined ? [""] : v === null ? ['<span class="nm">…</span>', "num"] : v === "timeout" ? ['<span class="bad">timeout</span>', "num"] : [fmtHours(v), "num"]; })]));
}

function providerTable() {
  table("provider-table", [["Source"], ["Provider field"], ["n", "num"], ["median (first seen − provider)", "num"], ["p10", "num"], ["p90", "num"], ["provider after first seen", "num"], ["timestamp changed later", "num"]],
    L.provider_vs_first_seen.map((r) => [[esc(label(r.source))], [`<code>${esc(r.field)}</code>`], [r.n, "num"], [fmtHours(r.median_h), "num"], [fmtHours(r.p10_h), "num"], [fmtHours(r.p90_h), "num"], [pct(r.frac_provider_after_first_seen), "num"], [pct(r.frac_provider_timestamp_changed), "num"]]));
  if (!L.provider_vs_first_seen.length) $("provider-table").innerHTML = '<tbody><tr><td class="nm">No sightings with provider timestamps yet.</td></tr></tbody>';
}

function burstTable() {
  const keys = Object.keys(L.last_burst || {});
  if (!keys.length) { $("burst-table").innerHTML = '<tbody><tr><td class="nm">No OPERA sightings yet.</td></tr></tbody>'; return; }
  table("burst-table", [["Source"], ["First burst: median", "num"], ["p90", "num"], ["Last burst: median", "num"], ["p90", "num"], ["settled / n", "num"]],
    keys.map((k) => { const f = L.overall[k] || {}, l = L.last_burst[k]; return [[esc(label(k))], [f.measured ? fmtHours(f.median_h) : '<span class="nm">not measured</span>', "num"], [f.measured ? fmtHours(f.p90_h) : "", "num"], [l.measured ? fmtHours(l.median_h) : '<span class="nm">not measured</span>', "num"], [l.measured ? fmtHours(l.p90_h) : "", "num"], [`${l.n_events} / ${l.n}`, "num"]]; }));
}

function auditNote() {
  const a = L.audit;
  if (!a) return;
  const ok = !a.missing_rows && !a.duplicate_rows && !a.bad_status;
  $("audit-note").innerHTML = `Tracking-table audit: ${a.granules.toLocaleString()} granules, ${a.rows.toLocaleString()} rows; ` +
    `<span class="${ok ? "good" : "bad"}">${a.missing_rows} missing, ${a.duplicate_rows} duplicate, ${a.bad_status} invalid</span>. ` +
    "Every granule the reference catalogue listed has a row at every source for its product; each is seen, still pending (censored) or timed out.";
}

function healthTable(health) {
  const entries = Object.entries((health && health.sources) || {});
  table("health-table", [["Source"], ["Last success"], ["Status"], ["Polled", "num"], ["Found", "num"], ["Last error"]],
    entries.map(([k, e]) => [[esc(label(k) || k)], [`${fmtStamp(e.last_success_utc)} <span class="nm">(${[localStamp(e.last_success_utc), fmtAge(ageHours(e.last_success_utc))].filter(Boolean).join(" · ")})</span>`],
      [e.failing_since_utc ? `<span class="bad">failing since ${fmtStamp(e.failing_since_utc)}</span> <span class="nm">(${localStamp(e.failing_since_utc)})</span>` : '<span class="good">ok</span>'],
      [e.granules_polled ?? "", "num"], [e.granules_found ?? "", "num"], [esc(e.last_error || ""), "", e.last_error_utc ? `at ${fmtStamp(e.last_error_utc)} (${localStamp(e.last_error_utc)})` : ""]]));
}

// ------------------------------------------------------------------ main
const MISSIONS = [
  { key: "s1", label: "Sentinel-1", file: "latency.json", question: "a Sentinel-1 acquisition", note: "" },
  {
    key: "s2", label: "Sentinel-2", file: "latency_s2.json", question: "a Sentinel-2 product",
    note: "Universe: every L1C and L2A product CDSE publishes for a fixed 1-in-8 sample of MGRS tiles (about 3,800 products a day). Latency is measured from the datatake start (ESA's ContentDate), which is up to ~30 min before a given tile is imaged. Earth Search and Planetary Computer are mirrors; HLS S30 covers land tiles only (learned coverage).",
  },
  {
    key: "landsat", label: "Landsat 8/9", file: "latency_landsat.json", question: "a Landsat 8/9 scene",
    note: "Universe: every Landsat 8/9 Collection 2 L1 and L2 surface-reflectance scene USGS's public STAC lists. Landsat 8 real-time products are not in that STAC (checked 2026-09-29): Landsat 8 scenes appear only as T1/T2, days after sensing, so its numbers are latency to a tiered product. HLS L30 is matched on the L1 scene it was made from (learned coverage).",
  },
  {
    key: "nisar", label: "NISAR", file: "latency_nisar.json", question: "a NISAR acquisition",
    note: "L-band. Universe: every L0B (raw, per datatake segment) and L1 RSLC (per frame) product ASF lists in NASA's CMR (PROVISIONAL collections). GCOV is matched to its RSLC frame. Searching is anonymous; downloading needs an Earthdata login.",
  },
  {
    key: "nisar_s", label: "NISAR S-band", file: "latency_nisar_s.json", question: "a NISAR S-band product",
    note: "S-band, from ISRO's Bhoonidhi (its search needs a login; the poller uses repository secrets). Universe: every NISAR_SSAR_RSLC item sensed in the last 10 days; latency is the first hourly sighting after sensing. Bhoonidhi documents no publication time, so a sighting counts only when the previous poll ran within 3 h. GCOV is matched to its RSLC by name, assuming ISRO names S-band products like JPL's L-band ones.",
  },
  {
    key: "modis", label: "MODIS", file: "latency_modis.json", question: "a MODIS granule (Terra, Aqua)",
    note: "Universe: every 5-minute Level-1B 1 km granule (MOD021KM, MYD021KM) that NASA's near-real-time system LANCE lists in CMR, day and night. The other sources are matched on platform and granule start: the standard Level-1B at LAADS (collections 6.1 and 7) and the snow-cover swath product MOD10_L2/MYD10_L2 (LANCE NRT, NSIDC), made for daytime land granules only (learned coverage). LANCE deletes its NRT products after 7 days.",
  },
  {
    key: "viirs", label: "VIIRS", file: "latency_viirs.json", question: "a VIIRS granule (Suomi NPP, NOAA-20, NOAA-21)",
    note: "Universe: every 6-minute Level-1B 750 m granule (VNP02MOD, VJ102MOD, VJ202MOD) that LANCE lists in CMR, day and night. Matched on platform and granule start: the standard Level-1B at LAADS and the snow-cover swath product VNP10/VJ110/VJ210 (LANCE NRT, NSIDC), daytime land only (learned coverage).",
  },
  {
    key: "s3", label: "Sentinel-3", file: "latency_s3.json", question: "a Sentinel-3 frame (OLCI, SLSTR)",
    note: "Universe: every near-real-time OLCI full-resolution (OL_1_EFR) and SLSTR radiance (SL_1_RBT) frame CDSE publishes. Matched on the frame (platform, cycle, relative orbit, frame): the non-time-critical versions at CDSE, EUMETSAT's OLCI copy, and the Level-2 land products OLCI LFR and SLSTR LST at CDSE and Planetary Computer (land frames only: learned coverage).",
  },
];
let meta = {};

function tabs(active) {
  $("missions").replaceChildren(...MISSIONS.map((m) => {
    const b = Object.assign(document.createElement("button"), { textContent: m.label, className: m.key === active ? "active" : "" });
    b.addEventListener("click", () => { window.location.hash = m.key; });
    return b;
  }));
}

async function main() {
  const key = MISSIONS.some((m) => m.key === window.location.hash.slice(1)) ? window.location.hash.slice(1) : "s1";
  const mission = MISSIONS.find((m) => m.key === key);
  tabs(key);
  document.body.dataset.mission = key;
  $("question").textContent = `How long until ${mission.question} is downloadable?`;
  $("mission-note").textContent = mission.note;
  $("mission-note").hidden = !mission.note;
  let latency, health;
  try {
    [latency, health, meta] = await Promise.all([
      getJSON(`./data/${mission.file}`),
      key === "s1" ? getJSON("./data/health.json").catch(() => null) : Promise.resolve(null),
      getJSON("./data/upcoming_passes.meta.json").catch(() => ({})),
    ]);
  } catch (err) {
    $("lede").textContent = key === "nisar_s"
      ? "No NISAR S-band measurements yet: Bhoonidhi's search needs a login, and the poller tracks it once the repository secrets BHOONIDHI_USER_ID and BHOONIDHI_PASSWORD are set."
      : `No ${mission.label} measurements published yet (${err.message}): the poller starts tracking it on its next runs.`;
    ["summary-table", "strata-table", "provider-table", "health-table"].forEach((id) => { $(id).innerHTML = ""; });
    $("km-chart").innerHTML = ""; $("trend-chart").innerHTML = "";
    return;
  }
  if (key !== "s1") health = latency.health;
  L = latency;
  hidden.clear();
  showFreshness({ scheduleAt: meta.generated_at, latencyAt: L.data_as_of, failing: failingSources(health) });
  const platforms = [...new Set(L.curves.map((c) => c.platform))].sort((a, b) => (a === "all" ? -1 : b === "all" ? 1 : a.localeCompare(b)));
  $("km-platform").innerHTML = (platforms.length ? platforms : ["all"]).map((p) => `<option value="${p}">${p === "all" ? "all platforms" : p}</option>`).join("");
  $("strata-source").innerHTML = '<option value="">all</option>' + orderedSources().map((k) => `<option value="${k}">${esc(label(k))}</option>`).join("");
  $("km-platform").onchange = drawKM;
  $("strata-source").onchange = strataTable;
  summaryTable(); drawKM(); drawTrend(); strataTable(); providerTable(); burstTable(); healthTable(health); auditNote();
  if (key !== "s1") return; // the latest-granules table is Sentinel-1 only
  try {
    LATEST = await getJSON("./data/latest.json");
    const plats = [...new Set(LATEST.granules.map((g) => g.platform))].sort();
    $("latest-platform").innerHTML += plats.map((p) => `<option>${p}</option>`).join("");
    ["latest-type", "latest-platform", "latest-q"].forEach((id) => $(id).addEventListener("input", latestTable));
    latestTable();
  } catch (err) {
    $("latest-table").innerHTML = `<tbody><tr><td class="nm">latest.json unavailable: ${esc(err.message)}</td></tr></tbody>`;
  }
}
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (L) { drawKM(); drawTrend(); } }, 150); });
window.addEventListener("hashchange", () => main().catch((err) => { $("lede").textContent = `Failed to load latency data: ${err.message}`; }));
main().catch((err) => { $("lede").textContent = `Failed to load latency data: ${err.message}`; });

// The tour's last step (tour.js), handed over by the map page; its end
// goes back to the map, started over.
if (pendingStep()) {
  const tab = (href) => document.querySelector(`header nav a[href="${href}"]`);
  const tour = new Tour([{
    title: "How fast does data arrive?",
    body: "<p>You are on the <b>Delivery</b> tab now. Every hour this site checks each archive for new scenes: these are the measured <b>hours from imaging to download</b>, per mission and source, the numbers behind the map's <b>likely downloadable</b> times.</p>",
    marks: [{ target: () => ($("summary-table").offsetHeight > 20 ? $("summary-table") : $("summary-card")), label: "Median · p90 · p99 hours", side: "bottom" }, { target: "#missions", label: "Pick a mission", side: "right" }],
  }], {
    first: 6, dirty: true,
    onEnd: async () => {
      forgetSettings();
      await tour.pointAt(tab("index.html"), "Back to the map, started over");
      tour.ripple();
      flash(tab("index.html"));
      await sleep(700);
      location.href = "index.html";
    },
  });
  tour.back = () => { tour.close(); handOver(5, "index.html"); };
  tour.show(0);
  flash(tab("dashboard.html")); // the tab just switched to
}
