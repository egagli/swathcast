// A thin wrapper on MapLibre GL (globe projection) with the few operations the
// map page needs: groups of styled footprints with tooltips, popups and
// clicks, fit/pan, a globe/flat toggle, and point/box/polygon drawing.
//
// Each group is one GeoJSON source with fill, line and circle layers; a
// footprint's style lives in its feature properties, so thousands of
// footprints are one draw call. Later-added footprints draw on top.

/* global maplibregl */

const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const ESRI = (service) => [`https://server.arcgisonline.com/ArcGIS/rest/services/${service}/MapServer/tile/{z}/{y}/{x}`];
const ESRI_IMAGERY_ATTR = 'Imagery &copy; <a href="https://www.esri.com/">Esri</a>, Maxar, Earthstar Geographics, and the GIS User Community';

// Basemaps: raster tiles under the footprints. layers(dark) -> [{tiles,
// attribution, maxzoom, paint?}], drawn bottom to top; invert: in dark mode
// the layer is inverted and desaturated (OSM has no dark variant).
export const BASEMAPS = {
  streets: { label: "Streets", bg: ["#aad3df", "#0d0d0d"], layers: () => [{ tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: OSM_ATTR, maxzoom: 19, invert: true }] },
  light: {
    label: "Light", title: "Esri Light Gray Canvas (Dark Gray in dark mode): muted, so the footprint colours stand out", bg: ["#d8d8d8", "#262626"],
    layers: (dark) => [
      { tiles: ESRI(`Canvas/World_${dark ? "Dark" : "Light"}_Gray_Base`), attribution: "&copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors", maxzoom: 16 },
      { tiles: ESRI(`Canvas/World_${dark ? "Dark" : "Light"}_Gray_Reference`), attribution: "", maxzoom: 16 },
    ],
  },
  imagery: { label: "Imagery", title: "Esri World Imagery", bg: ["#0b1a2a", "#0b1a2a"], layers: () => [{ tiles: ESRI("World_Imagery"), attribution: ESRI_IMAGERY_ATTR, maxzoom: 19 }] },
  hybrid: {
    label: "Imagery + labels", title: "Esri World Imagery with Esri's boundaries and place names", bg: ["#0b1a2a", "#0b1a2a"],
    layers: () => [
      { tiles: ESRI("World_Imagery"), attribution: ESRI_IMAGERY_ATTR, maxzoom: 19 },
      { tiles: ESRI("Reference/World_Boundaries_and_Places"), attribution: "Labels &copy; Esri", maxzoom: 19 },
    ],
  },
  topo: { label: "Topographic", title: "Esri World Topographic Map", bg: ["#c9dce8", "#c9dce8"], layers: () => [{ tiles: ESRI("World_Topo_Map"), attribution: "&copy; Esri and its data contributors", maxzoom: 19 }] },
};
export const DEFAULT_BASEMAP = "light";

export function prefersDark() {
  const t = document.documentElement.dataset.theme;
  if (t === "dark") return true;
  if (t === "light") return false;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

// Raster paint: an inverted, desaturated OSM in dark mode.
function basemapPaint(dark, invert) {
  return dark && invert
    ? { "raster-brightness-min": 0.92, "raster-brightness-max": 0.08, "raster-saturation": -0.75, "raster-contrast": 0.1 }
    : { "raster-brightness-min": 0, "raster-brightness-max": 1, "raster-saturation": 0, "raster-contrast": 0 };
}

// The basemap's sources and layers, as a style fragment (ids "bm-0", "bm-1", ...).
function basemapStyle(key, dark) {
  const bm = BASEMAPS[key] || BASEMAPS[DEFAULT_BASEMAP];
  const sources = {}, layers = [];
  bm.layers(dark).forEach((l, i) => {
    sources[`bm-${i}`] = { type: "raster", tiles: l.tiles, tileSize: 256, attribution: l.attribution, maxzoom: l.maxzoom || 19 };
    layers.push({ id: `bm-${i}`, type: "raster", source: `bm-${i}`, paint: basemapPaint(dark, l.invert) });
  });
  return { sources, layers, bg: bm.bg[dark ? 1 : 0] };
}

// Map options, top right under the zoom buttons: the basemap, and a slot
// (view.optionsEl) for the page's own toggles (globe, live satellites).
class MapOptionsControl {
  constructor(view) { this.view = view; }
  onAdd() {
    const el = document.createElement("div");
    el.className = "maplibregl-ctrl maplibregl-ctrl-group map-opts basemap-ctrl";
    // on phones the options fold behind one button (CSS shows it there only)
    const fold = Object.assign(document.createElement("button"), { type: "button", className: "mo-fold", textContent: "Layers" });
    fold.setAttribute("aria-expanded", "false");
    fold.addEventListener("click", () => { const open = !el.classList.contains("open"); el.classList.toggle("open", open); fold.setAttribute("aria-expanded", String(open)); });
    el.append(fold);
    const row = document.createElement("label");
    row.className = "mo-row";
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", "Basemap");
    sel.title = "Basemap";
    Object.entries(BASEMAPS).forEach(([k, b]) => sel.append(Object.assign(document.createElement("option"), { value: k, textContent: b.label, title: b.title || "" })));
    sel.value = this.view.basemap;
    sel.addEventListener("change", () => this.view.setBasemap(sel.value));
    this.view.onBasemap.push((k) => { sel.value = k; });
    row.append(Object.assign(document.createElement("span"), { textContent: "Map" }), sel);
    this.view.optionsEl = Object.assign(document.createElement("div"), { className: "mo-checks" });
    el.append(row, this.view.optionsEl);
    return el;
  }
  onRemove() {}
}

const DEFAULT_STYLE = { color: "#333", weight: 1.5, opacity: 0.9, fillColor: null, fillOpacity: 0.1, fill: true };

export class MapView {
  constructor(container, { center = [-40, 25], zoom = 3, minZoom = 3, attribution = "", globe = true, basemap = DEFAULT_BASEMAP } = {}) {
    this.basemap = BASEMAPS[basemap] ? basemap : DEFAULT_BASEMAP;
    this.onBasemap = [];
    const bm = basemapStyle(this.basemap, prefersDark());
    this.map = new maplibregl.Map({
      container,
      center,
      zoom,
      minZoom, // further out, the footprints are too small to read
      maxZoom: 13,
      attributionControl: { compact: true, customAttribution: attribution },
      style: {
        version: 8,
        projection: { type: globe ? "globe" : "mercator" },
        sources: bm.sources,
        layers: [{ id: "bg", type: "background", paint: { "background-color": bm.bg } }, ...bm.layers],
      },
    });
    // no zoom/compass buttons: wheel, pinch, double-click and the keyboard
    // zoom; right-drag (two fingers) rotates; the legend's "Zoom in" buttons
    // go to each mission's native footprints
    this.map.addControl(new MapOptionsControl(this), "top-right");
    this.groups = [];
    this.ready = new Promise((resolve) => this.map.on("load", resolve));
    this.tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "tip", offset: 14, maxWidth: "320px" });
    this.popupRef = null;
    this.drawing = null;
    this.ready.then(() => this._wire());
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => this.applyTheme());
    }
  }

  applyTheme() {
    this.setBasemap(this.basemap, { force: true });
  }

  // Swaps the basemap's raster layers (under everything else); the sources
  // of the old one are removed, so its attribution goes with it.
  setBasemap(key, { force = false } = {}) {
    if (!BASEMAPS[key] || (key === this.basemap && !force)) return;
    this.basemap = key;
    this.onBasemap.forEach((f) => f(key));
    this.ready.then(() => {
      const map = this.map;
      if (this.basemap !== key) return;
      const style = map.getStyle();
      style.layers.filter((l) => l.id.startsWith("bm-")).forEach((l) => map.removeLayer(l.id));
      Object.keys(style.sources).filter((id) => id.startsWith("bm-")).forEach((id) => map.removeSource(id));
      const bm = basemapStyle(key, prefersDark());
      const before = map.getStyle().layers.find((l) => l.id !== "bg");
      Object.entries(bm.sources).forEach(([id, src]) => map.addSource(id, src));
      bm.layers.forEach((l) => map.addLayer(l, before && before.id));
      map.setPaintProperty("bg", "background-color", bm.bg);
    });
  }

  setGlobe(on) {
    this.map.setProjection({ type: on ? "globe" : "mercator" });
  }

  group(name, opts = {}) {
    const g = new Group(this, name, opts);
    this.groups.push(g);
    return g;
  }

  _wire() {
    const interactive = () => this.groups.filter((g) => g.interactive && g.visible).flatMap((g) => g.layerIds);
    const hit = (e) => {
      const ids = interactive().filter((id) => this.map.getLayer(id));
      if (!ids.length) return this.extraHit ? this.extraHit(e.lngLat) : null;
      const f = this.map.queryRenderedFeatures(e.point, { layers: ids })[0];
      if (!f) return this.extraHit ? this.extraHit(e.lngLat) : null;
      const g = this.groups.find((x) => x.layerIds.includes(f.layer.id));
      return g && g.items.get(f.properties._id);
    };
    this.map.on("mousemove", (e) => {
      if (this.drawing) return;
      const item = hit(e);
      this.map.getCanvas().style.cursor = item && (item.onClick || item.popup) ? "pointer" : "";
      const text = item && (typeof item.tooltip === "function" ? item.tooltip() : item.tooltip);
      if (text) this.tip.setLngLat(e.lngLat).setHTML(text).addTo(this.map);
      else this.tip.remove();
    });
    this.map.on("mouseout", () => this.tip.remove());
    this.map.on("click", (e) => {
      if (this.drawing) return;
      const item = hit(e);
      if (!item) return;
      this.tip.remove();
      if (item.popup) this.popup([e.lngLat.lng, e.lngLat.lat], item.popup());
      if (item.onClick) item.onClick(e);
    });
  }

  popup(lngLat, html, opts = {}) {
    this.closePopup();
    this.popupRef = new maplibregl.Popup({ maxWidth: "340px", ...opts }).setLngLat(lngLat).setHTML(html).addTo(this.map);
    return this.popupRef;
  }

  closePopup() {
    if (this.popupRef) this.popupRef.remove();
    this.popupRef = null;
  }

  fitBBox([w, s, e, n], { pad = 0.2, maxZoom = 8 } = {}) {
    const dx = (e - w) * pad, dy = (n - s) * pad;
    this.map.fitBounds([[w - dx, Math.max(-85, s - dy)], [e + dx, Math.min(85, n + dy)]], { maxZoom, duration: 600, padding: 20 });
  }

  // [w, s, e, n] of the visible map (lon may run past +/-180 in flat mode).
  viewBBox() {
    const b = this.map.getBounds();
    return [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  }

  containsBBox([w, s, e, n], shrink = 0.1) {
    const [W, S, E, N] = this.viewBBox();
    const dx = (E - W) * shrink, dy = (N - S) * shrink;
    return w >= W + dx && e <= E - dx && s >= S + dy && n <= N - dy;
  }

  // ------------------------------------------------------------ drawing
  // kind: "point" | "box" | "polygon"; done(geometry) on finish.
  draw(kind, done, group) {
    this.cancelDraw();
    const map = this.map;
    const pts = [];
    const d = { kind, done, group };
    this.drawing = d;
    map.getCanvas().style.cursor = "crosshair";
    map.doubleClickZoom.disable();
    const show = (cursor) => {
      group.clear();
      const ring = cursor ? [...pts, cursor] : pts;
      if (kind === "box" && pts.length === 1 && cursor) {
        group.add({ geojson: boxPolygon(pts[0], cursor), style: { color: "#000", weight: 2, fillOpacity: 0.05 } });
      } else if (kind === "polygon" && ring.length) {
        group.add({ geojson: { type: "LineString", coordinates: ring.length > 1 ? ring : [ring[0], ring[0]] }, style: { color: "#000", weight: 2, fill: false } });
        pts.forEach((p) => group.add({ geojson: { type: "Point", coordinates: p }, style: { color: "#000", weight: 4 } }));
      }
      group.flush();
    };
    const finish = (geom) => { this.cancelDraw(); done(geom); };
    d.onClick = (e) => {
      const p = [e.lngLat.lng, e.lngLat.lat];
      // a click on the last corner again (the second click of a double-click) adds nothing
      if (pts.length) {
        const last = map.project(pts[pts.length - 1]);
        if (Math.hypot(last.x - e.point.x, last.y - e.point.y) < 4) return;
      }
      if (kind === "point") return finish({ type: "Point", coordinates: [wrapLon(p[0]), p[1]] });
      if (kind === "box") {
        if (!pts.length) { pts.push(p); show(); return; }
        return finish(boxGeometry(pts[0], p));
      }
      // polygon: click the first vertex (or double-click) to finish
      if (pts.length >= 3) {
        const first = map.project(pts[0]);
        if (Math.hypot(first.x - e.point.x, first.y - e.point.y) < 12) return finish(closeRing(pts));
      }
      pts.push(p);
      show();
    };
    d.onMove = (e) => { if (pts.length) show([e.lngLat.lng, e.lngLat.lat]); };
    d.onDbl = (e) => { e.preventDefault(); if (kind === "polygon" && pts.length >= 3) finish(closeRing(pts)); };
    d.onKey = (e) => { if (e.key === "Escape") this.cancelDraw(); if (e.key === "Enter" && kind === "polygon" && pts.length >= 3) finish(closeRing(pts)); };
    map.on("click", d.onClick);
    map.on("mousemove", d.onMove);
    map.on("dblclick", d.onDbl);
    window.addEventListener("keydown", d.onKey);
  }

  cancelDraw() {
    const d = this.drawing;
    if (!d) return;
    this.map.off("click", d.onClick);
    this.map.off("mousemove", d.onMove);
    this.map.off("dblclick", d.onDbl);
    window.removeEventListener("keydown", d.onKey);
    d.group.clear();
    d.group.flush();
    this.drawing = null;
    this.map.getCanvas().style.cursor = "";
    setTimeout(() => this.map.doubleClickZoom.enable(), 300);
    if (this.onDrawEnd) this.onDrawEnd();
  }
}

class Group {
  constructor(view, name, { interactive = true, dashed = false } = {}) {
    this.view = view;
    this.name = name;
    this.interactive = interactive;
    this.visible = true;
    this.items = new Map();
    this.features = [];
    this.nextId = 1;
    this.layerIds = [`${name}-fill`, `${name}-line`, `${name}-point`];
    this.pending = false;
    view.ready.then(() => {
      const map = view.map;
      map.addSource(name, { type: "geojson", data: { type: "FeatureCollection", features: [] }, tolerance: 0.2 });
      map.addLayer({
        id: `${name}-fill`, type: "fill", source: name, filter: ["all", ["==", ["geometry-type"], "Polygon"], ["get", "fill"]],
        // no antialiasing: its 1 px outline, in each polygon's own colour, is drawn
        // after all the fills, so footprints hidden under others show their edges
        paint: { "fill-color": ["get", "fillColor"], "fill-opacity": ["get", "fillOpacity"], "fill-antialias": false },
      });
      map.addLayer({
        id: `${name}-line`, type: "line", source: name, filter: ["==", ["geometry-type"], "LineString"],
        layout: { "line-join": "round" },
        paint: { "line-color": ["get", "color"], "line-width": ["get", "weight"], "line-opacity": ["get", "opacity"], ...(dashed ? { "line-dasharray": [3, 2] } : {}) },
      });
      map.addLayer({
        id: `${name}-point`, type: "circle", source: name, filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-radius": ["+", 3, ["get", "weight"]], "circle-color": ["get", "color"], "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 },
      });
      this.flush();
    });
  }

  // item: {geojson, style, tooltip, popup, onClick}; returns a handle.
  add(item) {
    const id = this.nextId++;
    const handle = new Footprint(this, id, item);
    this.items.set(id, handle);
    this.schedule();
    return handle;
  }

  clear() {
    this.items.clear();
    this.schedule();
    return this;
  }

  show() { this.setVisible(true); return this; }
  hide() { this.setVisible(false); return this; }
  setVisible(on) {
    this.visible = on;
    this.view.ready.then(() => this.layerIds.forEach((id) => this.view.map.setLayoutProperty(id, "visibility", on ? "visible" : "none")));
  }

  schedule() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => this.flush());
  }

  flush() {
    this.pending = false;
    const src = this.view.map.getSource && this.view.map.getSource(this.name);
    if (!src) return;
    const features = [];
    this.items.forEach((h) => features.push(...h.features()));
    src.setData({ type: "FeatureCollection", features });
  }
}

class Footprint {
  constructor(group, id, { geojson, style = {}, tooltip = null, popup = null, onClick = null }) {
    Object.assign(this, { group, id, geojson, tooltip, popup, onClick });
    this.style = { ...DEFAULT_STYLE, ...style };
    this.bbox = geomBBox(geojson);
  }

  features() {
    const s = this.style;
    const props = {
      _id: this.id, color: s.color, weight: s.weight, opacity: s.opacity,
      fillColor: s.fillColor || s.color, fillOpacity: s.fillOpacity, fill: s.fill !== false,
    };
    const geoms = this.geojson.type === "GeometryCollection" ? this.geojson.geometries : [this.geojson];
    const out = [];
    geoms.forEach((g) => {
      if (g.type !== "Polygon" && g.type !== "MultiPolygon") { out.push({ type: "Feature", properties: props, geometry: g }); return; }
      const polys = g.type === "MultiPolygon" ? g.coordinates : [g.coordinates];
      if (props.fill) polys.forEach((c) => out.push({ type: "Feature", properties: props, geometry: { type: "Polygon", coordinates: c } }));
      if (s.weight > 0 && s.opacity > 0) outlineLines(g).forEach((line) => out.push({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: line } }));
    });
    return out;
  }

  setStyle(style) {
    this.style = { ...this.style, ...style };
    this.group.schedule();
  }

  center() {
    const [w, s, e, n] = this.bbox;
    return [(w + e) / 2, (s + n) / 2];
  }
}

// ------------------------------------------------------------ geometry helpers
// A polygon's outline as lines, without the segments that lie on +/-180:
// those are where a footprint was split at the antimeridian, not edges.
const onSeam = (a, b) => Math.abs(Math.abs(a[0]) - 180) < 1e-9 && Math.abs(Math.abs(b[0]) - 180) < 1e-9;
export function outlineLines(g) {
  const polys = g.type === "MultiPolygon" ? g.coordinates : g.type === "Polygon" ? [g.coordinates] : [];
  const lines = [];
  polys.forEach((rings) => rings.forEach((ring) => {
    let cur = [ring[0]];
    for (let i = 1; i < ring.length; i++) {
      if (onSeam(ring[i - 1], ring[i])) { if (cur.length > 1) lines.push(cur); cur = [ring[i]]; } else cur.push(ring[i]);
    }
    if (cur.length > 1) lines.push(cur);
  }));
  return lines;
}

export function geomBBox(g) {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  const walk = (c) => {
    if (typeof c[0] === "number") {
      w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]);
    } else c.forEach(walk);
  };
  (g.type === "GeometryCollection" ? g.geometries : [g]).forEach((x) => walk(x.coordinates));
  return [w, s, e, n];
}

const wrapLon = (x) => ((((x + 180) % 360) + 360) % 360) - 180;
const closeRing = (pts) => ({ type: "Polygon", coordinates: [[...pts.map(([x, y]) => [x, y]), pts[0]]] });

function boxPolygon([x0, y0], [x1, y1]) {
  const [w, e] = [Math.min(x0, x1), Math.max(x0, x1)], [s, n] = [Math.min(y0, y1), Math.max(y0, y1)];
  return { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] };
}

// A box in world coordinates, split into two pieces if it crosses the antimeridian.
export function boxGeometry(a, b) {
  const s = Math.min(a[1], b[1]), n = Math.max(a[1], b[1]);
  let w = Math.min(a[0], b[0]), e = Math.max(a[0], b[0]);
  const shift = Math.round((w + e) / 720) * 360;
  w -= shift; e = Math.min(e - shift, w + 360);
  const rect = (x0, x1) => [[[x0, s], [x1, s], [x1, n], [x0, n], [x0, s]]];
  if (e > 180) return { type: "MultiPolygon", coordinates: [rect(w, 180), rect(-180, e - 360)] };
  if (w < -180) return { type: "MultiPolygon", coordinates: [rect(w + 360, 180), rect(-180, e)] };
  return { type: "Polygon", coordinates: rect(w, e) };
}
