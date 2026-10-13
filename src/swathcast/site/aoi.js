// Areas of interest: reading (GeoJSON, WKT, "w,s,e,n", the link's short
// form), checking, and the antimeridian. No DOM and no turf, so the tests
// run it under node.
//
// An AOI is a GeoJSON Point, Polygon or MultiPolygon whose longitudes are
// all within [-180, 180]: one that crosses 180 deg is stored as a
// MultiPolygon split there (the Box tool draws them so too). aoiBBox and
// aoiParts see through the split: the one is continuous across 180 (its east
// edge may exceed 180), the other is the pieces to search catalogues with.

const wrapLon = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
const r6 = (v) => Math.round(v * 1e6) / 1e6;

// ------------------------------------------------------------ reading
// Minimal WKT reader: POINT, POLYGON, MULTIPOLYGON (2D).
export function parseWKT(text) {
  const t = text.trim().toUpperCase();
  const m = t.match(/^(POINT|POLYGON|MULTIPOLYGON)\s*(\(.*\))$/s);
  if (!m) {
    if (/^(POINT|POLYGON|MULTIPOLYGON)\b/.test(t)) throw new Error("the WKT is incomplete or has a typo (check the brackets)");
    throw new Error("use GeoJSON, WKT (POINT, POLYGON or MULTIPOLYGON) or a box as west,south,east,north");
  }
  let coords;
  try {
    const json = m[2].replace(/(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)/g, "[$1,$2]").replace(/\(/g, "[").replace(/\)/g, "]");
    coords = JSON.parse(json);
  } catch {
    throw new Error(`the ${m[1]} is incomplete or has a typo (check the brackets and commas)`);
  }
  if (m[1] === "POINT") return { type: "Point", coordinates: coords[0] };
  return { type: m[1] === "POLYGON" ? "Polygon" : "MultiPolygon", coordinates: coords };
}

// "w,s,e,n" (four numbers): a box.
function parseBox(text) {
  const v = text.split(/[,\s]+/).filter(Boolean).map(Number);
  if (v.length !== 4 || v.some((x) => !Number.isFinite(x))) return null;
  let [w, s, e, n] = v;
  if (s > n) [s, n] = [n, s];
  if (e < w) e += 360; // a box across 180: west 170, east -170
  return { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] };
}

// Pasted text -> a checked AOI: {geometry, note}. Throws with a sentence a
// person can act on.
export function parseAOI(text) {
  const s = String(text || "").trim();
  if (!s) throw new Error("nothing to read: paste GeoJSON, WKT or west,south,east,north");
  let g;
  if (s.startsWith("{")) {
    let obj;
    try { obj = JSON.parse(s); } catch { throw new Error("this looks like GeoJSON but is not valid JSON"); }
    if (obj.type === "FeatureCollection") {
      if (!obj.features || !obj.features.length) throw new Error("the FeatureCollection has no features");
      g = obj.features[0].geometry;
    } else g = obj.type === "Feature" ? obj.geometry : obj;
    if (!g || !g.type) throw new Error("no geometry found in the GeoJSON");
  } else {
    g = parseBox(s) || parseWKT(s);
  }
  return checkAOI(g);
}

// ------------------------------------------------------------ checking
// Ranges, rings and the antimeridian. Returns {geometry, note}: the
// geometry normalised (longitudes within [-180, 180], split at 180), and a
// note when something was read in a way the person should know about.
export function checkAOI(g) {
  if (!g || !["Point", "Polygon", "MultiPolygon"].includes(g.type)) {
    throw new Error(`a ${g && g.type ? g.type : "shape"} cannot be an AOI: use a point or a polygon`);
  }
  const notes = [];
  const pt = (p, where) => {
    if (!Array.isArray(p) || p.length < 2 || !Number.isFinite(Number(p[0])) || !Number.isFinite(Number(p[1]))) {
      throw new Error(`${where}: every point needs a longitude and a latitude`);
    }
    const [x, y] = [Number(p[0]), Number(p[1])];
    if (Math.abs(y) > 90) {
      throw new Error(`latitude ${y} is out of range${Math.abs(x) <= 90 ? ": is it written lat, lon? Write longitude first" : ""}`);
    }
    if (Math.abs(x) > 540) throw new Error(`longitude ${x} is out of range`);
    return [x, y];
  };
  if (g.type === "Point") {
    const [x, y] = pt(g.coordinates, "the point");
    return { geometry: { type: "Point", coordinates: [r6(wrapLon(x)), y] }, note: "" };
  }
  const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
  if (!Array.isArray(polys) || !polys.length) throw new Error("the polygon has no coordinates");
  const out = [];
  polys.forEach((rings) => {
    if (!Array.isArray(rings) || !rings.length) throw new Error("a polygon has no rings");
    let ring = rings[0].map((p) => pt(p, "the polygon"));
    const distinct = new Set(ring.map((p) => p.join(",")));
    if (distinct.size < 3) throw new Error("a polygon needs at least 3 different corners");
    const [a, z] = [ring[0], ring[ring.length - 1]];
    if (a[0] !== z[0] || a[1] !== z[1]) { ring = [...ring, a]; notes.push("closed the ring"); }
    out.push(...splitAt180(ring, notes));
  });
  const geometry = out.length === 1 ? { type: "Polygon", coordinates: [out[0]] } : { type: "MultiPolygon", coordinates: out.map((r) => [r]) };
  return { geometry, note: [...new Set(notes)].join("; ") };
}

// A ring as one or two rings within [-180, 180]. A ring whose longitudes
// jump by more than 180 between corners is read as crossing 180 (the short
// way round), except one that winds around a pole, which is kept as it is.
function splitAt180(ring, notes) {
  const cont = [ring[0].slice()];
  for (let i = 1; i < ring.length; i++) {
    const prev = cont[i - 1][0];
    let x = ring[i][0];
    x += 360 * Math.round((prev - x) / 360);
    cont.push([x, ring[i][1]]);
  }
  const winds = Math.abs(cont[cont.length - 1][0] - cont[0][0]) > 1;
  if (winds) return [ring.map(([x, y]) => [Math.abs(x) <= 180 ? x : wrapLon(x), y])];
  // shift the whole ring so its west edge is within [-180, 180)
  const w = Math.min(...cont.map((p) => p[0]));
  const k = -360 * Math.floor((w + 180) / 360);
  const r = cont.map(([x, y]) => [x + k, y]);
  const e = Math.max(...r.map((p) => p[0]));
  const crossed = ring.some((p, i) => i > 0 && Math.abs(p[0] - ring[i - 1][0]) > 180) || ring.some((p) => Math.abs(p[0]) > 180);
  if (e <= 180) return [r.map(([x, y]) => [r6(x), y])];
  if (crossed) notes.push("read as crossing 180°");
  const west = clipX(r, 180, -1), east = clipX(r, 180, 1).map(([x, y]) => [x - 360, y]);
  return [west, east].filter((x) => x.length >= 4).map((x) => x.map(([a, b]) => [r6(a), r6(b)]));
}

// Sutherland-Hodgman against the vertical line x = x0: keep side < x0
// (side -1) or > x0 (side 1). Returns a closed ring (or []).
function clipX(ring, x0, side) {
  const inside = (p) => (side < 0 ? p[0] <= x0 : p[0] >= x0);
  const cut = (p, q) => { const t = (x0 - p[0]) / (q[0] - p[0]); return [x0, p[1] + t * (q[1] - p[1])]; };
  const open = ring.slice(0, -1), out = [];
  open.forEach((cur, i) => {
    const prev = open[(i + open.length - 1) % open.length];
    if (inside(cur)) {
      if (!inside(prev)) out.push(cut(prev, cur));
      out.push(cur);
    } else if (inside(prev)) out.push(cut(prev, cur));
  });
  return out.length >= 3 ? [...out, out[0]] : [];
}

// ------------------------------------------------------------ extent
const polysOf = (g) => (g.type === "Point" ? [] : g.type === "Polygon" ? [g.coordinates] : g.coordinates);
const boxOf = (pts) => {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
};

// The pieces of the AOI, one [w, s, e, n] each, all within [-180, 180]: what
// a catalogue is searched with. A Point gives one zero-size box.
export function aoiParts(g) {
  if (g.type === "Point") return [[g.coordinates[0], g.coordinates[1], g.coordinates[0], g.coordinates[1]]];
  return polysOf(g).map((rings) => boxOf(rings[0]));
}

// One [w, s, e, n] around the whole AOI, continuous across 180 (east may
// exceed 180): pieces west of 0 move east by 360 when that makes it narrower.
export function aoiBBox(g) {
  const parts = aoiParts(g);
  const span = (ps) => Math.max(...ps.map((p) => p[2])) - Math.min(...ps.map((p) => p[0]));
  const shifted = parts.map((p) => ((p[0] + p[2]) / 2 < 0 ? [p[0] + 360, p[1], p[2] + 360, p[3]] : p));
  const use = parts.length > 1 && span(shifted) < span(parts) ? shifted : parts;
  return [Math.min(...use.map((p) => p[0])), Math.min(...use.map((p) => p[1])), Math.max(...use.map((p) => p[2])), Math.max(...use.map((p) => p[3]))];
}

// The AOI's centre, [lon, lat] with lon within [-180, 180] (for local times).
export function aoiCenter(g) {
  if (g.type === "Point") return g.coordinates.slice(0, 2);
  const [w, s, e, n] = aoiBBox(g);
  return [r6(wrapLon((w + e) / 2)), (s + n) / 2];
}

// ------------------------------------------------------------ the link's form
// A point ("lon,lat"), one ring ("lon,lat;lon,lat;..."), else GeoJSON.
const r5 = (v) => Math.round(v * 1e5) / 1e5;
export function aoiParam(g) {
  if (g.type === "Point") return g.coordinates.map(r5).join(",");
  if (g.type === "Polygon" && g.coordinates.length === 1) return g.coordinates[0].map((pt) => pt.map(r5).join(",")).join(";");
  return JSON.stringify(g);
}
export function aoiFromParam(text) {
  if (text.trim().startsWith("{")) return parseAOI(text).geometry;
  const pts = text.split(";").map((pt) => pt.split(",").map(Number));
  if (!pts.length || pts.some((pt) => pt.length !== 2 || pt.some((v) => !Number.isFinite(v)))) throw new Error("not lon,lat pairs");
  if (pts.length === 1) return checkAOI({ type: "Point", coordinates: pts[0] }).geometry;
  return checkAOI({ type: "Polygon", coordinates: [pts] }).geometry;
}
