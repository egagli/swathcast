// The Satellites page: every satellite swathcast tracks, by mission, with its
// status, key dates and the agencies' notices (platforms.json, curated, each
// with its source), its newest data and the hourly satellite check
// (data/satellites.json), and how far the map plans and projects it
// (data/passes_index.json).
import { getJSON, fmtAge, ageHours, track } from "./common.js";

const MISSION_COLOR = { s1: "--s1", s2: "--s2", landsat: "--l8", nisar: "--nisar", modis: "--m6", viirs: "--m7", s3: "--m8" };
const STATE = {
  operational: ["Operational", "b-ok"],
  commissioning: ["Commissioning", "b-info"],
  extended: ["Extended mission", "b-info"],
  drifting: ["Drifting orbit", "b-warn"],
  degraded: ["Degraded", "b-warn"],
  other: ["See notes", "b-info"],
  retired: ["Retired", "b-off"],
  data_ended: ["Data ended", "b-off"],
  decommissioned: ["Decommissioned", "b-off"],
};
const CHECK = {
  ok: ["delivering", "b-ok", "new data arrives as usual"],
  silent: ["silent", "b-bad", "no new data from any distributor, while passes were due"],
  stream_silent: ["one stream silent", "b-warn", "one distributor has stopped delivering while another still does"],
  unknown: ["unknown", "b-off", "this site's sources for it are failing"],
  retired: ["no new data (retired)", "b-off", "expected: it no longer delivers"],
};
const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// "2 Nov 2026" from "2026-11-02" (a date, no time zone games)
const day = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : "";
};
const stamp = (iso) => (iso ? `${day(iso)} ${iso.slice(11, 16)} UTC` : "");
const link = (href, text) => `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(text)}</a>`;
const badge = ([text, cls], title = "") => `<span class="badge ${cls}"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`;

function statusCell(p) {
  const s = p.status, st = STATE[s.state] || STATE.other;
  const since = s.since ? ` <span class="nm">since ${day(s.since)}</span>` : "";
  return `${badge(st)}${since}<div class="nm">${esc(s.summary || "")} ${s.source ? link(s.source, "source") : ""}</div>`;
}
function datesCell(p) {
  const rows = [];
  if (p.launch && p.launch.date) rows.push(`Launched ${day(p.launch.date)}${p.launch.source ? ` ${link(p.launch.source, "↗")}` : ""}`);
  if (p.operations && p.operations.date) rows.push(`${esc(p.operations.what || "In operations")} ${day(p.operations.date)}${p.operations.source ? ` ${link(p.operations.source, "↗")}` : ""}`);
  return rows.map((r) => `<div>${r}</div>`).join("") || '<span class="nm">—</span>';
}
function dataCell(code, sats) {
  const v = sats && sats.platforms && sats.platforms[code];
  if (!v) return '<span class="nm">not tracked by this site\'s poller</span>';
  const c = CHECK[v.status] || CHECK.unknown;
  const streams = Object.values(v.streams || {});
  const lines = streams.map((s) => `<div class="nm">${esc(s.what)}: ${stamp(s.last_seen)} <span title="${esc(s.last_seen)}">(${fmtAge(ageHours(s.last_seen))})</span></div>`).join("");
  return `${badge([c[0], c[1]], c[2])}${v.note && v.status !== "ok" ? `<div class="nm">${esc(v.note)}</div>` : ""}${lines}`;
}
function mapCell(key, code, p, idx) {
  if (["retired", "data_ended", "decommissioned"].includes(p.status.state)) return '<span class="nm">not planned, predicted or projected; its past acquisitions are still listed</span>';
  const pr = idx && idx.projections && idx.projections[key];
  const plat = pr && pr.platforms && pr.platforms[code];
  const sensor = { s1: "sentinel-1", s2: "sentinel-2", nisar: "nisar" }[key];
  const planEnd = sensor && idx && idx.sensors && idx.sensors[sensor] && idx.sensors[sensor].plan_ends ? idx.sensors[sensor].plan_ends[code] : null;
  const wide = key === "modis" || key === "viirs" || key === "s3";
  const parts = [];
  if (plat) parts.push(`${wide ? "predicted from the orbit" : "planned"} to ${day(plat.from)}`, `projected to ${day(pr.to)} (its last ${plat.repeat_days}-day cycle repeated)`);
  else if (planEnd) parts.push(`planned to ${day(planEnd)}`);
  if (pr && pr.not_projected && pr.not_projected[code]) parts.push(`not projected: ${pr.not_projected[code]}`);
  return parts.length ? parts.map((x) => `<div class="nm">${esc(x)}</div>`).join("") : '<span class="nm">—</span>';
}
const SOURCE_SHORT = { cdse: "CDSE", earthdata: "NASA Earthdata", ospo: "NOAA OSPO", usgs: "USGS" };
// The fetched notices naming this satellite that the registry does not cite (newest 3).
function fetchedFor(code, p, feed) {
  const curated = new Set((p.notices || []).map((x) => x.url));
  return ((feed && feed.items) || []).filter((x) => x.platforms.includes(code) && !curated.has(x.url)).slice(0, 3);
}
const fetchedList = (items) => (items.length ? `<div class="nm feed">From the agencies' feeds (fetched daily):</div><ul class="notices feed">${items.map((x) => `<li><span class="d">${day(x.date)}</span>${link(x.url, x.title)} <span class="nm">${esc(SOURCE_SHORT[x.source] || x.source)}${x.resolved ? ` · resolved ${/^\d{4}-/.test(x.resolved) ? day(x.resolved) : esc(x.resolved)}` : ""}</span></li>`).join("")}</ul>` : "");
function updatesCell(p, feed) {
  const n = (p.notices || []).slice().sort((a, b) => (a.date < b.date ? 1 : -1));
  const list = n.length ? `<ul class="notices">${n.map((x) => `<li><span class="d">${day(x.date)}</span>${link(x.url, x.title)}</li>`).join("")}</ul>` : "";
  const page = p.status_page ? `<div class="nm">${link(p.status_page, "Latest notices ↗")}</div>` : "";
  return list + fetchedList(fetchedFor(p.code, p, feed)) + page || '<span class="nm">—</span>';
}

function render(reg, sats, idx, feed) {
  const box = document.getElementById("missions");
  box.innerHTML = reg.missions.map((m) => {
    const rows = m.platforms.map((p) => {
      const retired = ["retired", "data_ended", "decommissioned"].includes(p.status.state);
      return `<tr id="${esc(p.code)}"${retired ? ' class="retired"' : ""}>
        <td class="name" data-h="Satellite"><b>${esc(p.name)}</b><div class="nm">${esc(p.code)}${p.reviewed ? ` · reviewed ${day(p.reviewed)}` : ""}</div></td>
        <td data-h="Status">${statusCell(p)}</td>
        <td data-h="Dates">${datesCell(p)}</td>
        <td data-h="Newest data">${dataCell(p.code, sats)}</td>
        <td data-h="On the map">${mapCell(m.key, p.code, p, idx)}</td>
        <td data-h="Updates">${updatesCell(p, feed)}</td></tr>`;
    }).join("");
    const pages = (m.pages || []).map((x) => link(x.href, x.text)).join(" · ");
    // notices about the whole mission (no single satellite named)
    const wide = ((feed && feed.items) || []).filter((x) => x.missions.includes(m.key) && !x.platforms.length).slice(0, 3);
    return `<div class="card" id="m-${esc(m.key)}"><h2><span class="chip" style="background:var(${MISSION_COLOR[m.key] || "--muted"})"></span>${esc(m.label)}</h2>
      <p class="sub">${esc(m.operator || "")}${pages ? ` · ${pages}` : ""}</p>
      ${wide.length ? `<div class="mission-feed">${fetchedList(wide).replace("From the agencies' feeds (fetched daily):", "Mission-wide, from the agencies' feeds (fetched daily):")}</div>` : ""}
      <div class="scroll-x"><table class="sats"><thead><tr><th>Satellite</th><th>Status</th><th>Dates</th><th>Newest data <span class="nm">(hourly)</span></th><th>On the map</th><th>Updates</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }).join("");
  const n = reg.missions.reduce((a, m) => a + m.platforms.length, 0);
  const flagged = Object.entries((sats && sats.platforms) || {}).filter(([, v]) => v.status === "silent" || v.status === "stream_silent");
  document.getElementById("lede").innerHTML = `${n} satellites in ${reg.missions.length} missions. Status and notices reviewed ${day(reg.reviewed)}; newest data checked ${sats ? `${stamp(sats.checked_at)} (${fmtAge(ageHours(sats.checked_at))})` : "— (not available)"}.`
    + (flagged.length ? ` <b>Flagged now:</b> ${flagged.map(([p]) => `<a href="#${esc(p)}">${esc(p)}</a>`).join(", ")}.` : "");
  document.getElementById("keys").innerHTML = Object.values(STATE).filter((v, i, a) => a.indexOf(v) === i).map((s) => badge(s)).join(" ");
  const fresh = document.getElementById("fresh");
  if (fresh) fresh.textContent = sats ? `checked ${fmtAge(ageHours(sats.checked_at))}` : "";
  const src = document.getElementById("feeds");
  if (src) {
    src.innerHTML = feed ? `Fetched ${stamp(feed.fetched_at)} (${fmtAge(ageHours(feed.fetched_at))}): ${Object.values(feed.sources).map((x) => `${link(x.url, x.label)} ${x.ok ? `<span class="badge b-ok">ok</span>` : `<span class="badge b-bad" title="${esc(x.error || "")}">failed</span>`}`).join(" · ")}.`
      : "Not fetched yet (the daily notices job writes them).";
  }
  if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
}

Promise.all([
  getJSON("./platforms.json"),
  getJSON("./data/satellites.json").catch(() => null),
  getJSON("./data/passes_index.json").catch(() => null),
  getJSON("./data/notices.json").catch(() => null),
]).then(([reg, sats, idx, feed]) => {
  render(reg, sats, idx, feed);
  try { track("status_view", {}); } catch { /* analytics optional */ }
}).catch((err) => {
  document.getElementById("lede").textContent = `Could not load the satellite list: ${err.message}`;
});
