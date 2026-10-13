// A 2D canvas over the map, drawn in the same frame as MapLibre's own layers
// (from a custom layer's render call) with MapLibre's own projection matrix.
// It draws what MapLibre's GeoJSON layers cannot:
//   - points above the ground (the satellites, ~700 km up, and their orbits),
//     hidden where the Earth is between them and the camera;
//   - geometry poleward of 85 deg on the globe (Web Mercator tiles stop at
//     85.05 deg): spherical polygons with great-circle edges, clipped to the
//     visible hemisphere by d3-geo.
// Painters (paint(ctx, proj)) are called bottom to top on every map frame.
// On the globe MapLibre's world is the unit sphere: x = sin(lon) cos(lat),
// y = sin(lat), z = cos(lon) cos(lat); in flat (Mercator) mode, Mercator
// coordinates in [0, 1]. Heights are drawn in units of MapLibre's sphere,
// R = 6371.0088 km.
/* global maplibregl */
import { geoProjection, geoPath, geoRotation, geoArea, geoContains } from "https://cdn.jsdelivr.net/npm/d3-geo@3.1.1/+esm";

export const EARTH_R_KM = 6371.0088;
const D2R = Math.PI / 180;
const MAX_MERC_LAT = 85.0511;

export const sphereVec = (lon, lat, km = 0) => {
  const r = 1 + km / EARTH_R_KM, c = Math.cos(lat * D2R);
  return [r * Math.sin(lon * D2R) * c, r * Math.sin(lat * D2R), r * Math.cos(lon * D2R) * c];
};
const mercX = (lon) => (lon + 180) / 360;
const mercY = (lat) => {
  const p = Math.max(-MAX_MERC_LAT, Math.min(MAX_MERC_LAT, lat)) * D2R;
  return (180 - (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + p / 2))) / 360;
};
const mul = (m, x, y, z) => [ // column-major 4x4 (gl-matrix) times [x, y, z, 1]
  m[0] * x + m[4] * y + m[8] * z + m[12], m[1] * x + m[5] * y + m[9] * z + m[13],
  m[2] * x + m[6] * y + m[10] * z + m[14], m[3] * x + m[7] * y + m[11] * z + m[15],
];

// The camera position: the point the projection sends to w = 0 at the centre
// of the view, i.e. M^-1 (0, 0, 1, 0) (third column of the inverse).
function eyeOf(m) {
  const inv = invert4(m);
  if (!inv) return null;
  const w = inv[11];
  return Math.abs(w) < 1e-12 ? null : [inv[8] / w, inv[9] / w, inv[10] / w];
}
function invert4(a) {
  const [a00, a01, a02, a03, a10, a11, a12, a13, a20, a21, a22, a23, a30, a31, a32, a33] = a;
  const b00 = a00 * a11 - a01 * a10, b01 = a00 * a12 - a02 * a10, b02 = a00 * a13 - a03 * a10;
  const b03 = a01 * a12 - a02 * a11, b04 = a01 * a13 - a03 * a11, b05 = a02 * a13 - a03 * a12;
  const b06 = a20 * a31 - a21 * a30, b07 = a20 * a32 - a22 * a30, b08 = a20 * a33 - a23 * a30;
  const b09 = a21 * a32 - a22 * a31, b10 = a21 * a33 - a23 * a31, b11 = a22 * a33 - a23 * a32;
  let det = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
  if (!det) return null;
  det = 1 / det;
  return [
    (a11 * b11 - a12 * b10 + a13 * b09) * det, (a02 * b10 - a01 * b11 - a03 * b09) * det,
    (a31 * b05 - a32 * b04 + a33 * b03) * det, (a22 * b04 - a21 * b05 - a23 * b03) * det,
    (a12 * b08 - a10 * b11 - a13 * b07) * det, (a00 * b11 - a02 * b08 + a03 * b07) * det,
    (a32 * b02 - a30 * b05 - a33 * b01) * det, (a20 * b05 - a22 * b02 + a23 * b01) * det,
    (a10 * b10 - a11 * b08 + a13 * b06) * det, (a01 * b08 - a00 * b10 - a03 * b06) * det,
    (a30 * b04 - a31 * b02 + a33 * b00) * det, (a21 * b02 - a20 * b04 - a23 * b00) * det,
    (a11 * b07 - a10 * b09 - a12 * b06) * det, (a00 * b09 - a01 * b07 + a02 * b06) * det,
    (a31 * b01 - a30 * b03 - a32 * b00) * det, (a20 * b03 - a21 * b01 + a22 * b00) * det,
  ];
}

// d3 reads a ring wound the other way as the rest of the sphere; footprints
// and swaths are far smaller than a hemisphere.
export function spherical(geometry) {
  const f = { type: "Feature", properties: {}, geometry };
  if (geoArea(f) <= 2 * Math.PI) return f;
  const rev = (poly) => poly.map((ring) => ring.slice().reverse());
  return {
    type: "Feature", properties: {},
    geometry: geometry.type === "Polygon" ? { type: "Polygon", coordinates: rev(geometry.coordinates) }
      : { type: "MultiPolygon", coordinates: geometry.coordinates.map((p) => (geoArea({ type: "Polygon", coordinates: p }) > 2 * Math.PI ? rev(p) : p)) },
  };
}

// One frame's projection: lon/lat/km -> CSS pixels, occlusion by the Earth,
// and a d3 path for spherical geometry on the ground.
class Projector {
  constructor(map, pd, w, h) {
    this.map = map;
    this.w = w; this.h = h;
    this.t = pd.projectionTransition || 0; // 1 globe, 0 flat, between: MapLibre's zoom-in transition
    this.globe = this.t > 0.5;
    this.main = pd.mainMatrix;
    this.flat = pd.fallbackMatrix || pd.mainMatrix;
    this.centerLon = map.getCenter().lng;
    this.eye = this.globe ? eyeOf(this.main) : null;
    this.eyeR = this.eye ? Math.hypot(...this.eye) : Infinity;
  }

  // Nearest copy of a longitude to the view (flat mode draws one world copy).
  near(lon) { return lon + 360 * Math.round((this.centerLon - lon) / 360); }

  clip(lon, lat, km, wrap = true) {
    if (this.t >= 0.999) { const v = sphereVec(lon, lat, km); return mul(this.main, ...v); }
    const f = mul(this.flat, mercX(wrap ? this.near(lon) : lon), mercY(lat), 0);
    if (this.t <= 0.001) return f;
    const g = mul(this.main, ...sphereVec(lon, lat, km));
    return [f[0] + (g[0] - f[0]) * this.t, f[1] + (g[1] - f[1]) * this.t, g[2], f[3] + (g[3] - f[3]) * this.t];
  }

  // [x, y] in CSS pixels, or null when behind the camera. In flat mode the
  // longitude is taken as given with wrap = false (a line already shifted
  // to one world copy), else moved to the copy nearest the view.
  px(lon, lat, km = 0, wrap = true) {
    const c = this.clip(lon, lat, km, wrap);
    if (!(c[3] > 1e-9)) return null;
    return [(c[0] / c[3] + 1) * 0.5 * this.w, (1 - c[1] / c[3]) * 0.5 * this.h];
  }

  // Is the point hidden by the Earth? (globe only; a point on the ground is
  // visible where it faces the camera, one above it while the line of sight
  // clears the sphere)
  hidden(lon, lat, km = 0) {
    if (!this.globe || !this.eye) return false;
    const q = sphereVec(lon, lat, km), e = this.eye;
    const d = [q[0] - e[0], q[1] - e[1], q[2] - e[2]];
    const a = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
    const b = 2 * (e[0] * d[0] + e[1] * d[1] + e[2] * d[2]);
    const c = e[0] * e[0] + e[1] * e[1] + e[2] * e[2] - 1;
    const disc = b * b - 4 * a * c;
    if (disc <= 0) return false;
    const t1 = (-b - Math.sqrt(disc)) / (2 * a);
    return t1 > 1e-6 && t1 < 1 - 1e-6;
  }

  // A d3 path drawing ground geometry (great-circle edges) into ctx, clipped
  // to the visible cap of the globe; in flat mode, around the view's longitude.
  path(ctx) {
    if (this._path && this._ctx === ctx) return this._path;
    const self = this;
    let proj;
    if (this.globe && this.eye) {
      const [ex, ey, ez] = this.eye, r = this.eyeR;
      const lon = Math.atan2(ex, ez) / D2R, lat = Math.asin(ey / r) / D2R;
      const rot = geoRotation([-lon, -lat]);
      const raw = (l, p) => {
        const [lo, la] = rot.invert([l / D2R, p / D2R]);
        const q = self.px(lo, la, 0) || [0, 0];
        return [q[0], -q[1]];
      };
      proj = geoProjection(raw).rotate([-lon, -lat]).clipAngle(Math.max(1, Math.acos(Math.min(1, 1 / r)) / D2R - 0.3));
    } else {
      const c = this.centerLon;
      const raw = (l, p) => {
        const q = mul(this.flat, mercX(c + l / D2R), mercY(p / D2R), 0);
        return [(q[0] / q[3] + 1) * 0.5 * this.w, -(1 - q[1] / q[3]) * 0.5 * this.h];
      };
      proj = geoProjection(raw).rotate([-c, 0]);
    }
    // d3 shifts its output so that the projection's centre lands on
    // translate(): undo that, so the raw pixels come out as they are
    proj.scale(1).translate([0, 0]).precision(0.6);
    const [r0x, r0y] = proj.rotate();
    const o = proj([-r0x, -r0y]) || [0, 0], want = this.px(-r0x, -r0y, 0) || [0, 0];
    proj.translate([want[0] - o[0], want[1] - o[1]]);
    this._ctx = ctx;
    this._path = geoPath(proj, ctx);
    return this._path;
  }
}

export class GlobeOverlay {
  constructor(view) {
    this.view = view;
    this.map = view.map;
    this.painters = [];
    this.canvas = document.createElement("canvas");
    this.canvas.className = "sky-canvas";
    this.labels = document.createElement("div");
    this.labels.className = "sky-labels";
    const box = this.map.getCanvasContainer();
    box.insertBefore(this.labels, this.map.getCanvas().nextSibling);
    box.insertBefore(this.canvas, this.labels);
    this.proj = null;
    view.ready.then(() => {
      this.map.addLayer({
        id: "sky-overlay", type: "custom", renderingMode: "2d",
        render: (gl, args) => this.draw(args),
      });
    });
    this.map.on("resize", () => this.map.triggerRepaint());
  }

  // painter: {paint(ctx, proj)}; drawn in the order added.
  add(painter) { this.painters.push(painter); this.redraw(); return painter; }

  // Redraws the overlay alone, with the last frame's projection (the camera
  // has not moved since, or MapLibre would have drawn a frame and called
  // draw itself): the satellites move every second without a map repaint.
  redraw() {
    if (!this.last) { this.map.triggerRepaint(); return; }
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => { this.pending = false; this.draw(this.last); });
  }

  draw(args) {
    this.last = args;
    const map = this.map, el = map.getCanvas();
    const w = el.clientWidth, h = el.clientHeight, dpr = window.devicePixelRatio || 1;
    if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) {
      this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
      this.canvas.style.width = `${w}px`; this.canvas.style.height = `${h}px`;
    }
    const ctx = this.canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const proj = new Projector(map, args.defaultProjectionData, w, h);
    this.proj = proj;
    this.painters.forEach((p) => {
      if (p.visible === false) return;
      ctx.save();
      try { p.paint(ctx, proj); } catch (err) { console.error(err); }
      ctx.restore();
    });
  }
}

// Footprints poleward of 85 deg, on the globe only: the part of each
// polygon inside the polar caps (the GeoJSON layers draw the rest).
export class PolarCaps {
  constructor(overlay, edgeLat = 85) {
    this.overlay = overlay;
    this.edge = edgeLat;
    this.items = []; // {feature, fill, alpha, edge}
    overlay.add(this);
  }

  set(items) {
    this.items = items.map((it) => ({ ...it, feature: spherical(it.geojson) }));
    this.overlay.redraw();
  }

  // The topmost item under a point (drawn last = on top).
  pick(lon, lat) {
    for (let i = this.items.length - 1; i >= 0; i--) if (geoContains(this.items[i].feature, [lon, lat])) return this.items[i];
    return null;
  }

  paint(ctx, proj) {
    if (!proj.globe || !this.items.length) return;
    const path = proj.path(ctx);
    const caps = [1, -1].map((s) => spherical({ type: "Polygon", coordinates: [capRing(s * this.edge)] }));
    ctx.beginPath();
    caps.forEach((c) => path(c));
    ctx.clip();
    this.items.forEach((it) => {
      ctx.beginPath();
      path(it.feature);
      ctx.globalAlpha = it.alpha;
      ctx.fillStyle = it.fill;
      ctx.fill();
      if (it.edge) { ctx.globalAlpha = Math.min(1, it.alpha) * 0.35; ctx.strokeStyle = it.stroke || "#fff"; ctx.lineWidth = 0.5; ctx.stroke(); }
    });
  }
}

// A parallel as a closed ring (spherical() picks the side that is the cap).
function capRing(lat) {
  const n = 180, ring = [];
  for (let i = 0; i <= n; i++) ring.push([-180 + (360 * i) / n, lat]);
  return ring;
}
