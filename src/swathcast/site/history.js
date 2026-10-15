// Recent acquisitions of one footprint, read live from the catalogues (no
// estimates): each item is a product that exists. Every query is CORS-open:
//   Sentinel-1 burst   ASF Search, SLC-BURST by full burst ID (per polarization, grouped)
//   Sentinel-1 EW/SM   ASF Search, Level-0 (RAW) of the track over the swath centre
//   Sentinel-2 tile    Earth Search sentinel-2-l1c by MGRS tile (tile sensing time, cloud)
//   Landsat scene      Earth Search landsat-c2-l2 by WRS-2 path/row (USGS L2, days behind),
//                      merged with USGS's Level-1 list (this site's landsat_l1 files)
//   NISAR frame        NASA CMR, ASF L1 RSLC by track and frame (L-band), with each one's GCOV
//                      (for its preview); S-band: this site's nisar_s files (the poller's
//                      Bhoonidhi listing: its search needs a login)
//   MODIS/VIIRS tile   NASA CMR, LAADS Level-1B granules over the tile centre (one per pass)
//   Sentinel-3 tile    CDSE, OLCI EFR and SLSTR RBT products over the tile centre (one per orbit)
// Each acquisition carries links to its product (a viewer a person can use:
// ASF Vertex, STAC Browser, Earthdata Search) and what fetchBrowse() needs to
// find a browse image of it (see there).
import { ASF, CMR, ES as EARTH_SEARCH, ODATA, PC as PC_STAC, getJSON, iso, plat as platName } from "./catalogues.js";
import { sunElevation } from "./common.js";
import { gcovGrid } from "./drape.js";

const NISAR_RSLC = "C2853145197-ASF", NISAR_GCOV = "C2854338529-ASF";
const vertex = (scene) => `https://search.asf.alaska.edu/#/?searchType=List%20Search&searchList=${encodeURIComponent(scene)}`;
const stacBrowser = (collection, id) => `https://radiantearth.github.io/stac-browser/#/external/earth-search.aws.element84.com/v1/collections/${collection}/items/${encodeURIComponent(id)}`;
const earthdata = (collection, granule) => `https://search.earthdata.nasa.gov/search/granules?p=${collection}&pg[0][id]=${encodeURIComponent(granule)}`;
export const HISTORY_DAYS = 90;

const cache = new Map();
const plat = (p) => platName(p) || p; // an unknown platform is shown as the catalogue names it
// co-polarized channel first, as products are named: VV+VH, HH+HV
const POL_RANK = { HH: 0, VV: 1, HV: 2, VH: 3 };


// one acquisition per (platform, start minute): ASF lists VV and VH bursts separately
function groupBy(items, key) {
  const out = new Map();
  items.forEach((it) => {
    const k = key(it);
    const prev = out.get(k);
    if (!prev) out.set(k, { ...it, pols: new Set(it.pol ? [it.pol] : []) });
    else if (it.pol) prev.pols.add(it.pol);
  });
  return [...out.values()].map(({ pols, ...it }) => ({ ...it, pol: [...pols].sort((a, b) => (POL_RANK[a] ?? 9) - (POL_RANK[b] ?? 9)).join("+") || it.pol || null }));
}

async function s1Burst(c, since) {
  const id = `${String(c.burst.track).padStart(3, "0")}_${String(c.burst.bid).padStart(6, "0")}_${c.burst.swath}`;
  const q = new URLSearchParams({ dataset: "SLC-BURST", fullBurstID: id, start: iso(since), maxResults: "500", output: "geojson" });
  const d = await getJSON(`${ASF}?${q}`);
  const items = (d.features || []).map(({ properties: p }) => {
    // the burst's own SLC: .../S1C_IW_SLC__1SDV_20261001T020114_..._9AE6/IW2/VH/6.tiff
    const slc = (/\/(S1[A-D]_IW_SLC__\w+?)\//.exec(p.url || "") || [])[1];
    return {
      when: new Date(p.startTime), platform: plat(p.platform), direction: (p.flightDirection || "").toLowerCase(),
      track: p.pathNumber, pol: p.polarization, orbit: p.orbit, id: slc || p.sceneName,
      links: slc ? [{ href: vertex(slc), text: "ASF Vertex", title: `its SLC, ${slc}` }] : [],
      browse: { kind: "s1", platform: p.platform, when: p.startTime, point: [p.centerLon, p.centerLat] },
    };
  });
  return {
    source: `ASF burst catalogue (${id}, IW SLC bursts)`,
    items: groupBy(items, (x) => `${x.platform}|${Math.round(x.when / 6e4)}`),
  };
}

async function s1Swath(c, since) {
  const r = c.times[0] && c.times[0].pass;
  if (!r) return { source: "ASF", items: [], note: "No track for this swath." };
  const [lon, lat] = c.center;
  const q = new URLSearchParams({
    // ASF names stripmap by its beam, S1..S6
    platform: "SENTINEL-1", beamMode: r.mode === "SM" ? "S1,S2,S3,S4,S5,S6" : r.mode, processingLevel: "RAW", relativeOrbit: String(r.track),
    intersectsWith: `POINT(${lon.toFixed(4)} ${lat.toFixed(4)})`, start: iso(since), maxResults: "500", output: "geojson",
  });
  const d = await getJSON(`${ASF}?${q}`);
  const items = (d.features || []).map(({ properties: p }) => ({
    when: new Date(p.startTime), platform: plat(p.platform), direction: (p.flightDirection || "").toLowerCase(),
    track: p.pathNumber, pol: p.polarization, id: p.sceneName, orbit: p.orbit,
    links: [{ href: vertex(p.sceneName), text: "ASF Vertex", title: p.sceneName }],
    browse: { kind: "s1", platform: p.platform, when: p.startTime, point: [lon, lat], mode: r.mode },
  }));
  // a pass can be several consecutive Level-0 products: one per (platform, orbit)
  const grouped = new Map();
  items.sort((x, y) => x.when - y.when).forEach((x) => { const k = `${x.platform}|${x.orbit ?? Math.round(x.when / 6e5)}`; if (!grouped.has(k)) grouped.set(k, x); });
  return { source: `ASF Sentinel-1 Level-0, ${r.mode} track ${r.track} over the swath centre`, items: [...grouped.values()] };
}

async function stac(collections, query, since) {
  const body = { collections, query, datetime: `${iso(since)}/..`, sortby: [{ field: "properties.datetime", direction: "desc" }], limit: 200 };
  return getJSON(EARTH_SEARCH, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function s2Tile(c, since) {
  // Earth Search writes one-digit UTM zones without the zero: MGRS-9TWM
  const d = await stac(["sentinel-2-l1c"], { "grid:code": { eq: `MGRS-${c.tile.replace(/^0/, "")}` } }, since);
  // a datatake reprocessed is two products of one acquisition: keep one
  const seen = new Map();
  (d.features || []).forEach((f) => {
    const p = f.properties, uri = p["s2:product_uri"] || f.id;
    const key = uri.split("_").slice(0, 3).join("_"); // S2B_MSIL1C_20260928T190059
    if (!seen.has(key)) {
      const orbit = /_R(\d{3})_/.exec(uri); // S2B_MSIL1C_20260928T190059_N0511_R013_T10TET_...
      seen.set(key, {
        when: new Date(p.datetime), platform: plat(p.platform), track: p["sat:relative_orbit"] ?? (orbit ? Number(orbit[1]) : null),
        direction: "descending", cloud: p["eo:cloud_cover"], id: uri.replace(/\.SAFE$/, ""),
        links: [{ href: stacBrowser("sentinel-2-l1c", f.id), text: "STAC Browser", title: f.id }],
        browse: { kind: "s2", id: f.id },
      });
    }
  });
  return { source: `Earth Search, Sentinel-2 L1C tile ${c.tile} (tile sensing time)`, items: [...seen.values()] };
}

// USGS Level-1 scenes of the last 90 days, from this site's per-path files
// (landsat_l1/<path>.parquet, published by the hourly poller, which lists
// USGS's STAC: browsers cannot search it themselves). null if the site has
// no such file (not published, or no scene on that path).
const l1Files = new Map();
export function landsatL1(path) {
  const key = String(path).padStart(3, "0");
  if (!l1Files.has(key)) {
    const p = fetch(`./data/landsat_l1/${key}.parquet`)
      .then((r) => (r.ok ? r.arrayBuffer() : null))
      .then(async (buf) => {
        if (!buf) return null;
        const [{ parquetReadObjects }, { compressors }] = await Promise.all([
          import("https://cdn.jsdelivr.net/npm/hyparquet@1.16.0/+esm"),
          import("https://cdn.jsdelivr.net/npm/hyparquet-compressors@1.1.1/+esm"),
        ]);
        const rows = await parquetReadObjects({ file: buf, compressors, utf8: false });
        return rows.map((r) => ({
          path: Number(r.path), row: Number(r.row), when: new Date(typeof r.time === "bigint" ? Number(r.time) : r.time),
          id: String(r.id), platform: String(r.platform), category: String(r.category || ""),
        }));
      })
      .catch(() => null);
    l1Files.set(key, p);
  }
  return l1Files.get(key);
}
export const usgsItem = (id) => `https://landsatlook.usgs.gov/stac-server/collections/landsat-c2l1/items/${encodeURIComponent(id)}`;
// One scene, whatever its level: sensor, path/row and date ("LC09_047027_20260916").
export const sceneKey = (id) => { const p = String(id).split("_"); return `${p[0]}_${p[2]}_${p[3]}`; };

// A scene's acquisitions: Earth Search's Level-2 copies (cloud cover, a
// preview) merged with USGS's Level-1 list (every scene, night ones too,
// and the newest, whose Level-2 is not made yet).
async function landsatScene(c, since) {
  const pad = (n) => String(n).padStart(3, "0");
  const [d, l1] = await Promise.all([
    stac(["landsat-c2-l2"], { "landsat:wrs_path": { eq: pad(c.track) }, "landsat:wrs_row": { eq: pad(c.frame) } }, since),
    landsatL1(c.track).then((x) => x || []),
  ]);
  const items = (d.features || []).map((f) => ({
    when: new Date(f.properties.datetime), platform: plat(f.properties.platform), track: c.track,
    cloud: f.properties["eo:cloud_cover"], id: f.id, direction: c.direction, level: "L1 + L2",
    links: [{ href: stacBrowser("landsat-c2-l2", f.id), text: "STAC Browser", title: f.id }],
    browse: { kind: "landsat", id: f.id },
  }));
  const have = new Set(items.map((x) => sceneKey(x.id)));
  const [lon, lat] = c.center || [0, 0];
  l1.filter((s) => s.row === c.frame && s.when >= since && !have.has(sceneKey(s.id))).forEach((s) => {
    items.push({
      when: s.when, platform: s.platform, track: c.track, direction: c.direction, id: s.id,
      level: "L1 only", night: sunElevation(s.when, lon, lat) <= 0,
      links: [{ href: usgsItem(s.id), text: "USGS STAC", title: s.id }, { href: "https://earthexplorer.usgs.gov/", text: "EarthExplorer", title: "search it there (needs a login to download)" }],
    });
  });
  const note = l1.length
    ? "Level-1 from USGS (this site's hourly listing), Level-2 copies from Earth Search: night scenes and the newest have Level-1 only."
    : "Level-2 scenes appear days after USGS's Level-1, so the newest acquisitions can be missing here; night scenes have no Level-2.";
  return { source: `USGS Landsat Level-1 and Earth Search Level-2, path ${c.track} row ${c.frame}`, items, note };
}

// A set of channels as the plan writes them: ["HH", "HV", "HH", "HV"] -> "HH+HV".
const CHANNEL_SETS = ["HH", "VV", "HH+HV", "VV+VH", "HH+VV", "HH+HV+VH+VV"];
function nisarChannels(channels) {
  const set = new Set(channels);
  if (!set.size) return null;
  const named = CHANNEL_SETS.find((p) => p.split("+").length === set.size && p.split("+").every((x) => set.has(x)));
  return named || [...set].sort((a, b) => (POL_RANK[a] ?? 9) - (POL_RANK[b] ?? 9)).join("+");
}

// One acquisition's RSLC and GCOV share cycle, track, direction, frame and
// start (to the minute): NISAR_L1_PR_RSLC_033_011_D_094_4005_DHDH_A_20261012T233338_...
const nisarKey = (name) => { const m = /_(\d{3}_\d{3}_[AD]_\d{3})_\d{4}_\w{4}_[AB]_(\d{8}T\d{4})/.exec(name); return m ? `${m[1]}_${m[2]}` : null; };
const visuals = (u) => (u.RelatedUrls || []).filter((x) => x.Type === "GET RELATED VISUALIZATION" && /^https:/.test(x.URL)).map((x) => x.URL);

// NISAR S-band acquisitions of the last 90 days on one track, from this site's
// per-track files (nisar_s/<track>.parquet, from the poller's Bhoonidhi
// listing). null if the site has no such file.
const sFiles = new Map();
export function nisarS(track) {
  const key = String(track).padStart(3, "0");
  if (!sFiles.has(key)) {
    const p = fetch(`./data/nisar_s/${key}.parquet`)
      .then((r) => (r.ok ? r.arrayBuffer() : null))
      .then(async (buf) => {
        if (!buf) return null;
        const [{ parquetReadObjects }, { compressors }] = await Promise.all([
          import("https://cdn.jsdelivr.net/npm/hyparquet@1.16.0/+esm"),
          import("https://cdn.jsdelivr.net/npm/hyparquet-compressors@1.1.1/+esm"),
        ]);
        const rows = await parquetReadObjects({ file: buf, compressors, utf8: false });
        return rows.map((r) => ({
          track: Number(r.track), frame: Number(r.frame), direction: String(r.direction || ""), when: new Date(typeof r.time === "bigint" ? Number(r.time) : r.time),
          id: String(r.id), pol: r.pol ? String(r.pol) : null, partial: Boolean(r.partial), browse: r.browse ? String(r.browse) : null,
          corners: quadOf(r.corners), grid: quadOf(r.grid),
        }));
      })
      .catch(() => null);
    sFiles.set(key, p);
  }
  return sFiles.get(key);
}
const BHOONIDHI = "https://bhoonidhi.nrsc.gov.in/";
// "lon,lat,..." (8 numbers: four corners from the top-left, clockwise) as
// [[lon, lat] x 4], or null
function quadOf(v) {
  const n = v == null ? [] : String(v).split(",").map(Number);
  return n.length === 8 && n.every(Number.isFinite) ? [0, 2, 4, 6].map((i) => [n[i], n[i + 1]]) : null;
}

async function nisarFrame(c, since) {
  const query = (coll) => new URLSearchParams([
    ["collection_concept_id", coll], ["attribute[]", `int,TRACK_NUMBER,${c.track}`], ["attribute[]", `int,FRAME_NUMBER,${c.frame}`],
    ["temporal[]", `${iso(since)},`], ["sort_key", "-start_date"], ["page_size", "200"],
  ]);
  const [d, gc, sband] = await Promise.all([
    getJSON(`${CMR}?${query(NISAR_RSLC)}`),
    getJSON(`${CMR}?${query(NISAR_GCOV)}`).catch(() => ({ items: [] })),
    nisarS(c.track),
  ]);
  // each RSLC's GCOV (made for every RSLC, ~1 h later): its browse images are
  // geocoded and come one per channel
  const gcov = new Map((gc.items || []).map(({ umm: u }) => [nisarKey(u.GranuleUR), u]));
  const items = (d.items || []).map(({ umm: u }) => {
    const a = Object.fromEntries((u.AdditionalAttributes || []).map((x) => [x.Name, x.Values]));
    // frequency A (main band) and B (5 MHz side band): their channels together,
    // named as the plan names the mode (A HH + B VV is quasi-dual HH+VV)
    const pol = nisarChannels([...(a.FREQUENCY_A_POLARIZATION || []), ...(a.FREQUENCY_B_POLARIZATION || [])]);
    return {
      when: new Date(u.TemporalExtent.RangeDateTime.BeginningDateTime), platform: "NISAR", track: c.track,
      direction: (a.ASCENDING_DESCENDING || [""])[0].toLowerCase(), pol: pol ? `L ${pol}` : null,
      bandwidth: (a.RANGE_BANDWIDTH_CONCAT || [])[0] || null,
      partial: (a.FULL_FRAME || [""])[0] === "FALSE", id: u.GranuleUR,
      links: [{ href: earthdata(NISAR_RSLC, u.GranuleUR), text: "Earthdata Search", title: u.GranuleUR }],
      browse: nisarBrowse(u, gcov.get(nisarKey(u.GranuleUR))),
    };
  });
  const s = (sband || []).filter((r) => r.frame === c.frame && r.when >= since).map((r) => ({
    when: r.when, platform: "NISAR", track: r.track, direction: r.direction, pol: r.pol ? `S ${r.pol}` : "S", partial: r.partial, id: r.id,
    links: [{ href: BHOONIDHI, text: "Bhoonidhi", title: `${r.id} (search there; needs a login)` }],
    browse: r.browse ? { kind: "nisar_s", url: r.browse, corners: r.corners, grid: r.grid } : null,
  }));
  return {
    source: `NASA CMR, ASF NISAR L1 RSLC, track ${c.track} frame ${c.frame}${sband ? "; ISRO Bhoonidhi S-band RSLC (this site's hourly listing)" : ""}`,
    items: [...items, ...s],
    note: sband
      ? "S-band from ISRO's Bhoonidhi, listed hourly by this site (its search needs a login); previews are Bhoonidhi's thumbnails, read through the wsrv.nl image proxy (Bhoonidhi serves them without CORS)."
      : "L-band only: S-band products are at ISRO's Bhoonidhi, whose search needs a login.",
  };
}

// MODIS/VIIRS: the LAADS Level-1B granules (5-minute MODIS, 6-minute VIIRS,
// day and night) whose footprint holds the tile centre, one acquisition per
// pass (consecutive granules of a pass merged), with the view zenith angle
// at the centre from the granule's own geometry not being in CMR: the pass
// is listed with its day/night flag. Preview: NASA Worldview's (GIBS) true
// colour of that platform and day over the tile.
export const WIDE_L1B = {
  Terra: "C3861664481-LAADS", Aqua: "C3861713185-LAADS", // MOD021KM/MYD021KM collection 7
  SNPP: "C2105087643-LAADS", N20: "C2103811746-LAADS", N21: "C3478403600-LAADS",
};
// and LANCE's near-real-time copies (kept 7 days), for the newest passes
const WIDE_NRT = {
  Terra: "C1426414410-LANCEMODIS", Aqua: "C1426616847-LANCEMODIS",
  SNPP: "C2185497928-LANCEMODIS", N20: "C2208778455-LANCEMODIS", N21: "C3383723948-LANCEMODIS",
};
const WIDE_PLATS = { modis: ["Terra", "Aqua"], viirs: ["SNPP", "N20", "N21"] };
async function wideTile(c, since) {
  const [lon, lat] = c.center;
  const plats = WIDE_PLATS[c.mission];
  const q = new URLSearchParams([
    ...plats.flatMap((p) => [["collection_concept_id", WIDE_L1B[p]], ["collection_concept_id", WIDE_NRT[p]]]), ["point", `${lon.toFixed(4)},${lat.toFixed(4)}`],
    ["temporal[]", `${iso(since)},`], ["sort_key", "-start_date"], ["page_size", "2000"],
  ]);
  const d = await getJSON(`${CMR}?${q}`);
  const raw = (d.items || []).map(({ umm: u }) => {
    const platform = (u.Platforms || []).map((x) => platName(x.ShortName)).find(Boolean) || null;
    const name = ((u.DataGranule || {}).Identifiers || []).map((x) => x.Identifier)[0] || u.GranuleUR;
    const flag = (u.DataGranule || {}).DayNightFlag || "";
    const when = new Date(u.TemporalExtent.RangeDateTime.BeginningDateTime);
    const nrt = /NRT/.test(name);
    return {
      when, platform, id: name, nrt, level: nrt ? "NRT" : null, light: flag === "Night" ? "night" : flag === "Day" ? "day" : null,
      links: [{ href: earthdata(nrt ? WIDE_NRT[platform] : WIDE_L1B[platform] || "", name), text: "Earthdata Search", title: name }],
      browse: platform ? { kind: "gibs", platform, date: when.toISOString().slice(0, 10), bbox: c.bbox, clip: c.geojson, ...(flag === "Night" ? { night: true } : {}) } : null,
    };
  }).sort((a, b) => a.when - b.when || a.nrt - b.nrt); // a granule's standard product before its NRT copy
  // one pass: granules of one platform less than 15 min apart
  const items = [];
  raw.forEach((x) => {
    const prev = items.find((y) => y.platform === x.platform && x.when - y.last <= 15 * 60e3);
    if (prev) {
      prev.last = Math.max(prev.last, x.when);
      prev.starts.push(x.when);
      if (x.light === "day") prev.light = "day";
      if (!x.nrt) prev.level = null; // the standard product is there too
      if (x.browse && (!prev.browse || (prev.browse.night && !x.browse.night))) prev.browse = x.browse; // by day if it has a day granule
    } else items.push({ ...x, last: x.when, starts: [x.when] });
  });
  // the pass's own granules where Worldview has them (and those either side:
  // only the ones over the tile centre are listed)
  items.forEach((x) => {
    if (x.browse && !x.browse.night && GIBS_GRANULE[x.platform]) x.browse = { ...x.browse, granules: granuleTimes(x.starts) };
  });
  return { source: `NASA CMR, LAADS Level-1B granules over the tile centre (${plats.join(", ")}), and LANCE's near-real-time ones of the last 7 days`, items: items.map(({ last, nrt, starts, ...x }) => x),
    note: "Times are each pass's first granule over the tile centre (granules are 5-6 min long)." };
}

// Sentinel-3: the OLCI EFR and SLSTR RBT products at CDSE (near-real time
// and non-time-critical) whose footprint holds the tile centre, one
// acquisition per platform and orbit (cycle and relative orbit in the name),
// with its relative orbit and frame.
async function s3Tile(c, since) {
  const [lon, lat] = c.center;
  const filter = `Collection/Name eq 'SENTINEL-3' and OData.CSC.Intersects(area=geography'SRID=4326;POINT(${lon.toFixed(4)} ${lat.toFixed(4)})') and (contains(Name,'_OL_1_EFR_') or contains(Name,'_SL_1_RBT_')) and ContentDate/Start gt ${iso(since).replace("Z", ".000Z")}`;
  const d = await getJSON(`${ODATA}?${new URLSearchParams({ $filter: filter, $top: "1000", $orderby: "ContentDate/Start desc", $expand: "Assets" })}`);
  const by = new Map();
  (d.value || []).forEach((p) => {
    const m = /_\d{4}_(\d{3})_(\d{3})_(\d{4})_/.exec(p.Name);
    if (!m) return;
    const when = new Date(p.ContentDate.Start), plat = p.Name.slice(0, 3), k = `${plat}|${m[1]}|${m[2]}`;
    const inst = p.Name.includes("_OL_1_") ? "OLCI" : "SLSTR";
    const g = by.get(k) || by.set(k, { when, platform: plat, track: Number(m[2]), frame: Number(m[3]), id: p.Name.replace(/\.SEN3$/, ""), instruments: [], links: [] }).get(k);
    if (when < g.when) Object.assign(g, { when, frame: Number(m[3]) });
    if (!g.instruments.includes(inst)) g.instruments.push(inst);
    if (!g.links.some((l) => l.text === inst)) g.links.push({ href: `${ODATA}(${p.Id})?$expand=Attributes`, text: inst, title: p.Name });
    // the preview: the OLCI frame over the tile centre (true colour, by day),
    // else SLSTR's (thermal at night)
    const b = s3Browse(p, plat, c.geojson);
    const key = inst === "OLCI" ? "browse" : "slstrBrowse";
    if (b && (!g[key] || when < new Date(g[key].start))) g[key] = b;
  });
  const items = [...by.values()].map(({ slstrBrowse, ...x }) => ({ ...x, browse: x.browse || slstrBrowse || null, instruments: x.instruments.sort(), light: x.instruments.includes("OLCI") ? "day" : null }));
  return { source: "CDSE, Sentinel-3 OLCI EFR and SLSTR RBT products over the tile centre", items,
    note: "Previews: CDSE's quicklook of the frame, placed from the orbit: OLCI's true colour by day, SLSTR's (thermal) at night." };
}

// A Sentinel-3 preview request from a CDSE OData product (with its Assets):
// its quicklook, time span and footprint. Null without a quicklook.
export function s3Browse(p, platform, clip = null) {
  const ql = (p.Assets || []).find((a) => a.Type === "QUICKLOOK");
  const g = p.GeoFootprint;
  if (!ql || !g) return null;
  return {
    kind: "s3", platform, name: p.Name.replace(/\.SEN3$/, ""), slstr: p.Name.includes("_SL_1_"), start: p.ContentDate.Start, end: p.ContentDate.End, footprint: g, clip,
    // the download host serves it with CORS (the catalogue's link redirects there)
    url: ql.DownloadLink.replace("//catalogue.dataspace.copernicus.eu/", "//download.dataspace.copernicus.eu/"),
  };
}

function ummBBox(u) {
  const g = (((u.SpatialExtent || {}).HorizontalSpatialDomain || {}).Geometry) || {};
  const pts = (g.GPolygons || []).flatMap((p) => p.Boundary.Points.map((q) => [q.Longitude, q.Latitude]));
  if (!pts.length) return null;
  const lons = pts.map((q) => q[0]), lats = pts.map((q) => q[1]);
  return [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
}

export async function fetchHistory(c) {
  const key = `${c.mission}|${c.title}`;
  if (cache.has(key)) return cache.get(key);
  const since = new Date(Date.now() - HISTORY_DAYS * 864e5);
  let p;
  if (c.mission === "s1") p = c.burst ? s1Burst(c, since) : s1Swath(c, since);
  else if (c.mission === "s2") p = c.tile ? s2Tile(c, since) : Promise.resolve({ source: "", items: null, note: "Zoom in to a tile (level 4+) for its acquisition history." });
  else if (c.mission === "landsat") p = landsatScene(c, since);
  else if (c.mission === "modis" || c.mission === "viirs") p = wideTile(c, since);
  else if (c.mission === "s3") p = s3Tile(c, since);
  else p = nisarFrame(c, since);
  p = p.then((h) => ({ ...h, items: h.items && h.items.filter((x) => x.when < new Date()).sort((a, b) => b.when - a.when) }));
  cache.set(key, p);
  p.catch(() => cache.delete(key));
  return p;
}

// "7.0 d", or hours/minutes for passes the same day (Sentinel-2A/B/C).
export function fmtGap(ms) {
  const d = ms / 864e5;
  if (d >= 1) return `${d.toFixed(1)} d`;
  const h = ms / 3.6e6;
  return h >= 1 ? `${h.toFixed(1)} h` : `${Math.max(1, Math.round(ms / 6e4))} min`;
}

// Median gap between consecutive acquisitions, in days.
export function medianRevisit(items) {
  const t = items.map((x) => x.when.getTime()).sort((a, b) => a - b);
  const gaps = t.slice(1).map((v, i) => (v - t[i]) / 864e5).filter((g) => g > 0.02).sort((a, b) => a - b);
  if (!gaps.length) return null;
  const m = gaps.length >> 1;
  return gaps.length % 2 ? gaps[m] : (gaps[m - 1] + gaps[m]) / 2;
}

// Timeline strip: past (real) acquisitions filled, planned ones hollow, with
// the gap (Δ) between consecutive ones written between them. Labels go above
// or below the line, whichever is free; a gap with no room for its label
// (dense revisits; longest gaps are placed first) is still in each dot's tooltip.
// One lane per platform (S2A, S2B, S2C...), like the AOI's timeline; gaps
// between consecutive passes (any platform) are labelled above the lanes.
const LANES = ["S1A", "S1C", "S1D", "S2A", "S2B", "S2C", "L8", "L9", "NISAR", "Terra", "Aqua", "SNPP", "N20", "N21", "S3A", "S3B"];
export function timelineSVG(past, upcoming, color, { daysBack = HISTORY_DAYS, daysAhead = 30, width = 320 } = {}) {
  const now = Date.now(), pad = 8, labelW = 40, laneH = 14, top = 30;
  const t0 = now - daysBack * 864e5, t1 = now + daysAhead * 864e5;
  const x = (t) => labelW + ((t - t0) / (t1 - t0)) * (width - labelW - pad);
  const events = [...past.map((d) => ({ ...d, filled: true })), ...upcoming.map((d) => ({ ...d, filled: false }))]
    .filter((d) => d.when.getTime() >= t0 && d.when.getTime() <= t1)
    .sort((a, b) => a.when - b.when);
  const laneOf = (d) => d.platform || "";
  const lanes = [...new Set(events.map(laneOf))].sort((a, b) => (LANES.indexOf(a) + 1 || 99) - (LANES.indexOf(b) + 1 || 99) || a.localeCompare(b));
  if (!lanes.length) lanes.push("");
  const yOf = (l) => top + lanes.indexOf(l) * laneH + laneH / 2;
  const bottom = top + lanes.length * laneH, h = bottom + 22;
  // gaps under ~30 min are one pass seen twice (two products, two segments): no label
  const seq = [];
  events.forEach((d) => {
    const prev = seq[seq.length - 1];
    d.gap = prev ? d.when - prev.when : null;
    if (!prev || d.gap > 18e5) seq.push(d);
  });
  const gaps = seq.slice(1).map((b, i) => ({ a: seq[i], b, ms: b.when - seq[i].when }));
  // longest gaps first, so an unusual gap is always labelled when space runs out
  const used = [], charW = 4.9;
  const free = (l, w) => used.every(([a, b]) => l + w + 3 <= a || l >= b + 3);
  const labels = [...gaps].sort((p, q) => q.ms - p.ms).map(({ a, b, ms }) => {
    const text = `Δ ${fmtGap(ms)}`, w = text.length * charW;
    const mid = (x(a.when.getTime()) + x(b.when.getTime())) / 2;
    const left = Math.max(labelW, Math.min(width - pad - w, mid - w / 2));
    if (!free(left, w)) return "";
    used.push([left, left + w]);
    return `<text class="tl-gap" x="${(left + w / 2).toFixed(1)}" y="${top - 5}" text-anchor="middle">${text}</text>`;
  }).join("");
  const dot = (d) => `<circle cx="${x(d.when.getTime()).toFixed(1)}" cy="${yOf(laneOf(d))}" r="4" fill="${d.filled ? color : "var(--surface)"}" stroke="${color}" stroke-width="1.6"><title>${d.label}${d.gap ? ` · Δ ${fmtGap(d.gap)} after the previous` : ""}</title></circle>`;
  // end labels anchored inward so they stay inside the strip
  const anchor = (d) => (d === -daysBack ? "start" : d === daysAhead ? "end" : "middle");
  const lx = (d) => (d === -daysBack ? labelW - 4 : d === daysAhead ? width - pad + 4 : x(now + d * 864e5));
  const ticks = [-90, -60, -30, 0, 15, 30].filter((d) => d >= -daysBack && d <= daysAhead)
    .map((d) => `<line x1="${x(now + d * 864e5)}" x2="${x(now + d * 864e5)}" y1="${bottom + 1}" y2="${bottom + 5}" stroke="var(--muted)"/><text x="${lx(d)}" y="${h - 2}" text-anchor="${anchor(d)}">${d === 0 ? "now" : `${d > 0 ? "+" : "−"}${Math.abs(d)} d`}</text>`).join("");
  const laneLines = lanes.map((l) => `<text class="tl-lane" x="0" y="${yOf(l) + 3.5}">${l}</text><line x1="${labelW}" x2="${width - pad}" y1="${yOf(l)}" y2="${yOf(l)}" stroke="var(--hairline)" stroke-width="1.5"/>`).join("");
  return `<svg class="hist-tl" viewBox="0 0 ${width} ${h}" width="100%" role="img" aria-label="Acquisition timeline: ${past.length} recent, ${upcoming.length} planned">
    <rect x="${x(now)}" y="${top - 2}" width="${x(t1) - x(now)}" height="${bottom - top + 4}" fill="var(--surface-2)"/>
    ${laneLines}
    <line x1="${x(now)}" x2="${x(now)}" y1="${top - 4}" y2="${bottom + 2}" stroke="var(--ink-2)" stroke-dasharray="2 2"/>
    <text x="${labelW}" y="9" class="tl-l">recent (catalogue)</text><text x="${width - pad}" y="9" text-anchor="end" class="tl-l">planned</text>
    ${labels}${events.map(dot).join("")}${ticks}
  </svg>`;
}

// A NISAR acquisition's browse request: its GCOV's images if the GCOV is out,
// else its RSLC's.
function nisarBrowse(rslc, gcov) {
  const u = gcov && visuals(gcov).length ? gcov : rslc;
  const bw = (rslc.AdditionalAttributes || []).find((x) => x.Name === "RANGE_BANDWIDTH_CONCAT");
  return { kind: "nisar", urls: visuals(u), bbox: ummBBox(u), mhz: bw && bw.Values ? bw.Values[0] : null };
}

// ---------------------------------------------------------------- browse images
// A browse image of one past acquisition: {thumb, full, source, note, links, georef}
// with georef = {url, corners: [[lon, lat] x4 from the top-left, clockwise],
// key} when the image can be draped on the map (north-up, known extent, and
// served with CORS so WebGL may read it); key: "black"/"white", a background
// colour to make transparent. Sources (all public, no login; NISAR's and
// OPERA's exact extents come from the poller, see browseExtents):
//   Sentinel-1  Planetary Computer's RTC (UTM) or GRD (lat/lon) preview of the
//               same pass, in colour or one channel; failing those, OPERA's
//               RTC-S1 browse of the burst (placed approximately); failing
//               that, ASF's GRD browse (radar geometry, no CORS: listed, not
//               placed on the map)
//   Sentinel-2  the tile's L2A preview (Earth Search, Element 84's COG copy),
//               UTM, its extent from the item's projection
//   Landsat     Planetary Computer's rendered preview of the same scene, UTM
//               (USGS's own thumbnails need a login; Planetary Computer is
//               days behind, so the newest scenes may have none yet)
//   NISAR       ASF's browse of the GCOV (UTM, colour or one channel), else
//               of the RSLC (lat/lon, colour); S-band: Bhoonidhi's thumbnail
//               of the GCOV (UTM, exact from the grid the poller looked up),
//               else of the RSLC (radar geometry, placed approximately), read
//               through an image proxy (Bhoonidhi sends no CORS)
// Exact extents published by the poller (extents.py, with an Earthdata
// login): browse_extents/nisar_gcov/<track>.json {GCOV granule: [lon, lat] x 4
// from the top-left, clockwise} and
// browse_extents/opera/<track>.json {burst: [epsg, xmin, ymin, xmax, ymax]}.
// {} when the site has none (not published, or nothing on that track yet).
const extentFiles = new Map();
export function browseExtents(kind, track) {
  const key = `${kind}/${String(track).padStart(3, "0")}`;
  if (!extentFiles.has(key)) {
    extentFiles.set(key, fetch(`./data/browse_extents/${key}.json`).then((r) => (r.ok ? r.json() : {})).catch(() => ({})));
  }
  return extentFiles.get(key);
}
const browseCache = new Map();
export function fetchBrowse(b) {
  const key = JSON.stringify(b);
  if (!browseCache.has(key)) {
    const p = (BROWSE[b.kind] || (() => Promise.resolve(null)))(b);
    browseCache.set(key, p);
    p.catch(() => browseCache.delete(key));
  }
  return browseCache.get(key);
}

// NASA Worldview (GIBS): the day's corrected-reflectance true colour of one
// platform, a WMS image in lat/lon (EPSG:4326) over a box, served with CORS.
const GIBS_LAYER = {
  Terra: "MODIS_Terra_CorrectedReflectance_TrueColor", Aqua: "MODIS_Aqua_CorrectedReflectance_TrueColor",
  SNPP: "VIIRS_SNPP_CorrectedReflectance_TrueColor", N20: "VIIRS_NOAA20_CorrectedReflectance_TrueColor", N21: "VIIRS_NOAA21_CorrectedReflectance_TrueColor",
};
// At night: brightness temperature in a thermal band (MODIS band 31, 11 um;
// VIIRS I5, 11.45 um), and for SNPP and NOAA-20 also the Day/Night Band
// (low-light visible: moonlit cloud and snow, city lights). Daily mosaics
// only (no granule layers), as Worldview has them.
const GIBS_NIGHT = {
  Terra: [["Thermal (band 31)", "MODIS_Terra_Brightness_Temp_Band31_Night"]],
  Aqua: [["Thermal (band 31)", "MODIS_Aqua_Brightness_Temp_Band31_Night"]],
  SNPP: [["Thermal (I5)", "VIIRS_SNPP_Brightness_Temp_BandI5_Night"], ["Day/Night Band", "VIIRS_SNPP_DayNightBand_At_Sensor_Radiance"]],
  N20: [["Thermal (I5)", "VIIRS_NOAA20_Brightness_Temp_BandI5_Night"], ["Day/Night Band", "VIIRS_NOAA20_DayNightBand_At_Sensor_Radiance"]],
  N21: [["Thermal (I5)", "VIIRS_NOAA21_Brightness_Temp_BandI5_Night"]],
};
// and one granule (6 minutes) at a time, for the platforms it has them for
// (the last ~90 days, a few hours behind)
const GIBS_GRANULE = {
  SNPP: "VIIRS_SNPP_CorrectedReflectance_TrueColor_Granule", N20: "VIIRS_NOAA20_CorrectedReflectance_TrueColor_Granule",
};
const GRANULE_MS = 6 * 60e3;
// The 6-minute granule starts (ISO, as Worldview names them) of a pass's
// granules, with one more either side.
export function granuleTimes(starts) {
  const ts = starts.map((t) => Math.floor(new Date(t).getTime() / GRANULE_MS) * GRANULE_MS);
  const lo = Math.min(...ts) - GRANULE_MS, hi = Math.max(...ts) + GRANULE_MS, out = [];
  for (let t = lo; t <= hi; t += GRANULE_MS) out.push(new Date(t).toISOString().replace(".000Z", "Z"));
  return out;
}
export function gibsURL(layer, date, [w, s, e, n], size = 1024) {
  const h = Math.max(64, Math.round((size * (n - s)) / Math.max(1e-6, e - w)));
  const q = new URLSearchParams({ SERVICE: "WMS", REQUEST: "GetMap", VERSION: "1.1.1", LAYERS: layer, SRS: "EPSG:4326", STYLES: "", BBOX: [w, s, e, n].join(","), WIDTH: String(size), HEIGHT: String(Math.min(2048, h)), FORMAT: "image/png", TRANSPARENT: "TRUE", TIME: date });
  return `https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?${q}`;
}

// A footprint's points, all rings of a Polygon or MultiPolygon.
const ringPoints = (g) => (g.type === "Polygon" ? g.coordinates : g.type === "MultiPolygon" ? g.coordinates.flat() : []).flat();

// The night layers of a MODIS/VIIRS pass: the day's mosaic of each, the
// first shown, the others as channels. Their PNGs are transparent where
// there is no data, so no colour is keyed out (the warm sea is dark).
function gibsNight(b) {
  const layers = GIBS_NIGHT[b.platform];
  if (!layers || !b.bbox) return { note: "No Worldview night layer for this platform." };
  const [w, s, e, n] = b.bbox, [, first] = layers[0];
  const bands = layers.map(([label, layer]) => ({ label, thumb: gibsURL(layer, b.date, b.bbox, 256), url: gibsURL(layer, b.date, b.bbox, 1024) }));
  return {
    thumb: bands[0].thumb, full: bands[0].url,
    source: `NASA Worldview (GIBS) ${first.replace(/_/g, " ")}, ${b.date}: the night's mosaic of this platform's passes (brightness temperature)`,
    links: [{ href: `https://worldview.earthdata.nasa.gov/?v=${w},${s},${e},${n}&l=${first}&t=${b.date}`, text: "Worldview" }],
    bands: bands.length > 1 ? bands : null,
    georef: { url: bands[0].url, corners: bboxCorners(b.bbox), where: { kind: "latlon", bbox: b.bbox }, key: null, clip: b.clip || null },
  };
}

const BROWSE = {
  // Sentinel-3: CDSE's quicklook of the frame (OLCI EFR: true colour). It is
  // in the instrument's geometry (scan lines x across-track columns, no
  // lat/lon grid): map.js places it from the orbit (swathgeom.js
  // swathImageModel), within the footprint
  async s3(b) {
    return {
      thumb: b.url, full: b.url,
      source: `CDSE ${b.slstr ? "SLSTR quicklook (thermal infrared at night: cold cloud bright)" : "OLCI quicklook"} of ${b.name}, placed from the orbit (to a few km)`,
      // SLSTR's is thermal at night: warm sea is near black, so no colour is
      // keyed out (its margins are trimmed, and it is clipped to the footprint)
      georef: {
        url: b.url, corners: null, key: b.slstr ? null : "black", clip: b.clip,
        where: { kind: "swath", platform: b.platform, t0: Date.parse(b.start), t1: Date.parse(b.end), ring: ringPoints(b.footprint) },
      },
    };
  },
  // MODIS/VIIRS from NASA Worldview, placed exactly (lat/lon box): with
  // b.granules (the pass's granule starts; SNPP and NOAA-20), that pass
  // alone, else the platform's day mosaic (all its passes that day); shown
  // only within b.clip (the tile, or the pass's granule footprints)
  async gibs(b) {
    if (b.night) return gibsNight(b);
    const layer = GIBS_LAYER[b.platform];
    if (!layer || !b.bbox) return { note: "No Worldview layer for this platform." };
    const [w, s, e, n] = b.bbox;
    const mosaic = gibsURL(layer, b.date, b.bbox, 1024), granule = b.granules && b.granules.length ? GIBS_GRANULE[b.platform] : null;
    const day = `NASA Worldview (GIBS) ${layer.replace(/_/g, " ")}, ${b.date}: the day's mosaic of this platform's passes`;
    return {
      thumb: gibsURL(layer, b.date, b.bbox, 256), full: mosaic,
      source: granule ? `NASA Worldview (GIBS) ${granule.replace(/_/g, " ")}: this pass's granules (${b.granules[0].slice(11, 16)}-${b.granules.at(-1).slice(11, 16)} UTC)` : day,
      links: [{ href: `https://worldview.earthdata.nasa.gov/?v=${w},${s},${e},${n}&l=${granule || layer}&t=${granule ? b.granules[1] || b.granules[0] : b.date}`, text: "Worldview" }],
      georef: {
        url: granule ? gibsURL(granule, b.granules[0], b.bbox, 1024) : mosaic, urls: granule ? b.granules.map((t) => gibsURL(granule, t, b.bbox, 1024)) : null,
        fallback: granule ? mosaic : null, fallbackSource: granule ? day : null,
        corners: bboxCorners(b.bbox), where: { kind: "latlon", bbox: b.bbox }, key: "black", clip: b.clip || null,
      },
    };
  },
  // Planetary Computer's RTC preview (terrain-corrected, UTM), else its GRD
  // preview (warped to lat/lon), of the pass over the footprint, each also as
  // single channels (bands: VV, VH or HH, HV); else OPERA's RTC-S1 browse of
  // the burst over the point (sooner than Planetary Computer; its grid is not
  // published, so placed approximately); else ASF's GRD browse, which has no
  // CORS and is in radar geometry: listed, not placed.
  async s1(b) {
    const t = new Date(b.when);
    const datetime = `${iso(new Date(t - 90e3))}/${iso(new Date(t.getTime() + 90e3))}`;
    for (const coll of ["sentinel-1-rtc", "sentinel-1-grd"]) {
      let f = null;
      try {
        const d = await getJSON(PC_STAC, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ collections: [coll], intersects: { type: "Point", coordinates: b.point }, datetime, limit: 5 }) });
        f = (d.features || []).find((x) => x.assets && x.assets.rendered_preview);
      } catch { /* try the next */ }
      if (!f) continue;
      const href = f.assets.rendered_preview.href;
      const p = f.properties, bb = p["proj:bbox"];
      const epsg = p["proj:epsg"] || parseEPSG(p["proj:code"]);
      // RTC: UTM, its grid from proj:bbox (in metres); GRD: warped to lon/lat over its bbox
      const where = coll === "sentinel-1-rtc"
        ? (bb && epsg ? { kind: "utm", epsg, transform: [1, 0, bb[0], 0, -1, bb[3]], shape: [bb[3] - bb[1], bb[2] - bb[0]] } : null)
        : { kind: "latlon", bbox: bb && (epsg === 4326 || !epsg) ? bb : f.bbox };
      const corners = where && (where.kind === "utm" ? utmCorners(epsg, where.transform, where.shape) : bboxCorners(where.bbox));
      const bands = await s1Bands(coll, f, href);
      if (!bands.length) continue;
      return {
        thumb: bands[0].thumb, full: bands[0].url,
        source: `Planetary Computer's ${coll === "sentinel-1-rtc" ? "RTC" : "GRD"} preview of ${f.id}`,
        links: [{ href: `https://planetarycomputer.microsoft.com/api/data/v1/item/map?collection=${coll}&item=${encodeURIComponent(f.id)}`, text: "Planetary Computer viewer" }],
        bands: bands.length > 1 ? bands : null,
        georef: corners ? { url: bands[0].url, corners, where, key: null } : null,
      };
    }
    try {
      const opera = await operaBrowse(b);
      if (opera) return opera;
    } catch { /* try ASF's GRD browse */ }
    const q = new URLSearchParams({
      platform: b.platform, processingLevel: "GRD_HD,GRD_MD,GRD_MS,GRD_HS", intersectsWith: `POINT(${b.point[0].toFixed(4)} ${b.point[1].toFixed(4)})`,
      start: iso(new Date(t - 90e3)), end: iso(new Date(t.getTime() + 90e3)), output: "geojson", maxResults: "5",
    });
    const d = await getJSON(`${ASF}?${q}`);
    const f = (d.features || []).find((x) => x.properties.browse && x.properties.browse.length);
    if (!f) return { note: "No preview of this pass (yet): not at Planetary Computer or OPERA, no ASF browse." };
    const p = f.properties;
    return {
      thumb: p.browse[0], full: p.browse[0], source: `ASF browse of ${p.sceneName}`,
      note: "ASF's browse only (Planetary Computer and OPERA have no preview of this pass yet): in radar geometry and served without CORS, so it cannot be placed on the map.",
      links: [{ href: vertex(p.sceneName), text: "the GRD in ASF Vertex" }],
    };
  },
  async s2(b) {
    const l2a = b.id.replace(/_L1C$/, "_L2A");
    const d = await getJSON(EARTH_SEARCH, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ collections: ["sentinel-2-l2a"], ids: [l2a] }) });
    const f = (d.features || [])[0];
    const thumb = f && f.assets.thumbnail && f.assets.thumbnail.href;
    if (!thumb || !/^https:/.test(thumb)) return { note: "No public L2A preview of this tile (yet)." };
    const where = { kind: "utm", epsg: f.properties["proj:epsg"] || parseEPSG(f.properties["proj:code"]), transform: (f.assets.visual || {})["proj:transform"], shape: (f.assets.visual || {})["proj:shape"] };
    const corners = utmCorners(where.epsg, where.transform, where.shape);
    return {
      thumb, full: thumb, source: `Element 84's preview of ${l2a} (true colour, L2A)`,
      links: [{ href: stacBrowser("sentinel-2-l2a", l2a), text: "the L2A in STAC Browser" }],
      georef: corners ? { url: thumb, corners, where, key: "black" } : null,
    };
  },
  async landsat(b) {
    const d = await getJSON(PC_STAC, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ collections: ["landsat-c2-l2"], ids: [b.id] }) });
    const f = (d.features || [])[0];
    const a = f && f.assets.rendered_preview;
    if (!a) return { note: "No public preview yet: USGS's thumbnails need a login, and Planetary Computer does not have this scene yet (it is days behind)." };
    const sep = a.href.includes("?") ? "&" : "?";
    const where = { kind: "utm", epsg: f.properties["proj:epsg"] || parseEPSG(f.properties["proj:code"]), transform: f.properties["proj:transform"], shape: f.properties["proj:shape"] };
    const corners = utmCorners(where.epsg, where.transform, where.shape);
    return {
      thumb: `${a.href}${sep}max_size=512`, full: `${a.href}${sep}max_size=1024`, source: `Planetary Computer's rendered preview of ${b.id}`,
      links: [{ href: `https://planetarycomputer.microsoft.com/api/data/v1/item/map?collection=landsat-c2-l2&item=${encodeURIComponent(b.id)}`, text: "Planetary Computer viewer" }],
      georef: corners ? { url: `${a.href}${sep}max_size=1024`, corners, where, key: null } : null,
    };
  },
  // ASF's browse of the GCOV (each RSLC's, ~1 h later): geocoded on the
  // GCOV's own grid (UTM, terrain-corrected), the colour composite and each
  // channel (_NATIVE_A_HHHH.png: frequency A's HH); the grid's corners come
  // from the poller (browse_extents/nisar_gcov). Before the poller has them,
  // or before the GCOV exists, the lat/lon colour composite of the GCOV or
  // RSLC, fitted to the CMR footprint (0-5 km off).
  async nisar(b) {
    const thumb = b.urls.find((u) => /_LATLON_thumbnail\.png$/.test(u)) || b.urls.find((u) => /thumbnail/i.test(u));
    const full = b.urls.find((u) => /_LATLON\.png$/.test(u)) || thumb;
    if (!thumb && !full) return { note: "No browse image listed for this product." };
    const granule = (full || thumb).split("/").slice(-2)[0];
    const gcov = /_GCOV_/.test(granule), m = /^NISAR_\w\w_\w\w_\w+?_\d{3}_(\d{3})_/.exec(granule);
    const quad = gcov && m ? (await browseExtents("nisar_gcov", Number(m[1])))[granule] : null;
    const native = b.urls.find((u) => /_NATIVE\.png$/.test(u));
    if (quad && quad.length === 8 && native) {
      const corners = [0, 2, 4, 6].map((i) => [quad[i], quad[i + 1]]);
      const chans = b.urls.map((u) => [u, /_NATIVE_([AB])_([HVRL]{2})\2\.png$/.exec(u)]).filter(([, x]) => x)
        .map(([u, x]) => ({ freq: x[1], pol: x[2], url: u }))
        .sort((p, q) => p.freq.localeCompare(q.freq) || (POL_RANK[p.pol] ?? 9) - (POL_RANK[q.pol] ?? 9));
      // frequency A: the main band (20, 40 or 77 MHz: the fine range resolution);
      // B: a 5 MHz side band (~30 m slant-range resolution; mostly for
      // correcting the ionosphere), named apart when both are there
      const hasA = chans.some((x) => x.freq === "A"), hasB = chans.some((x) => x.freq === "B");
      const [mhzA, mhzB] = String(b.mhz || "").split("+").map((v) => (v ? `${Number(v)} MHz` : ""));
      const bandOf = (f) => (!hasA || !hasB ? "" : f === "A" ? ` (main band${mhzA ? `, ${mhzA}` : ""})` : ` (side band, ${mhzB || "5 MHz"})`);
      const bands = chans.map((x) => ({ label: `${x.pol}${bandOf(x.freq)}`, thumb: x.url, url: x.url }));
      const pols = [...new Set(chans.filter((x) => x.freq === (hasA ? "A" : "B")).map((x) => x.pol))];
      if (pols.length > 1) bands.unshift({ label: pols.join("+"), thumb: thumb || native, url: native });
      return {
        thumb: thumb || native, full: native, source: `ASF's browse of the GCOV ${granule} (geocoded, terrain-corrected)`,
        bands: bands.length > 1 ? bands : null,
        georef: { url: bands.length > 1 ? bands[0].url : native, corners, where: gcovGrid(corners), key: null },
      };
    }
    const corners = bboxCorners(b.bbox);
    return {
      thumb: thumb || full, full: full || thumb, source: `ASF's geocoded browse of the ${gcov ? "GCOV" : "RSLC"}`,
      note: gcov
        ? "The colour composite only, placed approximately (this site has not read the image's exact grid yet; the channels follow when it has)."
        : "The RSLC's colour composite, placed approximately: the GCOV, with its channels, follows the RSLC by about an hour.",
      // white around the frame, black along its jagged swath edges: both made
      // transparent. The bbox is the frame's; the image has a margin around it
      // (dataExtent: map.js fits the image's data, not its edges, to it).
      georef: full && corners ? { url: full, corners, where: { kind: "latlon", bbox: b.bbox }, key: "both", dataExtent: true } : null,
    };
  },
  // NISAR S-band: Bhoonidhi's thumbnail (public JPEG, one channel, grey, the
  // product decimated 20x) of the GCOV, else of the RSLC whose link the poller
  // keeps (b.url). Bhoonidhi sends no CORS, so the map reads it through the
  // wsrv.nl image proxy (which adds it). The GCOV's fills its UTM grid exactly:
  // placed from the grid's corners (b.grid, the poller's lookup of the GCOV
  // item) and cut to the data's (b.corners), else fitted to the data's
  // corners (its data found by a black key). The RSLC's is in
  // radar geometry: stretched between the data's corners (approximate).
  // Without corners (rows from before the poller kept them), listed only.
  async nisar_s(b) {
    const gcov = b.url.replace(/\/NISAR_S1_(\w\w)_RSLC_([^/]*)$/, "/NISAR_S2_$1_GCOV_$2");
    const geocoded = gcov !== b.url && (await loads(gcov)), url = geocoded ? gcov : b.url;
    const name = url.split("/").pop().replace(/\.jpg$/, "");
    const base = { full: url, links: [{ href: url, text: "the image at Bhoonidhi" }] };
    const source = `Bhoonidhi's thumbnail of ${name} (ISRO, one channel${geocoded ? ", geocoded" : ", radar geometry"}), via the wsrv.nl image proxy`;
    const img = wsrv(url, 2048), thumb = wsrv(url, 256);
    if (geocoded && b.grid) {
      // outside the data, black: cut away along the data's corners, as a black
      // key would also take dark data (water, radar shadow: ~9% of a frame)
      const where = gcovGrid(b.grid), clip = b.corners && where ? { type: "Polygon", coordinates: [[...b.corners, b.corners[0]]] } : null;
      return { ...base, thumb, source, georef: { url: img, corners: b.grid, where, clip, key: clip ? null : "black" } };
    }
    if (geocoded && b.corners) {
      return {
        ...base, thumb, source, note: "Placed approximately: fitted to the data's corners (this site has not read the image's grid yet)",
        georef: { url: img, corners: b.corners, fit: { ring: b.corners }, key: "black" },
      };
    }
    if (b.corners) {
      return {
        ...base, thumb, source, note: "The RSLC's image (the GCOV's is not out yet): in radar geometry, stretched between its corners, so a few km off",
        georef: { url: img, corners: b.corners, approx: true, key: null }, // all data, edge to edge
      };
    }
    return { ...base, thumb: url, source: source.replace(/, via the .*$/, ""), note: "Not placed: this row has no corners (listed before this site kept them)." };
  },
};

// An image through wsrv.nl (an open-source image proxy and cache): served
// with CORS, at most `w` px wide (never enlarged).
const wsrv = (url, w) => `https://wsrv.nl/?url=${encodeURIComponent(url)}&w=${w}&we`;
// Whether an image loads (an <img> needs no CORS, unlike fetch).
function loads(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
    img.src = url;
  });
}

// One channel of a Planetary Computer Sentinel-1 item, grey: RTC is gamma0
// (linear power), GRD amplitude (DN); the co-pol channel (VV, HH) is
// brighter than the cross-pol one (VH, HV). GRD amplitudes differ by mode
// (EW's run ~8x IW's), so a GRD is stretched by its own range (rescale).
const PC_DATA = "https://planetarycomputer.microsoft.com/api/data/v1/item";
export function singleBand(coll, id, pol, rescale = null) {
  const a = pol.toLowerCase(), co = a === "vv" || a === "hh";
  rescale ||= coll === "sentinel-1-rtc" ? (co ? "0,0.5" : "0,0.12") : (co ? "0,600" : "0,270");
  return `${PC_DATA}/preview.png?collection=${coll}&item=${encodeURIComponent(id)}&assets=${a}&rescale=${rescale}&colormap_name=gray&format=png`;
}

// The previews of a Planetary Computer Sentinel-1 item: colour (co-pol, cross-
// pol, their ratio) and each channel. Planetary Computer's own rendered
// preview names VV and VH whatever the item has, so it fails on HH/HV items
// (all of EW, and IW/stripmap over ice): the colour image is built here.
export async function s1Bands(coll, f, href) {
  const pols = (f.properties["sar:polarizations"] || []).filter((x) => f.assets[x.toLowerCase()]);
  const co = pols.find((x) => x === "VV" || x === "HH"), cross = pols.find((x) => x === "VH" || x === "HV");
  let range = {};
  if (coll === "sentinel-1-grd") {
    try {
      const q = new URLSearchParams([["collection", coll], ["item", f.id], ...pols.map((x) => ["assets", x.toLowerCase()]), ["max_size", "512"]]);
      const st = await getJSON(`${PC_DATA}/statistics?${q}`);
      pols.forEach((x) => { const b = st[`${x.toLowerCase()}_b1`]; if (b) range[x] = [b.percentile_2, b.percentile_98, b.mean]; });
    } catch { range = {}; /* the fixed stretch */ }
  }
  const r = (x) => (range[x] ? `${Math.round(range[x][0])},${Math.round(range[x][1])}` : null);
  const id = encodeURIComponent(f.id), size = (u, n) => `${u}&max_size=${n}`;
  let colour = null;
  if (co && cross && coll === "sentinel-1-rtc") {
    colour = co === "VV" ? href : href.replace(/vv/g, "hh").replace(/vh/g, "hv");
  } else if (co && cross) {
    const [c, x] = [co.toLowerCase(), cross.toLowerCase()];
    const ratio = range[co] && range[cross] ? (2 * range[co][2]) / range[cross][2] : 9;
    const scales = [r(co) || "0,600", r(cross) || "0,270", `0,${ratio.toFixed(1)}`];
    colour = `${PC_DATA}/preview.png?collection=${coll}&item=${id}&assets=${c}&assets=${x}&expression=${encodeURIComponent(`${c};${x};${c}/${x}`)}&asset_as_band=True&${scales.map((v) => `rescale=${v}`).join("&")}&format=png`;
  }
  const singles = pols.map((x) => ({ label: x, url: singleBand(coll, f.id, x, r(x)) }));
  const all = (colour ? [{ label: `${co}+${cross}`, url: colour }] : []).concat(singles);
  return all.map((b) => ({ label: b.label, thumb: size(b.url, 256), url: size(b.url, 1024) }));
}

// OPERA's RTC-S1 browse of the burst over the point in this pass (NASA CMR;
// the image at ASF, public with CORS). One burst (about 90 x 20 km), its UTM
// grid not published: map.js fits the image's data to the burst's footprint
// (georef.fit, drape.js fitUtmToData).
async function operaBrowse(b) {
  const t = new Date(b.when);
  const q = new URLSearchParams({
    short_name: "OPERA_L2_RTC-S1_V1", point: `${b.point[0].toFixed(4)},${b.point[1].toFixed(4)}`,
    temporal: `${iso(new Date(t - 180e3))},${iso(new Date(t.getTime() + 180e3))}`, page_size: "10",
  });
  const d = await getJSON(`${CMR}?${q}`);
  // the platform: "Sentinel-1C" (ASF) or "S1C"; OPERA's names carry "_S1C_"
  const unit = /([a-z])$/i.exec(String(b.platform || ""));
  const items = (d.items || []).filter((it) => !unit || it.umm.GranuleUR.includes(`_S1${unit[1].toUpperCase()}_`));
  for (const it of items) {
    const u = it.umm, urls = (u.RelatedUrls || []).filter((x) => x.Type === "GET RELATED VISUALIZATION" && /^https:/.test(x.URL)).map((x) => x.URL);
    const low = urls.find((x) => /_BROWSE_low-res\.png$/.test(x)), thumb = urls.find((x) => /_BROWSE_thumbnail\.png$/.test(x)) || low;
    const full = urls.find((x) => /_BROWSE\.png$/.test(x)) || low;
    const poly = (((u.SpatialExtent || {}).HorizontalSpatialDomain || {}).Geometry || {}).GPolygons;
    const ring = poly && poly[0] ? poly[0].Boundary.Points.map((p) => [p.Longitude, p.Latitude]) : null;
    if (!low || !ring) continue;
    const lons = ring.map((p) => p[0]), lats = ring.map((p) => p[1]);
    const box = [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
    // the burst's exact grid (the image spans it), when the poller has published it
    const burst = u.GranuleUR.split("_")[3], grid = (await browseExtents("opera", Number(burst.slice(1, 4))))[burst];
    if (grid) {
      const [epsg, x0, y0, x1, y1] = grid;
      const where = { kind: "utm", epsg, transform: [1, 0, x0, 0, -1, y1], shape: [y1 - y0, x1 - x0] };
      return {
        thumb, full, source: `OPERA's RTC-S1 browse of burst ${burst} (${u.GranuleUR})`,
        note: "One burst, from OPERA (Planetary Computer has no preview of this pass yet).",
        links: [{ href: `https://search.asf.alaska.edu/#/?dataset=OPERA-S1&searchType=List%20Search&searchList=${encodeURIComponent(u.GranuleUR)}`, text: "the burst in ASF Vertex" }],
        georef: { url: low, corners: utmCorners(epsg, where.transform, where.shape), where, key: null },
      };
    }
    return {
      thumb, full, source: `OPERA's RTC-S1 browse of burst ${u.GranuleUR.split("_")[3]} (${u.GranuleUR})`,
      note: "One burst, from OPERA (Planetary Computer has no preview of this pass yet): placed approximately (OPERA publishes its grid behind a login): expect it up to about 3 km off.",
      links: [{ href: `https://search.asf.alaska.edu/#/?dataset=OPERA-S1&searchType=List%20Search&searchList=${encodeURIComponent(u.GranuleUR)}`, text: "the burst in ASF Vertex" }],
      georef: { url: low, corners: bboxCorners(box), fit: { ring }, key: null },
    };
  }
  return null;
}

// North-up corners [[w, n], [e, n], [e, s], [w, s]] that belong to the
// part [x0, y0, x1, y1] (pixels) of a w x h image, widened to the whole
// image (linear in lon and lat: a lat/lon image).
export function widenToImage(corners, [x0, y0, x1, y1], w, h) {
  const [[W, N], [E], [, S]] = corners;
  const dx = (E - W) / (x1 - x0 + 1), dy = (N - S) / (y1 - y0 + 1);
  const w2 = W - x0 * dx, e2 = E + (w - 1 - x1) * dx, n2 = N + y0 * dy, s2 = S - (h - 1 - y1) * dy;
  return [[w2, n2], [e2, n2], [e2, s2], [w2, s2]];
}

// [w, s, e, n] -> corners from the top-left, clockwise.
const bboxCorners = (bb) => (bb && bb.length >= 4 ? [[bb[0], bb[3]], [bb[2], bb[3]], [bb[2], bb[1]], [bb[0], bb[1]]] : null);
const parseEPSG = (code) => { const m = /(\d+)$/.exec(code || ""); return m ? Number(m[1]) : null; };

// The four corners of a UTM raster (EPSG 326zz/327zz, affine transform, [rows, cols]) as lon/lat.
export function utmCorners(epsg, tr, shape) {
  if (!epsg || !tr || !shape) return null;
  const zone = epsg % 100, south = Math.floor(epsg / 100) === 327;
  if (!(Math.floor(epsg / 100) === 326 || south) || zone < 1 || zone > 60) return null;
  const [rows, cols] = shape;
  const at = (c, r) => utmToLonLat(tr[2] + c * tr[0] + r * tr[1], tr[5] + c * tr[3] + r * tr[4], zone, south);
  return [at(0, 0), at(cols, 0), at(cols, rows), at(0, rows)];
}

// UTM (WGS84) to lon/lat (Snyder 1987, the usual series), to well under a pixel here.
export function utmToLonLat(x, y, zone, south) {
  const a = 6378137, f = 1 / 298.257223563, k0 = 0.9996, e2 = f * (2 - f), ep2 = e2 / (1 - e2);
  const e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2));
  const m = (south ? y - 10000000 : y) / k0;
  const mu = m / (a * (1 - e2 / 4 - (3 * e2 * e2) / 64 - (5 * e2 ** 3) / 256));
  const p1 = mu + (1.5 * e1 - (27 / 32) * e1 ** 3) * Math.sin(2 * mu) + ((21 / 16) * e1 * e1 - (55 / 32) * e1 ** 4) * Math.sin(4 * mu)
    + ((151 / 96) * e1 ** 3) * Math.sin(6 * mu) + ((1097 / 512) * e1 ** 4) * Math.sin(8 * mu);
  const s1 = Math.sin(p1), c1 = Math.cos(p1), t1 = Math.tan(p1);
  const n1 = a / Math.sqrt(1 - e2 * s1 * s1), r1 = (a * (1 - e2)) / (1 - e2 * s1 * s1) ** 1.5;
  const C1 = ep2 * c1 * c1, T1 = t1 * t1, D = (x - 500000) / (n1 * k0);
  const lat = p1 - ((n1 * t1) / r1) * (D * D / 2 - ((5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * ep2) * D ** 4) / 24
    + ((61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * ep2 - 3 * C1 * C1) * D ** 6) / 720);
  const lon = (D - ((1 + 2 * T1 + C1) * D ** 3) / 6 + ((5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * ep2 + 24 * T1 * T1) * D ** 5) / 120) / c1;
  return [((zone - 1) * 6 - 180 + 3) + (lon * 180) / Math.PI, (lat * 180) / Math.PI];
}
