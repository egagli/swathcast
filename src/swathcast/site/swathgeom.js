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
export const GEOMETRY = {
  "sentinel-1": { look: "right-looking radar", modes: { IW: [369, 619], EW: [215, 625], SM: [215, 625] }, default: "IW" },
  nisar: { look: "left-looking radar", modes: { L: [-688, -446] }, default: "L" },
  "sentinel-2": { look: "looks straight down", modes: { MSI: [-145, 145] }, default: "MSI" },
  landsat: { look: "looks straight down", modes: { OLI: [-92.5, 92.5] }, default: "OLI" },
};
export const geometryOf = (sensor) => GEOMETRY[sensor && sensor.startsWith("landsat") ? "landsat" : sensor];
export function swathRange(sensor, mode) {
  const g = geometryOf(sensor);
  if (!g) return null;
  return g.modes[mode] || g.modes[g.default];
}

export const sensorOf = (platform) => (platform.startsWith("S1") ? "sentinel-1"
  : platform.startsWith("S2") ? "sentinel-2"
    : platform === "L8" ? "landsat-8" : platform === "L9" ? "landsat-9" : platform === "NISAR" ? "nisar" : null);

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
// The two ends of the instrument's line of sight across the track at t:
// [near, far] (or [left, right] for nadir-looking ones), with the sub-point.
export function crossTrack(satrec, t, range) {
  const p = position(satrec, new Date(t));
  const h = headingAt(satrec, t);
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

