// Recent acquisitions over an AOI and where their products are already up:
// each source the latency poller measures is searched live, over the AOI (a
// point, or the AOI's bounding box) for the last RECENT_DAYS, and its
// products are grouped into acquisitions (one platform, within 15 minutes).
// The sources and their queries mirror swathcast/sources/*.py; every one of
// these catalogues answers browsers (CORS) and needs no login. USGS's
// LandsatLook STAC (no CORS) and ISRO's Bhoonidhi (login) cannot be searched
// from here: for those only the measured latency is shown (USGS's Level-1
// comes from this site's own listing instead, see registerSource).
import { aoiParts } from "./aoi.js";
import { ASF, CMR, ES, ODATA, PC, getJSON, iso, plat } from "./catalogues.js";

export const RECENT_DAYS = 90;
const GROUP_MS = 15 * 60e3;

// The query geometries: the AOI itself if a point, else the bounding box of
// each of its pieces (two for an AOI across 180 deg: one box around both
// would span the whole latitude band).
function queryAreas(aoi) {
  if (aoi.type === "Point") {
    const [x, y] = aoi.coordinates;
    return [{ point: [x, y], wkt: `POINT(${x} ${y})`, geojson: aoi }];
  }
  return aoiParts(aoi).map(([w, s, e, n]) => {
    const ring = [[w, s], [e, s], [e, n], [w, n], [w, s]];
    return { bbox: [w, s, e, n], wkt: `POLYGON((${ring.map((p) => p.join(" ")).join(",")}))`, geojson: { type: "Polygon", coordinates: [ring] } };
  });
}

// ------------------------------------------------------------ catalogue searches
// Each returns products: {when, platform, id, href, download?}.
async function cdse(area, since, collection, names = null) {
  const t = since.toISOString().replace("Z", "").slice(0, 23);
  names ||= collection === "SENTINEL-1"
    ? "(contains(Name,'_GRDH_1S') or contains(Name,'_GRDM_1S') or contains(Name,'_SLC__1S')) and not contains(Name,'_WV_') and not endswith(Name,'_COG.SAFE')"
    : "(contains(Name,'_MSIL1C_') or contains(Name,'_MSIL2A_'))";
  const filter = `Collection/Name eq '${collection}' and OData.CSC.Intersects(area=geography'SRID=4326;${area.wkt}') and ContentDate/Start gt ${t}Z and ${names}`;
  // Sentinel-3 sees a place 1-3 times a day: up to CDSE's 1,000 a query
  const top = collection === "SENTINEL-3" ? "1000" : "500";
  const s3 = collection === "SENTINEL-3"; // with each product's quicklook (map.js previews)
  const d = await getJSON(`${ODATA}?${new URLSearchParams({ $filter: filter, $top: top, $orderby: "ContentDate/Start desc", ...(s3 ? { $expand: "Assets" } : {}) })}`);
  return (d.value || []).map((p) => ({
    when: new Date(p.ContentDate.Start), platform: p.Name.slice(0, 3), id: p.Name.replace(/\.(SAFE|SEN3)$/, ""), mode: s1Mode(p.Name),
    href: `${ODATA}(${p.Id})?$expand=Attributes`, linkText: "catalogue record", geometry: p.GeoFootprint || null,
    ...(s3 ? { odata: p } : {}),
  }));
}

// Sentinel-1 modes searched: IW, EW and stripmap, which ASF names by its
// beam (S1..S6); not wave mode. A product's mode is in its name.
const S1_MODES = "IW,EW,S1,S2,S3,S4,S5,S6";
export const s1Mode = (name) => { const m = /^S1[A-D]_(IW|EW|S[1-6])_/.exec(String(name || "")); return m ? (m[1].startsWith("S") ? "SM" : m[1]) : null; };

async function asf(area, since, level) {
  const q = new URLSearchParams({
    platform: "SENTINEL-1", beamMode: S1_MODES, processingLevel: level, intersectsWith: area.wkt,
    start: iso(since), output: "geojson", maxResults: "500",
  });
  const d = await getJSON(`${ASF}?${q}`);
  return (d.features || []).map(({ properties: p, geometry }) => ({
    when: new Date(p.startTime), platform: plat(p.platform), id: p.sceneName, geometry: geometry || null, mode: s1Mode(p.sceneName),
    direction: (p.flightDirection || "").toLowerCase() || null, track: p.pathNumber ?? null,
    href: `https://search.asf.alaska.edu/#/?searchType=List%20Search&searchList=${p.sceneName}`, linkText: "ASF Vertex",
  }));
}

async function stac(url, collection, area, since, keep = () => true) {
  const body = {
    collections: [collection], intersects: area.geojson, datetime: `${iso(since)}/..`, limit: 500,
    sortby: [{ field: "properties.datetime", direction: "desc" }],
  };
  const d = await getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return (d.features || []).filter(keep).map((f) => {
    const p = f.properties || {};
    const self = (f.links || []).find((l) => l.rel === "self");
    // Landsat: WRS-2 path from the scene ID (LC09_L2SP_045028_...)
    const wrs = /^LC0[89]_\w{4}_(\d{3})\d{3}_/.exec(f.id);
    return {
      when: new Date(p.datetime || p.start_datetime), platform: plat(p.platform), id: f.id, mode: s1Mode(f.id),
      direction: p["sat:orbit_state"] || null, track: p["sat:relative_orbit"] ?? (wrs ? Number(wrs[1]) : null), geometry: f.geometry || null,
      cloud: p["eo:cloud_cover"] ?? null,
      href: self ? self.href : `${url.replace(/\/search$/, "")}/collections/${collection}/items/${f.id}`, linkText: "STAC item",
    };
  });
}

async function cmr(params, area, since, platformOf, pageSize = 500) {
  const geo = area.point ? ["point", area.point.join(",")] : ["bounding_box", area.bbox.join(",")];
  const q = new URLSearchParams([...params, geo, ["temporal[]", `${iso(since)},`], ["page_size", String(pageSize)], ["sort_key", "-start_date"]]);
  const d = await getJSON(`${CMR}?${q}`);
  return (d.items || []).map(({ meta, umm }) => {
    const file = (umm.RelatedUrls || []).find((u) => u.Type === "GET DATA" && /\.h5$/.test(u.URL));
    // NISAR names carry cycle, track and direction: NISAR_L1_PR_RSLC_032_005_A_025_...
    const ni = /^NISAR_L\d_\w\w_\w+?_\d{3}_(\d{3})_([AD])_/.exec(umm.GranuleUR);
    // LANCE and LAADS URs are opaque numbers ("LANCEMODIS:3067030029"): the product's name is its producer id
    const producer = umm.GranuleUR.includes(":") && (((umm.DataGranule || {}).Identifiers || []).find((x) => x.IdentifierType === "ProducerGranuleId") || {}).Identifier;
    return {
      when: new Date(umm.TemporalExtent.RangeDateTime.BeginningDateTime), platform: platformOf(umm), id: producer || umm.GranuleUR,
      track: ni ? Number(ni[1]) : null, direction: ni ? (ni[2] === "A" ? "ascending" : "descending") : null, geometry: ummPolygon(umm),
      href: `https://cmr.earthdata.nasa.gov/search/concepts/${meta["concept-id"]}.json`, linkText: "CMR record", download: file ? file.URL : null,
      browseUrls: (umm.RelatedUrls || []).filter((u) => u.Type === "GET RELATED VISUALIZATION" && /^https:/.test(u.URL)).map((u) => u.URL),
    };
  });
}
// A granule's footprint from its UMM (GPolygons), as GeoJSON.
function ummPolygon(umm) {
  const g = (((umm.SpatialExtent || {}).HorizontalSpatialDomain || {}).Geometry) || {};
  const polys = (g.GPolygons || []).map((p) => [p.Boundary.Points.map((q) => [q.Longitude, q.Latitude])]);
  return polys.length ? (polys.length === 1 ? { type: "Polygon", coordinates: polys[0] } : { type: "MultiPolygon", coordinates: polys }) : null;
}
// The satellite a granule names (HLS: "LANDSAT-9", "Sentinel-2B"), as S2B, L9, …
const ummPlatform = (umm) => (umm.Platforms || []).map((x) => plat(x.ShortName)).find(Boolean) || null;
const s1FromName = (umm) => (/_(S1[A-D])_/.exec(umm.GranuleUR) || [])[1] || null;

// One search can serve two sources (CDSE GRD and SLC): searches are shared by name.
const SEARCHES = {
  cdse_s1: (a, t) => cdse(a, t, "SENTINEL-1"),
  cdse_s2: (a, t) => cdse(a, t, "SENTINEL-2"),
  cdse_s3_efr: (a, t) => cdse(a, t, "SENTINEL-3", "contains(Name,'_OL_1_EFR_')"),
  cdse_s3_rbt: (a, t) => cdse(a, t, "SENTINEL-3", "contains(Name,'_SL_1_RBT_')"),
};
// MODIS and VIIRS: one CMR collection per platform (sources/modis.py); a
// place is seen 2-4 times a day per platform, so up to CMR's 2,000 a query
const cmrAll = (ids) => (a, t) => cmr(Object.values(ids).map((c) => ["collection_concept_id", c]), a, t, ummPlatform, 2000);
const WIDE_COLLECTIONS = {
  lance_modis_l1b: { Terra: "C1426414410-LANCEMODIS", Aqua: "C1426616847-LANCEMODIS" },
  laads_modis_l1b: { Terra: "C1378227407-LAADS", Aqua: "C1379758607-LAADS" },
  laads_modis_l1b_c7: { Terra: "C3861664481-LAADS", Aqua: "C3861713185-LAADS" },
  lance_modis_snow: { Terra: "C2007659515-LANCEMODIS", Aqua: "C2007631329-LANCEMODIS" },
  nsidc_modis_snow: { Terra: "C3024162910-NSIDC_CPRD", Aqua: "C3090964320-NSIDC_CPRD" },
  lance_viirs_l1b: { SNPP: "C2185497928-LANCEMODIS", N20: "C2208778455-LANCEMODIS", N21: "C3383723948-LANCEMODIS" },
  laads_viirs_l1b: { SNPP: "C2105087643-LAADS", N20: "C2103811746-LAADS", N21: "C3478403600-LAADS" },
  lance_viirs_snow: { SNPP: "C2780575359-LANCEMODIS", N20: "C2781269342-LANCEMODIS", N21: "C4232151476-LANCEMODIS" },
  nsidc_viirs_snow: { SNPP: "C3173404788-NSIDC_CPRD", N20: "C3173450415-NSIDC_CPRD", N21: "C3824913163-NSIDC_CPRD" },
};

// EUMETSAT's Data Store (OpenSearch, open to browsers): OLCI EFR over the area.
const EUMETSAT = "https://api.eumetsat.int/data/search-products/1.0.0/os";
async function eumetsat(area, since) {
  const [w, s, e, n] = area.bbox || [area.point[0], area.point[1], area.point[0], area.point[1]];
  // pages of 100: a page of 500 can time out at their gateway, whose error has no CORS header
  const feats = [];
  for (let si = 0; si < 2000; si += 100) {
    const q = new URLSearchParams({ format: "json", pi: "EO:EUM:DAT:0409", bbox: [w, s, e, n].join(","), dtstart: iso(since), si: String(si), c: "100" });
    const d = await getJSON(`${EUMETSAT}?${q}`);
    feats.push(...(d.features || []));
    if (!(d.features || []).length || feats.length >= Number(d.totalResults || 0)) break;
  }
  return feats.map((f) => {
    const p = f.properties || {}, id = String(p.identifier || f.id).replace(/\.SEN3$/, "");
    return {
      when: new Date(String(p.date || "").split("/")[0]), platform: id.slice(0, 3), id, geometry: f.geometry || null,
      href: `https://data.eumetsat.int/product/EO:EUM:DAT:0409/${encodeURIComponent(p.identifier || f.id)}`, linkText: "EUMETSAT Data Store",
    };
  });
}
const byName = (re) => (p) => re.test(p.id);

// key -> {search (shared, optional), keep, run}. Keys are the poller's source keys.
export const SOURCES = {
  cdse_grd: { search: "cdse_s1", keep: byName(/_GRD[HM]_/) },
  cdse_slc: { search: "cdse_s1", keep: byName(/_SLC__/) },
  asf_grd: { run: (a, t) => asf(a, t, "GRD_HD,GRD_MD") },
  asf_slc: { run: (a, t) => asf(a, t, "SLC") },
  pc_grd: { run: (a, t) => stac(PC, "sentinel-1-grd", a, t, (f) => s1Mode(f.id) != null) },
  pc_rtc: { run: (a, t) => stac(PC, "sentinel-1-rtc", a, t, (f) => f.id.includes("_IW_")) },
  opera_rtc: { run: (a, t) => cmr([["short_name", "OPERA_L2_RTC-S1_V1"]], a, t, s1FromName) },
  opera_cslc: { run: (a, t) => cmr([["short_name", "OPERA_L2_CSLC-S1_V1"]], a, t, s1FromName) },
  cdse_s2_l1c: { search: "cdse_s2", keep: byName(/_MSIL1C_/) },
  cdse_s2_l2a: { search: "cdse_s2", keep: byName(/_MSIL2A_/) },
  es_s2_l1c: { run: (a, t) => stac(ES, "sentinel-2-l1c", a, t) },
  es_s2_l2a: { run: (a, t) => stac(ES, "sentinel-2-l2a", a, t) },
  pc_s2_l2a: { run: (a, t) => stac(PC, "sentinel-2-l2a", a, t) },
  hls_s30: { run: (a, t) => cmr([["collection_concept_id", "C2021957295-LPCLOUD"]], a, t, ummPlatform) },
  es_landsat_l2: { run: (a, t) => stac(ES, "landsat-c2-l2", a, t, (f) => /^LC0[89]/.test(f.id)) },
  pc_landsat_l2: { run: (a, t) => stac(PC, "landsat-c2-l2", a, t, (f) => /^LC0[89]/.test(f.id)) },
  hls_l30: { run: (a, t) => cmr([["collection_concept_id", "C2021957657-LPCLOUD"]], a, t, ummPlatform) },
  asf_nisar_l0b: { run: (a, t) => cmr([["collection_concept_id", "C2853089814-ASF"]], a, t, () => "NISAR") },
  asf_nisar_rslc: { run: (a, t) => cmr([["collection_concept_id", "C2853145197-ASF"]], a, t, () => "NISAR") },
  asf_nisar_gcov: { run: (a, t) => cmr([["collection_concept_id", "C2854338529-ASF"]], a, t, () => "NISAR") },
  ...Object.fromEntries(Object.entries(WIDE_COLLECTIONS).map(([k, ids]) => [k, { run: cmrAll(ids) }])),
  cdse_s3_efr: { search: "cdse_s3_efr", keep: byName(/_NR_/) },
  cdse_s3_efr_ntc: { search: "cdse_s3_efr", keep: byName(/_NT_/) },
  cdse_s3_rbt: { search: "cdse_s3_rbt", keep: byName(/_NR_/) },
  cdse_s3_rbt_ntc: { search: "cdse_s3_rbt", keep: byName(/_NT_/) },
  cdse_s3_lfr: { run: (a, t) => cdse(a, t, "SENTINEL-3", "contains(Name,'_OL_2_LFR_')") },
  cdse_s3_lst: { run: (a, t) => cdse(a, t, "SENTINEL-3", "contains(Name,'_SL_2_LST_')") },
  pc_s3_lfr: { run: (a, t) => stac(PC, "sentinel-3-olci-lfr-l2-netcdf", a, t) },
  pc_s3_lst: { run: (a, t) => stac(PC, "sentinel-3-slstr-lst-l2-netcdf", a, t) },
  eumetsat_s3_efr: { run: eumetsat },
};
// A source searched by the page itself (map.js: USGS Landsat Level-1, from
// this site's own files): run(area, since) -> products; a run that throws an
// Error with .unsearchable set leaves the source "not searchable from here".
export function registerSource(key, run) {
  SOURCES[key] = { run };
}

// Measured but not searchable from a browser.
export const NOT_SEARCHABLE = {
  usgs_landsat_l1: "this site has not published USGS's Level-1 list for this area",
  usgs_landsat_l2: "USGS's STAC does not answer browsers",
  bhoonidhi_nisar_s_rslc: "Bhoonidhi's search needs a login",
  bhoonidhi_nisar_s_gcov: "Bhoonidhi's search needs a login",
};
// Where a person can look for those instead.
export const PORTAL = {
  usgs_landsat_l1: "https://earthexplorer.usgs.gov/", usgs_landsat_l2: "https://earthexplorer.usgs.gov/",
  bhoonidhi_nisar_s_rslc: "https://bhoonidhi.nrsc.gov.in/", bhoonidhi_nisar_s_gcov: "https://bhoonidhi.nrsc.gov.in/",
};

// ------------------------------------------------------------ running them
// Results for one AOI: {source: Promise<products>}, cached per AOI version.
const runs = new Map();
export function searchSources(aoiVersion, aoi, keys) {
  if (!runs.has(aoiVersion)) runs.set(aoiVersion, { areas: queryAreas(aoi), since: new Date(Date.now() - RECENT_DAYS * 864e5), shared: {}, bySource: {} });
  const run = runs.get(aoiVersion);
  keys.filter((k) => SOURCES[k]).forEach((k) => {
    if (run.bySource[k]) return;
    const s = SOURCES[k];
    let p;
    // each piece of the AOI searched, the results merged (a product over both once)
    const each = (fn) => Promise.all(run.areas.map((a) => fn(a, run.since))).then((lists) => {
      const seen = new Set();
      return lists.flat().filter((x) => !seen.has(x.id) && seen.add(x.id));
    });
    if (s.search) {
      run.shared[s.search] ||= each(SEARCHES[s.search]);
      p = run.shared[s.search].then((all) => all.filter(s.keep));
    } else {
      p = each(s.run);
    }
    run.bySource[k] = p.then((items) => items.filter((x) => x.when < new Date()));
  });
  return run.bySource;
}

// Groups products (each tagged with its source) into acquisitions: one
// platform (when known), consecutive products within GROUP_MS of each other.
export function groupAcquisitions(products) {
  const sorted = products.slice().sort((a, b) => a.when - b.when);
  const groups = [];
  sorted.forEach((p) => {
    const g = groups.find((x) => p.when - x.last <= GROUP_MS && (!p.platform || !x.platform || x.platform === p.platform));
    if (g) {
      g.products.push(p);
      g.last = p.when;
      g.platform ||= p.platform;
      g.direction ||= p.direction;
      if (g.track == null) g.track = p.track;
      g.mode ||= p.mode;
    } else {
      groups.push({ first: p.when, last: p.when, platform: p.platform, direction: p.direction || null, track: p.track ?? null, mode: p.mode || null, products: [p] });
    }
  });
  return groups.map((g) => ({ ...g, when: g.first }));
}
