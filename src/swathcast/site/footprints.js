// Native footprints: Sentinel-1 bursts (ESA burst grid, via ASF's CMR burst
// map), Landsat WRS-2 path/rows, NISAR frames; and when each is next imaged.
//
// Sentinel-1 timing mirrors swathcast/orbit.py (tested there): a burst is
// imaged at ANX_k + time_from_anx on each orbit k of its track, and it is
// acquired if that instant is inside a planned IW datatake of the platform.

const CMR = "https://cmr.earthdata.nasa.gov/search/granules.json";
const BURST_MAP = "C2450786986-ASF";
const N_TRACKS = 175;
const NOMINAL_PERIOD_S = (12 * 86400) / N_TRACKS;
const T_BEAM_S = 2.758273;
const T_PRE_S = 2.299849;
const SUBSWATH_OFFSET_S = { IW1: 0.0008, IW2: 0.8333, IW3: 1.9113 };
const WINDOW_SLACK_MS = 3000;
export const MAX_BURSTS = 12000;

// ------------------------------------------------------------ S1 timing
export function burstTimeFromAnx(track, burstId, swath) {
  return (burstId - 1) * T_BEAM_S + T_PRE_S - NOMINAL_PERIOD_S * (track - 1) + SUBSWATH_OFFSET_S[swath];
}

export function parseModels(orbits) {
  const out = {};
  Object.entries((orbits && orbits.platforms) || {}).forEach(([p, m]) => {
    out[p] = { epoch: new Date(m.epoch).getTime(), period: m.period_s * 1000, offset: m.offset };
  });
  return out;
}

// Instants (ms) in [t0, t1] at which the platform images a burst.
export function burstTimes(model, track, tanxS, t0, t1) {
  const firstK = Math.floor((t0 - model.epoch) / model.period) - 1;
  const lastK = Math.ceil((t1 - model.epoch) / model.period) + 1;
  const mod = (a, n) => ((a % n) + n) % n;
  let k = firstK + mod(track - 1 - model.offset - firstK, N_TRACKS);
  const out = [];
  for (; k <= lastK; k += N_TRACKS) {
    const when = model.epoch + k * model.period + tanxS * 1000;
    if (when >= t0 && when <= t1) out.push(when);
  }
  return out;
}

// plans: {platform: [{begin, end, pass}] sorted by begin} for S1 IW datatakes.
export function acquisitions(models, plans, track, tanxS, t0, t1) {
  const found = [];
  Object.entries(models).forEach(([platform, model]) => {
    const plan = plans[platform];
    if (!plan || !plan.length) return;
    burstTimes(model, track, tanxS, t0, t1).forEach((when) => {
      // binary search for the last datatake starting before when + slack
      let lo = 0, hi = plan.length - 1, idx = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (plan[mid].begin - WINDOW_SLACK_MS <= when) { idx = mid; lo = mid + 1; } else hi = mid - 1;
      }
      for (let i = idx; i >= 0 && i >= idx - 3; i--) {
        const d = plan[i];
        if (d.begin - WINDOW_SLACK_MS <= when && d.end + WINDOW_SLACK_MS >= when) {
          found.push({ platform, when: new Date(when), pass: d.pass });
          break;
        }
      }
    });
  });
  return found.sort((a, b) => a.when - b.when);
}

export function buildPlans(rows) {
  const plans = {};
  rows.filter((r) => r.sensor === "sentinel-1" && r.mode === "IW").forEach((r) => {
    (plans[r.platform] ||= []).push({ begin: r.begin.getTime(), end: r.end.getTime(), pass: r });
  });
  Object.values(plans).forEach((p) => p.sort((a, b) => a.begin - b.begin));
  return plans;
}

// ------------------------------------------------------------ CMR burst map

function parsePolygon(str) {
  const v = str.trim().split(/\s+/).map(Number);
  const ring = [];
  for (let i = 0; i + 1 < v.length; i += 2) ring.push([v[i + 1], v[i]]); // lat lon -> lon lat
  return ring;
}

function unwrapRing(ring) {
  const lons = ring.map((p) => p[0]);
  if (Math.max(...lons) - Math.min(...lons) <= 180) return ring;
  return ring.map(([x, y]) => [x < 0 ? x + 360 : x, y]);
}

// The burst map never changes, so it is fetched in fixed 2-degree tiles,
// several at once, and each tile is kept: in memory for this visit, and in
// the browser's Cache Storage for the next ones. Panning then fetches only
// the tiles that are new; a return visit fetches none.
const TILE_DEG = 2;
// The most tiles one view or AOI fetches: past it, the burst map is not
// fetched and Sentinel-1 is drawn as its planned swaths (map.js).
export const MAX_TILES = 100;
const TILE_CONCURRENCY = 8;
const BURST_STORE = "swathcast-burst-map-v1";
const tileCache = new Map(); // "x,y" -> Promise<compact bursts>

async function storedTile(key) {
  try {
    if (typeof caches === "undefined") return null;
    const hit = await (await caches.open(BURST_STORE)).match(`https://swathcast.invalid/bursts/${key}`);
    return hit ? hit.json() : null;
  } catch { return null; }
}
async function storeTile(key, bursts) {
  try {
    if (typeof caches === "undefined") return;
    await (await caches.open(BURST_STORE)).put(`https://swathcast.invalid/bursts/${key}`,
      new Response(JSON.stringify(bursts), { headers: { "Content-Type": "application/json" } }));
  } catch { /* storage unavailable or full: memory only */ }
}

// One tile's IW bursts from CMR, every page: [[title, ring], ...], coordinates rounded to ~1 m.
async function cmrTile(x, y, signal) {
  const bbox = [x * TILE_DEG, Math.max(-90, y * TILE_DEG), (x + 1) * TILE_DEG, Math.min(90, (y + 1) * TILE_DEG)];
  const params = new URLSearchParams({
    concept_id: BURST_MAP,
    bounding_box: bbox.join(","),
    "granule_ur[]": "*_IW*",
    "options[granule_ur][pattern]": "true",
    page_size: "2000",
  });
  const out = [];
  let after = null;
  for (;;) {
    const r = await fetch(`${CMR}?${params}`, { signal, headers: after ? { "CMR-Search-After": after } : {} });
    if (!r.ok) throw new Error(`CMR burst map HTTP ${r.status}`);
    const hits = Number(r.headers.get("CMR-Hits") || 0);
    const page = (await r.json()).feed.entry || [];
    page.forEach((e) => {
      if (!e.polygons || !e.polygons.length) return;
      out.push([e.title, parsePolygon(e.polygons[0][0]).map(([lon, lat]) => [Math.round(lon * 1e5) / 1e5, Math.round(lat * 1e5) / 1e5])]);
    });
    after = r.headers.get("CMR-Search-After");
    if (!page.length || !after || out.length >= hits) break;
  }
  return out;
}

function tile(x, y, signal) {
  const key = `${x},${y}`;
  if (!tileCache.has(key)) {
    const p = storedTile(key).then((hit) => hit || cmrTile(x, y, signal).then((got) => { storeTile(key, got); return got; }));
    tileCache.set(key, p);
    p.catch(() => tileCache.delete(key));
  }
  return tileCache.get(key);
}

async function mapLimit(items, n, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) { const i = next++; out[i] = await fn(items[i]); }
  }));
  return out;
}

function tilesOf([w, s, e, n]) {
  const xs = [], ys = [];
  for (let x = Math.floor(w / TILE_DEG); x * TILE_DEG < e; x++) xs.push(x);
  for (let y = Math.floor(s / TILE_DEG); y * TILE_DEG < n; y++) ys.push(y);
  if (!xs.length) xs.push(Math.floor(w / TILE_DEG)); // a point
  if (!ys.length) ys.push(Math.floor(s / TILE_DEG));
  return { xs, ys };
}
// How many burst-map tiles [w, s, e, n] needs (compare with MAX_TILES).
export function burstTiles(bbox) {
  const { xs, ys } = tilesOf(bbox);
  return xs.length * ys.length;
}

// IW bursts intersecting [w, s, e, n] (lon -180..180). Throws if more than
// MAX_TILES tiles or MAX_BURSTS bursts (callers check burstTiles first).
export async function fetchBursts([w, s, e, n], { signal } = {}) {
  const { xs, ys } = tilesOf([w, s, e, n]);
  if (xs.length * ys.length > MAX_TILES) throw new Error("too many bursts");
  const tiles = await mapLimit(xs.flatMap((x) => ys.map((y) => [x, y])), TILE_CONCURRENCY, ([x, y]) => tile(x, y, signal));
  const seen = new Set(), bursts = [];
  tiles.flat().forEach(([title, raw]) => {
    const m = /^S1_(\d{3})_(\d{6})_(IW[123])-BURSTMAP$/.exec(title);
    if (!m || seen.has(title)) return; // a burst on a tile edge is in both tiles
    const ring = unwrapRing(raw);
    const lons = ring.map((p) => p[0]), lats = ring.map((p) => p[1]);
    if (!bboxOverlapWrapped([Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)], [w, s, e, n])) return;
    seen.add(title);
    const track = Number(m[1]), bid = Number(m[2]), swath = m[3];
    bursts.push({ id: `T${m[1]}-${m[2]}-${swath}`, track, bid, swath, tanx: burstTimeFromAnx(track, bid, swath), geojson: { type: "Polygon", coordinates: [ring] } });
  });
  if (bursts.length > MAX_BURSTS) throw new Error("too many bursts");
  return bursts;
}

// Compact "292398-292407" style label for a set of burst IDs on one track.
export function burstRange(bursts) {
  const ids = [...new Set(bursts.map((b) => b.bid))].sort((a, b) => a - b);
  const swaths = [...new Set(bursts.map((b) => b.swath))].sort();
  if (!ids.length) return "";
  const pad = (x) => String(x).padStart(6, "0");
  const range = ids.length === 1 ? pad(ids[0]) : `${pad(ids[0])}–${pad(ids[ids.length - 1])}`;
  return `${range} ${swaths.length === 3 ? "IW1–3" : swaths.join("/")}`;
}

// ------------------------------------------------------------ Landsat WRS-2
// Fallback without orbital elements: overpass UTC = day + (10.2 - lon/15) h at the footprint centroid.
const LANDSAT_LST_H = 10.2;
export function landsatDay(pass, centroidLon) {
  const t = pass.begin.getTime() - (LANDSAT_LST_H - centroidLon / 15) * 3.6e6;
  return Math.round(t / 86400000) * 86400000;
}
export function landsatTime(dayMs, lon) {
  return new Date(dayMs + (LANDSAT_LST_H - lon / 15) * 3.6e6);
}

// ------------------------------------------------------------ helpers
export function centroidLon(geojson) {
  const ring = geojson.type === "Polygon" ? geojson.coordinates[0] : geojson.coordinates[0][0];
  const lons = ring.map((p) => p[0]);
  let lon = lons.reduce((a, b) => a + b, 0) / lons.length;
  if (Math.max(...lons) - Math.min(...lons) > 180) {
    lon = lons.map((x) => (x < 0 ? x + 360 : x)).reduce((a, b) => a + b, 0) / lons.length;
  }
  return ((lon + 540) % 360) - 180;
}

// ------------------------------------------------------------ Sentinel-2 tiles
// s2_grid.parquet (built by swathcast/s2grid.py): corner 0 in 1e-4 degrees,
// corners 1-3 as offsets; antimeridian tiles carry longitudes past 180.
const S2_SCALE = 1e4;
export function s2Tile(row) {
  const x0 = Number(row.x0), y0 = Number(row.y0);
  const ring = [[x0, y0]];
  for (const k of [1, 2, 3]) ring.push([x0 + Number(row[`dx${k}`]), y0 + Number(row[`dy${k}`])]);
  const coords = ring.map(([x, y]) => [x / S2_SCALE, y / S2_SCALE]);
  coords.push(coords[0]);
  const xs = coords.map((c) => c[0]), ys = coords.map((c) => c[1]);
  return {
    tile: String(row.tile), zone: Number(String(row.tile).slice(0, 2)),
    geojson: { type: "Polygon", coordinates: [coords] },
    bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
    center: [xs.slice(0, 4).reduce((a, b) => a + b) / 4, ys.slice(0, 4).reduce((a, b) => a + b) / 4],
  };
}

// For an antimeridian tile (lon > 180) test against the view both ways.
export function bboxOverlapWrapped(a, b) {
  const hit = (x) => !(x[2] < b[0] || x[0] > b[2] || x[3] < b[1] || x[1] > b[3]);
  return hit(a) || hit([a[0] - 360, a[1], a[2] - 360, a[3]]) || hit([a[0] + 360, a[1], a[2] + 360, a[3]]);
}

export const toDate = (v) => (v instanceof Date ? v : new Date(typeof v === "bigint" ? Number(v) : v));

// ------------------------------------------------------------ filters
// "137", "10-20, 35", "" (= everything) -> [[lo, hi], ...] or null.
export function parseRanges(text) {
  const s = String(text || "").trim();
  if (!s) return null;
  const out = [];
  for (const part of s.split(/[,;\s]+/).filter(Boolean)) {
    const m = /^(\d+)(?:\s*[-–]\s*(\d+))?$/.exec(part);
    if (!m) throw new Error(`"${part}" is not a number or range`);
    const a = Number(m[1]), b = m[2] == null ? a : Number(m[2]);
    out.push([Math.min(a, b), Math.max(a, b)]);
  }
  return out;
}
export const inRanges = (ranges, v) => !ranges || (v != null && ranges.some(([a, b]) => v >= a && v <= b));

// S2 tile filter: comma-separated prefixes ("10T, 11U") -> predicate.
export function tileFilter(text) {
  const prefixes = String(text || "").toUpperCase().split(/[,;\s]+/).filter(Boolean);
  if (!prefixes.length) return () => true;
  if (prefixes.some((p) => !/^\d{1,2}[C-X]?[A-Z]{0,2}$/.test(p))) throw new Error("tiles look like 10TET or a prefix such as 10T");
  // a one-digit UTM zone is written with a leading zero in tile IDs ("1C" -> "01C")
  const norm = prefixes.map((p) => (/^\d(\D|$)/.test(p) ? `0${p}` : p));
  return (tile) => norm.some((p) => tile.startsWith(p));
}
