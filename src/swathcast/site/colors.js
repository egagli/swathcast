// Continuous colour ramps for "time until the next acquisition", interpolated
// in OKLab (perceptually even steps between the stops).

// Days on the colour axis, and the square-root scale that spreads the next
// few days (where most footprints fall) across more of the ramp.
export const HORIZON_D = 30;
export const scaleT = (days) => Math.sqrt(Math.min(Math.max(days, 0), HORIZON_D) / HORIZON_D);
export const TICKS_D = [0, 1, 3, 7, 14, 30];

export const RAMPS = {
  // dark green (about to happen) -> dark red (furthest out), as asked
  greenred: ["#00441b", "#1a9850", "#a6d96a", "#fee08b", "#fdae61", "#d73027", "#67001f"],
  // colour-vision-deficiency safe: viridis, dark (about to happen) -> light
  // (furthest out), lightness rising monotonically, like the green-red ramp
  cvd: ["#440154", "#443983", "#31688e", "#21918c", "#35b779", "#90d743", "#fde725"],
};

const srgbToLin = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const linToSrgb = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

export function hexToOklab(hex) {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => srgbToLin(v / 255));
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

export function oklabToHex([L, a, b]) {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const rgb = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  return `#${rgb.map((c) => Math.round(Math.min(1, Math.max(0, linToSrgb(c))) * 255).toString(16).padStart(2, "0")).join("")}`;
}

const labCache = new Map();
// Colour at t in [0, 1] along a ramp.
export function rampColor(name, t) {
  if (!labCache.has(name)) labCache.set(name, RAMPS[name].map(hexToOklab));
  const stops = labCache.get(name);
  const x = Math.min(Math.max(t, 0), 1) * (stops.length - 1);
  const i = Math.min(Math.floor(x), stops.length - 2), f = x - i;
  return oklabToHex(stops[i].map((v, k) => v + (stops[i + 1][k] - v) * f));
}

export const colorForDays = (name, days) => rampColor(name, scaleT(days));

// CSS gradient for the legend bar.
export function gradientCSS(name, steps = 24) {
  return `linear-gradient(to right, ${Array.from({ length: steps + 1 }, (_, i) => rampColor(name, i / steps)).join(", ")})`;
}
