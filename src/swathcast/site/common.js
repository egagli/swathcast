// Shared helpers: data loading, freshness banner, latency formatting.

export const SCHEDULE_CADENCE_H = 24; // schedule.yml runs daily
export const LATENCY_CADENCE_H = 1; // poll.yml runs hourly

// Fixed categorical slot per source (colour follows the entity, never its rank).
export const SOURCE_SLOT = {
  cdse_grd: 1, cdse_slc: 7, asf_grd: 2, asf_slc: 8,
  pc_grd: 3, pc_rtc: 6, opera_rtc: 4, opera_cslc: 5,
  // other missions: same slot = same distributor role (reference 1/7, ESA-side mirror 2/8, PC 3, NASA HLS 4)
  cdse_s2_l1c: 1, cdse_s2_l2a: 7, es_s2_l1c: 2, es_s2_l2a: 8, pc_s2_l2a: 3, hls_s30: 4,
  usgs_landsat_l1: 1, usgs_landsat_l2: 7, es_landsat_l2: 8, pc_landsat_l2: 3, hls_l30: 4,
  asf_nisar_l0b: 1, asf_nisar_rslc: 7, asf_nisar_gcov: 5,
  bhoonidhi_nisar_s_rslc: 7, bhoonidhi_nisar_s_gcov: 5,
  // MODIS/VIIRS: LANCE near-real-time 1, standard Level-1B 2/8, snow 5 (NRT) and 4 (NSIDC)
  lance_modis_l1b: 1, laads_modis_l1b: 2, laads_modis_l1b_c7: 8, lance_modis_snow: 5, nsidc_modis_snow: 4,
  lance_viirs_l1b: 1, laads_viirs_l1b: 2, lance_viirs_snow: 5, nsidc_viirs_snow: 4,
  // Sentinel-3: CDSE NRT 1/7, NTC 2/8, EUMETSAT 6, Level-2 5/4, Planetary Computer 3 (two products, labelled)
  cdse_s3_efr: 1, cdse_s3_rbt: 7, cdse_s3_efr_ntc: 2, cdse_s3_rbt_ntc: 8, eumetsat_s3_efr: 6,
  cdse_s3_lfr: 5, cdse_s3_lst: 4, pc_s3_lfr: 3, pc_s3_lst: 3,
};
export const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
export const sourceColor = (key) => css(`--c${SOURCE_SLOT[key] || 1}`);

export async function getJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

export function fmtStamp(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

// The same instant in the viewer's time zone: "9:08 PM PDT, your time" (with
// its date when not the UTC one), for the page's own timestamps (data as of,
// last success), which belong to no place.
export function localStamp(iso) {
  if (!iso) return "";
  return fmtBoth(new Date(iso)).split(" · ")[1] || "";
}

export function ageHours(iso, now = Date.now()) {
  if (!iso) return Infinity;
  return (now - new Date(iso).getTime()) / 3.6e6;
}

export function fmtAge(h) {
  if (!Number.isFinite(h)) return "never";
  if (h < 0) return "in the future (clock skew?)";
  if (h < 1) return `${Math.round(h * 60)} min ago`;
  if (h < 48) return `${h.toFixed(1)} h ago`;
  return `${(h / 24).toFixed(1)} days ago`;
}

// Fills #fresh and #banner. Either product older than 2x its cadence is stale.
export function showFreshness({ scheduleAt, latencyAt, failing = [] }) {
  const el = document.getElementById("fresh");
  const sAge = ageHours(scheduleAt);
  const lAge = ageHours(latencyAt);
  el.innerHTML = "";
  const part = (label, iso, age) => {
    const span = document.createElement("span");
    span.innerHTML = `${label} <b title="${localStamp(iso)}">${fmtStamp(iso)}</b> (${fmtAge(age)})`;
    return span;
  };
  el.append(part("schedule generated", scheduleAt, sAge), document.createTextNode(" · "),
    part("latency data as of", latencyAt, lAge));
  const problems = [];
  if (sAge > 2 * SCHEDULE_CADENCE_H) problems.push(`the pass schedule is ${fmtAge(sAge)} old (expected daily)`);
  if (lAge > 2 * LATENCY_CADENCE_H) problems.push(`latency data is ${fmtAge(lAge)} old (expected hourly)`);
  if (failing.length) problems.push(`failing sources: ${failing.join(", ")}`);
  const banner = document.getElementById("banner");
  if (problems.length) {
    banner.innerHTML = `<strong>Data may be stale.</strong> ${problems.join("; ")}. ` +
      `See <a href="https://github.com/egagli/swathcast/actions/workflows/poll.yml" target="_blank" rel="noopener">Actions</a>.`;
    banner.classList.add("show");
  }
}

export function failingSources(health) {
  return Object.entries((health && health.sources) || {})
    .filter(([, e]) => e.failing_since_utc)
    .map(([k]) => k);
}

export function fmtHours(h) {
  if (h == null || !Number.isFinite(h)) return "—";
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
  if (h < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function fmtWhen(d, ref) {
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const sameDay = ref && d.toISOString().slice(0, 10) === ref.toISOString().slice(0, 10);
  return sameDay ? `${hh}:${mm}` : `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${hh}:${mm}`;
}

// Round a predicted time up to the next 10 minutes: "by ~16:10".
export function roundUp10(d) {
  const step = 10 * 60 * 1000;
  return new Date(Math.ceil(d.getTime() / step) * step);
}

// Mirrors swathcast.stats.expected_availability (tested in Python).
export function expectedAvailability(begin, platform, sourceExpected) {
  let e = sourceExpected && sourceExpected[platform];
  if (!e || !e.measured) e = sourceExpected && sourceExpected.all;
  if (!e || !e.measured || e.median_h == null) return null;
  const add = (h) => (h == null ? null : new Date(begin.getTime() + h * 3.6e6));
  return { stratum: e.stratum, n_events: e.n_events, median_at: add(e.median_h), p90_at: add(e.p90_h) };
}

// ------------------------------------------------------------ local time
// "Local" means the civil time zone at the footprint (tz-lookup, CC0), not
// the viewer's. If the lookup library cannot load, the viewer's zone is used
// and labelled as such.
const TZ_LOOKUP = "https://cdn.jsdelivr.net/npm/@photostructure/tz-lookup@11.7.0/+esm";
let tzlookup = null;
export const tzReady = import(TZ_LOOKUP).then((m) => { tzlookup = m.default; }).catch(() => null);

export function zoneAt(lon, lat) {
  if (tzlookup && Number.isFinite(lon) && Number.isFinite(lat)) {
    try { return tzlookup(Math.max(-89.9, Math.min(89.9, lat)), ((lon + 540) % 360) - 180); } catch { /* fall through */ }
  }
  return null;
}

const fmtCache = new Map();
const zoneNames = new Map();
// "PDT", "CEST", …: en-US knows American abbreviations, en-GB European ones;
// elsewhere both give an offset such as "GMT+5:45".
function zoneName(d, zone) {
  const key = `${zone}|${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`;
  if (!zoneNames.has(key)) {
    const name = (loc) => new Intl.DateTimeFormat(loc, { timeZone: zone || undefined, timeZoneName: "short" })
      .formatToParts(d).find((x) => x.type === "timeZoneName")?.value || "";
    const us = name("en-US");
    zoneNames.set(key, us.startsWith("GMT") ? (name("en-GB").startsWith("GMT") ? us : name("en-GB")) : us);
  }
  return zoneNames.get(key);
}
function partsIn(d, zone) {
  const key = zone || "";
  if (!fmtCache.has(key)) {
    // numeric parts, formatted below like fmtWhen; en-US gives "PDT"-style zone names.
    // Local times are 12-hour ("7:01 PM"); UTC stays 24-hour.
    fmtCache.set(key, new Intl.DateTimeFormat("en-US", {
      timeZone: zone || undefined, weekday: "short", day: "numeric", month: "numeric",
      hour: "numeric", minute: "2-digit", hourCycle: "h12", timeZoneName: "short",
    }));
  }
  const p = Object.fromEntries(fmtCache.get(key).formatToParts(d).map((x) => [x.type, x.value]));
  return {
    date: `${p.weekday} ${Number(p.day)} ${MONTHS[Number(p.month) - 1]}`,
    time: `${Number(p.hour)}:${p.minute} ${String(p.dayPeriod || "").toUpperCase()}`.trim(),
    tz: zoneName(d, zone) || p.timeZoneName || "",
  };
}

// Local time at [lon, lat]: {date, time, tz, zone, viewer}.
export function localTime(d, ll) {
  const zone = ll ? zoneAt(ll[0], ll[1]) : null;
  return { ...partsIn(d, zone), zone, viewer: !zone };
}

// "Tue 1 Oct 02:01 UTC · Mon 30 Sep 19:01 PDT" (the local date only if it differs).
export function fmtBoth(d, ll, ref) {
  const utc = fmtWhen(d, ref);
  const loc = localTime(d, ll);
  const sameDate = loc.date === `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  const label = loc.viewer ? `${loc.tz}, your time` : loc.tz;
  return `${utc} UTC · ${sameDate || (ref && utc.length <= 5) ? "" : `${loc.date} `}${loc.time} ${label}`;
}

// Two-line table cell: UTC on top, local underneath (the local date only
// when it differs from the UTC one). Orbit-timed passes (Sentinel-2,
// Landsat, NISAR) were within seconds of the real acquisitions in our
// checks, so they get a tooltip rather than an "(est.)" label.
export function whenCell(d, ll, fromOrbit, extra = "") {
  const loc = localTime(d, ll);
  const sameDate = loc.date === `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  const utc = `<span${fromOrbit ? ' title="timed from the orbit (SGP4) on the plan\'s date: within seconds in our checks"' : ""}>${utcText(d)}</span>`;
  const title = loc.zone ? `local time at the footprint (${loc.zone})` : "your browser's time zone";
  return `${utc}<br><span class="nm local" title="${title}">${sameDate ? "" : `${loc.date} `}${loc.time} ${loc.tz}${extra ? ` · ${extra}` : ""}</span>`;
}

// "Tue 6 Oct 19:21 UTC" ("19:21 UTC" on ref's UTC date): every UTC time on
// the page is written so, with its zone.
export const utcText = (d, ref) => `${fmtWhen(d, ref)} UTC`;

// "in 5.2 d", "in 35 min", "3.0 h ago": time until or since d.
export function rel(d, now = Date.now()) {
  const ms = d - now;
  return ms >= 0 ? `in ${fmtHours(ms / 3.6e6)}` : `${fmtHours(-ms / 3.6e6)} ago`;
}

// "Tuesday 6 Oct" and today / tomorrow / in 3 days / yesterday / 3 days ago (UTC days).
const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
export function dayLabel(d, now = new Date()) {
  const days = Math.round((Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()) - new Date(now).setUTCHours(0, 0, 0, 0)) / 864e5);
  const r = days === 0 ? "today" : days === 1 ? "tomorrow" : days === -1 ? "yesterday" : days > 0 ? `in ${days} days` : `${-days} days ago`;
  return { name: `${DAY_NAMES[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`, rel: r };
}

// Solar elevation (degrees) at [lon, lat] at time d: NOAA's low-precision
// formulas, good to a fraction of a degree, which is all "day or night" needs.
export function sunElevation(d, lon, lat) {
  const rad = Math.PI / 180;
  const jd = d.getTime() / 864e5 + 2440587.5, n = jd - 2451545.0;
  const L = (280.46 + 0.9856474 * n) % 360, g = ((357.528 + 0.9856003 * n) % 360) * rad;
  const lambda = (L + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * rad;
  const eps = (23.439 - 4e-7 * n) * rad;
  const dec = Math.asin(Math.sin(eps) * Math.sin(lambda));
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lambda), Math.cos(lambda));
  const gmst = (18.697374558 + 24.06570982441908 * n) % 24;
  const ha = ((gmst * 15 + lon) * rad) - ra;
  return Math.asin(Math.sin(lat * rad) * Math.sin(dec) + Math.cos(lat * rad) * Math.cos(dec) * Math.cos(ha)) / rad;
}

// The map settings this browser remembered (basemap, opacity, palette,
// globe...), forgotten: part of starting over (Reset, the tour's end).
export function forgetSettings() {
  try {
    Object.keys(localStorage).filter((k) => k.startsWith("swathcast.") || k.startsWith("s1lat.")).forEach((k) => localStorage.removeItem(k));
  } catch { /* storage unavailable */ }
}
