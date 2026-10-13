// Orbit and viewing geometry for the live satellites (sats.js), with no
// imports, so it can be checked on its own (tests/test_site_swath.py):
// SGP4 positions (satellite.js, a global), ground heading, and the strip of
// ground each instrument sees across its track.
/* global satellite */

export const TRACK_STEP_S = 20;
const R_KM = 6371.0088;

// Where each instrument looks, as signed distances across the ground track
// (km, positive to the right of the direction of travel), per mode:
//   Sentinel-1 IW  incidence 29.1-46.0 deg, 250 km, right-looking: GRD
//                  footprint centres 488-500 km right of the track (ephemeris.py)
//   Sentinel-1 EW  incidence 18.9-47.0 deg, 400 km
//   Sentinel-1 SM  one of six 80 km beams between ~20 and 47 deg; the plan
//                  does not say which, so the extent of all six is drawn
//   NISAR          ~240 km swath, left-looking: frame centres 561-579 km left
//                  (plans.py NISAR_FRAME_OFFSET_KM = 567)
//   Sentinel-2 MSI 290 km, centred on nadir
//   Landsat OLI    185 km, centred on nadir
//   MODIS, VIIRS   scanning across the track: 2,342 km and 3,036-3,096 km
//                  (WIDE_SWATH_KM: measured on real granules, per platform)
//   Sentinel-3     SLSTR nadir view -500 to +996 km, OLCI -336 to +975 km:
//                  both tilted away from the sun, to the right of travel
export const GEOMETRY = {
  "sentinel-1": { look: "right-looking radar", modes: { IW: [369, 619], EW: [215, 625], SM: [215, 625] }, default: "IW" },
  nisar: { look: "left-looking radar", modes: { L: [-688, -446] }, default: "L" },
  "sentinel-2": { look: "looks straight down", modes: { MSI: [-145, 145] }, default: "MSI" },
  landsat: { look: "looks straight down", modes: { OLI: [-92.5, 92.5] }, default: "OLI" },
  // scan: "inertial": whiskbroom scanners on satellites that are not yaw-steered, so
  // each scan line is perpendicular to the orbit's inertial direction, not to the
  // ground track over the rotating Earth (2.9 deg apart, median; up to ~4 deg at
  // the equator, ~70 km along-track at MODIS's swath edge). Checked on 200
  // LANCE granules' corners (2026-10-13): median 61-77 km off with the ground
  // heading, 17-35 km with the inertial one (see docs/methodology.md).
  modis: { look: "scans across the track", modes: { MODIS: [-1175, 1175] }, default: "MODIS", scan: "inertial" },
  viirs: { look: "scans across the track", modes: { VIIRS: [-1531, 1531] }, default: "VIIRS", scan: "inertial" },
  "sentinel-3": { look: "looks down, tilted away from the sun", modes: { SLSTR: [-500, 996], OLCI: [-336, 975] }, default: "SLSTR" },
};
export const geometryOf = (sensor) => GEOMETRY[sensor && sensor.startsWith("landsat") ? "landsat" : sensor];

// The wide-swath sensors without a plan, per platform (mirrors
// swathcast/wideswath.py SWATH_KM and S3_OLCI_KM): the signed cross-track
// extent of the imaged strip (km, + right of travel), measured on real
// footprints (2026-10-13), and the orbit's altitude for the view angle.
export const WIDE_SWATH_KM = {
  Terra: [-1178, 1178], Aqua: [-1171, 1171], SNPP: [-1548, 1548], N20: [-1526, 1526], N21: [-1518, 1518],
  S3A: [-500, 996], S3B: [-500, 996],
};
export const S3_OLCI_KM = [-336, 975];
export const ALTITUDE_KM = { Terra: 705, Aqua: 705, SNPP: 834, N20: 834, N21: 834, S3A: 814, S3B: 814 };
const EARTH_R_KM = 6371.0;
// View zenith angle (deg) at a point xtrackKm from the ground track, seen
// from altKm (spherical Earth), as wideswath.view_zenith_deg.
export function viewZenith(xtrackKm, altKm) {
  const g = Math.abs(xtrackKm) / EARTH_R_KM, r = EARTH_R_KM + altKm;
  const los = Math.atan2(Math.sin(g) * EARTH_R_KM, r - EARTH_R_KM * Math.cos(g));
  return ((los + g) * 180) / Math.PI;
}
export function swathRange(sensor, mode) {
  const g = geometryOf(sensor);
  if (!g) return null;
  return g.modes[mode] || g.modes[g.default];
}

const WIDE_SENSOR = { Terra: "modis", Aqua: "modis", SNPP: "viirs", N20: "viirs", N21: "viirs" };
export const sensorOf = (platform) => (platform.startsWith("S1") ? "sentinel-1"
  : platform.startsWith("S2") ? "sentinel-2" : platform.startsWith("S3") ? "sentinel-3"
    : platform === "L8" ? "landsat-8" : platform === "L9" ? "landsat-9" : platform === "NISAR" ? "nisar" : WIDE_SENSOR[platform] || null);

export function position(satrec, date) {
  const pv = satellite.propagate(satrec, date);
  if (!pv || !pv.position || typeof pv.position === "boolean") return null;
  const geo = satellite.eciToGeodetic(pv.position, satellite.gstime(date));
  return { lon: satellite.degreesLong(geo.longitude), lat: satellite.degreesLat(geo.latitude), km: geo.height };
}

// Ground heading (deg from north) of the sub-satellite point at t: the
// direction of travel over the rotating Earth, which is what a zero-Doppler
// radar looks perpendicular to.
export function headingAt(satrec, t) {
  const a = position(satrec, new Date(t - 500)), b = position(satrec, new Date(t + 500));
  if (!a || !b) return null;
  return bearing(a.lon, a.lat, b.lon, b.lat);
}

const D2R = Math.PI / 180;
function bearing(lon1, lat1, lon2, lat2) {
  const p1 = lat1 * D2R, p2 = lat2 * D2R, dl = (lon2 - lon1) * D2R;
  const y = Math.sin(dl) * Math.cos(p2), x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return Math.atan2(y, x) / D2R;
}
// The point distKm from (lon, lat) along the great circle at bearing brg.
export function destination(lon, lat, brg, distKm) {
  const d = distKm / R_KM, b = brg * D2R, p1 = lat * D2R, l1 = lon * D2R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [((l2 / D2R + 540) % 360) - 180, p2 / D2R];
}
// The orbit's direction of travel in an Earth frame frozen at t (the inertial
// velocity's heading): what a scanner without yaw steering scans across.
export function inertialHeadingAt(satrec, t) {
  const gmst = satellite.gstime(new Date(t));
  const ll = (dt) => {
    const pv = satellite.propagate(satrec, new Date(t + dt));
    if (!pv || !pv.position || typeof pv.position === "boolean") return null;
    const geo = satellite.eciToGeodetic(pv.position, gmst);
    return [satellite.degreesLong(geo.longitude), satellite.degreesLat(geo.latitude)];
  };
  const a = ll(-500), b = ll(500);
  return a && b ? bearing(a[0], a[1], b[0], b[1]) : null;
}
// The heading an instrument's line of sight is perpendicular to: the ground
// track over the rotating Earth (radars steer to zero Doppler; Sentinel-2,
// Landsat and Sentinel-3 are yaw-steered), or the inertial one (MODIS, VIIRS).
export function scanHeadingAt(satrec, t, sensor) {
  const g = geometryOf(sensor);
  return g && g.scan === "inertial" ? inertialHeadingAt(satrec, t) : headingAt(satrec, t);
}
// The two ends of the instrument's line of sight across the track at t:
// [near, far] (or [left, right] for nadir-looking ones), with the sub-point.
export function crossTrack(satrec, t, range, sensor = null) {
  const p = position(satrec, new Date(t));
  const h = scanHeadingAt(satrec, t, sensor);
  if (!p || h == null) return null;
  const pt = (d) => (d >= 0 ? destination(p.lon, p.lat, h + 90, d) : destination(p.lon, p.lat, h - 90, -d));
  return { sub: p, heading: h, a: pt(range[0]), b: pt(range[1]) };
}

// Ground track with continuous longitudes (no jump at the antimeridian):
// [lon, lat, km, t (ms)] per step.
export function groundTrack(satrec, t0, t1, stepS = TRACK_STEP_S) {
  const coords = [];
  let prev = null;
  for (let t = t0; t <= t1; t += stepS * 1000) {
    const p = position(satrec, new Date(t));
    if (!p) continue;
    let lon = p.lon;
    if (prev != null) lon += Math.round((prev - lon) / 360) * 360;
    coords.push([lon, p.lat, p.km, t]);
    prev = lon;
  }
  return coords;
}

const hav = (lon1, lat1, lon2, lat2) => {
  const p1 = lat1 * D2R, p2 = lat2 * D2R, a = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(((lon2 - lon1) * D2R) / 2) ** 2;
  return 2 * R_KM * Math.asin(Math.sqrt(Math.min(1, a)));
};
// When the ground track passes closest to (lon, lat) in [t0, t1] (ms), as
// ephemeris.closest_approach: {t (ms), xtrack (km, signed: + right of the
// direction of travel), ascending}; null if the closest point is at an end
// of the window (the pass is outside it).
export function closestApproach(satrec, lon, lat, t0, t1, stepS = 10) {
  let best = null;
  for (let t = t0; t <= t1; t += stepS * 1000) {
    const p = position(satrec, new Date(t));
    if (!p) continue;
    const d = hav(lon, lat, p.lon, p.lat);
    if (!best || d < best.d) best = { t, d };
  }
  if (!best || best.t - t0 < stepS * 1000 || t1 - best.t < stepS * 1000) return null;
  // golden-section search between the neighbouring steps, to ~0.05 s
  let a = best.t - stepS * 1000, b = best.t + stepS * 1000;
  const g = (Math.sqrt(5) - 1) / 2, f = (t) => { const p = position(satrec, new Date(t)); return p ? hav(lon, lat, p.lon, p.lat) : Infinity; };
  let c = b - g * (b - a), e = a + g * (b - a), fc = f(c), fe = f(e);
  while (b - a > 50) {
    if (fc < fe) { b = e; e = c; fe = fc; c = b - g * (b - a); fc = f(c); } else { a = c; c = e; fc = fe; e = a + g * (b - a); fe = f(e); }
  }
  const t = (a + b) / 2, p = position(satrec, new Date(t)), h = headingAt(satrec, t);
  if (!p || h == null) return null;
  const toPoint = bearing(p.lon, p.lat, lon, lat);
  const right = Math.sin((toPoint - h) * D2R) > 0;
  const q = position(satrec, new Date(t + 1000));
  return { t, xtrack: (right ? 1 : -1) * hav(lon, lat, p.lon, p.lat), ascending: q ? q.lat > p.lat : null };
}

// When a scanner without yaw steering (MODIS, VIIRS) images (lon, lat): the
// instant its scan line passes through the point, from the closest approach
// t0 (ms). The line is skewed from the ground track's perpendicular, so at the
// swath edge this is up to ~10 s from t0. Secant steps on the point's
// along-track offset from the scan line.
export function scanTime(satrec, lon, lat, t0) {
  const off = (t) => {
    const p = position(satrec, new Date(t)), h = inertialHeadingAt(satrec, t);
    if (!p || h == null) return null;
    return hav(p.lon, p.lat, lon, lat) * Math.cos((bearing(p.lon, p.lat, lon, lat) - h) * D2R);
  };
  let a = t0, b = t0 + 2000, fa = off(a), fb = off(b);
  for (let i = 0; i < 6 && fa != null && fb != null && fb !== fa; i++) {
    const c = b - (fb * (b - a)) / (fb - fa);
    [a, fa, b, fb] = [b, fb, c, off(c)];
    if (Math.abs(b - a) < 20) break;
  }
  return fb == null || !Number.isFinite(b) || Math.abs(b - t0) > 60e3 ? t0 : b;
}

