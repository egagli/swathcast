// Draping a browse image on the map exactly. MapLibre stretches an image
// linearly in Web Mercator between its four corners, but browse images are
// on a lat/lon grid (NISAR, Sentinel-1 GRD previews) or a UTM grid
// (Sentinel-2, Landsat, Sentinel-1 RTC): stretched as they are, a 3 deg tall
// image at 48 N lands 2.4 km off in its middle. So each image is resampled
// first onto a grid that is regular in Web Mercator, over its lon/lat box;
// those four corners are then exact.
//
// An image's grid ("where"): {kind: "latlon", bbox: [w, s, e, n]} (the
// image's own edges), or {kind: "utm", epsg, transform, shape} (the full
// raster's affine transform and [rows, cols]; the image may be a smaller
// rendering of it). The mapping functions are pure (tests run them under
// node); resample() needs a canvas.
import { utmToLonLat } from "./history.js";

const D2R = Math.PI / 180;
const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * D2R) / 2));
const latOfMercY = (y) => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) / D2R;

// lon/lat (WGS84) to UTM [easting, northing] (Snyder 1987), the inverse of
// history.js utmToLonLat; to well under a metre within a zone.
export function lonLatToUtm(lon, lat, zone, south) {
  const a = 6378137, f = 1 / 298.257223563, k0 = 0.9996, e2 = f * (2 - f), ep2 = e2 / (1 - e2);
  const phi = lat * D2R, lam = lon * D2R, lam0 = ((zone - 1) * 6 - 180 + 3) * D2R;
  const N = a / Math.sqrt(1 - e2 * Math.sin(phi) ** 2), T = Math.tan(phi) ** 2, C = ep2 * Math.cos(phi) ** 2;
  const A = Math.cos(phi) * (lam - lam0);
  const M = a * ((1 - e2 / 4 - (3 * e2 * e2) / 64 - (5 * e2 ** 3) / 256) * phi
    - ((3 * e2) / 8 + (3 * e2 * e2) / 32 + (45 * e2 ** 3) / 1024) * Math.sin(2 * phi)
    + ((15 * e2 * e2) / 256 + (45 * e2 ** 3) / 1024) * Math.sin(4 * phi)
    - ((35 * e2 ** 3) / 3072) * Math.sin(6 * phi));
  const x = k0 * N * (A + ((1 - T + C) * A ** 3) / 6 + ((5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5) / 120) + 500000;
  let y = k0 * (M + N * Math.tan(phi) * (A * A / 2 + ((5 - T + 9 * C + 4 * C * C) * A ** 4) / 24
    + ((61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6) / 720));
  if (south) y += 10000000;
  return [x, y];
}

const utmZone = (epsg) => ({ zone: epsg % 100, south: Math.floor(epsg / 100) === 327 });

// The image's four corners (top-left, clockwise) as lon/lat.
export function imageCorners(where) {
  if (where.kind === "latlon") {
    const [w, s, e, n] = where.bbox;
    return [[w, n], [e, n], [e, s], [w, s]];
  }
  const { zone, south } = utmZone(where.epsg), tr = where.transform, [rows, cols] = where.shape;
  const at = (c, r) => utmToLonLat(tr[2] + c * tr[0] + r * tr[1], tr[5] + c * tr[3] + r * tr[4], zone, south);
  return [at(0, 0), at(cols, 0), at(cols, rows), at(0, rows)];
}

// lon/lat -> the image's pixel [u, v] (0..width, 0..height), for an image
// of width x height pixels.
export function lonLatToPixel(where, width, height) {
  if (where.kind === "latlon") {
    const [w, s, e, n] = where.bbox;
    return (lon, lat) => [((lon - w) / (e - w)) * width, ((n - lat) / (n - s)) * height];
  }
  const { zone, south } = utmZone(where.epsg), tr = where.transform, [rows, cols] = where.shape;
  // the inverse of a north-up affine (no rotation terms in these rasters)
  return (lon, lat) => {
    const [x, y] = lonLatToUtm(lon, lat, zone, south);
    const c = (x - tr[2]) / tr[0], r = (y - tr[5]) / tr[4];
    return [(c / cols) * width, (r / rows) * height];
  };
}

// The output grid: the image's lon/lat box, its rows regular in Web
// Mercator. Returns {bbox, toSource(i, j) -> [u, v]} for an output of
// outW x outH pixels (i, j: output pixel centres).
export function mercatorGrid(where, width, height, outW, outH) {
  const cs = imageCorners(where);
  const lons = cs.map((p) => p[0]), lats = cs.map((p) => p[1]);
  const bbox = [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
  const [w, s, e, n] = bbox, y0 = mercY(n), y1 = mercY(s);
  const src = lonLatToPixel(where, width, height);
  const toSource = (i, j) => src(w + ((i + 0.5) / outW) * (e - w), latOfMercY(y0 - ((j + 0.5) / outH) * (y0 - y1)));
  return { bbox, toSource };
}

// Resamples a canvas (the keyed browse image) onto the Mercator grid:
// returns {canvas, corners}. The source position is computed exactly every
// STEP pixels and interpolated between (well under a pixel here).
const STEP = 16;
export function resample(srcCanvas, where) {
  const width = srcCanvas.width, height = srcCanvas.height;
  const outW = width, outH = height;
  const { bbox, toSource } = mercatorGrid(where, width, height, outW, outH);
  const src = srcCanvas.getContext("2d").getImageData(0, 0, width, height).data;
  const out = document.createElement("canvas");
  out.width = outW;
  out.height = outH;
  const ctx = out.getContext("2d"), img = ctx.createImageData(outW, outH), dst = img.data;
  const gx = Math.ceil(outW / STEP) + 1, gy = Math.ceil(outH / STEP) + 1;
  const grid = new Float64Array(gx * gy * 2);
  for (let b = 0; b < gy; b++) for (let a = 0; a < gx; a++) {
    const [u, v] = toSource(Math.min(a * STEP, outW - 1), Math.min(b * STEP, outH - 1));
    grid[(b * gx + a) * 2] = u; grid[(b * gx + a) * 2 + 1] = v;
  }
  for (let j = 0; j < outH; j++) {
    const b = Math.min(Math.floor(j / STEP), gy - 2), fy = (j - b * STEP) / STEP;
    for (let i = 0; i < outW; i++) {
      const a = Math.min(Math.floor(i / STEP), gx - 2), fx = (i - a * STEP) / STEP;
      const k = (b * gx + a) * 2, k2 = ((b + 1) * gx + a) * 2;
      const u = (grid[k] * (1 - fx) + grid[k + 2] * fx) * (1 - fy) + (grid[k2] * (1 - fx) + grid[k2 + 2] * fx) * fy;
      const v = (grid[k + 1] * (1 - fx) + grid[k + 3] * fx) * (1 - fy) + (grid[k2 + 1] * (1 - fx) + grid[k2 + 3] * fx) * fy;
      const x = Math.floor(u), y = Math.floor(v);
      if (x < 0 || y < 0 || x >= width || y >= height) continue; // outside the image: transparent
      const s = (y * width + x) * 4, d = (j * outW + i) * 4;
      dst[d] = src[s]; dst[d + 1] = src[s + 1]; dst[d + 2] = src[s + 2]; dst[d + 3] = src[s + 3];
    }
  }
  ctx.putImageData(img, 0, 0);
  const [w, s, e, n] = bbox;
  return { canvas: out, corners: [[w, n], [e, n], [e, s], [w, s]] };
}

// An image whose UTM grid is not published (OPERA's RTC-S1 browse), fitted
// to its data: the data's pixel extent [x0, y0, x1, y1] (inclusive) spans the
// footprint's UTM box [X0, Y0, X1, Y1] east-west, with square pixels, centred
// on the box north-south (the catalogue's footprint leaves out the bursts'
// overlap, so its north-south extent is short). Approximate: against
// Planetary Computer's exact RTC of the same passes (6 bursts, 2026-10), its
// corners land 1.5-3 km off (fitting the corners instead is no better).
export function fitUtmToData(epsg, [X0, Y0, X1, Y1], [x0, y0, x1, y1], width, height) {
  const s = (X1 - X0) / (x1 - x0 + 1);
  const yc = (y0 + y1 + 1) / 2, Yc = (Y0 + Y1) / 2;
  return { kind: "utm", epsg, transform: [s, 0, X0 - x0 * s, 0, -s, Yc + yc * s], shape: [height, width] };
}

// The UTM zone (as an EPSG code) of a lon/lat point, and a ring's box in it.
export const utmEpsg = (lon, lat) => (lat < 0 ? 32700 : 32600) + Math.min(60, Math.floor((lon + 180) / 6) + 1);
export function utmBox(epsg, ring) {
  const { zone, south } = utmZone(epsg);
  const xy = ring.map(([lon, lat]) => lonLatToUtm(lon, lat, zone, south));
  const xs = xy.map((p) => p[0]), ys = xy.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
