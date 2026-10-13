// What the catalogue searches share (recent.js: over an AOI; history.js:
// one footprint): endpoints, platform names, and a JSON fetch that retries
// once, after a short wait, on a refusal or a server error (CDSE's firewall
// and ASF both drop the odd request under load).
export const ASF = "https://api.daac.asf.alaska.edu/services/search/param";
export const CMR = "https://cmr.earthdata.nasa.gov/search/granules.umm_json";
export const ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products";
export const PC = "https://planetarycomputer.microsoft.com/api/stac/v1/search";
export const ES = "https://earth-search.aws.element84.com/v1/search";

export const iso = (d) => d.toISOString().replace(/\.\d+Z$/, "Z");

// Catalogue platform names -> this site's: "Sentinel-1C" -> "S1C"; null if unknown.
const PLATFORM = {
  "sentinel-1a": "S1A", "sentinel-1c": "S1C", "sentinel-1d": "S1D",
  "sentinel-2a": "S2A", "sentinel-2b": "S2B", "sentinel-2c": "S2C",
  "landsat-8": "L8", "landsat-9": "L9", "landsat_8": "L8", "landsat_9": "L9", nisar: "NISAR",
  terra: "Terra", aqua: "Aqua", "suomi-npp": "SNPP", "noaa-20": "N20", "noaa-21": "N21",
  "sentinel-3a": "S3A", "sentinel-3b": "S3B",
};
export const plat = (p) => PLATFORM[String(p || "").toLowerCase()] || null;

const RETRY = new Set([403, 429, 500, 502, 503, 504]);
export const RETRY_MS = 2000;
export async function getJSON(url, init, { retries = 1, wait = RETRY_MS } = {}) {
  for (let attempt = 0; ; attempt++) {
    let r;
    try {
      r = await fetch(url, init);
    } catch (err) {
      if (attempt < retries) { await new Promise((res) => setTimeout(res, wait)); continue; }
      throw new Error(`${new URL(url).host}: ${err.message}`);
    }
    if (r.ok) return r.json();
    if (attempt < retries && RETRY.has(r.status)) { await new Promise((res) => setTimeout(res, wait)); continue; }
    throw new Error(`${new URL(url).host} HTTP ${r.status}`);
  }
}
