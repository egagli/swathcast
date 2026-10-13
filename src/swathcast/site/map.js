// Upcoming-pass map: one view. Every selected mission's native footprints
// (Sentinel-1 IW bursts, Sentinel-2 MGRS tiles, Landsat WRS-2 scenes, NISAR
// frames, MODIS/VIIRS sinusoidal tiles, Sentinel-3 frames) are coloured by
// the time to their next acquisition that passes the filters. Filters are per mission (FILTERS: platform, direction, mode,
// polarisation for the SAR missions only, track/path, frame/row/burst/tile);
// the dates and the AOI are shared. An AOI is
// an optional filter: with one, only footprints over it are drawn and the list
// becomes the passes over it, with measured delivery latency for Sentinel-1.
// The AOI UX and the WKB decoder were adapted from next_pass's viewer
// (Apache-2.0, see NOTICE).
import { parquetReadObjects } from "https://cdn.jsdelivr.net/npm/hyparquet@1.16.0/+esm";
import { compressors } from "https://cdn.jsdelivr.net/npm/hyparquet-compressors@1.1.1/+esm";
import {
  css, getJSON, showFreshness, failingSources, fmtWhen, fmtBoth, whenCell, fmtHours, roundUp10,
  expectedAvailability, tzReady, sunElevation, utcText, rel, dayLabel, forgetSettings, localTime,
} from "./common.js";
import { aoiBBox, aoiCenter as centreOfAOI, aoiFromParam, aoiParam, parseAOI, checkAOI } from "./aoi.js";
import {
  acquisitions, bboxOverlapWrapped, buildPlans, burstRange, burstTiles, fetchBursts, inRanges, parseModels,
  parseRanges, s2Tile, tileFilter, toDate, MAX_TILES,
} from "./footprints.js";
import { MapView, BASEMAPS, DEFAULT_BASEMAP } from "./mlmap.js";
import { HORIZON_D, TICKS_D, colorForDays, gradientCSS, scaleT } from "./colors.js";
import { LiveSatellites } from "./sats.js";
import { GlobeOverlay, PolarCaps } from "./overlay.js";
import { fitUtmToData, resample, utmBox, utmEpsg } from "./drape.js";
import { fetchBrowse, fetchHistory, landsatL1, medianRevisit, timelineSVG, usgsItem, widenToImage, HISTORY_DAYS } from "./history.js";
import { Tour, flash, handOver, markTourSeen, pendingStep, sleep, tourSeen, waitFor } from "./tour.js";
import { ALTITUDE_KM, S3_OLCI_KM, WIDE_SWATH_KM, closestApproach, destination, headingAt, position, viewZenith } from "./swathgeom.js";
import { RECENT_DAYS, SOURCES as SEARCHABLE, NOT_SEARCHABLE, PORTAL, registerSource, searchSources, groupAcquisitions } from "./recent.js";

// Zoom levels are MapLibre's. minZoom: below it the footprints in view are too
// many to draw (without an AOI); Sentinel-1/2 then draw their planned
// datatake swaths instead (overview), the others ask to zoom in.
// trackName / frameName: what each mission calls its track and frame, used
// for every label, column, note and filter (one name per thing).
const MISSIONS = [
  { key: "s1", label: "Sentinel-1", cssVar: "--s1", footprint: "IW bursts", minZoom: 6, overview: "planned swaths", trackName: "relative orbit", frameName: "burst", sensor: "sentinel-1" },
  { key: "s2", label: "Sentinel-2", cssVar: "--s2", footprint: "MGRS tiles", minZoom: 4, overview: "planned swaths", trackName: "relative orbit", frameName: "tile", sensor: "sentinel-2" },
  { key: "landsat", label: "Landsat 8/9", cssVar: "--l8", footprint: "WRS-2 scenes", minZoom: 3, trackName: "path", frameName: "row", sensor: "landsat" },
  { key: "nisar", label: "NISAR", cssVar: "--nisar", footprint: "frames", minZoom: 0, trackName: "track", frameName: "frame", sensor: "nisar" },
  // no plans: passes predicted from the orbit (see "MODIS, VIIRS, Sentinel-3" below)
  { key: "modis", label: "MODIS", cssVar: "--m6", footprint: "sinusoidal tiles", minZoom: 0, trackName: "tile h", frameName: "tile v", sensor: "modis" },
  { key: "viirs", label: "VIIRS", cssVar: "--m7", footprint: "sinusoidal tiles", minZoom: 0, trackName: "tile h", frameName: "tile v", sensor: "viirs" },
  // its products' frames are ~1,300 km wide and every place lies under ~20
  // of them: drawn on the same tiles, each pass labelled with its frame
  { key: "s3", label: "Sentinel-3", cssVar: "--m8", footprint: "sinusoidal tiles", minZoom: 0, trackName: "relative orbit", frameName: "frame", sensor: "sentinel-3" },
];
// Per-mission filters. Polarisation exists only for the SAR missions. Every
// mission has its two directions, labelled with the local solar time of its
// passes (sun-synchronous orbits: the same everywhere, measured from the
// plans at |lat| < 40). Sentinel-2 images on its descending passes only;
// Landsat's ascending passes (night side, thermal data only) start unticked.
const FILTERS = {
  s1: { dirs: true, modes: ["IW", "EW", "SM"], pols: true, track: "Relative orbit", frame: "Burst ID", trackPh: "e.g. 137 or 10-20", framePh: "e.g. 290000-290100" },
  s2: { dirs: true, pols: false, track: "Relative orbit", frame: "MGRS tile", trackPh: "e.g. 13 or 10-20", framePh: "e.g. 10TET or 10T", tiles: true },
  landsat: { dirs: true, pols: false, track: "WRS-2 path", frame: "WRS-2 row", trackPh: "e.g. 46 or 40-48", framePh: "e.g. 27 or 20-30" },
  nisar: { dirs: true, pols: true, track: "Track", frame: "Frame", trackPh: "e.g. 134 or 1-20", framePh: "e.g. 59 or 50-80" },
  // The wide-swath sensors image day and night: their filter is the light at
  // the footprint (night off at the start, as Landsat's night side), and for
  // MODIS/VIIRS the view zenith angle (pixels grow ~2-5x towards the swath
  // edges). Tile h/v filter the tiles, not the passes.
  modis: { lights: true, vza: true, cellRanges: true, track: "Tile h", frame: "Tile v", trackPh: "e.g. 9 or 8-10", framePh: "e.g. 4 or 3-5" },
  viirs: { lights: true, vza: true, cellRanges: true, track: "Tile h", frame: "Tile v", trackPh: "e.g. 9 or 8-10", framePh: "e.g. 4 or 3-5" },
  s3: { lights: true, framePass: true, track: "Relative orbit", frame: "Frame", trackPh: "e.g. 81 or 80-90", framePh: "e.g. 1800 or 1620-2160" },
};
const SOLAR = {
  s1: { ascending: "~6 PM", descending: "~6 AM" },
  s2: { ascending: "~10:30 PM", descending: "~10:30 AM" },
  landsat: { ascending: "~10 PM", descending: "~10 AM" },
  nisar: { ascending: "~6 AM", descending: "~6 PM" },
};
// directions not ticked at the start (Landsat's night side), and those a mission never images
const DIRS_OFF = { landsat: ["ascending"] };
const DIRS_NONE = { s2: ["ascending"] };
const dirsOff = (key) => [...(DIRS_OFF[key] || []), ...(DIRS_NONE[key] || [])];
// "descending (~10 AM solar time)"
const dirText = (mission, dir) => (dir && SOLAR[mission] && SOLAR[mission][dir] ? `${dir} (${SOLAR[mission][dir]} solar time)` : dir || "");
const WIDE_PLATFORMS = { modis: ["Terra", "Aqua"], viirs: ["SNPP", "N20", "N21"], s3: ["S3A", "S3B"] };
const WIDE_MISSION = Object.fromEntries(Object.entries(WIDE_PLATFORMS).flatMap(([k, ps]) => ps.map((p) => [p, k])));
const missionOf = (platform) => WIDE_MISSION[platform] || (platform.startsWith("S1") ? "s1" : platform.startsWith("S2") ? "s2" : platform.startsWith("L") ? "landsat" : "nisar");
const MISSION = Object.fromEntries(MISSIONS.map((m) => [m.key, m]));
const POPUP_SOURCES = ["cdse_grd", "cdse_slc", "pc_grd", "pc_rtc", "asf_grd", "asf_slc", "opera_rtc", "opera_cslc"];
const MAX_LIST = 400;
const PLATFORM_NAMES = {
  S1A: "Sentinel-1A", S1C: "Sentinel-1C", S1D: "Sentinel-1D", S2A: "Sentinel-2A", S2B: "Sentinel-2B", S2C: "Sentinel-2C", L8: "Landsat 8", L9: "Landsat 9", NISAR: "NISAR",
  Terra: "Terra (MODIS)", Aqua: "Aqua (MODIS)", SNPP: "Suomi NPP (VIIRS)", N20: "NOAA-20 (VIIRS)", N21: "NOAA-21 (VIIRS)", S3A: "Sentinel-3A", S3B: "Sentinel-3B",
};

const missionColor = (key) => css((MISSION[key] || { cssVar: "--muted" }).cssVar);
// satellites and chips: sensor keys (landsat-8/-9 keep their own colours)
const sensorColor = (sensor) => css({ "sentinel-1": "--s1", "sentinel-2": "--s2", "landsat-8": "--l8", "landsat-9": "--l9", nisar: "--nisar", modis: "--m6", viirs: "--m7", "sentinel-3": "--m8" }[sensor] || "--muted");

let globeOn = true;
try { globeOn = localStorage.getItem("s1lat.globe") !== "flat"; } catch { /* storage unavailable */ }
// The basemap: the link's (#bm=imagery), else the last one picked here.
let basemap = new URLSearchParams(location.hash.slice(1)).get("bm");
if (!BASEMAPS[basemap]) { try { basemap = localStorage.getItem("swathcast.basemap"); } catch { /* storage unavailable */ } }
const view = new MapView("map", {
  globe: globeOn, basemap: BASEMAPS[basemap] ? basemap : DEFAULT_BASEMAP,
  attribution: 'plans: <a href="https://sentinels.copernicus.eu/" target="_blank" rel="noopener">ESA</a>, <a href="https://landsat.usgs.gov/" target="_blank" rel="noopener">USGS</a>, <a href="https://science.nasa.gov/mission/nisar/" target="_blank" rel="noopener">NASA</a> · bursts: <a href="https://asf.alaska.edu/" target="_blank" rel="noopener">ASF</a> · orbits: <a href="https://celestrak.org/" target="_blank" rel="noopener">CelesTrak</a> · previews: <a href="https://worldview.earthdata.nasa.gov/" target="_blank" rel="noopener">NASA Worldview</a>',
});
const map = view.map;
view.onBasemap.push((k) => {
  try { localStorage.setItem("swathcast.basemap", k); } catch { /* storage unavailable */ }
  writeHash();
});
window.__map = map; // for debugging and the browser tests
window.__view = view;

function decodeWKB(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let pos = 0;
  function geom() {
    const little = dv.getUint8(pos) === 1; pos += 1;
    const type = dv.getUint32(pos, little) & 0xff; pos += 4;
    const f64 = () => { const v = dv.getFloat64(pos, little); pos += 8; return v; };
    const u32 = () => { const v = dv.getUint32(pos, little); pos += 4; return v; };
    if (type === 1) return { type: "Point", coordinates: [f64(), f64()] };
    if (type === 3) return { type: "Polygon", coordinates: Array.from({ length: u32() }, () => Array.from({ length: u32() }, () => [f64(), f64()])) };
    if (type === 6) return { type: "MultiPolygon", coordinates: Array.from({ length: u32() }, () => geom().coordinates) };
    throw new Error(`Unsupported WKB geometry type ${type}`);
  }
  return geom();
}

const state = window.__state = {
  missions: new Set(["s1"]), mf: {}, allPlatforms: new Set(), planEnds: {}, palette: "greenred", opacity: 0.3,
  rows: [], loadedSensors: new Set(), aoiVersion: 0, recent: null, recentPlanned: [], inView: [], latency: null, models: {}, plans: {}, wrs2: null, scenes: null, s2: null, nisar: null,
  aoi: null, start: null, end: null, scheduleEnd: null, health: {},
  cells: [], passes: [], notes: [], missionState: {},
  // the one selection: a pass or recent acquisition in the list ({kind, key})
  // or a footprint on the map ({kind: "cell", key: its title}); null: none
  sel: null, token: 0,
  listTab: "planned", // the AOI's list: "planned" (ahead) or "past" (the last RECENT_DAYS)
};
// Draw order: footprints, highlight, AOI outline, drawing in progress; then
// the overlay (overlay.js): footprints poleward of 85 deg on the globe, and
// the live satellites over everything.
// One layer per mission, so a change to one mission's footprints re-sends only
// that mission's to the map. Bottom to top: the wide-swath sensors' large
// tiles and frames, Landsat, Sentinel-2, NISAR, Sentinel-1.
const DRAW_ORDER = ["modis", "viirs", "s3", "landsat", "s2", "nisar", "s1"];
const gridLayers = Object.fromEntries(DRAW_ORDER.map((k) => [k, view.group(`grid-${k}`)]));
const highlightLayer = view.group("highlight", { interactive: false });
const aoiLayer = view.group("aoi", { interactive: false, dashed: true });
const drawLayer = view.group("draw", { interactive: false });
const overlay = new GlobeOverlay(view);
window.__overlay = overlay; // for debugging and the browser tests

// ---------------------------------------------------------------- loading
// Each data file is downloaded once; a download can start early (prefetch)
// and be decoded later.
const downloads = new Map();
function download(name) {
  if (!downloads.has(name)) {
    const p = fetch(`./data/${name}`).then((r) => {
      if (!r.ok) throw new Error(`${name} unavailable (${r.status})`);
      return r.arrayBuffer();
    });
    downloads.set(name, p);
    p.catch(() => downloads.delete(name));
  }
  return downloads.get(name);
}
const decodeParquet = (buf) => parquetReadObjects({ file: buf, compressors, utf8: false });
const parquet = (name) => download(name).then(decodeParquet);

function toRow(r) {
  const geojson = decodeWKB(r.geometry);
  return {
    sensor: r.sensor, platform: String(r.platform), mode: r.mode, direction: r.orbit_direction || null,
    track: r.track == null ? null : Number(r.track), frame: r.frame == null ? null : Number(r.frame),
    polarization: r.polarization || null, modeSource: r.mode_source || null, datatake: r.datatake_id || null,
    begin: toDate(r.begin_time), end: toDate(r.end_time ?? r.begin_time),
    estimated: Boolean(r.time_is_estimated), timing: r.timing || null, geojson, bbox: turf.bbox(geojson),
  };
}

// The schedule comes as one file per sensor, listed in passes_index.json with
// each sensor's plan ends and polarizations (publish.split_schedule). The
// Sentinel-1/2 files load first; NISAR's, most of the rows, only once NISAR is
// selected (it is downloaded in the background after the first draw). A site
// without the index falls back to the single upcoming_passes.parquet.
const FIRST_SENSORS = ["sentinel-1", "sentinel-2"];
const scheduleIndex = fetch("./data/passes_index.json").then((r) => (r.ok ? r.json() : null)).catch(() => null);
let allRows = null;
const sensorRows = new Map();
function rowsOf(sensor) {
  if (!sensorRows.has(sensor)) {
    const p = scheduleIndex.then((idx) => {
      if (!idx) {
        allRows ||= parquet("upcoming_passes.parquet").then((raw) => raw.filter((r) => r.geometry).map(toRow));
        return allRows.then((rows) => rows.filter((r) => r.sensor === sensor));
      }
      const entry = idx.sensors[sensor];
      return entry ? parquet(entry.file).then((raw) => raw.filter((r) => r.geometry).map(toRow)) : [];
    });
    sensorRows.set(sensor, p);
    p.catch(() => sensorRows.delete(sensor));
  }
  return sensorRows.get(sensor);
}
// Adds a sensor's rows to state.rows (once); true when they were new.
async function ensureSensor(sensor) {
  if (state.loadedSensors.has(sensor)) return false;
  const rows = await rowsOf(sensor);
  if (state.loadedSensors.has(sensor)) return false;
  state.loadedSensors.add(sensor);
  state.rows.push(...rows);
  if (sensor === "nisar") state.nisar = null;
  return true;
}

// Started as soon as this module runs: the downloads need neither the map nor
// the time-zone lookup, which load() waits for. (index.html preloads the
// index and the Sentinel-1/2 files, so they start with the page itself.)
const firstData = Promise.all([
  scheduleIndex,
  Promise.all(FIRST_SENSORS.map(rowsOf)),
  getJSON("./data/upcoming_passes.meta.json").catch(() => ({})),
  getJSON("./data/latency.json").catch(() => null),
  getJSON("./data/health.json").catch(() => null),
  getJSON("./data/orbits.json").catch(() => null),
]);
firstData.catch(() => {}); // reported by load()

// Plan ends, platforms and polarizations of every sensor: from the index,
// or (no index) from the rows, all loaded then.
function scheduleSummary(idx, rows) {
  const ends = {}, pols = {};
  if (idx) {
    Object.entries(idx.sensors).forEach(([, e]) => Object.entries(e.plan_ends).forEach(([p, t]) => { ends[p] = new Date(t); }));
    Object.entries(idx.sensors).forEach(([sensor, e]) => { pols[sensor] = e.polarizations; });
    return { ends, pols, end: idx.schedule_end ? new Date(idx.schedule_end) : null };
  }
  rows.forEach((r) => {
    if (!ends[r.platform] || r.begin > ends[r.platform]) ends[r.platform] = r.begin;
    if (r.polarization) (pols[r.sensor] ||= new Set()).add(r.polarization);
  });
  Object.keys(pols).forEach((k) => { pols[k] = [...pols[k]]; });
  const end = rows.length ? new Date(Math.max(...rows.map((r) => r.begin.getTime()))) : null;
  return { ends, pols, end };
}

async function load() {
  const [idx, firstRows, meta, latency, health, orbits] = await firstData;
  state.latency = latency;
  addHealth("s1", health);
  state.models = parseModels(orbits);
  state.rows = firstRows.flat();
  state.loadedSensors = new Set(FIRST_SENSORS);
  if (!idx) { // single file: every sensor is in it
    state.rows = await allRows;
    state.loadedSensors = new Set(state.rows.map((r) => r.sensor).concat(FIRST_SENSORS, ["nisar"]));
  }
  state.plans = buildPlans(state.rows);
  const summary = scheduleSummary(idx, state.rows);
  const hi = summary.end || new Date();
  state.scheduleEnd = hi;
  state.planEnds = {};
  Object.entries(summary.ends).forEach(([p, t]) => { (state.planEnds[missionOf(p)] ||= {})[p] = t; });
  legend();
  const s = document.getElementById("date-start"), e = document.getElementById("date-end");
  s.min = e.min = isoDay(new Date()); s.max = e.max = isoDay(hi);
  s.value = state.start = isoDay(new Date());
  e.value = state.end = defaultEnd();
  showFreshness({ scheduleAt: meta.generated_at, latencyAt: latency && latency.data_as_of, failing: failingSources(health) });
  // each mission's filters, from what is in its data
  const platforms = new Set(Object.keys(summary.ends));
  ["L8", "L9", ...Object.values(WIDE_PLATFORMS).flat()].forEach((p) => platforms.add(p));
  state.allPlatforms = platforms;
  MISSIONS.forEach(({ key, sensor }) => {
    const spec = FILTERS[key];
    const entries = (summary.pols[sensor] || []).flatMap((p) => polEntries(p));
    const pols = spec.pols ? [...new Set(entries.map(polKey))].sort(polKeyOrder) : null;
    state.mf[key] = {
      platforms: new Set([...platforms].filter((p) => missionOf(p) === key)),
      dirs: spec.dirs ? new Set(["ascending", "descending"].filter((d) => !dirsOff(key).includes(d))) : null,
      modes: spec.modes ? new Set(spec.modes) : null,
      allPols: pols, pols: pols ? new Set(pols) : null,
      lights: spec.lights ? new Set(["day"]) : null,
      trackText: "", frameText: "", vzaText: "",
    };
  });
  const restored = readHash();
  buildControls();
  if (!Object.keys(state.models).length) state.notes.push("Orbit timing unavailable: Sentinel-1 bursts cannot be timed.");
  if (restored.aoi) setAOI(restored.aoi, { fly: true, refresh: false });
  await refresh();
  // NISAR's file in the background, so selecting NISAR does not wait for it
  if (idx && idx.sensors.nisar) setTimeout(() => download(idx.sensors.nisar.file).catch(() => {}), 1500);
}

// Day or night at a place and time: the sun above the horizon or not.
// Landsat's night scenes (ascending passes, and polar winter) carry thermal
// data only; its ascending direction is unticked by default (DIRS_OFF).
const lightAt = (when, lon, lat) => (sunElevation(when, lon, lat) > 0 ? "day" : "night");

async function loadS2() {
  if (state.s2) return state.s2;
  const [grid, passes] = await Promise.all([parquet("s2_grid.parquet"), parquet("s2_tile_passes.parquet")]);
  const byTile = new Map();
  passes.forEach((p) => {
    const t = String(p.tile);
    (byTile.get(t) || byTile.set(t, []).get(t)).push({
      when: toDate(p.time), begin: toDate(p.begin), platform: String(p.platform),
      track: Number(p.track), direction: String(p.direction), pol: null, estimated: true,
      coverage: p.coverage == null ? null : Number(p.coverage),
    });
  });
  state.s2 = { tiles: grid.map(s2Tile), byTile };
  return state.s2;
}

async function loadLandsat() {
  if (state.wrs2) return state.wrs2;
  const [grid, scenes] = await Promise.all([parquet("wrs2_grid.parquet"), parquet("landsat_scenes.parquet")]);
  const byCell = new Map();
  scenes.forEach((s) => {
    const k = `${Number(s.path)}|${Number(s.row)}`;
    (byCell.get(k) || byCell.set(k, []).get(k)).push({
      when: toDate(s.time), platform: String(s.platform), track: Number(s.path),
      direction: String(s.direction), pol: null, estimated: true,
    });
  });
  state.wrs2 = grid.map((c) => {
    const geojson = decodeWKB(c.geometry);
    const k = `${Number(c.path)}|${Number(c.row)}`;
    const [lon, lat] = centerOf(geojson);
    const times = (byCell.get(k) || []).sort((a, b) => a.when - b.when);
    times.forEach((t) => { t.light = lightAt(t.when, lon, lat); });
    return { path: Number(c.path), row: Number(c.row), direction: String(c.direction), geojson, bbox: turf.bbox(geojson), times };
  });
  return state.wrs2;
}

// USGS Landsat Level-1 over the AOI, for the recent side of the list: the
// WRS-2 scenes over it, looked up in this site's per-path Level-1 files.
registerSource("usgs_landsat_l1", async () => {
  const grid = await loadLandsat();
  const aoi = state.aoi, box = aoiBBox(aoi);
  const cells = grid.filter((c) => bboxOverlapWrapped(c.bbox, box) && intersects(aoi, c.geojson, c.bbox));
  const want = new Set(cells.map((c) => `${c.path}|${c.row}`));
  const files = await Promise.all([...new Set(cells.map((c) => c.path))].map(landsatL1));
  if (files.length && files.every((f) => f == null)) throw Object.assign(new Error("not published"), { unsearchable: true });
  const since = Date.now() - RECENT_DAYS * 864e5;
  return files.flat().filter((x) => x && want.has(`${x.path}|${x.row}`) && x.when.getTime() >= since).map((x) => ({
    when: x.when, platform: x.platform, id: x.id, track: x.path, direction: x.row > 122 ? "ascending" : "descending",
    href: usgsItem(x.id), linkText: "USGS STAC",
  }));
});

function nisarFrames() {
  if (state.nisar) return state.nisar;
  const frames = new Map();
  state.rows.filter((r) => r.sensor === "nisar").forEach((r) => {
    const key = `${r.track}|${r.frame}|${r.direction}`;
    if (!frames.has(key)) frames.set(key, { track: r.track, frame: r.frame, direction: r.direction, geojson: r.geojson, bbox: r.bbox, times: [] });
    frames.get(key).times.push({ when: r.begin, platform: "NISAR", track: r.track, direction: r.direction, pol: r.polarization, radarMode: r.mode, modeSource: r.modeSource, estimated: true, row: r });
  });
  frames.forEach((f) => f.times.sort((a, b) => a.when - b.when));
  state.nisar = [...frames.values()];
  return state.nisar;
}

// ---------------------------------------------------------------- MODIS, VIIRS, Sentinel-3
// None of them has a plan: they image on every orbit (OLCI in daylight).
// The schedule job predicts their passes from the orbit (wideswath.py), per
// MODIS sinusoidal tile: each pass whose swath covers the tile centre, with
// the centre's cross-track distance (Sentinel-3's also with its relative
// orbit and time after the node, which give its products' frame). Over an AOI every
// candidate pass is timed again at the AOI itself (swathgeom.closestApproach,
// with the poller's tle.json) and kept only if its swath covers the AOI.
const WIDE_FILES = { modis: "modis_tile_passes.parquet", viirs: "viirs_tile_passes.parquet", s3: "s3_tile_passes.parquet" };
const S3_FRAME_S = 180;
const isWide = (key) => key in WIDE_FILES;
const WIDE_MODE = { modis: "MODIS", viirs: "VIIRS", s3: "SLSTR" };
const wideLoads = {};
const vzaOf = (platform, xtrack) => (xtrack == null || !ALTITUDE_KM[platform] ? null : viewZenith(xtrack, ALTITUDE_KM[platform]));
function loadWide(key) {
  if (!wideLoads[key]) {
    const p = (async () => {
      const [grid, passes] = await Promise.all([parquet("sin_grid.parquet"), parquet(WIDE_FILES[key])]);
      const by = new Map(), ends = {};
      passes.forEach((r) => {
        const k = String(r.tile);
        const t = {
          when: toDate(r.time), platform: String(r.platform), direction: String(r.direction), pol: null, estimated: true,
          xtrack: Number(r.xtrack_km), track: r.track == null ? null : Number(r.track),
        };
        if (r.anx_s != null) { // Sentinel-3: its frame, and its orbit's node
          t.frame = Math.floor(Number(r.anx_s) / S3_FRAME_S) * S3_FRAME_S;
          t.node = t.when.getTime() - Number(r.anx_s) * 1000;
        }
        t.vza = vzaOf(t.platform, t.xtrack);
        (by.get(k) || by.set(k, []).get(k)).push(t);
        if (!ends[t.platform] || t.when > ends[t.platform]) ends[t.platform] = t.when;
      });
      const cells = grid.map((c) => {
        const geojson = decodeWKB(c.geometry), center = centerOf(geojson);
        const times = (by.get(String(c.tile)) || []).sort((a, b) => a.when - b.when);
        times.forEach((t) => { t.light = lightAt(t.when, center[0], center[1]); });
        return { tile: String(c.tile), h: Number(c.h), v: Number(c.v), title: `tile ${c.tile}`, geojson, bbox: turf.bbox(geojson), center, times };
      });
      state.planEnds[key] = ends; // predicted to the schedule's end
      legendMissions();
      return cells;
    })();
    wideLoads[key] = p;
    p.catch(() => { delete wideLoads[key]; });
  }
  return wideLoads[key];
}

// Orbital elements for timing passes at the AOI (the live satellites' file).
let wideSatrecs = null;
function satrecsFor() {
  wideSatrecs ||= getJSON("./data/tle.json").then((j) => Object.fromEntries(
    Object.entries(j.satellites || {}).filter(([p]) => WIDE_MISSION[p]).map(([p, x]) => [p, satellite.json2satrec(x.omm)]),
  )).catch(() => ({}));
  return wideSatrecs;
}

// A pass of a wide-swath sensor over the AOI, from the footprints' candidate
// times: timed at the AOI centre, its cross-track distance there, whether the
// swath reaches the AOI (its corners and centre: any of it), the view zenith
// angle and, for Sentinel-3, which instruments see it (OLCI: by day, and
// within its narrower strip). null if the swath misses the AOI.
function refineAtAOI(p, satrec) {
  const [lon, lat] = aoiCenter();
  // no elements to time it with: the footprint's own time, unchecked
  if (!satrec) return { ...p, timedAtAOI: false };
  const r = closestApproach(satrec, lon, lat, p.when.getTime() - 900e3, p.when.getTime() + 900e3);
  if (!r) return null;
  const [lo, hi] = WIDE_SWATH_KM[p.platform];
  const [w, s, e, n] = aoiBBox(state.aoi);
  const pts = state.aoi.type === "Point" ? [[lon, lat]] : [[lon, lat], [w, s], [w, n], [e, s], [e, n]];
  const xs = pts.map(([x, y]) => (x === lon && y === lat ? r.xtrack : (closestApproach(satrec, x, y, r.t - 300e3, r.t + 300e3, 5) || r).xtrack));
  const covered = xs.some((x) => x >= lo && x <= hi) || (Math.min(...xs) < lo && Math.max(...xs) > hi);
  if (!covered) return null;
  const when = new Date(r.t), light = lightAt(when, lon, lat);
  const q = { ...p, when, whenEnd: when, xtrack: r.xtrack, vza: vzaOf(p.platform, r.xtrack), light, timedAtAOI: true };
  if (p.mission === "s3") {
    // the frame imaging the AOI: its time after the orbit's node
    if (p.node != null) q.frame = Math.floor((r.t - p.node) / 1000 / S3_FRAME_S) * S3_FRAME_S;
    const olci = light === "day" && xs.some((x) => x >= S3_OLCI_KM[0] && x <= S3_OLCI_KM[1]);
    q.instruments = olci ? ["OLCI", "SLSTR"] : ["SLSTR"];
    q.mode = q.instruments.join(" + ");
  }
  return q;
}

// The strip a wide-swath pass images around the AOI (±3 min of the pass):
// what the list highlights for it, rather than a 1,000 km tile.
function swathStrip(platform, satrec, t) {
  const [lo, hi] = WIDE_SWATH_KM[platform], ring = [], back = [];
  for (let dt = -180; dt <= 180; dt += 15) {
    const at = t + dt * 1000, p = position(satrec, new Date(at)), h = headingAt(satrec, at);
    if (!p || h == null) continue;
    ring.push(destination(p.lon, p.lat, h + 90, hi));
    back.unshift(destination(p.lon, p.lat, h - 90, -lo));
  }
  const pts = [...ring, ...back];
  if (pts.length < 4) return null;
  let prev = null;
  const cont = pts.map(([x, y]) => { let l = x; if (prev != null) l += 360 * Math.round((prev - l) / 360); prev = l; return [l, y]; });
  return { type: "Polygon", coordinates: [[...cont, cont[0]]] };
}

// ---------------------------------------------------------------- filters
// A polarization field: "VV+VH" (Sentinel-1), or per band for NISAR, one
// entry per mode imaged that date: "L HH+HV, S RH+RV" -> [{band, pol}, ...].
// The filter's keys are the entries themselves ("L HH", "S HH" differ:
// NISAR's two radars are separate instruments with separate products).
const BAND_NAME = { L: "L-band", S: "S-band" };
function polEntries(p) {
  return (p ? String(p).split(/,\s*/).filter(Boolean) : []).map((e) => {
    const m = /^([LS]) (.+)$/.exec(e);
    return m ? { band: m[1], pol: m[2] } : { band: null, pol: e };
  });
}
const polKey = (e) => (e.band ? `${e.band} ${e.pol}` : e.pol);
// Names for the less obvious channel sets (pills and text).
const POL_PILL = { "HH+HV+VH+VV": "Quad (HH+HV+VH+VV)", "RH+RV": "Compact (RH+RV)" };
const POL_NAME = { "HH+VV": "HH+VV (quasi-dual)", "HH+HV+VH+VV": "quad-pol", "RH+RV": "RH+RV (compact)" };
const POL_TITLE = {
  HH: "single-pol: H transmit, H receive", VV: "single-pol: V transmit, V receive",
  "HH+HV": "dual-pol: H transmit, H and V receive", "VV+VH": "dual-pol: V transmit, V and H receive",
  "HH+VV": "quasi-dual: HH and VV on two separate 5 MHz sub-bands",
  "HH+HV+VH+VV": "quad-pol: HH, HV, VH and VV", "RH+RV": "compact-pol: right-circular transmit, H and V receive",
};
// For display: "L-band HH+HV · S-band RH+RV (compact)"
function polText(p) {
  return polEntries(p).map(({ band, pol }) => `${band ? `${BAND_NAME[band]} ` : ""}${POL_NAME[pol] || pol}`).join(" · ");
}
const POL_ORDER = ["HH", "VV", "HH+HV", "VV+VH", "HH+VV", "HH+HV+VH+VV", "RH+RV"];
function polOrder(a, b) {
  const r = (p) => POL_ORDER.indexOf(p) + 1 || 99;
  return r(a) - r(b) || a.localeCompare(b);
}
const polKeyOrder = (a, b) => {
  const [ea, eb] = [polEntries(a)[0], polEntries(b)[0]];
  return String(ea.band).localeCompare(String(eb.band)) || polOrder(ea.pol, eb.pol);
};
// NISAR radar modes of one date, per band, with the L-band range bandwidth:
// "L:SCI:DH:40M+05N:FS:B4:D02  S:DB:CP:37W:B4" -> L HH+HV 40+5 MHz, S RH+RV.
// (40M+05N is 40 MHz on frequency A plus 5 MHz on B, as ASF's RSLC
// products confirm: RANGE_BANDWIDTH_CONCAT "40+5".)
const NISAR_POL = { SH: "HH", SV: "VV", DH: "HH+HV", DV: "VV+VH", QP: "HH+HV+VH+VV", QD: "HH+VV", CP: "RH+RV" };
function nisarModes(mode) {
  const out = new Map();
  String(mode || "").split(/[,\s]+/).forEach((m) => {
    const [band, , code, bw] = m.split(":");
    if (!["L", "S"].includes(band) || !code || code === "--") return;
    const pol = NISAR_POL[code] || code;
    const mhz = band === "L" && bw ? bw.split("+").map((x) => parseInt(x, 10)).filter(Number.isFinite).join("+") : "";
    out.set(`${band}|${pol}|${mhz}`, { band, pol, mhz });
  });
  return [...out.values()];
}
// One line per mode: "L-band HH+HV 40+5 MHz". Falls back to the pol field.
function nisarModeText(mode, pol, bandwidth) {
  let modes = nisarModes(mode);
  if (!modes.length) modes = polEntries(pol).map((e) => ({ ...e, mhz: bandwidth || "" }));
  if (!modes.length) return "";
  return modes.map((e) => `${e.band ? `${BAND_NAME[e.band]} ` : ""}<span title="${POL_TITLE[e.pol] || ""}">${POL_NAME[e.pol] || e.pol}</span>${e.mhz ? ` <span class="sub">${e.mhz} MHz</span>` : ""}`).join("<br>");
}
const platformOn = (p) => { const m = state.mf[missionOf(p)]; return Boolean(m && m.platforms.has(p)); };

function windowMs() {
  let t0 = new Date(`${state.start}T00:00:00Z`).getTime();
  let t1 = new Date(`${state.end}T23:59:59Z`).getTime();
  if (t0 > t1) [t0, t1] = [t1, t0];
  return [Math.max(t0, Date.now()), t1];
}

// Parsed text filters per mission: {track, frames, tiles, error}.
function filters() {
  const m = {};
  MISSIONS.forEach(({ key }) => {
    const mf = state.mf[key];
    const out = { track: null, frames: null, tiles: () => true, vzaMax: null, errors: [] };
    try { out.track = parseRanges(mf.trackText); } catch (e) { out.errors.push(`${FILTERS[key].track}: ${e.message}`); }
    if (FILTERS[key].vza && mf.vzaText.trim()) {
      const v = Number(mf.vzaText.trim());
      if (Number.isFinite(v) && v > 0 && v <= 90) out.vzaMax = v;
      else out.errors.push(`${VZA_LABEL}: give an angle in degrees, 1 to 90`);
    }
    try {
      if (FILTERS[key].tiles) out.tiles = tileFilter(mf.frameText);
      else out.frames = parseRanges(mf.frameText);
    } catch (e) { out.errors.push(`${FILTERS[key].frame}: ${e.message}`); }
    m[key] = out;
  });
  const [t0, t1] = windowMs();
  return { m, t0, t1 };
}

const VZA_LABEL = "Max view zenith (°)";
// Does one acquisition pass its mission's filters? A SAR acquisition whose
// polarisation is unknown is kept; one imaged in several modes (NISAR) passes
// if any of them is selected.
function timeOK(t, f) {
  const w = t.when.getTime();
  return w >= f.t0 && w <= f.t1 && filterOK(t, f);
}
// The same, without the dates (for acquisitions in the recent past).
function filterOK(t, f) {
  const key = missionOf(t.platform), mf = state.mf[key];
  return mf.platforms.has(t.platform)
    && (!mf.dirs || !t.direction || mf.dirs.has(t.direction))
    && (!mf.modes || !t.mode || mf.modes.has(t.mode))
    && (!mf.pols || t.pol == null || polEntries(t.pol).some((e) => mf.pols.has(polKey(e))))
    && (!mf.lights || !t.light || mf.lights.has(t.light))
    && (f.m[key].vzaMax == null || t.vza == null || t.vza <= f.m[key].vzaMax)
    && (FILTERS[key].cellRanges || inRanges(f.m[key].track, t.track))
    && (!FILTERS[key].framePass || inRanges(f.m[key].frames, t.frame));
}

const shiftLon = (g, dx) => ({ type: g.type, coordinates: g.coordinates.map((ring) => ring.map(([x, y]) => [x + dx, y])) });
function intersects(aoi, geojson, bbox) {
  if (!bboxOverlapWrapped(bbox || turf.bbox(geojson), turf.bbox(aoi))) return false;
  try {
    if (turf.booleanIntersects(aoi, geojson)) return true;
    const [w, , e] = bbox || turf.bbox(geojson);
    if (e > 180 && geojson.type === "Polygon") return turf.booleanIntersects(aoi, shiftLon(geojson, -360));
    if (w < -180 && geojson.type === "Polygon") return turf.booleanIntersects(aoi, shiftLon(geojson, 360));
  } catch { /* invalid geometry */ }
  return false;
}

function centerOf(geojson) {
  const ring = geojson.type === "Polygon" ? geojson.coordinates[0] : geojson.coordinates[0][0];
  const n = Math.max(1, ring.length - 1);
  return [ring.slice(0, n).reduce((a, p) => a + p[0], 0) / n, ring.slice(0, n).reduce((a, p) => a + p[1], 0) / n];
}

// ---------------------------------------------------------------- footprints
// A cell: {mission, geojson, bbox, center, title, track, frame, frameLabel,
// direction, times: [{when, platform, direction, pol, track, pass...}]}.
// One cell per planned datatake swath (Sentinel-1 EW/SM always, IW and
// Sentinel-2 when zoomed out): a single acquisition, dropped once filtered out.
function datatakeCells(key, area, keep) {
  const now = Date.now(), from = state.aoi ? now - PAST_MS[key] : now;
  return state.rows.filter((r) => r.sensor === MISSION[key].sensor && keep(r) && r.end.getTime() >= from && bboxOverlapWrapped(r.bbox, area)).map((r) => {
    const t = { when: r.begin, begin: r.begin, platform: r.platform, track: r.track, direction: r.direction, pol: r.polarization, mode: r.mode, pass: r, estimated: false };
    const ahead = r.end.getTime() >= now;
    return {
      mission: key, datatake: true, geojson: r.geojson, bbox: r.bbox, center: centerOf(r.geojson),
      title: `${r.mode} datatake${r.datatake ? ` ${r.datatake}` : ""}`, track: r.track, frame: null, frameLabel: r.mode, direction: r.direction,
      // a datatake under way is still ahead; one that has ended is a pass of the recent past
      times: ahead ? [t] : [], past: ahead ? [] : [t],
    };
  });
}

// Planned passes of the recent past, kept so that a pass imaged but not yet
// delivered anywhere is still listed (the catalogues list the rest): as far
// back as the schedule keeps them (schedule.py LOOKBACK_DAYS, about each
// mission's p90 delivery time plus a day; Landsat Level-1 takes ~10 days).
const PAST_MS = { s1: 2 * 864e5, s2: 2 * 864e5, nisar: 3 * 864e5, landsat: 11 * 864e5, modis: 2 * 864e5, viirs: 2 * 864e5, s3: 2 * 864e5 };
const pastOf = (list, now, key) => (list || []).filter((x) => x.when.getTime() < now && x.when.getTime() >= now - PAST_MS[key]);

// The zoom the page reasons with. On the globe MapLibre changes its zoom as
// the view pans north or south (by log2(cos(latitude)), so the globe keeps
// its size on screen): the same view reads 1 zoom level lower at 60 degrees
// than at the equator. The page uses the equator-equivalent zoom there
// instead, which does not change while panning; on the flat map MapLibre's
// own zoom already does not.
function viewZoom() {
  const z = map.getZoom();
  if (!isGlobe()) return z;
  const lat = Math.min(85, Math.abs(map.getCenter().lat));
  return z - Math.log2(Math.cos((lat * Math.PI) / 180));
}
const isGlobe = () => ((map.getProjection && map.getProjection()?.type) || "mercator") === "globe";

// [w, s, e, n] with an edge past +/-180 split into two boxes within it.
function splitArea([w, s, e, n]) {
  if (e - w >= 360) return [[-180, s, 180, n]];
  if (e > 180) return [[w, s, 180, n], [-180, s, e - 360, n]];
  if (w < -180) return [[w + 360, s, 180, n], [-180, s, e, n]];
  return [[w, s, e, n]];
}
// The area a mission's footprints are built for: the AOI's box, or the view
// with a margin (see MARGIN).
const buildArea = (key) => (state.aoi ? aoiBBox(state.aoi) : padded(viewBounds(), MARGIN[key]));
// Burst-map tiles Sentinel-1 would need here (more than MAX_TILES: swaths).
const s1Tiles = () => splitArea(buildArea("s1")).reduce((n, p) => n + burstTiles(p), 0);
// Planned swaths instead of native footprints: zoomed out (without an AOI)
// or, for Sentinel-1, an area too large for the burst map.
function overviewOf(key) {
  const m = MISSION[key];
  if (!m.overview) return false;
  if (!state.aoi && viewZoom() < m.minZoom - 0.3) return true;
  return key === "s1" && (s1Tiles() > MAX_TILES || !Object.keys(state.models).length);
}
// The zoom at which Sentinel-1's bursts appear here: level 6, or further in
// until the view plus its margin needs no more than MAX_TILES burst-map
// tiles. Estimated for each candidate zoom from the canvas size at the
// view's latitude (MapLibre: 512 px tiles), with a 20 % margin, so one press
// of "Zoom in" gets there.
function s1BurstZoom() {
  const canvas = map.getCanvas(), lat = Math.min(80, Math.abs(map.getCenter().lat));
  const pad = 1 + 2 * MARGIN.s1;
  for (let z = MISSION.s1.minZoom; z < 13; z += 0.5) {
    // degrees per pixel: viewZoom is equator-equivalent on the globe, MapLibre's own on the flat map
    const deg = 360 / (512 * 2 ** z), cos = Math.cos((lat * Math.PI) / 180);
    const w = canvas.clientWidth * deg * pad / (isGlobe() ? cos : 1);
    const h = canvas.clientHeight * deg * pad * (isGlobe() ? 1 : cos);
    if ((Math.ceil(w / 2) + 1) * (Math.ceil(h / 2) + 1) <= 0.8 * MAX_TILES) return z;
  }
  return 13;
}
const fineZoom = (m) => (m.key === "s1" ? s1BurstZoom() : m.minZoom);

async function missionCells(key, area, f, token) {
  const now = Date.now();
  const cells = [];
  if (key === "s1") {
    const overview = overviewOf("s1");
    cells.push(...datatakeCells("s1", area, (r) => overview || r.mode !== "IW"));
    if (overview) return cells;
    let bursts;
    try {
      bursts = (await Promise.all(splitArea(area).map((p) => fetchBursts([p[0], Math.max(-90, p[1]), p[2], Math.min(90, p[3])])))).flat();
    } catch (err) {
      if (token !== state.token) return null;
      if (!/too many/.test(err.message)) throw err;
      // denser than the burst map allows here: the IW swaths instead
      const out = [...cells, ...datatakeCells("s1", area, (r) => r.mode === "IW")];
      out.fallback = true;
      return out;
    }
    if (token !== state.token) return null;
    const end = state.scheduleEnd.getTime();
    bursts.forEach((b) => {
      if (!inRanges(f.m.s1.frames, b.bid)) return;
      // with an AOI, also the planned passes of the recent past (see PAST_MS)
      const all = acquisitions(state.models, state.plans, b.track, b.tanx, state.aoi ? now - PAST_MS.s1 : now, end).map((a) => ({
        when: a.when, platform: a.platform, track: b.track, direction: a.pass.direction, pol: a.pass.polarization,
        mode: "IW", pass: a.pass, estimated: false,
      }));
      cells.push({
        mission: "s1", geojson: b.geojson, bbox: turf.bbox(b.geojson), center: centerOf(b.geojson), title: b.id,
        track: b.track, frame: b.bid, frameLabel: `${String(b.bid).padStart(6, "0")} ${b.swath}`, burst: b,
        times: all.filter((x) => x.when.getTime() >= now), past: all.filter((x) => x.when.getTime() < now),
      });
    });
  } else if (key === "s2") {
    if (overviewOf("s2")) return datatakeCells("s2", area, (r) => r.mode === "NOBS");
    const s2 = await loadS2();
    if (token !== state.token) return null;
    s2.tiles.forEach((t) => {
      if (!bboxOverlapWrapped(t.bbox, area) || !f.m.s2.tiles(t.tile)) return;
      cells.push({
        mission: "s2", geojson: t.geojson, bbox: t.bbox, center: t.center, title: `tile ${t.tile}`,
        track: null, frame: t.tile, frameLabel: t.tile, tile: t.tile, times: (s2.byTile.get(t.tile) || []).filter((x) => x.when.getTime() >= now), past: pastOf(s2.byTile.get(t.tile), now, "s2"),
      });
    });
  } else if (key === "landsat") {
    const grid = await loadLandsat();
    if (token !== state.token) return null;
    grid.forEach((c) => {
      const lf = f.m.landsat;
      if (!bboxOverlapWrapped(c.bbox, area) || !inRanges(lf.frames, c.row) || !inRanges(lf.track, c.path)) return;
      cells.push({
        mission: "landsat", geojson: c.geojson, bbox: c.bbox, center: centerOf(c.geojson),
        title: `path ${c.path} row ${c.row}`, track: c.path, frame: c.row, frameLabel: String(c.row), direction: c.direction,
        times: c.times.filter((x) => x.when.getTime() >= now), past: pastOf(c.times, now, "landsat"),
      });
    });
  } else if (isWide(key)) {
    const grid = await loadWide(key);
    if (token !== state.token) return null;
    const wf = f.m[key], byTile = FILTERS[key].cellRanges; // MODIS/VIIRS: h and v filter the tiles
    // with an AOI, also the tiles around it: a pass can cover the AOI without
    // covering the centre of any tile under it (candidates, see wideOverAOI)
    const [alon, alat] = state.aoi ? aoiCenter() : [0, 0];
    grid.forEach((c) => {
      if (byTile && (!inRanges(wf.track, c.h) || !inRanges(wf.frames, c.v))) return;
      const over = bboxOverlapWrapped(c.bbox, area);
      const near = state.aoi && turf.distance([alon, alat], c.center, { units: "kilometers" }) < 2400;
      if (!over && !near) return;
      cells.push({
        mission: key, geojson: c.geojson, bbox: c.bbox, center: c.center, title: c.title, tile: c.tile,
        track: null, frame: null, frameLabel: c.tile, direction: null,
        times: c.times.filter((x) => x.when.getTime() >= now), past: pastOf(c.times, now, key), candidate: !over,
      });
    });
  } else {
    await ensureSensor("nisar");
    if (token !== state.token) return null;
    nisarFrames().forEach((fr) => {
      const nf = f.m.nisar;
      if (!bboxOverlapWrapped(fr.bbox, area) || !inRanges(nf.frames, fr.frame) || !inRanges(nf.track, fr.track) || (fr.direction && !state.mf.nisar.dirs.has(fr.direction))) return;
      cells.push({
        mission: "nisar", geojson: fr.geojson, bbox: fr.bbox, center: centerOf(fr.geojson),
        title: `track ${fr.track} frame ${fr.frame ?? "?"}`, track: fr.track, frame: fr.frame, frameLabel: fr.frame == null ? "" : String(fr.frame),
        direction: fr.direction, times: fr.times.filter((x) => x.when.getTime() >= now), past: pastOf(fr.times, now, "nisar"),
      });
    });
  }
  // S1 bursts and S2 tiles have no static direction/track: drop those the
  // filters can never match.
  return cells;
}

// ---------------------------------------------------------------- passes over the AOI
function aoiCenter() {
  return state.aoi ? centreOfAOI(state.aoi) : null;
}

function passKey(c, t) {
  if (c.mission === "s1") return `s1|${t.platform}|${t.pass.begin.getTime()}`;
  // one orbit: Sentinel-3 frames share their node; tiles are minutes apart
  // (made exact by refineAtAOI, then merged by wideOverAOI)
  if (c.mission === "s3" && t.node != null) return `s3|${t.platform}|${Math.round(t.node / 60e3)}`;
  if (isWide(c.mission)) return `${c.mission}|${t.platform}|${Math.round(t.when.getTime() / 1200e3)}`;
  if (c.mission === "s2") return `s2|${t.platform}|${t.begin.getTime()}`;
  const day = t.when.toISOString().slice(0, 10);
  return c.mission === "landsat" ? `ls|${t.platform}|${t.track}|${t.direction}|${day}` : `ni|${t.track}|${t.direction}|${day}`;
}

function buildPasses(cells) {
  const out = groupPasses(cells.filter((c) => !isWide(c.mission)), (c) => c.visible);
  MISSIONS.filter((m) => isWide(m.key) && state.missions.has(m.key)).forEach((m) => out.push(...wideOverAOI(m.key, filters())));
  // Whole datatakes without native footprints: S1 EW/SM, S2 calibration takes.
  const f = filters();
  state.rows.forEach((r) => {
    const mission = r.sensor === "sentinel-1" ? "s1" : r.sensor === "sentinel-2" ? "s2" : null;
    if (!mission || !state.missions.has(mission)) return;
    if (mission === "s1" || r.mode === "NOBS") return; // S1 datatakes are drawn as cells
    const t = { when: r.begin, platform: r.platform, direction: r.direction, pol: r.polarization, track: r.track, mode: mission === "s1" ? r.mode : null };
    if (!timeOK(t, f) || !intersects(state.aoi, r.geojson, r.bbox)) return;
    out.push({
      key: `dt|${r.platform}|${r.begin.getTime()}`, mission, sensor: r.sensor, platform: r.platform, direction: r.direction,
      track: r.track, pol: r.polarization, mode: r.mode, estimated: false, pass: r, cells: [], when: r.begin, whenEnd: r.end,
      shapes: [nearAOI(r.geojson)], footprints: "datatake", ll: aoiCenter(),
      note: mission === "s1" ? `${r.mode} datatake (bursts are drawn for IW only), clipped to the AOI's surroundings.` : `${r.mode} datatake (not a nominal observation: no user tiles expected).`,
    });
  });
  return out;
}

// The passes of a wide-swath mission over the AOI (ahead, or of the recent
// past), from its footprints' candidate times: each timed again at the AOI,
// dropped if its swath misses the AOI, then filtered with what was found
// there (light, view angle). Each pass highlights the strip it images
// around the AOI (MODIS, VIIRS) or its frames over the AOI (Sentinel-3).
function wideOverAOI(key, f, past = false) {
  const b = built[key];
  if (!b || !b.candidates || !state.aoi) return [];
  const now = Date.now(), mf = state.mf[key];
  const pick = (c) => (past ? c.past || [] : c.times).filter((t) => mf.platforms.has(t.platform));
  const merged = new Map();
  groupPasses(b.candidates, pick).forEach((p) => {
    const q = refineAtAOI(p, (state.satrecs || {})[p.platform]);
    if (!q || !(past ? q.when.getTime() < now && filterOK(q, f) : timeOK(q, f))) return;
    const k = `${key}|${q.platform}|${Math.round(q.when.getTime() / 60e3)}`;
    const have = merged.get(k);
    if (have) { have.cells.push(...q.cells); return; }
    merged.set(k, { ...q, key: k, cells: [...p.cells] });
  });
  return [...merged.values()].map((q) => {
    q.cells = [...new Set(q.cells)].filter((c) => intersects(state.aoi, c.geojson, c.bbox));
    const satrec = (state.satrecs || {})[q.platform];
    const strip = satrec && q.timedAtAOI ? swathStrip(q.platform, satrec, q.when.getTime()) : null;
    q.shapes = strip ? [strip] : q.cells.map((c) => c.geojson);
    q.footprints = footprintLabel(q);
    return q;
  });
}

// The footprints' acquisitions (timesOf(cell)) grouped into passes over the AOI.
function groupPasses(cells, timesOf) {
  const passes = new Map();
  cells.forEach((c) => timesOf(c).forEach((t) => {
    const k = passKey(c, t);
    if (!passes.has(k)) {
      passes.set(k, {
        key: k, mission: c.mission, sensor: MISSION[c.mission].sensor, platform: t.platform, direction: t.direction || c.direction || null,
        track: t.track ?? c.track, pol: t.pol, mode: t.mode || t.radarMode || (c.mission === "s2" ? "NOBS" : c.mission === "landsat" ? "OLI/TIRS" : WIDE_MODE[c.mission] || null),
        modeSource: t.modeSource || null, light: t.light || null, vza: t.vza ?? null, xtrack: t.xtrack ?? null, node: t.node ?? null, frame: t.frame ?? null,
        estimated: t.estimated, pass: t.pass || null, cells: [], times: [], ll: aoiCenter(),
      });
    }
    const p = passes.get(k);
    p.cells.push(c);
    p.times.push(t.when);
  }));
  const out = [...passes.values()].map((p) => {
    p.when = new Date(Math.min(...p.times));
    p.whenEnd = new Date(Math.max(...p.times));
    p.shapes = p.cells.map((c) => (c.datatake ? nearAOI(c.geojson) : c.geojson));
    p.footprints = footprintLabel(p);
    return p;
  });
  return out;
}

// A datatake swath (thousands of km long) cut to the part around the AOI:
// within NEAR_KM of its centre, so the excerpt ends in arcs rather than in
// the straight north-south and east-west edges of a lon/lat box, which read
// as footprint edges.
const NEAR_KM = 400;
function nearAOI(geojson) {
  try {
    const c = aoiCenter();
    const [x0, y0, x1, y1] = aoiBBox(state.aoi);
    const r = NEAR_KM + turf.distance([x0, y0], [x1, y1], { units: "kilometers" }) / 2;
    const cut = turf.intersect(turf.featureCollection([turf.feature(geojson), turf.circle(c, r, { units: "kilometers", steps: 96 })]));
    return cut ? cut.geometry : geojson;
  } catch { return geojson; }
}

function footprintLabel(p) {
  const n = p.cells.length;
  if (p.mission === "s3" && p.frame != null) return `frame ${p.frame}`;
  if (isWide(p.mission)) return !n ? "" : n <= 2 ? p.cells.map((c) => c.tile).sort().join(" ") : `${n} tiles`;
  if (p.cells.length && p.cells.every((c) => c.datatake)) return "swath";
  if (p.mission === "s1") return `${n} burst${n > 1 ? "s" : ""}`;
  if (p.mission === "s2") return n <= 3 ? p.cells.map((c) => c.tile).sort().join(" ") : `${n} tiles`;
  if (p.mission === "landsat") {
    const rows = [...new Set(p.cells.map((c) => c.frame))].sort((a, b) => a - b);
    return rows.length === 1 ? `row ${rows[0]}` : `rows ${rows[0]}–${rows[rows.length - 1]}`;
  }
  const frames = [...new Set(p.cells.map((c) => c.frame))].sort((a, b) => a - b);
  return frames.length === 1 ? `frame ${frames[0]}` : `frames ${frames[0]}–${frames[frames.length - 1]}`;
}

// "the AOI 412 km right of the ground track, view zenith 38°"
function wideGeometryText(p) {
  if (p.xtrack == null) return "";
  const side = p.xtrack >= 0 ? "right" : "left";
  return `the AOI ${Math.round(Math.abs(p.xtrack))} km ${side} of the ground track${p.vza != null ? `, view zenith ${Math.round(p.vza)}°` : ""}`;
}
function passNote(p) {
  if (p.note) return p.note;
  if (isWide(p.mission)) {
    const name = MISSION[p.mission].label;
    const timed = p.timedAtAOI ? `timed at the AOI from the orbit (SGP4): ${wideGeometryText(p)}` : "timed at its footprint from the orbit (SGP4; the AOI could not be timed: no orbital elements)";
    const what = p.mission === "s3"
      ? `Relative orbit ${p.track}, ${p.footprints || "frame unknown"} (the products' frame: seconds after the ascending node). Sentinel-3 has no acquisition plan: SLSTR images on every orbit, OLCI in daylight${p.instruments ? ` (here: ${p.instruments.join(" and ")})` : ""}.`
      : `${name} has no acquisition plan: it images on every orbit, day and night; the pass is predicted from the orbit (${p.footprints}).`;
    return `${what} ${cap(timed)}.`;
  }
  const span = Math.round((p.whenEnd - p.when) / 1000);
  if (p.mission === "s1" && p.cells.some((c) => c.datatake)) {
    return `${p.mode} datatake ${fmtWhen(p.pass.begin)}–${p.pass.end.toISOString().slice(11, 16)} UTC${p.pol ? `, ${polText(p.pol)}` : ""} (bursts are drawn for IW only); the swath is clipped to the AOI's surroundings.`;
  }
  if (p.mission === "s1") {
    const bursts = p.cells.map((c) => c.burst);
    return `T${String(p.track).padStart(3, "0")}-${burstRange(bursts)}, imaged within ${Math.max(1, span)} s from ${p.when.toISOString().slice(11, 19)} UTC; the datatake runs ${fmtWhen(p.pass.begin)}–${p.pass.end.toISOString().slice(11, 16)} UTC${p.pol ? `, ${polText(p.pol)}` : ""}.`;
  }
  if (p.mission === "s2") return `Relative orbit ${p.track}; tile times from the orbit (SGP4), ~10 s. STAC items carry the datatake start instead.`;
  if (p.mission === "landsat") return `WRS-2 path ${p.track}, ${p.footprints}; scene times from the orbit (SGP4) on USGS's acquisition calendar.`;
  const pol = p.pol ? `, ${polText(p.pol)}` : ", polarization not in the plan";
  const sband = polEntries(p.pol).some((e) => e.band === "S") ? " S-band data come from ISRO (Bhoonidhi), not ASF." : "";
  const inferred = p.modeSource === "earlier cycle" ? " (the plan leaves this date's mode blank: the frame's last listed mode, which matched ASF's products for 97% of L-band frames; the S-band part is not checked)" : "";
  return `Track ${p.track}, ${p.footprints}${pol}${inferred}${p.mode ? `; radar mode ${p.mode}` : ""}; times from the orbit (SGP4) on the NISAR plan's dates.${sband}`;
}

// ---------------------------------------------------------------- latency
// Measured delivery latency per mission: the poller's latency files
// (Sentinel-1's loads with the page, the others once an AOI is drawn).
const LATENCY_FILES = {
  s2: ["latency_s2.json"], landsat: ["latency_landsat.json"], nisar: ["latency_nisar.json", "latency_nisar_s.json"],
  modis: ["latency_modis.json"], viirs: ["latency_viirs.json"], s3: ["latency_s3.json"],
};
const MISSION_SOURCES = {
  s1: POPUP_SOURCES,
  s2: ["cdse_s2_l1c", "cdse_s2_l2a", "es_s2_l1c", "es_s2_l2a", "pc_s2_l2a", "hls_s30"],
  landsat: ["usgs_landsat_l1", "usgs_landsat_l2", "es_landsat_l2", "pc_landsat_l2", "hls_l30"],
  nisar: ["asf_nisar_l0b", "asf_nisar_rslc", "asf_nisar_gcov", "bhoonidhi_nisar_s_rslc", "bhoonidhi_nisar_s_gcov"],
  modis: ["lance_modis_l1b", "laads_modis_l1b", "laads_modis_l1b_c7", "lance_modis_snow", "nsidc_modis_snow"],
  viirs: ["lance_viirs_l1b", "laads_viirs_l1b", "lance_viirs_snow", "nsidc_viirs_snow"],
  s3: ["cdse_s3_efr", "eumetsat_s3_efr", "cdse_s3_efr_ntc", "cdse_s3_lfr", "pc_s3_lfr", "cdse_s3_rbt", "cdse_s3_rbt_ntc", "cdse_s3_lst", "pc_s3_lst"],
};
// Daytime-only products: snow cover (MODIS, VIIRS), and OLCI (Sentinel-3)
const DAY_ONLY = new Set(["lance_modis_snow", "nsidc_modis_snow", "lance_viirs_snow", "nsidc_viirs_snow"]);
const OLCI_SOURCES = new Set(["cdse_s3_efr", "eumetsat_s3_efr", "cdse_s3_efr_ntc", "cdse_s3_lfr", "pc_s3_lfr"]);
const S_BAND_SOURCES = new Set(["bhoonidhi_nisar_s_rslc", "bhoonidhi_nisar_s_gcov"]);
const latencyLoads = {};
function missionLatency(key) { // {expected, sources}, or null while loading or unavailable
  if (key === "s1") return state.latency;
  if (!latencyLoads[key]) {
    const entry = { value: null, done: false };
    latencyLoads[key] = entry;
    Promise.all(LATENCY_FILES[key].map((f) => getJSON(`./data/${f}`).catch(() => null))).then((parts) => {
      const v = { expected: {}, sources: {} };
      parts.filter(Boolean).forEach((j) => { Object.assign(v.expected, j.expected || {}); Object.assign(v.sources, j.sources || {}); addHealth(key, j.health); });
      entry.value = Object.keys(v.expected).length ? v : null;
      entry.done = true;
      scheduleRender();
    });
  }
  return latencyLoads[key].value;
}
const latencyLoading = (key) => key !== "s1" && !(latencyLoads[key] && latencyLoads[key].done);
function sourceLabel(key, mission = "s1") {
  const l = missionLatency(mission);
  return (l && l.sources[key] && l.sources[key].label) || key;
}
// The sources that deliver a pass. NISAR: Bhoonidhi (S-band) only for a pass
// with S-band, ASF (L-band) unless the pass is S-band only.
function sourcesFor(mission, pol, p = null) {
  const keys = MISSION_SOURCES[mission];
  // a night pass: no snow products; Sentinel-3 without OLCI over the AOI: SLSTR's only
  if ((mission === "modis" || mission === "viirs") && p && p.light === "night") return keys.filter((k) => !DAY_ONLY.has(k));
  if (mission === "s3" && p && p.instruments && !p.instruments.includes("OLCI")) return keys.filter((k) => !OLCI_SOURCES.has(k));
  if (mission === "s3" && p && !p.instruments && p.light === "night") return keys.filter((k) => !OLCI_SOURCES.has(k));
  if (mission !== "nisar") return keys;
  const bands = new Set(polEntries(pol).map((e) => e.band));
  return keys.filter((k) => (S_BAND_SOURCES.has(k) ? bands.has("S") : bands.has("L") || !bands.size));
}
function expectations(p) {
  if (p.mission === "s1" && p.mode && p.mode !== "IW") return { note: `Latency is measured for IW only (this pass is ${p.mode}).`, list: [] };
  const lat = missionLatency(p.mission);
  if (!lat) return { note: latencyLoading(p.mission) ? "Loading measured latency…" : "Latency data unavailable.", list: [] };
  const list = sourcesFor(p.mission, p.pol, p).filter((k) => lat.expected[k]).map((key) => ({
    key, label: sourceLabel(key, p.mission), exp: expectedAvailability(p.when, p.platform, lat.expected[key]), failing: failingFor(p.mission, key),
  }));
  return { note: null, list };
}

// Poller health per mission and source (health.json for Sentinel-1, each
// mission's latency file for the others). A health entry can cover several
// sources: Sentinel-1's "cdse" lists CDSE's GRD and SLC, "cdse_s2" its L1C and
// L2A, "asf_nisar" ASF's NISAR products.
function addHealth(mission, h) {
  Object.entries((h && h.sources) || {}).forEach(([k, e]) => { (state.health[mission] ||= {})[k] = e; });
}
function failingFor(mission, key) {
  const hit = Object.entries(state.health[mission] || {}).find(([k, e]) => e.failing_since_utc && (key === k || key.startsWith(`${k}_`)));
  return hit ? { since: new Date(hit[1].failing_since_utc), error: hit[1].last_error || "" } : null;
}
const failingNote = (f) => `this site's poller has been failing on this source since ${fmtWhen(f.since)} UTC, so the source may be down or slow; the estimate is from earlier measurements`;
const failingMark = (f) => (f ? ` <span class="warn" title="${esc(`${failingNote(f)}. ${f.error}`)}">⚠</span>` : "");
let renderPending = false;
function scheduleRender() {
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; renderList(); });
}
function earliest(p) {
  const e = expectations(p);
  const measured = e ? e.list.filter((x) => x.exp) : [];
  return measured.length ? measured.reduce((a, b) => (b.exp.median_at < a.exp.median_at ? b : a)) : null;
}
const fw = (d, p, ref) => fmtBoth(d, p.ll, ref);
// Optical passes: is the AOI sunlit? Landsat's ascending passes are at night.
const OPTICAL = new Set(["s2", "landsat", "modis", "viirs", "s3"]);
function sunAt(p) {
  if (!OPTICAL.has(p.mission) || !p.ll) return null;
  return sunElevation(p.when, p.ll[0], p.ll[1]);
}
function sunText(p) {
  const e = sunAt(p);
  if (e == null) return "";
  return e > 0 ? `sun ${Math.round(e)}°` : "night";
}
const sunNote = (p) => { const e = sunAt(p); return e == null ? "" : e > 0 ? ` Sun ${Math.round(e)}° above the horizon at the AOI.` : ` Night at the AOI (sun ${Math.round(-e)}° below the horizon): thermal data only are useful.`; };
// Instrument or mode, as listed: ESA's "NOBS" (nominal observation) is the MSI imaging normally.
function modeText(p) {
  if (p.mission === "nisar") return nisarModeText(p.mode, p.pol) || '<span class="nm">not in the plan</span>';
  if (isWide(p.mission)) {
    const sun = sunText(p), inst = p.mission === "s3" ? (p.instruments || ["SLSTR"]).join(" + ") : WIDE_MODE[p.mission];
    const sub = [p.vza != null ? `<span title="view zenith angle at the AOI: pixels grow towards the swath edges">VZA ${Math.round(p.vza)}°</span>` : null, sun].filter(Boolean).join(" · ");
    return `${inst}${sub ? `<div class="nm${sun === "night" ? " warn-ink" : ""}">${sub}</div>` : ""}`;
  }
  if (p.mission === "s1") return [p.pol ? polText(p.pol) : null, p.mode !== "IW" ? p.mode : null].filter(Boolean).join(" ") || p.mode || "";
  const inst = p.mission === "s2" ? (p.mode === "NOBS" ? "MSI" : p.mode || "MSI") : p.mode || "OLI/TIRS";
  const sun = sunText(p);
  return `${inst}${sun ? `<div class="nm${sun === "night" ? " warn-ink" : ""}">${sun}</div>` : ""}`;
}
const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// One description of a pass or an acquisition, used everywhere (cards, map
// label, timeline, calendar): "Sentinel-2C · descending · relative orbit 56"
// (with its footprints: "· 10TET"); short: "S2C desc · relative orbit 56".
function passLabel(p, { short = false, footprints = !short } = {}) {
  const m = MISSION[p.mission];
  const plat = short ? p.platform || m.label : PLATFORM_NAMES[p.platform] || p.platform || m.label;
  const direction = p.direction || (p.mission === "s2" ? "descending" : null); // Sentinel-2: descending only
  const dir = direction ? (short ? (direction === "ascending" ? "asc" : "desc") : dirText(p.mission, direction)) : null;
  const track = p.track != null && p.track !== "" ? `${m.trackName} ${p.track}` : null;
  return [plat, dir, track, footprints ? p.footprints : null].filter(Boolean).join(" · ");
}
// A pass's details, in the card under its row: what, how it was timed,
// and when each source is likely to have it.
// "3:10 AM PDT" (with its date when not the UTC date), local at [lon, lat]
function localText(d, ll) {
  const l = localTime(d, ll), utcDate = utcText(d).split(" ").slice(0, 3).join(" ");
  return `${l.date === utcDate ? "" : `${l.date} `}${l.time} ${l.tz}`;
}
function detailCard(p) {
  const card = document.createElement("div");
  card.className = "detail-card compact";
  const mode = plain(modeText(p));
  // what it is, in a line or two; when each source likely has it, in a small
  // table (UTC, local under the median; n and the stratum in the row's tooltip);
  // how it was timed and the rest, folded
  card.innerHTML = `<div class="headline">${passLabel(p)}</div>${mode ? `<div class="dc-mode">${esc(mode)}</div>` : ""}`;
  const e = expectations(p);
  if (e && e.note) card.insertAdjacentHTML("beforeend", `<div class="nm">${e.note}</div>`);
  if (e && e.list.length) {
    const short = (d) => utcText(roundUp10(d), p.when);
    const rows = e.list.map((x) => (x.exp
      ? `<tr title="${esc(`measured over ${x.exp.n_events} acquisitions, ${x.exp.stratum === "all" ? "all platforms" : x.exp.stratum}${x.failing ? `; ${failingNote(x.failing)}` : ""}`)}"><td>${x.label}${failingMark(x.failing)}</td><td class="t"><b>~${short(x.exp.median_at)}</b><div class="sub">${localText(roundUp10(x.exp.median_at), p.ll)}</div></td><td class="t nm">${x.exp.p90_at ? short(x.exp.p90_at) : "not yet known"}</td></tr>`
      : `<tr><td>${x.label}</td><td class="nm" colspan="2">not measured</td></tr>`)).join("");
    card.insertAdjacentHTML("beforeend", `<table class="dl"><thead><tr><th>Likely downloadable at</th><th>median</th><th>90 %</th></tr></thead><tbody>${rows}</tbody></table>`);
  }
  const more = [passNote(p), sunNote(p).trim()].filter(Boolean).join(" ");
  const bursts = p.mission === "s1" && p.cells.length && p.cells.every((c) => c.burst)
    ? `<div><b>${p.cells.length} burst IDs:</b> <code>${[...new Set(p.cells.map((c) => c.title))].sort().join(" ")}</code></div>` : "";
  if (more || bursts) card.insertAdjacentHTML("beforeend", `<details class="dc-more"><summary>More about this pass</summary><div class="nm">${more}</div>${bursts}</details>`);
  return card;
}

// ---------------------------------------------------------------- recent passes over the AOI
// What the catalogues hold over the AOI for the last RECENT_DAYS (recent.js),
// grouped into acquisitions, plus the planned passes of the last day or two
// that no catalogue has yet. For each, every source of its mission: the
// product if it is up, else when it is expected from the measured latency.
const PLAN_MATCH_MS = 20 * 60e3;
// Sources that do not process every acquisition (see README: how latency is defined)
const PARTIAL_SOURCES = new Set(["pc_grd", "pc_rtc", "opera_rtc", "opera_cslc", "lance_modis_snow", "nsidc_modis_snow", "lance_viirs_snow", "nsidc_viirs_snow", "cdse_s3_lfr", "pc_s3_lfr", "cdse_s3_lst", "pc_s3_lst"]);
function updateRecent() {
  const missions = MISSIONS.filter((m) => state.missions.has(m.key)).map((m) => m.key);
  const sig = `${state.aoiVersion}|${missions.join(",")}`;
  if (!state.recent || state.recent.sig !== sig) {
    const rec = { sig, results: {}, acqs: [] };
    state.recent = rec;
    missions.forEach((m) => {
      missionLatency(m);
      MISSION_SOURCES[m].forEach((k) => { rec.results[k] = { mission: m, status: SEARCHABLE[k] ? "loading" : "unsearchable" }; });
    });
    const found = searchSources(state.aoiVersion, state.aoi, Object.keys(rec.results));
    Object.entries(found).forEach(([k, p]) => p.then(
      (items) => { rec.results[k] = { ...rec.results[k], status: "ok", items }; },
      (err) => { rec.results[k] = { ...rec.results[k], status: err.unsearchable ? "unsearchable" : "error", error: err.message }; },
    ).then(() => { if (state.recent === rec) { rebuildRecent(); scheduleRender(); } }));
  }
  rebuildRecent();
}

function rebuildRecent() {
  const rec = state.recent, f = filters(), now = Date.now();
  const acqs = [];
  MISSIONS.filter((m) => state.missions.has(m.key)).forEach(({ key: m }) => {
    const products = MISSION_SOURCES[m].flatMap((k) => {
      const r = rec.results[k];
      return r && r.status === "ok" ? r.items.map((x) => ({ ...x, source: k })) : [];
    });
    const groups = groupAcquisitions(products).map((g) => ({ ...g, mission: m }));
    // the plan's passes of the last day or two: matched to what the catalogues have, or still pending
    (state.recentPlanned || []).filter((p) => p.mission === m && p.when.getTime() < now).forEach((p) => {
      const g = groups.find((x) => (!x.platform || x.platform === p.platform) && Math.abs(x.when - p.when) <= PLAN_MATCH_MS);
      if (g) g.plan = p;
      else groups.push({ mission: m, when: p.when, platform: p.platform, direction: p.direction, track: p.track, products: [], plan: p });
    });
    groups.forEach((g) => {
      const p = g.plan;
      g.platform ||= p && p.platform;
      g.direction ||= p && p.direction;
      if (g.track == null && p) g.track = p.track;
      if (m === "s1") g.mode = (p && p.mode) || g.mode || null;
      g.pol = p ? p.pol : g.products.find((x) => x.pol)?.pol || null;
      // shown time: over the AOI (the plan's, or a tile/scene product's), not a datatake start
      const local = g.products.filter((x) => !/^(cdse_s2|pc_s2|asf_nisar_l0b)/.test(x.source)); // those carry the datatake start
      g.shown = p ? p.when : local.length ? local[0].when : g.when;
      if (m === "landsat" || isWide(m)) { const [lon, lat] = aoiCenter(); g.light = p && p.light ? p.light : lightAt(g.shown, lon, lat); }
      if (m === "s3") g.instruments = p ? p.instruments : null;
      g.key = `r|${m}|${g.platform || ""}|${Math.round(g.shown / 6e4)}`;
      if (g.platform && !filterOK({ platform: g.platform, direction: g.direction, track: g.track, pol: null, light: g.light }, f)) return;
      acqs.push(g);
    });
  });
  rec.acqs = acqs.sort((a, b) => b.shown - a.shown);
}

// Each source of a recent acquisition: up (its products), searching, failed,
// expected (median and 90% times), overdue, or not searchable from here.
const IW_ONLY = new Set(["pc_rtc", "opera_rtc", "opera_cslc"]);
function availability(a) {
  const lat = missionLatency(a.mission), rec = state.recent, now = Date.now();
  return sourcesFor(a.mission, a.pol, a).map((k) => {
    const r = rec.results[k] || { status: "unsearchable" };
    let exp = lat && lat.expected[k] ? expectedAvailability(a.when, a.platform || "all", lat.expected[k]) : null;
    const products = a.products.filter((x) => x.source === k);
    const label = sourceLabel(k, a.mission);
    if (products.length) return { key: k, label, status: "up", products };
    // a night scene: USGS makes Level-1 only (no Level-2, so no copies, no HLS)
    if (a.mission === "landsat" && a.light === "night" && k !== "usgs_landsat_l1") return { key: k, label, status: "na" };
    // Sentinel-1 EW and stripmap: no RTC (Planetary Computer, OPERA) or CSLC,
    // and delivery measured for IW only, so no expected time
    const notIW = a.mission === "s1" && a.mode && a.mode !== "IW";
    if (notIW && IW_ONLY.has(k)) return { key: k, label, status: "na" };
    if (notIW) exp = null;
    if (r.status === "loading") return { key: k, label, status: "searching", exp };
    if (r.status === "error") return { key: k, label, status: "error", error: r.error, exp };
    // USGS makes the Level-2 that Earth Search and Planetary Computer copy: a copy up means USGS has it
    if (r.status === "unsearchable" && k.startsWith("usgs_landsat") && a.products.some((x) => /landsat_l2$/.test(x.source))) return { key: k, label, status: "unsearchable", exp, impliedUp: true };
    if (r.status === "unsearchable") return { key: k, label, status: "unsearchable", exp };
    if (exp && exp.p90_at && now < exp.p90_at.getTime()) return { key: k, label, status: "expected", exp };
    // not found, and no measured latency to say it is late: not known yet
    if (!exp || !exp.p90_at) return { key: k, label, status: "unknown", exp };
    return { key: k, label, status: "missing", exp };
  });
}

function recentSummary(a) {
  const av = availability(a);
  const up = av.filter((x) => x.status === "up").length, searchable = av.filter((x) => !["unsearchable", "na"].includes(x.status)).length;
  const waiting = av.filter((x) => x.status === "expected").sort((x, y) => x.exp.median_at - y.exp.median_at)[0];
  const head = av.some((x) => x.status === "searching") && !up ? '<span class="nm">searching…</span>'
    : up ? `<b>${up}</b> of ${searchable} up` : "not up yet";
  const next = waiting ? `<div class="nm">next: ${waiting.label} ~${utcText(roundUp10(waiting.exp.median_at), new Date())}</div>` : "";
  return head + next;
}

function recentCard(a) {
  const card = document.createElement("div");
  card.className = "detail-card";
  const now = Date.now();
  const pol = a.mission === "nisar" ? nisarModeText(a.plan && a.plan.mode, a.pol).replace(/<[^>]+>/g, " ") : a.pol ? polText(a.pol) : "";
  const night = a.light === "night" ? " Night at the AOI: thermal data only." : "";
  card.innerHTML = `<div class="headline">${passLabel(a)}</div>`
    + `<div class="nm">${pol ? `${pol}. ` : ""}${a.products.length ? `Found in ${new Set(a.products.map((x) => x.source)).size} catalogue(s).` : isWide(a.mission) ? "Not in any catalogue yet: predicted from the orbit." : "Not in any catalogue yet: from the acquisition plan."}${night}</div>`;
  const ul = document.createElement("ul");
  ul.className = "lat-list avail";
  const today = new Date();
  const when = (d) => `${fmtWhen(roundUp10(d), today)} UTC`;
  const until = (d) => (d > now ? `, in ${fmtHours((d - now) / 3.6e6)}` : "");
  availability(a).forEach((x) => {
    let body;
    if (x.status === "up") {
      const p = x.products[0];
      body = `<span class="ok">✓ up</span> · <a href="${p.href}" target="_blank" rel="noopener" title="${p.id}">${p.linkText || "open"}</a>`
        + `${p.download ? ` · <a href="${p.download}" target="_blank" rel="noopener" title="needs an Earthdata login">download</a>` : ""}`
        + `${x.products.length > 1 ? ` <span class="nm" title="${x.products.map((y) => y.id).join("\n")}">+${x.products.length - 1} more products</span>` : ""}`;
    } else if (x.status === "expected") {
      body = `<span class="wait">◷</span> expected ~${when(x.exp.median_at)}${until(x.exp.median_at)} <span class="nm">(90% by ~${when(x.exp.p90_at)}${PARTIAL_SOURCES.has(x.key) ? "; if processed here" : ""})</span>`;
    } else if (x.status === "na") {
      body = a.mission === "s1"
        ? `<span class="nm">– none for ${a.mode === "SM" ? "stripmap" : a.mode} (made from IW only)</span>`
        : '<span class="nm">– none for night scenes (USGS makes Level-1 only)</span>';
    } else if (x.status === "unknown") {
      body = `<span class="nm">?</span> not found yet <span class="nm">(its delivery time is not measured${PARTIAL_SOURCES.has(x.key) ? "; this source does not process every acquisition" : ""})</span>`;
    } else if (x.status === "missing") {
      const within = x.exp && x.exp.p90_at ? `90% are up within ${fmtHours((x.exp.p90_at - a.when) / 3.6e6)}` : "latency not measured";
      body = `<span class="miss">–</span> not found <span class="nm">(${within}${PARTIAL_SOURCES.has(x.key) ? "; this source does not process every acquisition" : ""})</span>`;
    } else if (x.status === "unsearchable") {
      const portal = `<a href="${PORTAL[x.key]}" target="_blank" rel="noopener">search there</a>`;
      if (x.impliedUp) body = `<span class="ok">✓ up</span> <span class="nm">(its Level-2 copies are up)</span> · ${portal}`;
      else {
        const due = x.exp ? (now < x.exp.median_at ? `expected ~${when(x.exp.median_at)}${until(x.exp.median_at)}` : "probably up by now") : "latency not measured";
        body = `<span class="nm">?</span> ${due} <span class="nm">(${NOT_SEARCHABLE[x.key] || "not searchable from here"})</span> · ${portal}`;
      }
    } else if (x.status === "error") {
      body = `<span class="miss">!</span> search failed <span class="nm">(${x.error})</span>`;
    } else {
      body = '<span class="nm">searching…</span>';
    }
    ul.insertAdjacentHTML("beforeend", `<li><b>${x.label}</b>: ${body}</li>`);
  });
  card.append(ul);
  return card;
}

// ---------------------------------------------------------------- drawing
const colorFor = (hours) => (hours == null ? null : colorForDays(state.palette, hours / 24));
function cellStyle(c) {
  const fill = colorFor(c.visible.length ? (c.visible[0].when - Date.now()) / 3.6e6 : null);
  // Near-opaque fills drawn soonest-on-top: each spot shows its soonest next
  // acquisition; thin surface-coloured edges keep the footprints visible.
  // Planned swaths overlap heavily (near the poles 20+ in 12 days): no
  // outline, or the edges of swaths hidden under sooner ones would be drawn
  // over them; their soonest-on-top fills show the edges.
  // Other footprints converge towards the poles too (22 NISAR frames over a
  // point at 75 deg): their edges fade from 60 deg to a trace by 75 deg.
  const fade = Math.min(1, Math.max(0.15, (75 - Math.abs(c.center[1])) / 15));
  const edge = c.datatake ? { weight: 0, opacity: 0 } : { weight: 0.5, opacity: 0.75 * fade };
  const k = queue.some((q) => q.status === "ok") ? 0.25 : 1;
  return { color: css("--surface"), ...edge, fillColor: fill || "transparent", fillOpacity: fill ? state.opacity * k : 0 };
}
const nextOf = (c) => (c.visible.length ? c.visible[0].when.getTime() : Infinity);
// Footprint popup: who images it next, how often, and when it was last
// imaged; a timeline of its recent acquisitions (real products, read live
// from the catalogues; see history.js) and the planned ones; then one table
// of both: upcoming first (soonest first, as filtered), then recent (newest
// first, all of them). Same columns for both.
// Popup table columns: when (UTC, local, time until/since), platform,
// direction, track and frame (or their equivalent), polarization (SAR) or
// cloud cover (optical).
const DETAIL_HEAD = { s1: "Polarization", nisar: "Polarization", s2: "Cloud · coverage", landsat: "Cloud", modis: "View · light", viirs: "View · light", s3: "Instruments · view · light" };
const TIMING_NOTE = {
  s1: "times from the plan and the orbit timing, to about a second",
  s2: "tile times from the orbit (SGP4), to within ~10 s",
  landsat: "scene times from the orbit (SGP4), to within a few seconds",
  nisar: "times from the orbit (SGP4), to within ~15 s",
  modis: "when the ground track passes closest to the tile centre (SGP4); view zenith at the centre",
  viirs: "when the ground track passes closest to the tile centre (SGP4); view zenith at the centre",
  s3: "when the ground track passes closest to the tile centre (SGP4); frames from the orbit, within ~0.1 s of CDSE's products",
};
const PLAN_OF = {
  s1: "ESA's Sentinel-1 plan", s2: "ESA's Sentinel-2 plan", landsat: "USGS's acquisition calendar", nisar: "NASA's NISAR plan",
  modis: "predicted from the orbit (MODIS has no plan: it images on every orbit)", viirs: "predicted from the orbit (VIIRS has no plan: it images on every orbit)",
  s3: "predicted from the orbit (Sentinel-3 has no plan: SLSTR images on every orbit, OLCI in daylight)",
};
// The footprint popup's head: mission, the footprint's ID (one form), and
// what is the same for every row (its track and direction; for a tile,
// which crosses several relative orbits, only the direction).
function cellHead(c) {
  const m = MISSION[c.mission];
  const what = c.mission === "s1" && c.burst ? `IW burst ${c.title}` : c.mission === "s2" ? `MGRS tile ${c.tile}` : isWide(c.mission) && c.tile ? `sinusoidal tile ${c.tile}` : c.title;
  const fixed = [
    c.mission === "s1" && c.track != null ? `${m.trackName} ${c.track}` : null, // a burst ID or a swath does not say it
    dirText(c.mission, c.direction || (c.mission === "s2" ? "descending" : null)),
  ].filter(Boolean).join(" · ");
  return `<div class="headline"><span class="chip" style="background:${missionColor(c.mission)}"></span> ${m.label} · ${what}</div>${fixed ? `<div class="nm">${fixed}</div>` : ""}`;
}
// Only a tile's rows carry their relative orbit (a tile crosses several).
const rowTrack = (c) => c.mission === "s2" || c.mission === "s3";

// The frame-like part of a footprint: burst, tile, row or frame (one per popup).
function frameOf(c) {
  if (c.mission === "s1") return c.burst ? `${String(c.burst.bid).padStart(6, "0")} ${c.burst.swath}` : "";
  if (c.mission === "s2" || (isWide(c.mission) && c.tile)) return c.tile || "";
  return c.frame == null ? "" : String(c.frame);
}

function acqRow(c, x, planned, now) {
  const [utc, local] = fmtBoth(x.when, c.center).split(" · ");
  const when = rel(x.when, now);
  const track = x.track ?? c.track;
  const frame = frameOf(c);
  let detail = "";
  if (c.mission === "nisar") {
    detail = planned ? nisarModeText(x.radarMode, x.pol) : nisarModeText(null, x.pol, x.bandwidth);
    if (planned && x.modeSource === "earlier cycle") detail += '<div class="sub" title="The plan leaves this date\'s mode blank: this is the frame\'s last listed mode, which matched ASF\'s products for 97% of L-band frames.">mode assumed from an earlier cycle</div>';
    if (!planned && x.partial) detail += '<div class="sub">partial frame</div>';
  } else if (isWide(c.mission)) {
    const light = x.light === "night" ? '<span class="warn-ink">night</span>' : x.light === "day" ? "day" : "";
    const inst = c.mission === "s3" ? (x.instruments ? x.instruments.join(" + ") : planned ? (x.light === "day" ? "OLCI + SLSTR" : "SLSTR") : "") : "";
    const frame = c.mission === "s3" && x.frame != null ? `<span class="sub" title="the products' frame: seconds after the orbit's ascending node">frame ${x.frame}</span>` : "";
    detail = [inst, x.vza != null ? `VZA ${Math.round(x.vza)}°` : "", light, frame, x.level ? `<span class="sub">${esc(x.level)}</span>` : ""].filter(Boolean).join(" · ");
  } else if (c.mission === "s1") {
    detail = x.pol ? polText(x.pol) : "";
    if (x.mode && x.mode !== "IW") detail = `${x.mode}${detail ? ` ${detail}` : ""}`;
  } else if (planned) {
    detail = c.mission === "s2" ? (x.coverage == null ? "" : x.coverage < 0.95 ? `covers ~${Math.max(1, Math.round(x.coverage * 100))}% of the tile` : '<span class="sub">full tile</span>')
      : x.light === "night" ? '<span class="warn-ink">night</span> <span class="sub">thermal only</span>' : "";
  } else {
    detail = [x.cloud != null ? `cloud ${Math.round(x.cloud)}%` : "", x.night ? '<span class="warn-ink">night</span>' : "", x.level ? `<span class="sub">${esc(x.level)}</span>` : ""].filter(Boolean).join(" ");
  }
  const dash = '<span class="sub">—</span>';
  // past acquisitions: a browse image for the map's preview queue, and the product in a viewer
  let preview = dash, product = planned ? '<span class="sub">planned</span>' : dash;
  if (!planned) {
    product = (x.links || []).map((l) => `<a href="${esc(l.href)}" target="_blank" rel="noopener" title="${esc(l.title || "")}">${esc(l.text)} ↗</a>`).join("<br>") || dash;
    if (x.browse) {
      const k = browseItem(x.browse, { mission: c.mission, when: x.when, platform: x.platform, direction: x.direction || c.direction || "", track: track ?? "", frame, center: c.center });
      preview = `<button type="button" class="link-btn" data-browse="${esc(k)}">${queued(k) ? "Remove from map" : "Preview on map"}</button>`;
    }
  }
  return `<tr${x.id ? ` title="${esc(x.id)}"` : ""}><td class="when">${utc}<div class="sub">${local ? `${local} · ` : ""}<span class="rel">${when}</span></div></td>`
    + `<td>${x.platform}</td>${rowTrack(c) ? `<td>${track != null ? track : dash}</td>` : ""}`
    + `<td>${detail || dash}</td><td class="prod">${preview}</td><td class="prod">${product}</td></tr>`;
}
// The browse requests of the popup's rows, by index (data-browse).
// keyed by the request itself, so a button stays right whatever else is shown
const browseItems = new Map();
function browseItem(req, meta) {
  const key = JSON.stringify(req);
  if (!browseItems.has(key)) browseItems.set(key, { req, key, meta });
  return key;
}

// ---------------------------------------------------------------- preview queue
// Browse images picked with "Preview on map", listed in a box at the bottom
// right and draped on the map (MapLibre image sources) over their footprints:
// above the footprints, under the highlight and the AOI. The list is the
// drawing order, top drawn on top; drag a row's handle to reorder. Each row
// has its own opacity and a remove button. An image's background (black or
// white) is made transparent first (drapedImage).
const queue = []; // {id, key, meta, status: "loading"|"ok"|"error"|"unplaced", b, opacity, note}
let queueSeq = 0;
const queued = (key) => queue.some((q) => q.key === key);
const QUEUE_OPACITY = 0.95;
// A browse image made ready for the map: drawn on a canvas (at most 2048
// px), its background (black or white) made transparent, its data's extent
// found, then resampled onto a grid regular in Web Mercator (drape.js), so
// MapLibre's four corners place it exactly. Returns {url, corners}.
async function drapedImage(g) {
  const r = await fetch(g.url, { mode: "cors" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const bmp = await createImageBitmap(await r.blob());
  const scale = Math.min(1, 2048 / Math.max(bmp.width, bmp.height));
  const cv = Object.assign(document.createElement("canvas"), { width: Math.round(bmp.width * scale), height: Math.round(bmp.height * scale) });
  const ctx = cv.getContext("2d");
  ctx.drawImage(bmp, 0, 0, cv.width, cv.height);
  const d = ctx.getImageData(0, 0, cv.width, cv.height), px = d.data, key = g.key;
  // the pixels left opaque: the image's data, and its extent in pixels
  let x0 = cv.width, y0 = cv.height, x1 = -1, y1 = -1;
  for (let i = 0; i < px.length; i += 4) {
    const sum = px[i] + px[i + 1] + px[i + 2];
    if (px[i + 3] === 0 || (key && (key === "black" ? sum < 9 : key === "white" ? sum > 759 : sum < 9 || sum > 759))) { px[i + 3] = 0; continue; }
    const p = i / 4, x = p % cv.width, y = (p - x) / cv.width;
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  ctx.putImageData(d, 0, 0);
  let where = g.where;
  // no published grid (OPERA's burst browse): fitted to the footprint
  if (!where && g.fit && x1 >= 0) {
    const ring = g.fit.ring, lon = ring.reduce((t, p) => t + p[0], 0) / ring.length, lat = ring.reduce((t, p) => t + p[1], 0) / ring.length;
    const epsg = utmEpsg(lon, lat);
    where = fitUtmToData(epsg, utmBox(epsg, ring), [x0, y0, x1, y1], cv.width, cv.height);
  }
  // an image whose box is its data's extent, not its own (NISAR's browse has
  // an empty margin): the box widened to the whole image
  if (where && where.kind === "latlon" && g.dataExtent && x1 >= 0) {
    const c = widenToImage(bboxCorners(where.bbox), [x0, y0, x1, y1], cv.width, cv.height);
    where = { kind: "latlon", bbox: [c[0][0], c[2][1], c[1][0], c[0][1]] };
  }
  if (!where) return { url: cv.toDataURL("image/png"), corners: g.corners, canvas: null };
  const out = resample(cv, where);
  return { url: out.canvas.toDataURL("image/png"), corners: out.corners, canvas: out.canvas };
}
const bboxCorners = ([w, s, e, n]) => [[w, n], [e, n], [e, s], [w, s]];

function syncPreviewButtons() {
  document.querySelectorAll("button[data-browse]").forEach((btn) => {
    const it = browseItems.get(btn.dataset.browse);
    if (it) btn.textContent = btn.classList.contains("row-prev") ? (queued(it.key) ? "Remove" : "Preview") : queued(it.key) ? "Remove from map" : "Preview on map";
  });
}

async function togglePreview(btn) {
  const it = browseItems.get(btn.dataset.browse);
  if (!it) return;
  const have = queue.find((q) => q.key === it.key);
  if (have) { removePreview(have); return; }
  const q = { id: `browse-${++queueSeq}`, key: it.key, meta: it.meta, status: "loading", b: null, opacity: QUEUE_OPACITY, note: "" };
  queue.unshift(q); // newest on top
  renderQueue();
  try {
    const b = await fetchBrowse(it.req);
    if (!queue.includes(q)) return;
    q.b = b;
    if (!b || !b.thumb) { q.status = "none"; q.note = (b && b.note) || "No browse image found."; renderQueue(); return; }
    if (!b.georef) { q.status = "unplaced"; q.note = b.note || "Not georeferenced: not placed on the map."; renderQueue(); return; }
    const img = await drapedImage(b.georef);
    if (!queue.includes(q)) return;
    q.corners = img.corners;
    q.canvas = img.canvas; // Web Mercator, north up (previewCovers)
    map.addSource(q.id, { type: "image", url: img.url, coordinates: img.corners });
    map.addLayer({ id: q.id, type: "raster", source: q.id, paint: { "raster-opacity": q.opacity, "raster-fade-duration": 0 } }, map.getLayer("highlight-fill") ? "highlight-fill" : undefined);
    q.status = "ok";
    orderPreviewLayers();
  } catch (err) {
    if (!queue.includes(q)) return;
    q.status = "error";
    q.note = `Could not load it: ${err.message}`;
  }
  renderQueue();
}

// Another channel of a Sentinel-1 preview: the image swapped in place.
async function switchBand(q, i) {
  const band = q.b.bands[i];
  q.band = i;
  renderQueue();
  try {
    const img = await drapedImage({ ...q.b.georef, url: band.url });
    if (!queue.includes(q) || q.band !== i) return;
    const src = map.getSource(q.id);
    if (src) src.updateImage({ url: img.url, coordinates: img.corners });
  } catch (err) {
    q.note = `Could not load the ${band.label} image: ${err.message}`;
    renderQueue();
  }
}

// Whether a draped preview has data (opaque pixels) at every point [lon, lat].
const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
function previewCovers(q, points) {
  if (q.status !== "ok" || !q.corners) return false;
  if (!q.canvas) return points.every((p) => turf.booleanPointInPolygon(p, turf.polygon([[...q.corners, q.corners[0]]])));
  const [[w, n], [e, s]] = [q.corners[0], q.corners[2]], cv = q.canvas, ctx = cv.getContext("2d");
  return points.every(([lon, lat]) => {
    const x = Math.floor(((lon - w) / (e - w)) * cv.width), y = Math.floor(((mercY(n) - mercY(lat)) / (mercY(n) - mercY(s))) * cv.height);
    if (x < 0 || y < 0 || x >= cv.width || y >= cv.height) return false;
    return ctx.getImageData(x, y, 1, 1).data[3] > 0;
  });
}
const clearPreviews = () => [...queue].forEach(removePreview);

function removePreview(q) {
  const i = queue.indexOf(q);
  if (i >= 0) queue.splice(i, 1);
  if (map.getLayer(q.id)) map.removeLayer(q.id);
  if (map.getSource(q.id)) map.removeSource(q.id);
  renderQueue();
}

// Bottom of the list first, each moved to just under the highlight: the top row ends up on top.
function orderPreviewLayers() {
  const before = map.getLayer("highlight-fill") ? "highlight-fill" : undefined;
  [...queue].reverse().forEach((q) => { if (map.getLayer(q.id)) map.moveLayer(q.id, before); });
}

function fitPreview(q) {
  const cs = q.corners || (q.b && q.b.georef ? q.b.georef.corners : null);
  if (cs) view.fitBBox(turf.bbox({ type: "MultiPoint", coordinates: cs }), { pad: 0.15, maxZoom: 10 });
  else if (q.meta.center) map.easeTo({ center: q.meta.center });
}

const queueBox = document.getElementById("browse-queue");
let queueShowing = false;
function renderQueue() {
  syncPreviewButtons();
  const showing = queue.some((q) => q.status === "ok");
  if (showing !== queueShowing) { queueShowing = showing; drawCells(); } // footprints fade under previews
  queueBox.hidden = !queue.length;
  document.body.classList.toggle("has-queue", queue.length > 0);
  if (!queue.length) { queueBox.querySelector("tbody").replaceChildren(); return; }
  queueBox.querySelector(".bq-count").textContent = `(${queue.length})`;
  const rows = queue.map((q) => {
    const m = q.meta, tr = document.createElement("tr");
    tr.dataset.id = q.id;
    const [utc] = fmtBoth(m.when, m.center).split(" · ");
    const band = q.b && q.b.bands ? q.b.bands[q.band || 0] : null;
    const thumb = q.b && q.b.thumb
      ? `<img src="${esc(band ? band.thumb : q.b.thumb)}" alt=""${q.b.georef ? ' crossorigin="anonymous"' : ""}>`
      : `<span class="bq-ph">${q.status === "loading" ? "…" : "–"}</span>`;
    // Sentinel-1 from Planetary Computer: colour, or one channel
    const bands = q.status === "ok" && q.b.bands
      ? `<select class="bq-band" aria-label="Channel" title="Colour composite or one channel">${q.b.bands.map((x, i) => `<option value="${i}"${i === (q.band || 0) ? " selected" : ""}>${esc(x.label)}</option>`).join("")}</select>`
      : "";
    const op = q.status === "ok"
      ? `${bands}<input type="range" min="0" max="100" step="5" value="${Math.round(q.opacity * 100)}" aria-label="Opacity of this preview" title="Opacity">`
      : `<span class="nm" title="${esc(q.note)}">${q.status === "loading" ? "loading…" : q.status === "unplaced" ? "not on map" : q.status === "none" ? "none yet" : "failed"}</span>`;
    const src = q.b ? [q.b.source, q.note].filter(Boolean).join(". ") : "";
    tr.innerHTML = `<td class="bq-handle" tabindex="0" role="button" title="Drag (or arrow up/down) to reorder: top is drawn on top" aria-label="Reorder: arrow up or down">⠿</td>`
      + `<td class="bq-thumb">${q.b && q.b.full ? `<a href="${esc(q.b.full)}" target="_blank" rel="noopener" title="${esc(src || "full size")}">${thumb}</a>` : thumb}</td>`
      + `<td class="bq-when"><button type="button" class="link-btn bq-fit" title="Go to it">${utc}</button><div class="sub"><span class="chip" style="background:${missionColor(m.mission)}"></span>${MISSION[m.mission].label}${q.status === "ok" && q.b.georef && q.b.georef.fit ? ` · <span class="warn-ink" title="${esc(q.b.note || "")}">approx. place</span>` : ""}</div></td>`
      + `<td>${esc(m.platform)}</td><td>${esc(m.direction)}</td><td>${esc(m.track)}${m.frame ? ` / ${esc(m.frame)}` : ""}</td>`
      + `<td class="bq-op">${op}</td>`
      + '<td><button type="button" class="bq-x" title="Remove from the map" aria-label="Remove from the map">×</button></td>';
    tr.querySelector(".bq-x").addEventListener("click", () => removePreview(q));
    tr.querySelector(".bq-fit").addEventListener("click", () => fitPreview(q));
    const sel = tr.querySelector(".bq-band");
    if (sel) sel.addEventListener("change", () => switchBand(q, Number(sel.value)));
    const range = tr.querySelector("input[type=range]");
    if (range) range.addEventListener("input", () => { q.opacity = Number(range.value) / 100; if (map.getLayer(q.id)) map.setPaintProperty(q.id, "raster-opacity", q.opacity); });
    const handle = tr.querySelector(".bq-handle");
    handle.addEventListener("pointerdown", (e) => dragRow(e, tr));
    // keyboard: arrow up/down on the handle moves the row (and its image)
    handle.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const i = queue.indexOf(q), j = i + (e.key === "ArrowUp" ? -1 : 1);
      if (j < 0 || j >= queue.length) return;
      [queue[i], queue[j]] = [queue[j], queue[i]];
      orderPreviewLayers();
      renderQueue();
      queueBox.querySelector(`tr[data-id="${q.id}"] .bq-handle`)?.focus();
      document.getElementById("bq-live").textContent = `Moved to position ${j + 1} of ${queue.length}.`;
    });
    return tr;
  });
  queueBox.querySelector("tbody").replaceChildren(...rows);
}

// Drag to reorder (pointer events, so it works with touch): the row follows
// the pointer through the list; on release the queue takes the list's order.
function dragRow(e, tr) {
  e.preventDefault();
  const body = tr.parentElement;
  tr.classList.add("dragging");
  const move = (ev) => {
    const others = [...body.children].filter((r) => r !== tr);
    const after = others.find((r) => { const b = r.getBoundingClientRect(); return ev.clientY < b.top + b.height / 2; });
    if (after) body.insertBefore(tr, after); else body.append(tr);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    tr.classList.remove("dragging");
    const order = [...body.children].map((r) => r.dataset.id);
    queue.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
    orderPreviewLayers();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}
queueBox.querySelector(".bq-clear").addEventListener("click", clearPreviews);


const popupCols = (c) => (rowTrack(c) ? 6 : 5);
function acqTable(c, recentRows) {
  const now = Date.now(), n = popupCols(c);
  const hidden = c.times.length - c.visible.length;
  const hiddenNote = hidden > 0 ? ` <span class="nm">· ${hidden} more hidden by your filters</span>` : "";
  const upcoming = c.visible.length
    ? c.visible.map((t) => acqRow(c, t, true, now)).join("")
    : `<tr><td colspan="${n}" class="nm">Nothing planned${c.times.length ? " that matches your filters" : ""}.</td></tr>`;
  return `<div class="hist-scroll"><table class="data hist-table">
    <thead><tr><th>When <span class="nm">UTC · local</span></th><th>Platform</th>${rowTrack(c) ? "<th>Rel. orbit</th>" : ""}<th>${DETAIL_HEAD[c.mission]}</th><th>Preview</th><th>Product</th></tr></thead>
    <tbody><tr class="sec"><td colspan="${n}">Planned (${c.visible.length})${hiddenNote}</td></tr>${upcoming}</tbody>
    <tbody class="hist-recent">${recentRows}</tbody></table></div>`;
}
const recentSection = (c, count, body) => `<tr class="sec"><td colspan="${popupCols(c)}">Last ${HISTORY_DAYS} days${count == null ? "" : ` (${count})`} <span class="nm">from the catalogue, unfiltered</span></td></tr>${body}`;

// "Next in 2.1 d · typically every 12 d · last imaged 10 d ago" (the next
// pass's date is the table's first row).
function nextLine(c, h) {
  const now = Date.now(), t = c.visible[0];
  const parts = [t ? `Next <b>${rel(t.when, now)}</b>` : `<b>Nothing planned</b>${c.times.length ? " that matches your filters" : ""}`];
  if (h && h.items && h.items.length) {
    const rev = medianRevisit(h.items);
    // (MODIS, VIIRS: passes hours apart)
    if (rev) parts.push(`typically every ${rev < 1 ? `${(rev * 24).toFixed(1)} h` : rev < 1.5 ? `${rev.toFixed(1)} d` : `${Math.round(rev)} d`}`);
    parts.push(`last imaged ${rel(h.items[0].when, now)}`);
  } else if (h && h.items && !h.items.length) {
    parts.push(`not imaged in the last ${HISTORY_DAYS} days`);
  }
  return parts.join(" · ");
}

const TL_WIDTH = 640; // timeline drawn at the popup's width, so its labels stay small
function cellPopup(c) {
  const upcoming = c.visible.map((t) => ({ when: t.when, platform: t.platform, label: `${t.platform || ""} planned ${fmtWhen(t.when)} UTC` }));
  return `${cellHead(c)}<div class="hist" data-cell="1">
    <div class="hist-next">${nextLine(c)}</div>
    <div class="hist-tl-box">${timelineSVG([], upcoming, missionColor(c.mission), { width: TL_WIDTH })}</div>
    ${acqTable(c, recentSection(c, null, `<tr><td colspan="${popupCols(c)}" class="nm">Loading…</td></tr>`))}
    <div class="hist-foot nm"><div>Upcoming: ${PLAN_OF[c.mission]}; ${TIMING_NOTE[c.mission]}. ${planEndNote(c.mission)}.</div><div class="hist-src"></div></div></div>`;
}

// Where each platform's plan ends (ESA's Sentinel-1/2 plans reach ~3 weeks).
function planEndNote(key) {
  const ends = state.planEnds[key] || {};
  const parts = Object.entries(ends).sort().map(([p, d]) => `${p} ${fmtWhen(d).split(" ").slice(0, 3).join(" ")}`);
  if (parts.length && isWide(key)) return `Predicted to: ${parts.join(" · ")}`;
  return parts.length ? `Plans reach: ${parts.join(" · ")}` : `Schedule to ${fmtWhen(state.scheduleEnd)} UTC`;
}

// After the popup grew, pan so it is wholly inside the map (beside the panel).
function keepPopupInView(popup) {
  const el = popup.getElement && popup.getElement();
  if (!el) return;
  const r = el.getBoundingClientRect(), m = map.getContainer().getBoundingClientRect();
  const pad = map.getPadding ? map.getPadding() : { left: 0, right: 0, top: 0, bottom: 0 };
  const left = m.left + (pad.left || 0) + 8, right = m.right - 8, top = m.top + 8, bottom = m.bottom - (pad.bottom || 0) - 8;
  let dx = 0, dy = 0;
  if (r.top < top) dy = r.top - top; else if (r.bottom > bottom) dy = Math.min(r.bottom - bottom, r.top - top);
  if (r.left < left) dx = r.left - left; else if (r.right > right) dx = Math.min(r.right - right, r.left - left);
  if (dx || dy) map.panBy([dx, dy], { duration: 300 });
}

async function fillHistory(c, popup) {
  const el = popup.getElement && popup.getElement();
  const box = el && el.querySelector(".hist");
  if (!box) return;
  const recent = box.querySelector(".hist-recent"), tl = box.querySelector(".hist-tl-box"), src = box.querySelector(".hist-src");
  try {
    const h = await fetchHistory(c);
    if (!box.isConnected) return;
    if (h.items == null) { recent.innerHTML = recentSection(c, null, `<tr><td colspan="${popupCols(c)}" class="nm">${h.note}</td></tr>`); return; }
    const upcoming = c.visible.map((t) => ({ when: t.when, platform: t.platform, label: `${t.platform || ""} planned ${fmtWhen(t.when)} UTC` }));
    tl.innerHTML = timelineSVG(h.items.map((x) => ({ when: x.when, platform: x.platform, label: `${x.platform} ${fmtWhen(x.when)} UTC` })), upcoming, missionColor(c.mission), { width: TL_WIDTH });
    box.querySelector(".hist-next").innerHTML = nextLine(c, h);
    const now = Date.now();
    recent.innerHTML = recentSection(c, h.items.length, h.items.length
      ? h.items.map((x) => acqRow(c, x, false, now)).join("")
      : `<tr><td colspan="${popupCols(c)}" class="nm">None found.</td></tr>`);
    src.textContent = `Recent: ${h.source}.${h.note ? ` ${h.note}` : ""}`;
    recent.querySelectorAll("button[data-browse]").forEach((b) => b.addEventListener("click", () => togglePreview(b)));
    keepPopupInView(popup);
  } catch (err) {
    if (box.isConnected) recent.innerHTML = recentSection(c, null, `<tr><td colspan="${popupCols(c)}" class="bad">Could not load: ${err.message}</td></tr>`);
  }
}

// MapLibre extends any fill that reaches the edge of its Web Mercator tiles
// (+/-85.05 deg) to the pole (its globe "pole fill"), which draws polar
// swaths as wedges; so footprints are drawn clipped at +/-85 deg. (Their
// data, used for the AOI and the times, is not clipped.)
const DRAW_MAX_LAT = 85;
function drawable(geojson, bbox) {
  const [, s, , n] = bbox || turf.bbox(geojson);
  if (n <= DRAW_MAX_LAT && s >= -DRAW_MAX_LAT) return geojson;
  try {
    const g = turf.bboxClip(geojson, [-540, -DRAW_MAX_LAT, 540, DRAW_MAX_LAT]).geometry;
    const rings = g.type === "Polygon" ? g.coordinates : g.coordinates.flat();
    return rings.some((r) => r.length >= 4) ? g : null;
  } catch { return geojson; }
}

// Poleward of 85 deg the GeoJSON layers cannot draw (see drawable): on the
// globe, the overlay draws those parts of the footprints, in true spherical
// geometry, with the same colours and order; hovering and clicking them
// works as for the others. (The flat map ends at 85.05 deg.)
const polarCaps = new PolarCaps(overlay, DRAW_MAX_LAT);
function drawPolarCaps() {
  const items = DRAW_ORDER.flatMap((key) => state.cells.filter((c) => c.mission === key && (c.bbox[3] > DRAW_MAX_LAT || c.bbox[1] < -DRAW_MAX_LAT))
    .sort((a, b) => nextOf(b) - nextOf(a))
    .map((c) => { const st = cellStyle(c); return { cell: c, geojson: c.geojson, fill: st.fillColor, alpha: st.fillOpacity, edge: st.weight > 0, stroke: st.color }; }));
  polarCaps.set(items);
}
view.extraHit = (lngLat) => {
  if (!overlay.proj || !overlay.proj.globe || Math.abs(lngLat.lat) <= DRAW_MAX_LAT) return null;
  const it = polarCaps.pick(lngLat.lng, lngLat.lat);
  if (!it) return null;
  return { tooltip: () => cellTip(it.cell), onClick: () => selectCell(it.cell) };
};

// Redraws the given missions' layers (default: all) from state.cells.
function drawCells(keys = DRAW_ORDER) {
  drawPolarCaps();
  const now = Date.now();
  keys.forEach((key) => {
    const layer = gridLayers[key];
    layer.clear();
    // latest first, soonest on top; Landsat's night scenes under its day ones
    const night = (c) => (c.visible[0] && c.visible[0].light === "night" ? 1 : 0);
    state.cells.filter((c) => c.mission === key).sort((a, b) => night(b) - night(a) || nextOf(b) - nextOf(a)).forEach((c) => {
      const next = c.visible.length ? (c.visible[0].when - now) / 3.6e6 : null;
      if (c.drawGeo === undefined) c.drawGeo = drawable(c.geojson, c.bbox);
      if (!c.drawGeo) return;
      c.layer = layer.add({ geojson: c.drawGeo, style: cellStyle(c), onClick: () => selectCell(c) });
      c.layer.tooltip = () => cellTip(c, next);
    });
  });
}

function cellTip(c, next = c.visible.length ? (c.visible[0].when - Date.now()) / 3.6e6 : null) {
  return c.visible.length
    ? `${MISSION[c.mission].label} ${c.title} · next ${fmtBoth(c.visible[0].when, c.center)} (in ${fmtHours(next)})`
    : `${MISSION[c.mission].label} ${c.title} · nothing planned`;
}

// ---------------------------------------------------------------- selection
// One selection at a time (state.sel): an item of the AOI's list (a planned
// pass or a recent acquisition, by key) or a footprint picked on the map.
// Selecting one clears the others' highlight, map label and popup, and the
// list, the timeline and the map all show the same one.
function highlight(shapes, mission, { fill = true } = {}) {
  highlightLayer.clear();
  if (!shapes.length) return null;
  const style = fill
    ? { color: missionColor(mission), weight: 3, opacity: 1, fillColor: missionColor(mission), fillOpacity: 0.15 } // the AOI's passes: framed in their mission's colour
    : { color: css("--ink"), weight: 3, opacity: 1, fill: false };
  return highlightLayer.add({ geojson: shapes.length === 1 ? shapes[0] : { type: "GeometryCollection", geometries: shapes }, style });
}
const selected = (it) => Boolean(state.sel && it && state.sel.key === it.key);
function select(kind, key = null, { fit = true, scroll = false } = {}) {
  state.sel = kind ? { kind, key } : null;
  view.closePopup();
  highlightLayer.clear();
  const it = kind && kind !== "cell" ? listItems().find((x) => x.key === key) : null;
  if (it) {
    state.listTab = it.when.getTime() < Date.now() ? "past" : "planned"; // the tab it is in
    const shapes = it.kind === "pass" ? it.ref.shapes.map((g) => drawable(g)).filter(Boolean) : recentShapes(it.ref);
    const h = highlight(shapes, it.mission);
    if (h && fit) view.fitBBox(h.bbox, { pad: 0.3, maxZoom: 9 });
    // a one-line label on the map; the details are in the list
    const at = h ? h.center() : aoiCenter(), html = `<b>${passLabel(it.ref, { short: true })}</b> · ${utcText(it.when)} <span class="nm">${rel(it.when)}</span>`, opts = { className: "label-pop", maxWidth: "360px", offset: 6, focusAfterOpen: false };
    if (at) {
      popupBeside(h && h.bbox, at, html, opts);
      // placed again once the map has framed it (the room above and below changes)
      if (h && fit) map.once("moveend", () => { if (selected(it) && view.popupRef && view.popupRef.getElement()?.classList.contains("label-pop")) popupBeside(h.bbox, at, html, opts); });
    }
  }
  renderList();
  if (scroll) requestAnimationFrame(() => { const row = document.querySelector("#results tr.sel"); if (row) row.scrollIntoView({ block: "center", behavior: "smooth" }); });
}

// A popup clear of the frame it is about: off its north edge (or its south
// edge, where the screen has more room), with a gap between, so the frame
// stays in sight. A frame bigger than the screen gets it at its centre.
const POPUP_GAP = 18;
function popupBeside(bbox, at, html, opts) {
  if (!bbox) return view.popup(at, html, opts);
  const [w, s, e, n] = bbox, mid = e >= w ? (w + e) / 2 : at[0];
  const H = map.getCanvas().clientHeight, top = map.project([mid, n]).y, bottom = map.project([mid, s]).y;
  const roomAbove = top, roomBelow = H - bottom;
  if (roomAbove < 40 && roomBelow < 40) return view.popup(at, html, opts);
  return roomAbove >= roomBelow
    ? view.popup([mid, n], html, { ...opts, anchor: "bottom", offset: POPUP_GAP })
    : view.popup([mid, s], html, { ...opts, anchor: "top", offset: POPUP_GAP });
}

// A footprint picked on the map: its popup (history and plan).
function selectCell(c) {
  state.sel = { kind: "cell", key: `${c.mission}|${c.title}` };
  view.closePopup();
  const outline = highlight([c.drawGeo || drawable(c.geojson, c.bbox) || c.geojson], c.mission, { fill: false });
  const pop = popupBeside(outline && outline.bbox, c.center, cellPopup(c), { maxWidth: "720px" });
  requestAnimationFrame(() => keepPopupInView(pop)); // the map moves, the frame and its popup together
  fillHistory(c, pop);
  renderList(); // the list's selection, if any, is cleared
}

// A recent acquisition over the AOI (catalogue products, or a planned pass
// not yet catalogued): highlighted on the map, its card and row in the list.
// The footprint of a recent acquisition: a product's own (the scene, tile or
// frame, from the sources that publish whole scenes first), else the plan's.
const FOOTPRINT_SOURCES = {
  s1: ["asf_grd", "cdse_grd", "pc_grd", "asf_slc", "cdse_slc"],
  s2: ["es_s2_l1c", "es_s2_l2a", "cdse_s2_l1c", "cdse_s2_l2a", "pc_s2_l2a"],
  landsat: ["es_landsat_l2", "pc_landsat_l2"],
  nisar: ["asf_nisar_rslc", "asf_nisar_gcov"], // not Level-0B: its footprint is the whole datatake (thousands of km)
  modis: ["laads_modis_l1b_c7", "laads_modis_l1b", "lance_modis_l1b"],
  viirs: ["laads_viirs_l1b", "lance_viirs_l1b"],
  s3: ["cdse_s3_rbt", "cdse_s3_rbt_ntc", "cdse_s3_efr", "cdse_s3_efr_ntc"],
};
const isArea = (g) => g && (g.type === "Polygon" || g.type === "MultiPolygon");
// A box of about 600 km around a point, for a GIBS preview
function gibsBox(lon, lat) {
  const dy = 2.7, dx = Math.min(30, 2.7 / Math.max(0.1, Math.cos((lat * Math.PI) / 180)));
  return [Math.max(-180, lon - dx), Math.max(-90, lat - dy), Math.min(180, lon + dx), Math.min(90, lat + dy)].map((v) => Math.round(v * 1e4) / 1e4);
}
function recentShapes(a) {
  const prods = a.products || [];
  for (const src of FOOTPRINT_SOURCES[a.mission] || []) {
    const geoms = prods.filter((x) => x.source === src && isArea(x.geometry) && (!state.aoi || intersects(state.aoi, x.geometry))).map((x) => drawable(x.geometry)).filter(Boolean);
    if (geoms.length) return geoms;
  }
  if (a.plan && a.plan.shapes) return a.plan.shapes.map((g) => drawable(g)).filter(Boolean);
  // NISAR with only Level-0B so far: the frames of its track over the AOI
  if (a.mission === "nisar" && state.aoi && a.track != null && state.loadedSensors.has("nisar")) {
    const frames = nisarFrames().filter((f) => f.track === a.track && (!a.direction || f.direction === a.direction) && intersects(state.aoi, f.geojson, f.bbox));
    if (frames.length) return frames.map((f) => drawable(f.geojson, f.bbox)).filter(Boolean);
  }
  const g = prods.filter((x) => x.source !== "asf_nisar_l0b").map((x) => x.geometry).find(isArea);
  const d = g && drawable(g);
  return d ? [d] : [];
}

// A browse request for a recent acquisition (see history.js fetchBrowse),
// from its products; null when none of its sources has one.
function recentBrowse(a) {
  const of = (src) => (a.products || []).find((x) => x.source === src);
  if (a.mission === "s1" && a.platform) return { kind: "s1", platform: `Sentinel-1${a.platform.slice(2)}`, when: a.shown.toISOString(), point: aoiCenter() };
  if (a.mission === "s2") { const x = of("es_s2_l2a") || of("es_s2_l1c"); return x ? { kind: "s2", id: x.id } : null; }
  if (a.mission === "landsat") { const x = of("es_landsat_l2") || of("pc_landsat_l2"); return x ? { kind: "landsat", id: x.id } : null; }
  if (a.mission === "nisar") {
    const x = ["asf_nisar_rslc", "asf_nisar_gcov"].map(of).find((y) => y && y.browseUrls && y.browseUrls.length);
    return x ? { kind: "nisar", urls: x.browseUrls, bbox: isArea(x.geometry) ? turf.bbox(x.geometry) : null } : null;
  }
  // MODIS/VIIRS by day: NASA Worldview's (GIBS) true colour of that platform and day, around the AOI
  if ((a.mission === "modis" || a.mission === "viirs") && a.platform && a.light !== "night") {
    const [lon, lat] = aoiCenter();
    return { kind: "gibs", platform: a.platform, date: a.shown.toISOString().slice(0, 10), bbox: gibsBox(lon, lat) };
  }
  return null;
}
// ---------------------------------------------------------------- the AOI's list
// One list, nearest to now first on each side: the planned passes ahead (as
// filtered), soonest first, a "Now" line, then the acquisitions of the last
// RECENT_DAYS, newest first (catalogue products, and planned passes no
// catalogue has yet). listItems() is in time order. An item:
// {kind: "recent" | "pass", key, ref, when, mission, platform, direction, track, state}.
function listItems() {
  const now = Date.now();
  const past = (state.recent ? state.recent.acqs : []).map((a) => ({
    kind: "recent", key: a.key, ref: a, when: a.shown, mission: a.mission, platform: a.platform || LANE_UNKNOWN[a.mission],
    direction: a.direction, track: a.track, state: a.products.length ? "acquired" : "uncatalogued",
  }));
  const ahead = state.passes.filter((p) => p.when.getTime() >= now).map((p) => ({
    kind: "pass", key: p.key, ref: p, when: p.when, mission: p.mission, platform: p.platform,
    direction: p.direction, track: p.track, state: "planned",
  }));
  return [...past, ...ahead].sort((x, y) => x.when - y.when);
}

// ---------------------------------------------------------------- AOI timeline
// Along the bottom of the map while there is an AOI: the list's items from
// 30 days back to 30 ahead, one lane per satellite, in its mission's colour
// (filled: in a catalogue; hollow: planned; dashed: planned, in no
// catalogue yet). A lane is hatched after its plan's end (nothing is known
// there). Clicking a marker (or Enter on it) selects its row; arrow keys move
// between markers.
const TL_DAYS = 30;
const tlBox = document.getElementById("aoi-timeline");
const LANE_ORDER = ["S1A", "S1C", "S1D", "S1", "S2A", "S2B", "S2C", "S2", "L8", "L9", "L8/9", "NISAR", "Terra", "Aqua", "MODIS", "SNPP", "N20", "N21", "VIIRS", "S3A", "S3B", "S3"];
// products whose platform the catalogue does not give (HLS, for one)
const LANE_UNKNOWN = { s1: "S1", s2: "S2", landsat: "L8/9", nisar: "NISAR", modis: "MODIS", viirs: "VIIRS", s3: "S3" };
let tlItems = [];
function renderTimeline() {
  const on = Boolean(state.aoi);
  tlBox.hidden = !on;
  document.body.classList.toggle("has-timeline", on);
  if (!on) return;
  const now = Date.now(), t0 = now - TL_DAYS * 864e5, t1 = now + TL_DAYS * 864e5;
  const items = listItems().filter((it) => it.when.getTime() >= t0 && it.when.getTime() <= t1);
  tlItems = items;
  // on a phone: one lane for every satellite (the strip sits above the sheet)
  const compact = window.innerWidth <= 760;
  const laneOf = (it) => (compact ? "all" : it.platform);
  const lanes = compact ? ["all"] : [...new Set(items.map((x) => x.platform))].sort((a, b) => (LANE_ORDER.indexOf(a) + 1 || 99) - (LANE_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
  const W = Math.max(compact ? 200 : 300, tlBox.clientWidth - 20), labelW = compact ? 4 : 46, laneH = 16, top = 14;
  const H = top + Math.max(1, lanes.length) * laneH + 24;
  const x = (t) => labelW + ((t - t0) / (t1 - t0)) * (W - labelW - 6);
  const yOf = (p) => top + lanes.indexOf(p) * laneH + laneH / 2;
  const ticks = [];
  // a date every 5 days, or every 10 where the strip is narrow
  const every = W > 640 ? 5 : W > 300 ? 10 : 15;
  for (let d = -TL_DAYS; d <= TL_DAYS; d++) {
    const t = now + d * 864e5, major = d % every === 0;
    ticks.push(`<line x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${H - 22}" y2="${H - (major ? 16 : 19)}" stroke="var(--muted)" stroke-width="1"/>`);
    if (major && d !== 0) {
      const dt = new Date(t);
      ticks.push(`<text x="${x(t).toFixed(1)}" y="${H - 5}" text-anchor="${d === -TL_DAYS ? "start" : d === TL_DAYS ? "end" : "middle"}">${dt.getUTCDate()} ${fmtWhen(dt).split(" ")[2]}</text>`);
    }
  }
  // hatched after each platform's plan end
  const ends = compact ? "" : lanes.map((p) => {
    const end = (state.planEnds[missionOf(p)] || {})[p];
    if (!end || end.getTime() >= t1) return "";
    const xe = Math.max(x(now), x(end.getTime()));
    return `<rect x="${xe.toFixed(1)}" y="${yOf(p) - laneH / 2 + 2}" width="${(x(t1) - xe).toFixed(1)}" height="${laneH - 4}" fill="url(#tl-hatch)"><title>${esc(p)}: the plan ends ${utcText(end)}; nothing is known after it</title></rect>`;
  }).join("");
  const laneLines = lanes.map((p) => `<text class="lane" x="0" y="${yOf(p) + 3.5}">${compact ? "" : esc(p)}</text><line x1="${labelW}" x2="${W - 6}" y1="${yOf(p)}" y2="${yOf(p)}" stroke="var(--hairline)"/>`).join("");
  const marks = items.map((it, i) => {
    const c = missionColor(it.mission), filled = it.state === "acquired";
    const label = `${passLabel(it.ref, { short: true })}: ${fmtBoth(it.when, aoiCenter())} · ${it.state === "acquired" ? "acquired (in a catalogue)" : it.state === "uncatalogued" ? "planned, in no catalogue yet" : "planned"}`;
    return `<circle class="mk${selected(it) ? " sel" : ""}" data-i="${i}" data-key="${esc(it.key)}" tabindex="0" role="button" aria-label="${esc(label)}" cx="${x(it.when.getTime()).toFixed(1)}" cy="${yOf(laneOf(it))}" r="5" fill="${filled ? c : "var(--surface)"}" stroke="${c}" stroke-width="1.8"${it.state === "uncatalogued" ? ' stroke-dasharray="2 1.5"' : ""}><title>${esc(label)}</title></circle>`;
  }).join("");
  const nowX = x(now).toFixed(1);
  const loading = state.busy || (state.recent && Object.values(state.recent.results).some((r) => r.status === "loading"));
  const empty = items.length ? "" : `<text x="${(W / 2).toFixed(0)}" y="${top + 10}" text-anchor="middle">${loading ? "Finding passes…" : "No passes over this AOI in this window that match the filters."}</text>`;
  tlBox.innerHTML = `<div class="tl-head"><b>Passes over your AOI</b><span class="nm">30 days back to 30 ahead · ● acquired · ○ planned · hatched: after the plan ends · click one to select it</span></div>`
    + `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="group" aria-label="Passes over your AOI: ${items.length}">`
    + '<defs><pattern id="tl-hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="5" stroke="var(--muted)" stroke-width="1" opacity=".45"/></pattern></defs>'
    + `<rect x="${nowX}" y="${top - 4}" width="${(x(t1) - x(now)).toFixed(1)}" height="${H - top - 18}" fill="var(--surface-2)"/>`
    + ends + laneLines + ticks.join("") + empty
    + `<line x1="${nowX}" x2="${nowX}" y1="${top - 8}" y2="${H - 16}" stroke="var(--ink)" stroke-width="1.5"/><text x="${nowX}" y="${top - 9}" text-anchor="middle" style="font-weight:600;fill:var(--ink)">Now</text>`
    + marks + "</svg>";
  const mks = [...tlBox.querySelectorAll(".mk")];
  mks.forEach((el, i) => {
    el.addEventListener("click", () => pickItem(tlItems[Number(el.dataset.i)]));
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickItem(tlItems[Number(el.dataset.i)]); }
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); const n = mks[i + (e.key === "ArrowRight" ? 1 : -1)]; if (n) n.focus(); }
    });
  });
  document.body.style.setProperty("--tl-h", `${tlBox.offsetHeight + 42}px`);
}
// A list item picked from the timeline: selected, its row scrolled to.
function pickItem(it) {
  if (!it) return;
  select(it.kind, it.key, { scroll: true });
}
// redrawn at its own width (the panel widening or closing moves its left edge)
let tlWidth = 0;
new ResizeObserver(() => { const w = tlBox.clientWidth; if (state.aoi && w && Math.abs(w - tlWidth) > 2) { tlWidth = w; renderTimeline(); } }).observe(tlBox);

// ---------------------------------------------------------------- the list
const sortKey = (v) => (typeof v === "number" ? (Number.isFinite(v) ? String(v).padStart(16, "0") : "~") : String(v ?? "~"));
function sortRows(rows, keyFn, dir, tie) {
  return rows.map((r) => [sortKey(keyFn(r)), tie(r), r])
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] - b[1]) * dir)
    .map((x) => x[2]);
}
const chip = (mission) => `<span class="chip" style="background:${missionColor(mission)}"></span>`;
const TRACK_SHORT = { s1: "rel. orbit", s2: "rel. orbit", landsat: "path", nisar: "track", s3: "rel. orbit" };
const PAST_LIST_MAX = 600;

// After a new AOI the list opens at "Now" (until the person scrolls it).
let scrollToNow = false;
document.querySelector(".panel-scroll").addEventListener("wheel", () => { scrollToNow = false; }, { passive: true });
document.querySelector(".panel-scroll").addEventListener("touchmove", () => { scrollToNow = false; }, { passive: true });

// The AOI part in view: on phones the whole sheet scrolls (to it); elsewhere
// it is always in view and only its list scrolls (to its top).
function showAOIPart(behavior = "auto") {
  if (getComputedStyle(document.getElementById("sidebar")).overflowY === "auto") {
    const sheet = document.getElementById("sidebar"), part = document.getElementById("aoi-part");
    sheet.scrollTo({ top: part.offsetTop - document.querySelector(".panel-top").offsetHeight, behavior });
  } else document.querySelector(".panel-scroll").scrollTop = 0;
}

function renderList() {
  const box = document.getElementById("results");
  const title = document.getElementById("list-title");
  const notes = state.notes.map((n) => Object.assign(document.createElement("div"), { className: "note notice", textContent: n }));
  renderTimeline();
  const tabs = document.getElementById("list-tabs");
  if (!state.aoi) {
    // without an AOI there is no list: the map is the view
    title.hidden = true;
    tabs.hidden = true;
    box.replaceChildren(...notes, Object.assign(document.createElement("div"), { id: "empty", textContent: "Pick a point, a box or a polygon above to list the passes over it and see them on a timeline." }));
    return;
  }
  title.hidden = false;
  const items = listItems();
  const now = Date.now(), ll = aoiCenter();
  const past = items.filter((it) => it.when.getTime() < now).slice(-PAST_LIST_MAX), ahead = items.filter((it) => it.when.getTime() >= now).slice(0, MAX_LIST);
  const rec = state.recent;
  const searching = rec ? Object.values(rec.results).filter((r) => r.status === "loading").length : 0;
  // two tabs, one list at a time: the plan ahead, or the last RECENT_DAYS
  const tab = state.listTab;
  tabs.hidden = false;
  tabs.innerHTML = `<div role="tablist" aria-label="Passes over your AOI">`
    + `<button type="button" role="tab" class="lt-planned${tab === "planned" ? " on" : ""}" aria-selected="${tab === "planned"}" data-tab="planned">Planned <span class="n">${ahead.length}</span></button>`
    + `<button type="button" role="tab" class="lt-past${tab === "past" ? " on" : ""}" aria-selected="${tab === "past"}" data-tab="past">Last ${RECENT_DAYS} days <span class="n">${past.length}${searching ? "…" : ""}</span></button></div>`;
  tabs.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (state.listTab === b.dataset.tab) return;
    state.listTab = b.dataset.tab;
    document.querySelector(".panel-scroll").scrollTop = 0;
    renderList();
  }));
  box.dataset.tab = tab;
  const head = document.createElement("div");
  head.className = "list-head";
  head.innerHTML = tab === "planned"
    ? `<div class="nm">The agencies' plans in your dates, soonest first, with when each is likely downloadable.</div>`
    : `<div class="nm">Found in each catalogue now, newest first, with what is up.${searching ? ` Searching ${searching} catalogue${searching > 1 ? "s" : ""}…` : ""}</div>`;
  if (tab === "planned") head.append(exportBar(ahead.length));
  const shown = tab === "planned" ? ahead : [...past].reverse();
  if (!shown.length) {
    const none = tab === "planned" ? (state.busy ? "Finding passes…" : "Nothing planned over this AOI in the dates chosen that matches the filters.")
      : state.busy || searching ? "Finding passes…" : `Nothing over this AOI in the last ${RECENT_DAYS} days that matches the filters.`;
    box.replaceChildren(...notes, head, Object.assign(document.createElement("div"), { id: "empty", textContent: none }));
    return;
  }
  const t = document.createElement("table");
  t.className = "data pass-list";
  t.innerHTML = '<colgroup><col class="c-when"><col class="c-sat"><col class="c-status"><col class="c-prev"></colgroup><thead><tr><th>When <span class="nm">UTC · local</span></th><th>Satellite</th><th>Status</th><th><span class="sr-only">Preview</span></th></tr></thead>';
  const tbody = document.createElement("tbody");
  let day = null;
  const groupRow = (d) => {
    const dl = dayLabel(d);
    tbody.insertAdjacentHTML("beforeend", `<tr class="group"><td colspan="4">${dl.name} <span class="nm">UTC · ${dl.rel}</span></td></tr>`);
  };
  const addNow = () => {
    tbody.insertAdjacentHTML("beforeend", `<tr class="now-row" id="now-row"><td colspan="4"><span>Now</span> <span class="nm">${utcText(new Date(now))} · ${tab === "planned" ? "soonest first below" : "newest first below"}</span></td></tr>`);
    day = null; // the next day header is repeated under "Now"
  };
  // from "Now" outwards: the plan soonest first, or the last RECENT_DAYS newest first
  addNow();
  shown.forEach((it) => {
    const dk = it.when.toISOString().slice(0, 10);
    if (dk !== day) { day = dk; groupRow(it.when); }
    const tr = document.createElement("tr");
    tr.className = `item${selected(it) ? " sel" : ""}${it.kind === "recent" ? " past" : ""}`;
    tr.dataset.key = it.key;
    tr.tabIndex = 0;
    tr.setAttribute("aria-expanded", String(selected(it)));
    const m = MISSION[it.mission];
    const dirOf = it.direction || (it.mission === "s2" ? "descending" : null);
    const sat = `${chip(it.mission)} ${esc(it.platform || m.label)}<div class="nm">${[dirOf ? `<span title="${esc(dirText(it.mission, dirOf))}">${dirOf === "ascending" ? "asc" : "desc"}</span>` : null, it.track != null && it.track !== "" ? `${TRACK_SHORT[it.mission]} ${it.track}` : null, it.ref.light === "night" ? "night" : null].filter(Boolean).join(" · ")}</div>`;
    let status;
    if (it.kind === "pass") {
      const e = earliest(it.ref);
      status = e ? `~${utcText(roundUp10(e.exp.median_at), it.when)}${failingMark(e.failing)}<div class="nm">${e.label}</div>` : `<span class="nm">${latencyLoading(it.mission) ? "loading latency…" : "delivery not measured"}</span>`;
    } else status = selected(it) ? '<span class="nm">details below ▾</span>' : recentSummary(it.ref);
    const req = it.kind === "recent" ? recentBrowse(it.ref) : null;
    const k = req && browseItem(req, { mission: it.mission, when: it.ref.shown, platform: it.ref.platform || MISSION[it.mission].label, direction: it.direction || "", track: it.track ?? "", frame: "", center: ll });
    const prev = k ? `<button type="button" class="link-btn row-prev" data-browse="${esc(k)}" title="Drape its browse image on the map">${queued(k) ? "Remove" : "Preview"}</button>` : "";
    tr.innerHTML = `<td class="when">${whenCell(it.when, ll, it.kind === "pass" && it.ref.estimated, rel(it.when))}</td><td>${sat}</td><td class="status">${status}</td><td class="prev">${prev}</td>`;
    tr.querySelector(".row-prev")?.addEventListener("click", (e) => { e.stopPropagation(); togglePreview(e.currentTarget); });
    const toggle = () => select(selected(it) ? null : it.kind, selected(it) ? null : it.key);
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", (e) => { if (e.target === tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(); } });
    tr.addEventListener("mouseenter", () => tlBox.querySelector(`.mk[data-key="${CSS.escape(it.key)}"]`)?.classList.add("hov"));
    tr.addEventListener("mouseleave", () => tlBox.querySelector(`.mk[data-key="${CSS.escape(it.key)}"]`)?.classList.remove("hov"));
    tbody.append(tr);
    if (selected(it)) {
      const dr = document.createElement("tr"), td = document.createElement("td");
      dr.className = "detail-row";
      td.colSpan = 4;
      td.append(it.kind === "pass" ? detailCard(it.ref) : recentCard(it.ref));
      dr.append(td);
      tbody.append(dr);
    }
  });
  t.append(tbody);
  box.replaceChildren(...notes, head, t);
  // where the sticky "Now" and section rows sit (under the column names)
  t.style.setProperty("--th", `${t.tHead.offsetHeight}px`);
  t.style.setProperty("--now", `${t.querySelector("tr.now-row").offsetHeight}px`);
  if (scrollToNow) {
    // a new AOI: the list from its top (now, and the next pass)
    document.querySelector(".panel-scroll").scrollTop = 0;
    showAOIPart();
    if (!searching && !state.busy) scrollToNow = false;
  }
}

// ---------------------------------------------------------------- refresh
// The area in view as [w, s, e, n]. MapLibre's globe getBounds() is wrong
// near the poles (centred on 89 N it reports 83.6-86.4 N and 37 deg of
// longitude), so the screen is sampled too, and a visible pole means every
// longitude from that latitude up.
let boundsMemo = null;
function viewBounds() {
  const canvas = map.getCanvas(), w = canvas.clientWidth, h = canvas.clientHeight;
  const c = map.getCenter();
  const key = [c.lng, c.lat, map.getZoom(), map.getBearing(), map.getPitch(), w, h, (map.getProjection && map.getProjection()?.type) || ""].join("|");
  if (boundsMemo && boundsMemo.key === key) return boundsMemo.value;
  const value = computeViewBounds(w, h);
  boundsMemo = { key, value };
  return value;
}
function computeViewBounds(w, h) {
  const b = map.getBounds();
  let west = b.getWest(), south = b.getSouth(), east = b.getEast(), north = b.getNorth();
  for (let i = 0; i <= 8; i++) {
    for (let j = 0; j <= 8; j++) {
      const ll = map.unproject([(w * i) / 8, (h * j) / 8]);
      if (ll && Number.isFinite(ll.lat)) { south = Math.min(south, ll.lat); north = Math.max(north, ll.lat); }
    }
  }
  const poleVisible = (lat) => {
    const pt = map.project([0, lat]);
    if (!(pt.x >= 0 && pt.x <= w && pt.y >= 0 && pt.y <= h)) return false;
    const back = map.unproject(pt); // on the far side of the globe it lands elsewhere
    return Math.abs(back.lat - lat) < 1;
  };
  if (poleVisible(90)) north = 90;
  if (poleVisible(-90)) south = -90;
  if (north > 84 || south < -84) { west = -180; east = 180; }
  return [west - 0.25, Math.max(-90, south - 0.25), east + 0.25, Math.min(90, north + 0.25)];
}

// Per mission, the footprints last built, reused while its filters, the dates,
// the AOI and the zoom regime are unchanged and the view stays inside the area
// they were built for: the view plus a margin. Most pans and zooms then
// rebuild nothing and send nothing to the map; only the list follows the view.
const built = {};
const drawn = {}; // per mission, the build its layer shows (null: empty)
const MARGIN = { s1: 0.25, s2: 0.5, landsat: 0.5, nisar: 1, modis: 1, viirs: 1, s3: 1 }; // of the view's width/height, each side
function padded([w, s, e, n], k) {
  const dx = (e - w) * k, dy = (n - s) * k;
  if (e - w + 2 * dx >= 360) return [-180, Math.max(-90, s - dy), 180, Math.min(90, n + dy)];
  return [Math.max(-180, w - dx), Math.max(-90, s - dy), Math.min(180, e + dx), Math.min(90, n + dy)];
}
// Is view v inside area a? Longitudes compared modulo 360 (a wide view runs past +/-180).
function contains(a, v) {
  if (a[1] > v[1] || a[3] < v[3]) return false;
  if (a[2] - a[0] >= 360 - 1e-6) return true;
  if (v[2] - v[0] >= 360) return false;
  return [-360, 0, 360].some((k) => a[0] <= v[0] + k && a[2] >= v[2] + k);
}
const setKey = (x) => (x ? [...x].sort().join(",") : "");
function missionSig(key) {
  const mf = state.mf[key];
  return [state.start, state.end, state.aoiVersion, overviewOf(key), setKey(mf.platforms), setKey(mf.dirs), setKey(mf.modes), setKey(mf.pols), setKey(mf.lights), mf.trackText, mf.frameText, mf.vzaText].join("|");
}

async function refresh() {
  if (!state.scheduleEnd) return; // the map can move before the data is in; load() refreshes
  writeHash();
  updateFilterSummaries();
  const token = ++state.token;
  const f = filters();
  if (state.aoi && !state.satrecs && MISSIONS.some((m) => isWide(m.key) && state.missions.has(m.key))) {
    state.satrecs = await satrecsFor();
    if (token !== state.token) return;
  }
  MISSIONS.forEach(({ key }) => { const el = document.getElementById(`err-${key}`); if (el) el.textContent = f.m[key].errors.join(" · "); });
  const zoom = viewZoom();
  const viewArea = viewBounds();
  for (const m of MISSIONS) {
    if (!state.missions.has(m.key)) continue;
    if (!state.aoi && zoom < m.minZoom - 0.3 && !m.overview) {
      built[m.key] = null;
      state.missionState[m.key] = { mode: "hidden" };
      continue;
    }
    const sig = missionSig(m.key), b = built[m.key];
    if (b && b.sig === sig && (state.aoi || contains(b.area, viewArea))) continue;
    if (!b || b.sig !== sig) state.busy = true;
    const area = buildArea(m.key);
    try {
      const got = await missionCells(m.key, area, f, token);
      if (got == null || token !== state.token) return;
      // MODIS/VIIRS/Sentinel-3 over an AOI: their passes come from these candidates (wideOverAOI)
      const candidates = state.aoi && isWide(m.key) ? got : null;
      let shown = state.aoi ? got.filter((c) => !c.candidate && intersects(state.aoi, c.geojson, c.bbox)) : got.filter((c) => !c.candidate);
      const pastCells = state.aoi ? shown.filter((c) => c.past && c.past.length) : [];
      shown.forEach((c) => { c.visible = c.times.filter((t) => timeOK(t, f)).sort((x, y) => x.when - y.when); });
      // Only footprints with a planned acquisition that passes the filters are
      // drawn: one with nothing planned (or all of it filtered out) is left
      // transparent, so the basemap shows through.
      shown = shown.filter((c) => c.visible.length);
      built[m.key] = { sig, area, cells: shown, pastCells, candidates, fallback: Boolean(got.fallback) };
    } catch (err) {
      if (token !== state.token) return;
      built[m.key] = { sig: null, area: [0, 0, 0, 0], cells: [], pastCells: [], error: err.message };
    }
  }
  // the layers whose content changed (a deselected mission's build is kept for re-selection)
  const want = (key) => (state.missions.has(key) && built[key]) || null;
  const changed = DRAW_ORDER.filter((key) => want(key) !== (drawn[key] || null));
  state.cells = MISSIONS.filter((m) => state.missions.has(m.key) && built[m.key]).flatMap((m) => built[m.key].cells);
  state.inView = state.aoi ? state.cells : state.cells.filter((c) => bboxOverlapWrapped(c.bbox, viewArea));
  state.busy = false;
  if (changed.length) {
    if (state.sel && state.sel.kind === "cell") select(null);
    state.passes = state.aoi ? buildPasses(state.cells) : [];
    const pastCells = MISSIONS.filter((m) => state.missions.has(m.key) && built[m.key]).flatMap((m) => built[m.key].pastCells || []);
    state.recentPlanned = state.aoi ? groupPasses(pastCells.filter((c) => !isWide(c.mission)), (c) => c.past.filter((t) => filterOK(t, f))) : [];
    MISSIONS.filter((m) => isWide(m.key) && state.missions.has(m.key)).forEach((m) => state.recentPlanned.push(...wideOverAOI(m.key, f, true)));
    drawCells(changed);
    changed.forEach((key) => { drawn[key] = want(key); });
  }
  MISSIONS.forEach((m) => {
    if (!state.missions.has(m.key) || !built[m.key]) return;
    const bm = built[m.key];
    const mine = state.inView.filter((c) => c.mission === m.key), swaths = mine.filter((c) => c.datatake).length;
    state.missionState[m.key] = bm.error ? { mode: "error", msg: bm.error }
      : { mode: overviewOf(m.key) || bm.fallback ? "overview" : "native", native: mine.length - swaths, swaths, fallback: bm.fallback };
  });
  if (state.aoi) updateRecent();
  renderList();
  // the panel's status line: only what needs attention
  const msgs = [...MISSIONS.filter((m) => state.missions.has(m.key) && state.missionState[m.key]?.mode === "error")
    .map((m) => `${m.label}: ${state.missionState[m.key].msg}`)];
  // With an AOI the map may still be flying to it: judge by the AOI, not the view.
  const [, vs, , vn] = state.aoi ? aoiBBox(state.aoi) : viewArea;
  if (state.missions.size && !globeBox.checked && (vn > DRAW_MAX_LAT || vs < -DRAW_MAX_LAT)) {
    msgs.push(`The flat map ends at ${DRAW_MAX_LAT}°: turn on the globe (Map options, top right) to see the footprints poleward of it; an AOI there still lists its passes.`);
  }
  if (!state.missions.size) msgs.push("Pick a mission.");
  document.getElementById("grid-status").textContent = msgs.join(" · ");
  legendMissions();
  syncSatellites();
}

// The legend's mission lines: for each selected mission, what the map shows
// now (native footprints, planned swaths, hidden), from which zoom its native
// footprints appear (a button to go there), and where its plan ends. Above
// them, without an AOI, the zoom ladder: the current zoom against each
// mission's native-footprint zoom.
const LADDER_MAX = 9;
const nativeName = (m) => m.footprint;
// The latest date any platform of a mission is planned to (null: unknown).
function planEndOf(key) {
  const ends = Object.values(state.planEnds[key] || {});
  return ends.length ? new Date(Math.min(...ends.map((d) => d.getTime()))) : null;
}
function legendMissions() {
  const el = document.getElementById("detail-hint");
  const ms = MISSIONS.filter((m) => state.missions.has(m.key));
  if (!ms.length) { el.replaceChildren(); return; }
  const z = viewZoom(), endSel = new Date(`${state.end}T23:59:59Z`);
  const lines = ms.map((m) => {
    const st = state.missionState[m.key] || { mode: "native" };
    const fz = fineZoom(m);
    const counts = st.native || st.swaths ? `${st.native ? `${st.native.toLocaleString()} ${nativeName(m)}` : ""}${st.native && st.swaths ? " + " : ""}${st.swaths ? `${st.swaths.toLocaleString()} swaths` : ""} ${state.aoi ? "over the AOI" : "in view"}` : "";
    let what, btn = "";
    if (st.mode === "error") what = `<span class="bad">${esc(st.msg)}</span>`;
    else if (st.mode === "hidden") { what = `hidden at this zoom · ${nativeName(m)} from zoom ${fz}`; btn = fz; }
    else if (st.mode === "overview") {
      if (state.aoi) what = "planned swaths (the AOI is too large for burst footprints)";
      else { what = `planned swaths · ${nativeName(m)} from zoom ${fz}`; btn = fz; }
    } else what = st.native === 0 && !st.swaths ? `nothing planned ${state.aoi ? "over the AOI" : "here"}` : nativeName(m);
    const end = planEndOf(m.key);
    if (end && st.mode === "native" && !st.native && !st.swaths && end < new Date(`${state.start}T00:00:00Z`)) {
      what = '<span class="warn-ink">nothing known in your dates: the plan ends before them</span>';
    }
    const endTxt = end ? `<span class="${end < endSel ? "warn-ink" : "nm"}" title="${end < endSel ? "your dates run past the end of this plan: nothing is known after it" : isWide(m.key) ? "no plan: passes predicted from the orbit up to this date" : "the agency's plan reaches this date"}">${isWide(m.key) ? "predicted" : "plan"} to ${fmtWhen(end).split(" ").slice(0, 3).join(" ")}</span>` : "";
    const zoomBtn = btn !== "" ? `<button type="button" data-z="${btn}" title="Zoom in to level ${btn}">Zoom in</button>` : "";
    // native footprints: one line; otherwise what is shown goes on a second line
    const short = st.mode === "native" && !/warn-ink/.test(what);
    return `<div class="ml" title="${esc(counts)}"><div class="ml-head"><span class="chip" style="background:${missionColor(m.key)}"></span><b>${m.label}</b>${short ? ` <span class="ml-what">${what}</span>` : ""} ${endTxt}${zoomBtn}</div>${short ? "" : `<div class="ml-what">${what}</div>`}</div>`;
  }).join("");
  // the ladder: only while some mission waits on a zoom
  const waiting = ms.filter((m) => !state.aoi && fineZoom(m) > 0 && ["hidden", "overview"].includes((state.missionState[m.key] || {}).mode));
  const x = (v) => `${(100 * Math.min(v, LADDER_MAX) / LADDER_MAX).toFixed(1)}%`;
  const ticks = waiting.length ? [...new Set(ms.filter((m) => fineZoom(m) > 0).map(fineZoom))].map((zz) => {
    const here = ms.filter((m) => fineZoom(m) === zz);
    return `<span class="tick${z >= zz - 0.3 ? "" : " off"}" data-z="${zz}" style="left:${x(zz)}">${here.map((m) => `<i style="background:${missionColor(m.key)}"></i>`).join("")}${zz}</span>`;
  }).join("") : "";
  const ladder = waiting.length ? `<div class="dh-ladder" aria-hidden="true" title="Zoom ${z.toFixed(1)}"><div class="bar"></div><div class="fill" style="width:${x(z)}"></div>${ticks}<div class="now" style="left:${x(z)}"></div></div>` : "";
  el.innerHTML = `${waiting.length ? `<div class="dh-zoom nm">Zoom ${z.toFixed(1)}</div>` : ""}${ladder}<div class="dh-lines">${lines}</div>`;
  el.querySelectorAll("button[data-z]").forEach((b) => b.addEventListener("click", () => map.easeTo({ zoom: Number(b.dataset.z) + 0.1 + (map.getZoom() - viewZoom()), duration: 900 })));
}
// The zoom readout and the ladder follow the zoom frame by frame (the rest
// follows refresh(), once the map stops).
let ladderFrame = 0;
map.on("zoom", () => {
  if (ladderFrame) return;
  ladderFrame = requestAnimationFrame(() => {
    ladderFrame = 0;
    const z = viewZoom(), box = document.getElementById("detail-hint");
    const label = box.querySelector(".dh-zoom");
    if (label) label.textContent = `Zoom ${z.toFixed(1)}`;
    const ladder = box.querySelector(".dh-ladder");
    if (!ladder) return;
    const at = `${(100 * Math.min(z, LADDER_MAX) / LADDER_MAX).toFixed(1)}%`;
    ladder.querySelector(".fill").style.width = at;
    ladder.querySelector(".now").style.left = at;
    ladder.title = `Zoom ${z.toFixed(1)}`;
    ladder.querySelectorAll(".tick").forEach((t) => t.classList.toggle("off", z < Number(t.dataset.z) - 0.3));
  });
});
let moveTimer = null;
// Without an AOI the footprints follow the view; with one they are fixed,
// and nothing follows the view.
map.on("moveend", () => { clearTimeout(moveTimer); if (!state.aoi) moveTimer = setTimeout(refresh, 150); });

// ---------------------------------------------------------------- controls
function checkbox(label, checked, onChange, extra = "") {
  const l = document.createElement("label");
  l.className = `check ${extra}`;
  const cb = Object.assign(document.createElement("input"), { type: "checkbox", checked });
  cb.addEventListener("change", () => onChange(cb.checked));
  l.append(cb, document.createTextNode(` ${label}`));
  return l;
}

// The mission list: each mission's own filters sit under it, shown while it
// is selected.
function toggleSet(set, v) { return (on) => { on ? set.add(v) : set.delete(v); refresh(); }; }

function checkRow(label, values, set, fmt = (v) => v, title = () => "") {
  const row = document.createElement("div");
  row.className = "frow";
  const checks = Object.assign(document.createElement("span"), { className: "checks" });
  checks.append(...values.map((v) => Object.assign(checkbox(fmt(v), set.has(v), toggleSet(set, v), "pill"), { title: title(v) })));
  row.append(Object.assign(document.createElement("span"), { className: "flabel", textContent: label }), checks);
  return row;
}

// Polarization pills. Sentinel-1: one row. NISAR: one row per band, since
// each band has its own channel sets (compact-pol is S-band only, quad and
// quasi-dual L-band only); the band's own checkbox selects or clears the row.
function polRows(mf) {
  const bands = [...new Set(mf.allPols.map((k) => polEntries(k)[0].band))];
  if (bands.length === 1 && bands[0] == null) {
    return [checkRow("Polarization", mf.allPols, mf.pols, (v) => POL_PILL[v] || v, (v) => POL_TITLE[v] || "")];
  }
  const head = Object.assign(document.createElement("div"), { className: "flabel fhead", textContent: "Polarization" });
  return [head, ...bands.map((band) => {
    const keys = mf.allPols.filter((k) => polEntries(k)[0].band === band);
    const row = checkRow("", keys, mf.pols, (k) => { const p = polEntries(k)[0].pol; return POL_PILL[p] || p; }, (k) => POL_TITLE[polEntries(k)[0].pol] || "");
    const pills = [...row.querySelectorAll(".checks input")];
    const all = Object.assign(document.createElement("input"), { type: "checkbox" });
    const sync = () => {
      const n = keys.filter((k) => mf.pols.has(k)).length;
      all.checked = n === keys.length; all.indeterminate = n > 0 && n < keys.length;
    };
    all.addEventListener("change", () => {
      keys.forEach((k) => (all.checked ? mf.pols.add(k) : mf.pols.delete(k)));
      pills.forEach((cb) => { cb.checked = all.checked; });
      sync();
      refresh();
    });
    pills.forEach((cb) => cb.addEventListener("change", sync));
    sync();
    const label = Object.assign(document.createElement("label"), { className: "check flabel band", title: `All ${BAND_NAME[band] || band} channel sets` });
    label.append(all, document.createTextNode(` ${BAND_NAME[band] || band}`));
    row.firstChild.replaceWith(label);
    return row;
  })];
}

function textFilter(key, which) {
  const spec = { ...FILTERS[key], vza: VZA_LABEL, vzaPh: "e.g. 45 (any if empty)" };
  const l = document.createElement("label");
  const input = Object.assign(document.createElement("input"), { placeholder: spec[`${which}Ph`], value: state.mf[key][`${which}Text`] });
  if (which === "vza") l.title = "Leave out passes that see the footprint more obliquely than this: MODIS/VIIRS pixels grow 2-5x towards the swath edges (VZA up to ~65-70°)";
  if (!(which === "frame" && spec.tiles)) input.inputMode = "numeric";
  let timer = null;
  input.addEventListener("input", () => { state.mf[key][`${which}Text`] = input.value; clearTimeout(timer); timer = setTimeout(refresh, 300); });
  l.append(Object.assign(document.createElement("span"), { textContent: spec[which] }), input);
  return l;
}

const cap = (d) => d[0].toUpperCase() + d.slice(1);

function buildControls() {
  const box = document.getElementById("missions");
  box.replaceChildren(...MISSIONS.map((m) => {
    const spec = FILTERS[m.key], mf = state.mf[m.key];
    const wrap = document.createElement("div");
    wrap.className = "mission-block";
    wrap.dataset.on = state.missions.has(m.key) ? "1" : "";
    const row = document.createElement("div");
    row.className = "mission-row";
    const main = checkbox(m.label, state.missions.has(m.key), (on) => {
      on ? state.missions.add(m.key) : state.missions.delete(m.key);
      wrap.dataset.on = on ? "1" : "";
      updateFilterSummaries();
      refresh();
    }, "mission");
    main.insertBefore(Object.assign(document.createElement("span"), { className: "chip", style: `background:${missionColor(m.key)}` }), main.childNodes[1]);
    // the filters fold to a one-line summary (the list below stays in reach)
    const toggle = Object.assign(document.createElement("button"), { className: "ftoggle", id: `ftoggle-${m.key}`, type: "button" });
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      if (!state.missions.has(m.key)) return; // greyed out until the mission is ticked
      const open = wrap.dataset.open !== "1";
      wrap.dataset.open = open ? "1" : "";
      toggle.setAttribute("aria-expanded", String(open));
    });
    row.append(main, toggle);
    const box2 = document.createElement("div");
    box2.className = "mfilters";
    const plats = [...mf.platforms].sort();
    if (plats.length > 1) box2.append(checkRow("Platform", plats, mf.platforms));
    if (mf.dirs) {
      const row = checkRow("Direction", ["ascending", "descending"], mf.dirs, (d) => `${cap(d)} (${SOLAR[m.key][d]})`,
        (d) => `${cap(d)} passes, at about ${SOLAR[m.key][d].slice(1)} local solar time${m.key === "landsat" && d === "ascending" ? ": the night side, thermal data only" : ""}`);
      (DIRS_NONE[m.key] || []).forEach((d) => {
        const l = [...row.querySelectorAll("label.check")].find((x) => x.textContent.trim().toLowerCase().startsWith(d));
        if (l) { l.querySelector("input").disabled = true; l.classList.add("off"); l.title = `${m.label} images on its descending passes only`; }
      });
      box2.append(row);
    }
    if (mf.modes) box2.append(checkRow("Mode", spec.modes, mf.modes));
    if (mf.lights) {
      box2.append(checkRow("Light", ["day", "night"], mf.lights, cap,
        (v) => (v === "day" ? "Passes with the sun up at the footprint" : `Passes in the dark at the footprint: thermal data only${m.key === "s3" ? " (SLSTR; OLCI images by day)" : ""}`)));
    }
    if (mf.pols && mf.allPols.length) box2.append(...polRows(mf));
    const texts = document.createElement("div");
    texts.className = "filters";
    texts.append(textFilter(m.key, "track"), textFilter(m.key, "frame"));
    if (spec.vza) texts.append(textFilter(m.key, "vza"));
    box2.append(texts, Object.assign(document.createElement("div"), { className: "err", id: `err-${m.key}` }));
    wrap.append(row, box2);
    return wrap;
  }));
  updateFilterSummaries();
}

// What differs from the start: "S1C · descending · relative orbit 137",
// or what is left out when that is shorter ("no L-band Quad"). A text
// filter that cannot be read is named as such (it is not applied).
function filterSummary(key) {
  const mf = state.mf[key], def = defaultsOf(key), spec = FILTERS[key];
  const parts = [];
  const name = {
    platforms: (v) => v, dirs: cap, modes: (v) => v, lights: cap,
    pols: (v) => { const e = polEntries(v)[0]; const p = POL_PILL[e.pol] || e.pol; return e.band ? `${e.band}-band ${p}` : p; },
  };
  SET_FIELDS.forEach(([, field]) => {
    if (!mf[field] || !def[field]) return;
    const init = initialOf(key, field);
    if (sameSet(mf[field], init)) return;
    const on = def[field].filter((v) => mf[field].has(v)), off = def[field].filter((v) => !mf[field].has(v));
    if (!on.length) parts.push(`no ${field === "dirs" ? "direction" : field === "pols" ? "polarization" : field === "lights" ? "light" : field.replace(/s$/, "")}`);
    else if (off.length < on.length) parts.push(`no ${off.map(name[field]).join(", ")}`);
    else parts.push(on.map(name[field]).join("+"));
  });
  const f = filters().m[key];
  const bad = (which) => f.errors.some((e) => e.startsWith(spec[which]));
  if (mf.trackText.trim()) parts.push(bad("track") ? `${MISSION[key].trackName}: invalid` : `${MISSION[key].trackName} ${mf.trackText.trim()}`);
  if (mf.frameText.trim()) parts.push(bad("frame") ? `${MISSION[key].frameName}: invalid` : `${MISSION[key].frameName} ${mf.frameText.trim()}`);
  if (mf.vzaText.trim()) parts.push(f.errors.some((e) => e.startsWith(VZA_LABEL)) ? "view zenith: invalid" : `VZA ≤ ${mf.vzaText.trim()}°`);
  return parts;
}
function updateFilterSummaries() {
  MISSIONS.forEach(({ key }) => {
    const el = document.getElementById(`ftoggle-${key}`);
    if (!el) return;
    const parts = filterSummary(key), on = state.missions.has(key);
    el.textContent = parts.length ? parts.join(" · ") : "Filters";
    el.title = !on ? `Tick ${MISSION[key].label} to filter it` : parts.length ? `Filters: ${parts.join(", ")} (click to change)` : "No filters: every acquisition (click to filter)";
    el.setAttribute("aria-disabled", String(!on));
    el.classList.toggle("narrowed", on && parts.length > 0);
    el.classList.toggle("invalid", on && parts.some((p) => p.endsWith(": invalid")));
  });
}

function legend() {
  const el = document.getElementById("grid-legend");
  const ticks = TICKS_D.map((d) => `<span style="left:${(100 * scaleT(d)).toFixed(1)}%">${d === HORIZON_D ? `${d}+` : d} d</span>`).join("");
  el.innerHTML = `<div class="ramp"><div class="ramp-bar" style="background:${gradientCSS(state.palette)}"></div><div class="ramp-ticks">${ticks}</div></div>`
    + '<span class="none-key">nothing planned in your dates, or filtered out: not drawn</span>';
}

// The dates: an emptied one goes back to its default; a start after the end
// swaps the two (on screen too, so what is shown is what is used).
["date-start", "date-end"].forEach((id) => document.getElementById(id).addEventListener("change", (e) => {
  const s = document.getElementById("date-start"), en = document.getElementById("date-end");
  if (!e.target.value) e.target.value = id === "date-start" ? s.min : defaultEnd();
  if (s.value > en.value) [s.value, en.value] = [en.value, s.value];
  state.start = s.value;
  state.end = en.value;
  refresh();
}));
document.getElementById("grid-palette").addEventListener("change", (e) => {
  state.palette = e.target.checked ? "cvd" : "greenred";
  try { localStorage.setItem("s1lat.palette", state.palette); } catch { /* storage unavailable */ }
  legend();
  drawCells();
  renderList();
});
// Footprint opacity (the colours over the basemap): 30 %, so the ground
// shows through (also after Reset and in the tour), until the slider is moved,
// after which the slider's value is kept. Under a preview the footprints
// fade to a quarter of that, so the image is not tinted.
const opacityInput = document.getElementById("opacity");
const OPACITY = { view: 0.3, aoi: 0.3 };
let opacityChosen = null;
try { const v = localStorage.getItem("swathcast.opacity"), o = Number(v); if (v != null && o >= 0 && o <= 1) opacityChosen = o; } catch { /* storage unavailable */ }
const baseOpacity = () => (opacityChosen != null ? opacityChosen : state.aoi ? OPACITY.aoi : OPACITY.view);
function syncOpacity() {
  state.opacity = baseOpacity();
  opacityInput.value = String(Math.round(state.opacity * 100));
  document.getElementById("opacity-val").textContent = `${opacityInput.value}%`;
}
syncOpacity();
let opacityTimer = null;
opacityInput.addEventListener("input", () => {
  opacityChosen = Number(opacityInput.value) / 100;
  syncOpacity();
  try { localStorage.setItem("swathcast.opacity", String(opacityChosen)); } catch { /* storage unavailable */ }
  clearTimeout(opacityTimer);
  opacityTimer = setTimeout(drawCells, 60);
});
try {
  if (localStorage.getItem("s1lat.palette") === "cvd") { state.palette = "cvd"; document.getElementById("grid-palette").checked = true; }
} catch { /* storage unavailable */ }

// ---------------------------------------------------------------- shareable URL
// The page's state lives in the URL hash, so a reload keeps it and a link
// shares it: #aoi=-121.735,46.786&m=s1,s2&to=2026-10-20&s1.dir=descending&tab=recent&bm=imagery.
// Only what differs from the defaults is written. An AOI is a point
// ("lon,lat"), a polygon ring ("lon,lat;lon,lat;…") or GeoJSON.
const SET_FIELDS = [["plat", "platforms"], ["dir", "dirs"], ["mode", "modes"], ["pol", "pols"], ["light", "lights"]];
const r5 = (v) => Math.round(v * 1e5) / 1e5;
// Every value a mission's set filter can take (null: no such filter).
function defaultsOf(key) {
  const spec = FILTERS[key];
  return {
    platforms: [...state.allPlatforms].filter((p) => missionOf(p) === key),
    dirs: spec.dirs ? ["ascending", "descending"] : null,
    modes: spec.modes || null, pols: state.mf[key].allPols, lights: spec.lights ? ["day", "night"] : null,
  };
}
// What a set filter starts with: everything, but Landsat's ascending (night)
// passes and the wide-swath sensors' night passes.
const initialOf = (key, field) => (field === "dirs" ? defaultsOf(key).dirs.filter((d) => !dirsOff(key).includes(d))
  : field === "lights" ? ["day"] : defaultsOf(key)[field]);
const sameSet = (set, list) => set.size === list.length && list.every((v) => set.has(v));
// The default date range: today to a month out (within the schedule).
const isoDay = (d) => d.toISOString().slice(0, 10);
function defaultEnd() {
  const e = document.getElementById("date-end");
  const v = isoDay(new Date(Date.now() + 30 * 864e5));
  return e.max && v > e.max ? e.max : v;
}
let lastHash = location.hash.slice(1);
function writeHash() {
  if (!state.scheduleEnd) return;
  const q = new URLSearchParams();
  if (state.aoi) q.set("aoi", aoiParam(state.aoi));
  const missions = MISSIONS.filter((m) => state.missions.has(m.key)).map((m) => m.key);
  if (missions.join(",") !== "s1") q.set("m", missions.join(",") || "none");
  if (state.start && state.start !== isoDay(new Date())) q.set("from", state.start);
  if (state.end && state.end !== defaultEnd()) q.set("to", state.end);
  missions.forEach((key) => {
    const mf = state.mf[key], def = defaultsOf(key);
    SET_FIELDS.forEach(([short, field]) => {
      if (!mf[field] || !def[field]) return;
      if (!sameSet(mf[field], initialOf(key, field))) q.set(`${key}.${short}`, def[field].filter((v) => mf[field].has(v)).join(",") || "none");
    });
    if (mf.trackText.trim()) q.set(`${key}.track`, mf.trackText.trim());
    if (mf.frameText.trim()) q.set(`${key}.frame`, mf.frameText.trim());
    if (mf.vzaText.trim()) q.set(`${key}.vza`, mf.vzaText.trim());
  });
  if (view.basemap !== DEFAULT_BASEMAP) q.set("bm", view.basemap);
  const hash = q.toString().replace(/%2C/g, ",").replace(/%3B/g, ";");
  if (hash === lastHash) return;
  lastHash = hash;
  history.replaceState(null, "", hash ? `#${hash}` : location.pathname + location.search);
}
// Applies the hash to state (after load() set the defaults); returns {aoi}.
function readHash() {
  const q = new URLSearchParams(location.hash.slice(1));
  const out = { aoi: null };
  if (q.has("m")) state.missions = new Set(q.get("m").split(",").filter((k) => MISSION[k]));
  const iso = /^\d{4}-\d{2}-\d{2}$/;
  const s = document.getElementById("date-start"), e = document.getElementById("date-end");
  const clamp = (v) => (v < s.min ? s.min : v > s.max ? s.max : v);
  if (iso.test(q.get("from") || "")) s.value = state.start = clamp(q.get("from"));
  if (iso.test(q.get("to") || "")) e.value = state.end = clamp(q.get("to"));
  MISSIONS.forEach(({ key }) => {
    const mf = state.mf[key], def = defaultsOf(key);
    SET_FIELDS.forEach(([short, field]) => {
      const v = q.get(`${key}.${short}`);
      if (v == null || !mf[field] || !def[field]) return;
      const want = new Set(v.split(","));
      mf[field] = new Set(def[field].filter((x) => want.has(x)));
    });
    if (q.has(`${key}.track`)) mf.trackText = q.get(`${key}.track`);
    if (q.has(`${key}.frame`)) mf.frameText = q.get(`${key}.frame`);
    if (q.has(`${key}.vza`) && FILTERS[key].vza) mf.vzaText = q.get(`${key}.vza`);
  });
  // links from the short-lived day/night filter: day was descending, night ascending
  const light = q.get("landsat.light");
  if (light && !q.has("landsat.dir")) state.mf.landsat.dirs = new Set(light.split(",").map((v) => (v === "night" ? "ascending" : v === "day" ? "descending" : null)).filter(Boolean));
  if (q.has("aoi")) {
    try { out.aoi = aoiFromParam(q.get("aoi")); } catch (err) { state.notes.push(`Could not read the link's AOI (${err.message}).`); }
  }
  return out;
}
// A link pasted into this tab changes only the hash: start over from it.
window.addEventListener("hashchange", () => { if (location.hash.slice(1) !== lastHash) location.reload(); });
document.getElementById("share").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget, label = btn.textContent;
  writeHash();
  try { await navigator.clipboard.writeText(location.href); btn.textContent = "Link copied"; } catch { btn.textContent = "Copy the address bar"; }
  setTimeout(() => { btn.textContent = label; }, 1800);
});

// Start over: no AOI, the default missions, filters and dates, and the map
// settings this browser remembered (basemap, opacity, palette, globe...).
function startOver() {
  forgetSettings();
  lastHash = "";
  history.replaceState(null, "", location.pathname + location.search);
  location.reload();
}
document.getElementById("reset").addEventListener("click", () => {
  if (confirm("Reset the map? This clears the AOI, the mission filters, the dates and the saved map settings.")) startOver();
});

// ---------------------------------------------------------------- export
// The upcoming passes over the AOI, as filtered: a CSV, or a calendar
// (.ics, one event per pass, with where it is likely downloadable first).
const isoZ = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");
const plain = (html) => html.replace(/<div[^>]*>|<br\s*\/?>/g, " · ").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
function exportRows() {
  return sortRows(state.passes.filter((p) => p.when.getTime() >= Date.now()), (p) => p.when.getTime(), 1, (p) => p.when).map((p) => {
    const e = earliest(p);
    return {
      p, e,
      cols: {
        time_utc: isoZ(p.when), end_utc: isoZ(p.whenEnd), local_time: fmtBoth(p.when, p.ll).split(" · ")[1] || "",
        mission: MISSION[p.mission].label, platform: p.platform, direction: p.direction || "", track: p.track ?? "",
        footprints: p.footprints, mode: plain(modeText(p)),
        likely_downloadable_utc: e ? isoZ(roundUp10(e.exp.median_at)) : "", likely_downloadable_from: e ? e.label : "",
        likely_downloadable_p90_utc: e && e.exp.p90_at ? isoZ(roundUp10(e.exp.p90_at)) : "",
      },
    };
  });
}
function saveFile(name, type, text) {
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
const stamp = () => new Date().toISOString().slice(0, 10);
function exportCSV() {
  const rows = exportRows();
  if (!rows.length) return;
  const head = Object.keys(rows[0].cols);
  const q = (v) => (/[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const lines = [head.join(","), ...rows.map((r) => head.map((h) => q(r.cols[h])).join(","))];
  saveFile(`swathcast-passes-${stamp()}.csv`, "text/csv", `${lines.join("\n")}\n`);
}
// RFC 5545 text: escape, then fold lines at 75 octets (73 characters keeps
// multi-byte characters such as "·" inside the limit most of the time).
const icsText = (v) => String(v).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\n/g, "\\n");
const icsFold = (line) => line.match(/.{1,73}/gu).join("\r\n ");
function exportICS() {
  const rows = exportRows();
  if (!rows.length) return;
  const t = (d) => isoZ(d).replace(/[-:]/g, "");
  const now = t(new Date()), c = aoiCenter();
  const events = rows.map(({ p, e, cols }) => {
    const end = new Date(Math.max(p.whenEnd.getTime(), p.when.getTime() + 60e3));
    const desc = [`${passLabel(p)}.`, cols.mode, passNote(p),
      e ? `Likely downloadable from ${e.label} by ~${fmtWhen(roundUp10(e.exp.median_at))} UTC (90%: ${e.exp.p90_at ? `${fmtWhen(roundUp10(e.exp.p90_at))} UTC` : "not yet known"}).` : "",
      `A planned acquisition from the agencies' plans; plans can change. ${location.href}`].filter(Boolean).join("\n");
    const what = passLabel(p, { short: true });
    return ["BEGIN:VEVENT", `UID:${p.key.replace(/[^\w.-]/g, "-")}@swathcast`, `DTSTAMP:${now}`, `DTSTART:${t(p.when)}`, `DTEND:${t(end)}`,
      `SUMMARY:${icsText(`${what} over AOI`)}`, c ? `GEO:${r5(c[1])};${r5(c[0])}` : null,
      `DESCRIPTION:${icsText(desc)}`, "TRANSP:TRANSPARENT", "END:VEVENT"].filter(Boolean).map(icsFold).join("\r\n");
  });
  const cal = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//swathcast//passes//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:swathcast passes", ...events, "END:VCALENDAR"].join("\r\n");
  saveFile(`swathcast-passes-${stamp()}.ics`, "text/calendar", `${cal}\r\n`);
}
function exportBar(n) {
  const bar = document.createElement("div");
  bar.className = "export-bar";
  if (!n) return bar;
  bar.append(Object.assign(document.createElement("span"), { className: "nm", textContent: `Save the ${n} planned:` }));
  [["CSV", exportCSV, "the passes below, as filtered, as a table"], ["Calendar (.ics)", exportICS, "one event per pass, for Google Calendar, Outlook or Apple Calendar"]].forEach(([label, fn, title]) => {
    const b = Object.assign(document.createElement("button"), { textContent: label, title });
    b.addEventListener("click", fn);
    bar.append(b);
  });
  return bar;
}

// ---------------------------------------------------------------- AOI
// fly: move the map to the AOI when it is out of view or too small to see
// (a point has no extent: it is shown with ~25 km around it). The geometry
// is checked first (aoi.js: ranges, rings, the antimeridian); nothing
// changes if it is not usable. Returns the check's note ("" or what was
// read in a way worth saying).
function setAOI(raw, { fly = false, refresh: doRefresh = true } = {}) {
  const { geometry, note } = checkAOI(raw);
  aoiLayer.clear();
  aoiLayer.add({ geojson: geometry, style: { color: css("--ink"), weight: 2.5, opacity: 1, fillOpacity: 0.03 } });
  state.aoi = geometry;
  state.aoiVersion++;
  state.listTab = "planned";
  syncOpacity();
  state.sel = null;
  view.closePopup();
  highlightLayer.clear();
  document.body.dataset.aoi = "1";
  if (fly) {
    let [w, s, e, n] = aoiBBox(geometry);
    const k = 0.25;
    if (e - w < k) { w -= k; e += k; }
    if (n - s < k) { s -= k; n += k; }
    try {
      if (!view.containsBBox([w, s, e, n]) || viewZoom() < 5) view.fitBBox([w, s, e, n], { pad: 0.3, maxZoom: 9 });
    } catch { /* the map cannot frame it (a pole): the list still works */ }
  }
  // the passes over it are what was asked for: bring the list into view, at "Now"
  scrollToNow = true;
  requestAnimationFrame(() => showAOIPart("smooth"));
  if (doRefresh) refresh();
  return note;
}
function clearAOI() {
  view.cancelDraw();
  aoiLayer.clear();
  highlightLayer.clear();
  view.closePopup();
  state.aoi = state.sel = state.recent = null;
  state.aoiVersion++;
  syncOpacity();
  delete document.body.dataset.aoi;
  refresh();
}
function clearActive() { document.querySelectorAll(".btns button").forEach((b) => b.classList.remove("active")); }
view.onDrawEnd = () => { clearActive(); document.getElementById("draw-hint").textContent = ""; };
function startDraw(kind, btn) {
  view.cancelDraw();
  document.getElementById("paste-box").classList.remove("show");
  btn.classList.add("active");
  document.getElementById("draw-hint").textContent = {
    point: "Click the map to drop a point.",
    box: "Click one corner, then the opposite corner.",
    polygon: "Click the vertices; click the first one, double-click or press Enter to finish (Esc cancels).",
  }[kind];
  view.draw(kind, (g) => {
    try { setAOI(g, { fly: kind === "point" }); } catch (err) { document.getElementById("draw-hint").textContent = `Could not use that shape: ${err.message}.`; }
  }, drawLayer);
}
document.getElementById("paste").addEventListener("click", () => {
  view.cancelDraw();
  document.getElementById("paste-box").classList.toggle("show");
});
document.getElementById("paste-apply").addEventListener("click", () => {
  const err = document.getElementById("paste-err");
  err.textContent = "";
  try {
    const { geometry, note } = parseAOI(document.getElementById("paste-text").value);
    setAOI(geometry, { fly: true });
    document.getElementById("paste-box").classList.remove("show");
    document.getElementById("draw-hint").textContent = note ? `AOI ${note}.` : "";
  } catch (ex) {
    err.textContent = `Could not read the AOI: ${ex.message}.`;
  }
});
document.getElementById("draw-point").addEventListener("click", (e) => startDraw("point", e.currentTarget));
document.getElementById("draw-box").addEventListener("click", (e) => startDraw("box", e.currentTarget));
document.getElementById("draw-poly").addEventListener("click", (e) => startDraw("polygon", e.currentTarget));
document.getElementById("clear-aoi").addEventListener("click", clearAOI);

// ---------------------------------------------------------------- what each satellite is imaging now
// Per platform, the intervals its plan has it imaging, from the loaded
// schedule: Sentinel-1/2 datatakes (begin-end), NISAR frames and Landsat
// scenes (one time each: imaging while within half a frame/scene of it; the
// frames and scenes of a pass abut). A platform whose plan is not loaded
// here (NISAR, Landsat until selected) answers null: unknown.
const HALF_SPAN_MS = { nisar: 18e3, landsat: 16e3 }; // frames ~35 s long, scenes ~30 s apart
let imagingIdx = { sig: null, by: new Map() };
function imagingIndex() {
  const sig = `${state.rows.length}|${state.wrs2 ? state.wrs2.length : 0}`;
  if (imagingIdx.sig === sig) return imagingIdx.by;
  const by = new Map();
  const add = (p, a, b, mode, label) => (by.get(p) || by.set(p, []).get(p)).push([a, b, mode, label]);
  state.rows.forEach((r) => {
    if (r.sensor === "sentinel-1") add(r.platform, r.begin.getTime(), r.end.getTime(), r.mode, `${r.mode}${r.polarization ? ` ${r.polarization}` : ""}`);
    else if (r.sensor === "sentinel-2") add(r.platform, r.begin.getTime(), r.end.getTime(), "MSI", r.mode === "NOBS" ? "nominal observation" : r.mode);
    else if (r.sensor === "nisar") { const t = r.begin.getTime(); add("NISAR", t - HALF_SPAN_MS.nisar, t + HALF_SPAN_MS.nisar, "L", r.polarization ? polText(r.polarization) : ""); }
  });
  (state.wrs2 || []).forEach((c) => c.times.forEach((x) => { const t = x.when.getTime(); add(x.platform, t - HALF_SPAN_MS.landsat, t + HALF_SPAN_MS.landsat, "OLI", "OLI/TIRS"); }));
  by.forEach((list) => list.sort((a, b) => a[0] - b[0]));
  imagingIdx = { sig, by };
  return by;
}
const planLoaded = (platform) => {
  const m = missionOf(platform);
  if (m === "nisar") return state.loadedSensors.has("nisar");
  if (m === "landsat") return Boolean(state.wrs2);
  return state.loadedSensors.has(MISSION[m].sensor);
};
function imagingAt(platform, t) {
  // no plan: MODIS, VIIRS and SLSTR image on every orbit (OLCI by day)
  if (WIDE_MISSION[platform]) return { mode: WIDE_MODE[WIDE_MISSION[platform]], label: WIDE_MISSION[platform] === "s3" ? "SLSTR, and OLCI in daylight" : "on every orbit" };
  if (!state.scheduleEnd || !planLoaded(platform)) return null;
  const list = imagingIndex().get(platform) || [];
  // the last interval starting at or before t (intervals do not nest)
  let lo = 0, hi = list.length - 1, k = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (list[mid][0] <= t) { k = mid; lo = mid + 1; } else hi = mid - 1; }
  for (let i = k; i >= 0 && i >= k - 3; i--) if (list[i][1] >= t) return { mode: list[i][2], label: list[i][3] };
  return false;
}

// ---------------------------------------------------------------- view options and satellites
const sats = new LiveSatellites(view, overlay, { colorOf: sensorColor, labelOf: (p) => PLATFORM_NAMES[p] || p, imaging: imagingAt });
window.__sats = sats; // for debugging and the browser tests
let satsOn = true;
try { satsOn = localStorage.getItem("s1lat.sats") !== "off"; } catch { /* storage unavailable */ }
// Globe and live satellites: toggles in the map options widget, by the basemap.
function optionPill(id, label, title) {
  const l = Object.assign(document.createElement("label"), { className: "check pill", title });
  l.append(Object.assign(document.createElement("input"), { type: "checkbox", id }), document.createTextNode(` ${label}`));
  view.optionsEl.append(l);
  return l.firstChild;
}
const globeBox = optionPill("opt-globe", "Globe", "A globe (on) or a flat Web Mercator map (off)");
const satsBox = optionPill("opt-sats", "Live satellites", "Where each selected satellite is now, at its altitude, with what its instrument sees");
globeBox.checked = globeOn;
satsBox.checked = satsOn;
globeBox.addEventListener("change", () => {
  view.setGlobe(globeBox.checked);
  overlay.redraw();
  try { localStorage.setItem("s1lat.globe", globeBox.checked ? "globe" : "flat"); } catch { /* ignore */ }
});
satsBox.addEventListener("change", () => {
  if (satsBox.checked) { syncSatellites(); sats.start(); } else sats.stop();
  try { localStorage.setItem("s1lat.sats", satsBox.checked ? "on" : "off"); } catch { /* ignore */ }
});
// Satellites follow the selection: selected missions' ticked platforms.
function syncSatellites() {
  sats.setPlatforms(new Set(sats.sats.map((x) => x.platform).filter((p) => state.missions.has(missionOf(p)) && platformOn(p))));
}
async function loadSatellites() {
  await view.ready;
  try {
    const n = await sats.load("./data/tle.json", new Set([...state.allPlatforms]));
    if (!n) throw new Error("no orbital elements for the scheduled platforms");
    syncSatellites();
    if (satsBox.checked) sats.start();
  } catch (err) {
    satsBox.disabled = true;
    satsBox.parentElement.title = `Live positions unavailable: ${err.message}`;
  }
}

// The floating panel can be hidden (on phones it folds to a bottom sheet).
// It opens on every visit: the filters and the lists live there, so a panel
// hidden once must not hide them on the next visit.
const panelToggle = (closed) => {
  document.body.classList.toggle("panel-closed", closed);
  const btn = document.getElementById("panel-close");
  btn.textContent = closed ? (window.innerWidth <= 760 ? "⌃" : "⟩") : (window.innerWidth <= 760 ? "⌄" : "⟨");
  btn.title = btn.ariaLabel = closed ? "Show the panel" : "Hide the panel";
  setCameraPadding();
};
// Fit and centre on the part of the map the panel does not cover.
function setCameraPadding() {
  const panel = document.getElementById("sidebar"), closed = document.body.classList.contains("panel-closed");
  const phone = window.innerWidth <= 760;
  const pad = { top: 0, right: 0, bottom: 0, left: 0 };
  if (!closed && phone) pad.bottom = panel.offsetHeight;
  if (!closed && !phone) pad.left = panel.offsetWidth + 12;
  map.setPadding(pad);
}
window.addEventListener("resize", () => setCameraPadding());
document.getElementById("panel-close").addEventListener("click", () => panelToggle(!document.body.classList.contains("panel-closed")));
document.getElementById("panel-open").addEventListener("click", () => panelToggle(false));
try { ["s1lat.panel", "swathcast.wide"].forEach((k) => localStorage.removeItem(k)); } catch { /* storage unavailable */ }
panelToggle(false);

// Times stay current while the page is open: every minute the list and the
// timeline are redrawn ("in 2.1 h"), every 10 minutes the footprints are
// rebuilt (their colours, and passes now past move to the recent side).
let lastRebuild = Date.now();
setInterval(() => {
  // not while someone is using the list (a rebuilt list would lose their place)
  if (document.hidden || !state.scheduleEnd || document.getElementById("results").contains(document.activeElement)) return;
  if (Date.now() - lastRebuild > 10 * 60e3) {
    lastRebuild = Date.now();
    Object.keys(built).forEach((k) => { built[k] = null; });
    refresh();
  } else renderList();
}, 60e3);

// Phones: the legend folds to its ramp; this button opens the rest.
document.getElementById("legend-fold").addEventListener("click", (e) => {
  const open = !document.body.classList.contains("legend-open");
  document.body.classList.toggle("legend-open", open);
  e.currentTarget.setAttribute("aria-expanded", String(open));
});

// ---------------------------------------------------------------- tour
// A first visit (no link state in the address) gets a six-step tour, one
// idea per step: missions, dates, a footprint, an AOI (Paradise, on Mt.
// Rainier), its passes, then the Delivery page. What it does, it does with a
// visible red pointer (tour.click), so each change has a cause on screen.
// Its end starts over.
const PARADISE = [-121.7357, 46.7861];
// Mt. Rainier: its summit and points around the massif, with a margin, all
// of which a previewed scene must cover (not cut off at an edge)
const SUMMIT = [-121.7603, 46.8523];
const RAINIER = [SUMMIT, ...[[-121.93, 46.75], [-121.59, 46.75], [-121.59, 46.96], [-121.93, 46.96], [-121.93, 46.8523], [-121.59, 46.8523], [-121.7603, 46.75], [-121.7603, 46.96]]];
const visible = (sel) => { const el = document.querySelector(sel); return el && el.getClientRects().length ? el : null; };
const settled = () => state.scheduleEnd && !state.busy;
const searched = () => settled() && state.recent && !Object.values(state.recent.results).some((r) => r.status === "loading");
const onScreen = (ll) => { const p = map.project(ll), r = map.getCanvas().getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; };
const mapClick = (ll) => map.fire("click", { point: map.project(ll), lngLat: maplibregl.LngLat.convert(ll), originalEvent: null });
const atParadise = () => state.aoi && state.aoi.type === "Point" && state.aoi.coordinates[0] === PARADISE[0];
async function mapSettles() {
  await new Promise((r) => { if (!map.isMoving()) r(); else map.once("moveend", r); });
  await sleep(400); // the refresh after a move starts 150 ms after it ends
  await waitFor(settled, 20000);
  await new Promise((r) => { if (map.loaded()) r(); else map.once("idle", r); });
}
function noAOI() { if (state.aoi) clearAOI(); document.getElementById("sidebar").scrollTop = 0; }
// Every mission ticked, one visible click each.
// Steps 1-3 show Landsat 8/9 alone: Sentinel-1 (the default) unticked, Landsat ticked.
async function landsatOnly(current) {
  for (const input of document.querySelectorAll(".mission-row label.mission input")) {
    if (!current()) return;
    const name = input.closest("label").textContent.trim(), want = name.startsWith("Landsat");
    if (input.checked !== want) await tour.click(input, `${want ? "Ticking" : "Unticking"} ${name}`);
  }
}
// The missions with plans (Sentinel-1/2, Landsat, NISAR); MODIS, VIIRS and
// Sentinel-3 image every place daily and would fill the list.
const TOUR_MISSIONS = ["s1", "s2", "landsat", "nisar"];
async function tickAll(current) {
  for (const block of document.querySelectorAll(".mission-block")) {
    if (!current()) return;
    const input = block.querySelector(".mission-row label.mission input"), name = input.closest("label").textContent.trim();
    const want = TOUR_MISSIONS.some((k) => MISSION[k].label === name);
    if (input.checked !== want) await tour.click(input, `${want ? "Ticking" : "Unticking"} ${name}`);
  }
}
// Preview buttons tried in order until a scene shows all of Mt. Rainier
// (data at each of its points); one that does not is taken off again.
async function previewRainier(buttons, current) {
  for (const btn of buttons.slice(0, 5)) {
    if (!current() || !btn.isConnected) return;
    await tour.click(btn, "Adding this scene to the map");
    const q = queue.find((x) => x.key === btn.dataset.browse);
    await waitFor(() => !current() || !q || q.status !== "loading", 30000);
    if (!current() || !q) return;
    if (previewCovers(q, RAINIER)) { tour.say("All of Mt. Rainier is in this one"); return true; }
    tour.say("Mt. Rainier is cut off here: trying another");
    await sleep(900);
    removePreview(q);
  }
  tour.say("");
  return false;
}
// the chosen scene shown: the map on Mt. Rainier
async function showRainier(current) {
  await sleep(1800);
  if (!current()) return;
  tour.say("");
  view.closePopup();
  map.flyTo({ center: SUMMIT, zoom: 9, duration: 1600 });
}
const cloudOf = (text) => { const m = /cloud (\d+)%/.exec(text); return m ? Number(m[1]) : null; };
// clear optical scenes first (least cloud), then radar (newest first), then the rest
function rankScenes(list) {
  const rank = ({ cloud, covers }) => (covers === false ? 3 : cloud == null ? 1 : cloud <= 20 ? 0 : 2);
  return list.map((x, i) => ({ ...x, i })).sort((a, b) => rank(a) - rank(b) || (rank(a) === 0 ? a.cloud - b.cloud : a.i - b.i)).map((x) => x.btn);
}
const popupScenes = () => rankScenes([...document.querySelectorAll(".maplibregl-popup tr")].filter((tr) => tr.querySelector("button[data-browse]"))
  .map((tr) => ({ btn: tr.querySelector("button[data-browse]"), cloud: cloudOf(tr.textContent) })));
function listScenes() {
  const items = new Map(listItems().map((it) => [it.key, it]));
  return rankScenes([...document.querySelectorAll("#results tr.item.past")].filter((tr) => tr.querySelector(".row-prev")).map((tr) => {
    const it = items.get(tr.dataset.key), shapes = it ? recentShapes(it.ref) : [];
    const clouds = it ? it.ref.products.map((x) => x.cloud).filter((c) => c != null) : [];
    return {
      btn: tr.querySelector(".row-prev"), cloud: clouds.length ? Math.min(...clouds) : null,
      covers: shapes.length ? RAINIER.every((p) => shapes.some((g) => isArea(g) && turf.booleanPointInPolygon(p, g))) : undefined,
    };
  }));
}
const tour = new Tour([
  {
    title: "Choose missions",
    body: "<p>Select the missions of interest, here we select <b>Landsat 8/9</b>. <b>Filters ▾</b> narrows each one: mode, direction, platform.</p>",
    marks: [{ target: "#sec-missions", label: "Missions + filters", side: "right" }],
    dirty: true,
    enter: async (current) => { clearPreviews(); noAOI(); view.closePopup(); await sleep(600); await landsatOnly(current); },
  },
  {
    title: "Pick your dates",
    body: "<p>Each footprint on the map is coloured by the <b>time until its next pass</b> in these dates.</p>",
    marks: [{ target: "#sec-dates", label: "Date range", side: "right" }, { target: "#map-legend", label: "Colour = time to next pass", side: "top" }],
    enter: (current) => { clearPreviews(); noAOI(); view.closePopup(); return landsatOnly(current); },
  },
  {
    title: "Click a footprint",
    body: "<p>Any footprint opens its <b>plan</b> and its <b>last 90 days</b>. <b>Preview on map</b> lays a scene's image on the map.</p>",
    marks: [
      { target: () => visible(".maplibregl-popup:not(.label-pop) .maplibregl-popup-content"), label: "Plan + last 90 days", side: "top" },
      { target: () => visible("#browse-queue"), label: "Scenes on the map", side: "top" },
    ],
    place: "dock", dirty: true,
    busy: "Opening a footprint at Mt. Rainier…",
    enter: async (current) => {
      clearPreviews();
      noAOI();
      view.closePopup();
      await landsatOnly(current);
      if (!current()) return;
      map.flyTo({ center: PARADISE, zoom: 6.6, duration: 1600 }); // a whole Landsat scene, and room for its popup beside it
      await mapSettles();
      if (!current()) return;
      await tour.tap(onScreen(PARADISE), "Clicking the footprint at Mt. Rainier", () => mapClick(PARADISE));
      if (!(await waitFor(() => !current() || visible(".maplibregl-popup [data-browse]"), 20000)) || !current()) return;
      tour.say("");
      await sleep(1500); // a moment to see the popup before anything is added
      if (await previewRainier(popupScenes(), current)) await showRainier(current);
    },
  },
  {
    title: "Analyse an area",
    body: "<p>With <b>the planned missions</b> ticked, drop a <b>point</b> (or draw a box or polygon, or paste one). Here: <b>Paradise, Mt. Rainier</b>.</p>",
    marks: [{ target: ".aoi-head", label: "Your area of interest", side: "right" }],
    dirty: true,
    enter: async (current) => {
      clearPreviews();
      view.closePopup();
      await tickAll(current);
      if (!current() || atParadise()) return;
      if (!view.containsBBox([PARADISE[0] - 0.05, PARADISE[1] - 0.05, PARADISE[0] + 0.05, PARADISE[1] + 0.05])) {
        map.flyTo({ center: PARADISE, zoom: 7.5, duration: 1200 });
        await mapSettles();
      }
      if (!current()) return;
      await tour.click(document.getElementById("draw-point"), "Choosing Point");
      if (!current()) return;
      await tour.tap(onScreen(PARADISE), "Dropping a point at Paradise", () => mapClick(PARADISE));
      tour.say("");
      map.easeTo({ center: PARADISE, zoom: 6.6, duration: 1200 }); // a little way out: the passes' footprints in view
    },
  },
  {
    title: "Every pass over it",
    body: "<p>Two tabs: <b>Planned</b> passes say when the data is <b>likely downloadable</b>; the <b>last 90 days</b> have <b>Preview</b>. <b>Click a marker on the timeline</b> to select its pass.</p>",
    marks: [{ target: "#list-tabs", label: "Planned or last 90 days", side: "right" }, { target: () => visible("#aoi-timeline"), label: "Click a marker to select it", side: "top" }],
    dirty: true,
    busy: "Finding the passes…",
    enter: async (current) => {
      clearPreviews();
      if (TOUR_MISSIONS.some((k) => !state.missions.has(k))) await tickAll(current);
      if (!current()) return;
      if (!atParadise()) { view.closePopup(); setAOI({ type: "Point", coordinates: PARADISE }, { fly: true }); }
      showAOIPart();
      await waitFor(() => !current() || searched(), 30000);
      if (!current()) return;
      await sleep(800);
      // the past: its tab, then a scene of it on the map
      if (state.listTab !== "past") await tour.click(document.querySelector('#list-tabs [data-tab="past"]'), "Opening the last 90 days");
      if (!current()) return;
      if (await previewRainier(listScenes(), current)) await showRainier(current);
      if (!current()) return;
      await sleep(3000); // the scene on the map, before the timeline
      // a planned pass picked on the timeline: selected in the list, with its delivery
      const mk = [...document.querySelectorAll("#aoi-timeline .mk")].find((m) => !m.dataset.key.startsWith("r|"));
      if (mk) await tour.click(mk, "Clicking a planned pass on the timeline");
      tour.say("");
    },
    next: {
      label: "Next: Delivery →",
      go: async () => {
        const tab = document.querySelector('header nav a[href="dashboard.html"]');
        // the tab "clicked" visibly, then the tour handed over to that page
        await tour.pointAt(tab, "Switching to the Delivery tab");
        tour.ripple();
        flash(tab);
        await sleep(700);
        tour.close();
        handOver(6, "dashboard.html");
      },
    },
  },
], { onEnd: startOver });
async function startTour(step = 1) {
  markTourSeen();
  // the footprints at 30 %, the ground under them in sight
  opacityChosen = null;
  try { localStorage.removeItem("swathcast.opacity"); } catch { /* storage unavailable */ }
  syncOpacity();
  drawCells();
  await waitFor(settled, 30000);
  tour.show(step - 1);
}
document.getElementById("tour-btn").addEventListener("click", () => startTour(1));

legend();
tzReady.then(async () => { await view.ready; await load(); })
  .then(() => {
    const step = pendingStep();
    if (step) { tour.dirty = true; startTour(Math.min(step, 5)); }
    else if (!tourSeen() && location.hash.length <= 1) startTour(1);
  })
  .then(loadSatellites)
  .catch((err) => { document.getElementById("fresh").textContent = `Failed to load pass data: ${err.message}`; });
