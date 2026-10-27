"""Acquisition plans, read straight from the agencies.

* **Sentinel-1 / Sentinel-2**: ESA's acquisition-plan pages list one KML per
  platform per plan update (S1: several a week, each ~3 weeks ahead; S2:
  fortnightly). Files overlap and later ones re-plan or cancel datatakes, so
  they are merged **newest wins**: each file owns the time from its start to
  where a newer file takes over. (Concatenating them instead
  would have kept 27 S1 datatakes on 2026-09-29 that the newest plan had dropped.) Every
  placemark attribute is kept: datatake id, mode, swath, polarisation,
  absolute/relative orbit.
* **Landsat 8/9**: USGS's 16-day cycle calendar (``cycles.json``: cycle day
  of each date) and ``cycle_path_row.json`` (paths *and rows* acquired on each
  cycle day), on the WRS-2 outlines from USGS's LandsatLook map service.
* **NISAR**: the observation-plan KMZ linked from NASA's NISAR data page:
  per frame, the dates and radar mode (whose third token is the polarisation).
  Each date is listed twice and may carry several modes, merged here; dates
  whose mode is blank (about 20% of the ROP358 plan) take the frame's last
  listed mode (``mode_source`` = "earlier cycle"; that predicted the real
  RSLC polarisations for 517 of 535 frames, vs 1,438 of 1,438 when listed).

Polarisations are the real channels ("VV+VH", "HH", "HH+HV+VH+VV"), not the
agencies' codes: :data:`POLARIZATIONS`, checked against CDSE product names
(S1) and ASF RSLC ``FREQUENCY_*_POLARIZATION`` attributes (NISAR L-band),
2026-09-29. Only the SAR missions have one. NISAR's are given per band
("L HH+HV, S RH+RV"): 12 % of its frame-dates (ROP358, 1-13 Oct 2026) are
joint L+S acquisitions.

Times: Sentinel-1/2 datatake begin/end are ESA's. Landsat scenes and NISAR
frames have only a date in their plans; their times are refined from the
orbit (CelesTrak elements, SGP4: when the ground track passes closest), see
:func:`orbit_refine`, falling back to the nodal-crossing formula.
"""

from __future__ import annotations

import io
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

KML = "{http://www.opengis.net/kml/2.2}"
ESA = "https://sentinels.copernicus.eu"
ESA_PAGES = {
    "sentinel-1": f"{ESA}/web/sentinel/copernicus/sentinel-1/acquisition-plans",
    "sentinel-2": f"{ESA}/web/sentinel/copernicus/sentinel-2/acquisition-plans",
}
#: s1c_mp_user_20260929t172851_20261021t194000, s2b_mp_acq__kml_20260924t120000_20261012t150000
ESA_FILE_RE = re.compile(
    r"/documents/d/sentinel/(?P<name>(?P<plat>s[12][a-z])_mp_(?:user|acq__kml)_"
    r"(?P<start>\d{8}t\d{6})_(?P<end>\d{8}t\d{6}))"
)
USGS_JSON = "https://landsat.usgs.gov/sites/default/files/landsat_acq/assets/json"
WRS2_SERVICE = (
    "https://nimbus.cr.usgs.gov/arcgis/rest/services/LLook_Outlines/MapServer/1/query"
)
NISAR_DATA_PAGE = "https://science.nasa.gov/mission/nisar/data/"
NISAR_PLAN_RE = re.compile(
    r"https://assets\.science\.nasa\.gov/[^\"'\s]*ObservationPlan[^\"'\s]*\.kmz"
)
LANDSAT_LST_H = {"descending": 10.2, "ascending": 22.2}  # USGS: 10:12 +/- 5 min
NISAR_LST_H = {"ascending": 6.0, "descending": 18.0}  # NASA mission overview

SCHEDULE_COLUMNS = [
    "sensor",
    "platform",
    "mode",
    "orbit_direction",
    "track",
    "begin_time",
    "end_time",
    "time_is_estimated",
    "geometry",
    "frame",
    "polarization",
    "datatake_id",
    "absolute_orbit",
    "swath",
    "timing",
    "mode_source",
]

#: Agency polarisation code -> channels. ESA S1 plan ``Polarisation`` and the
#: third token of a NISAR radar-mode mnemonic. NISAR QD ("quasi-dual") is HH
#: and VV on two separate 5 MHz sub-bands (RSLC products SVSH / SHSV).
POLARIZATIONS = {
    "SH": "HH",
    "SV": "VV",
    "DH": "HH+HV",
    "DV": "VV+VH",
    "HH": "HH",
    "VV": "VV",
    "HV": "HV",
    "VH": "VH",
    "QP": "HH+HV+VH+VV",
    "QD": "HH+VV",
    "CP": "RH+RV",  # NISAR S-band compact pol: right-circular transmit, H and V receive
}


def true_polarization(code) -> str | None:
    if code is None or (isinstance(code, float) and np.isnan(code)):
        return None
    code = str(code).strip().upper()
    return POLARIZATIONS.get(code, code or None)


class NotFound(RuntimeError):
    """The server answered 404: retrying within this run will not help."""


def get(session, url, *, attempts: int = 4, timeout: int = 120, **kw):
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except OSError as exc:  # requests errors are OSErrors
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                raise NotFound(f"{url}: {exc}") from exc
            last = exc
            time.sleep(2 ** (i + 1))
    raise RuntimeError(f"{url}: {last}")


def cached(session, url: str, cache_dir: Path, name: str) -> Path:
    """Plan files never change once published, so download each once."""
    path = Path(cache_dir) / name
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(get(session, url).content)
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- ESA (S1, S2)
@dataclass(frozen=True)
class PlanFile:
    platform: str  # S1C
    start: pd.Timestamp
    end: pd.Timestamp
    url: str
    name: str


def esa_plan_files(sensor: str, html: str) -> list[PlanFile]:
    """Every plan file linked from an ESA acquisition-plans page.

    The platform comes from the file name, not the page section: on
    2026-09-29 an S1D file was listed under Sentinel-1C.
    """
    files = {}
    for m in ESA_FILE_RE.finditer(html):
        name = m["name"]
        files[name] = PlanFile(
            platform=m["plat"].upper(),
            start=pd.Timestamp(m["start"].upper(), tz="UTC"),
            end=pd.Timestamp(m["end"].upper(), tz="UTC"),
            url=f"{ESA}/documents/d/sentinel/{name}",
            name=name,
        )
    want = "S1" if sensor == "sentinel-1" else "S2"
    return sorted(
        (f for f in files.values() if f.platform.startswith(want)),
        key=lambda f: f.start,
    )


def _ring(text: str):
    return [tuple(map(float, p.split(",")[:2])) for p in text.split()]


def parse_esa_kml(source) -> pd.DataFrame:
    """One row per placemark: begin/end, every ExtendedData field, the ring."""
    rows = []
    for _, el in ET.iterparse(source, events=("end",)):
        if el.tag != f"{KML}Placemark":
            continue
        data = {d.get("name"): d.findtext(f"{KML}value") for d in el.iter(f"{KML}Data")}
        coords = el.findtext(f".//{KML}LinearRing/{KML}coordinates")
        rows.append(
            {
                "begin": el.findtext(f".//{KML}begin"),
                "end": el.findtext(f".//{KML}end"),
                "ring": _ring(coords) if coords else None,
                **data,
            }
        )
        el.clear()
    df = pd.DataFrame(rows)
    if len(df):
        df["begin"] = pd.to_datetime(df["begin"], utc=True, format="ISO8601")
        df["end"] = pd.to_datetime(df["end"], utc=True, format="ISO8601")
    return df


def newest_wins(parts: list[tuple[PlanFile, pd.DataFrame]]) -> pd.DataFrame:
    """Merge overlapping plan files of one platform: each datatake comes from
    the newest file whose window contains its start."""
    covered: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    keep = []
    for f, df in sorted(parts, key=lambda p: p[0].start, reverse=True):
        if df.empty:
            continue
        inside = (df["begin"] >= f.start) & (df["begin"] < f.end)
        for s, e in covered:
            inside &= ~((df["begin"] >= s) & (df["begin"] < e))
        take = df[inside].copy()
        take["plan_file"] = f.name
        keep.append(take)
        covered.append((f.start, f.end))
    return pd.concat(keep, ignore_index=True) if keep else pd.DataFrame()


def sentinel_plans(sensor: str, start, end, session, cache_dir: Path) -> pd.DataFrame:
    html = get(session, ESA_PAGES[sensor]).text
    files = [f for f in esa_plan_files(sensor, html) if f.end > start and f.start < end]
    if not files:
        raise RuntimeError(f"{sensor}: no current plan files on {ESA_PAGES[sensor]}")
    by_platform: dict[str, list] = {}
    for f in files:
        # ESA can list a plan file days before the document exists (S2B
        # 20261022-20261109 was a 404 from 2026-10-22 to 10-27). Skip it: the
        # same platform's older files still cover most of its window.
        try:
            path = cached(session, f.url, cache_dir / "esa", f"{f.name}.kml")
        except NotFound as exc:
            LOGGER.warning(
                "%s %s: skipping listed plan file: %s", sensor, f.platform, exc
            )
            continue
        by_platform.setdefault(f.platform, []).append((f, parse_esa_kml(path)))
    if not by_platform:
        raise RuntimeError(
            f"{sensor}: none of the current plan files could be downloaded"
        )
    out = []
    for platform, parts in by_platform.items():
        merged = newest_wins(parts)
        if merged.empty:
            continue
        merged = merged[(merged["end"] > start) & (merged["begin"] < end)]
        merged["platform"] = platform
        out.append(merged)
        LOGGER.info(
            "%s %s: %d datatakes from %d files",
            sensor,
            platform,
            len(merged),
            len(parts),
        )
    return pd.concat(out, ignore_index=True)


def sentinel_schedule(sensor: str, start, end, session, cache_dir: Path):
    import geopandas as gpd
    from shapely.geometry import Polygon

    df = sentinel_plans(sensor, start, end, session, cache_dir)
    s1 = sensor == "sentinel-1"
    num = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    out = pd.DataFrame(
        {
            "sensor": sensor,
            "platform": df["platform"],
            "mode": df["Mode"].astype(str),
            "orbit_direction": "",
            "track": num("OrbitRelative"),
            "begin_time": df["begin"],
            "end_time": df["end"],
            "time_is_estimated": False,
            "frame": None,
            "polarization": [true_polarization(v) for v in df["Polarisation"]]
            if s1
            else None,
            "datatake_id": df["DatatakeId"] if s1 else df.get("ID"),
            "absolute_orbit": num("OrbitAbsolute"),
            "swath": df["Swath"] if s1 else None,
            "timing": "plan",
            "mode_source": "plan",
        }
    )
    geoms = [Polygon(r) if r and len(r) >= 4 else None for r in df["ring"]]
    return gpd.GeoDataFrame(out, geometry=geoms, crs="EPSG:4326")


# ---------------------------------------------------------------- Landsat
def wrs2_outlines(session):
    """WRS-2 path/row outlines (57,784 cells) from USGS LandsatLook."""
    import geopandas as gpd
    from shapely.geometry import Polygon

    rows, offset = [], 0
    while True:
        payload = get(
            session,
            WRS2_SERVICE,
            params={
                "where": "1=1",
                "outFields": "PATH,ROW,MODE",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "json",
                "resultOffset": offset,
                "resultRecordCount": 2000,
            },
        ).json()
        feats = payload.get("features", [])
        for f in feats:
            rings = (f.get("geometry") or {}).get("rings")
            if rings:
                a = f["attributes"]
                rows.append(
                    {
                        "path": int(a["PATH"]),
                        "row": int(a["ROW"]),
                        "direction": "ascending" if a["MODE"] == "A" else "descending",
                        "geometry": Polygon(rings[0]),
                    }
                )
        if not payload.get("exceededTransferLimit") or not feats:
            break
        offset += len(feats)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def landsat_calendar(cycles: dict, cycle_path_row: dict, start: date, days: int):
    """(platform, date, path, row) acquired, from USGS's cycle calendar.

    ``cycles.json`` gives the cycle day of each of 16 consecutive dates from
    1970-01-01 (Landsat 8's calendar; Landsat 9's cycle-day numbering in
    ``cycle_path_row.json`` refers to the same days).
    """
    seq = [int(cycles["landsat_8"][f"1/{d}/1970"]["cycle"]) for d in range(1, 17)]
    if sorted(seq) != list(range(1, 17)):
        raise ValueError("unexpected USGS cycle reference")
    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        cycle = seq[(day - date(1970, 1, 1)).days % 16]
        for mission, platform in (("landsat_8", "L8"), ("landsat_9", "L9")):
            for entry in cycle_path_row[mission].get(str(cycle), []):
                for row in entry.get("rows", []):
                    out.append((platform, day, int(entry["path"]), int(row)))
    return pd.DataFrame(out, columns=["platform", "day", "path", "row"])


def formula_time(day, lon, direction: str, lst_h: dict) -> pd.Timestamp:
    """UTC time of a nodal-crossing local solar time at ``lon`` on ``day``."""
    base = pd.Timestamp(day)
    base = base.tz_localize("UTC") if base.tzinfo is None else base.tz_convert("UTC")
    return base.floor("D") + pd.Timedelta(hours=lst_h[direction] - lon / 15.0)


# ---------------------------------------------------------------- NISAR
def nisar_plan_url(html: str) -> str:
    urls = sorted(set(NISAR_PLAN_RE.findall(html)))
    if not urls:
        raise RuntimeError(f"no NISAR observation plan linked from {NISAR_DATA_PAGE}")
    return urls[-1]


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAG = re.compile(r"<[^>]+>")


def _cells(description: str):
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", description or "", re.S | re.I):
        cells = [
            _TAG.sub("", c).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        ]
        if len(cells) >= 2:
            yield cells


def parse_nisar_kmz(data: bytes) -> pd.DataFrame:
    """One row per (frame, planned date): track, frame, direction, mode, ring."""
    rows = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.endswith(".kml"))
        with z.open(name) as fh:
            for _, el in ET.iterparse(fh, events=("end",)):
                if el.tag != f"{KML}Placemark":
                    continue
                coords = el.findtext(f".//{KML}Polygon//{KML}coordinates")
                desc = el.findtext(f"{KML}description") or ""
                pm = el.findtext(f"{KML}name") or ""
                el.clear()
                if not coords:
                    continue
                attrs, dates = {}, []
                for a, b, *_ in _cells(desc):
                    if _DATE.match(a):
                        dates.append((a, b))
                    elif a in ("track", "frame", "passDirection"):
                        attrs[a] = b
                m = re.match(r"^T(\d+)_F(\d+)$", pm, re.I)
                track = attrs.get("track") or (m and m[1])
                frame = attrs.get("frame") or (m and m[2])
                for d, mode in dates:
                    rows.append(
                        {
                            "track": int(track) if track else None,
                            "frame": int(frame) if frame else None,
                            "direction": attrs.get("passDirection", "").lower(),
                            "day": d,
                            "mode": mode,
                            "ring": _ring(coords),
                        }
                    )
    return merge_nisar_dates(pd.DataFrame(rows))


def join_rings(rings: list) -> list:
    """One ring from the pieces of a footprint split at +/-180: each piece
    shifted by 360 to sit next to the largest, then unioned. Longitudes may
    then leave [-180, 180] (the schedule re-splits them for the site)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    polys = [
        Polygon(r) for r in {tuple(map(tuple, r)) for r in rings if r and len(r) >= 4}
    ]
    if len(polys) < 2:
        return rings[0]
    main = max(polys, key=lambda q: q.area)
    ref = main.centroid.x
    shifted = []
    for q in polys:
        dx = 360.0 * round((ref - q.centroid.x) / 360.0)
        shifted.append(Polygon([(x + dx, y) for x, y in q.exterior.coords]))
    merged = unary_union([q.buffer(1e-7) for q in shifted])  # ~1 cm: close seams
    if merged.geom_type != "Polygon":
        merged = max(merged.geoms, key=lambda q: q.area)
    return [tuple(c) for c in merged.exterior.coords]


def merge_nisar_dates(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (frame, date): the plan lists each date twice, sometimes
    with different modes; blank modes take the frame's last listed one."""
    if df.empty:
        return df.assign(mode_source=pd.Series(dtype=str))
    df = df.assign(_i=np.arange(len(df)))
    # A frame across the antimeridian is two placemarks, one piece on each
    # side of 180 (ROP358: 299 frames; the east piece is often a sliver):
    # join the pieces into one ring with continuous longitudes.
    frame_keys = ["track", "frame", "direction"]
    joined = {
        k: join_rings(list(g))
        for k, g in df.groupby(frame_keys, dropna=False)["ring"]
        if len({tuple(r) for r in g if r}) > 1
    }
    if joined:
        df["ring"] = [
            joined.get(k, r)
            for k, r in zip(
                df[frame_keys].itertuples(index=False, name=None),
                df["ring"],
                strict=True,
            )
        ]
    keys = ["track", "frame", "direction", "day"]
    first = df.groupby(keys, dropna=False, sort=False).agg(
        ring=("ring", "first"),
        _i=("_i", "min"),
        mode=(
            "mode",
            lambda m: ", ".join(
                dict.fromkeys(x.strip() for v in m for x in v.split(",") if x.strip())
            ),
        ),
    )
    df = first.reset_index().sort_values(["track", "frame", "direction", "day"])
    listed = df["mode"].where(df["mode"] != "")
    carried = listed.groupby(
        [df["track"], df["frame"], df["direction"]], dropna=False
    ).ffill()
    df["mode_source"] = np.where(
        listed.notna(), "plan", np.where(carried.notna(), "earlier cycle", "none")
    )
    df["mode"] = carried.fillna("")
    return df.sort_values("_i").drop(columns="_i").reset_index(drop=True)


def nisar_polarization(mode: str) -> str | None:
    """Channels of a NISAR radar mode, per band: each mnemonic starts with its
    band (``L:`` from JPL's radar, distributed by ASF; ``S:`` from ISRO's,
    distributed by ISRO's Bhoonidhi). One date can carry several modes, and a
    joint mode both bands: 'L:SCI:DH:40M+05N:FS:B4:D02  S:DB:CP:37W:B4' ->
    'L HH+HV, S RH+RV'."""
    found = set()
    for m in re.split(r"[,\s]+", mode or ""):
        parts = m.split(":")
        if len(parts) > 2 and parts[0] in ("L", "S") and parts[2] and parts[2] != "--":
            found.add((parts[0], true_polarization(parts[2])))
    return (
        ", ".join(f"{band} {pol}" for band, pol in sorted(found, key=_pol_order))
        or None
    )


_POL_ORDER = ["HH", "VV", "HH+HV", "VV+VH", "HH+VV", "HH+HV+VH+VV", "RH+RV"]


def _pol_order(entry):
    band, pol = entry
    rank = _POL_ORDER.index(pol) if pol in _POL_ORDER else len(_POL_ORDER)
    return (band != "L", rank, pol)


# ---------------------------------------------------------------- orbit refinement
def orbit_refine(
    sat, lon, lat, guess: pd.Timestamp, direction: str, half_window_s=2700
):
    """Closest approach of the ground track to (lon, lat) within +/- 45 min of
    ``guess`` (less than half an orbit, so one pass), on a pass in
    ``direction``. Returns a Timestamp, or None if no such pass."""
    from swathcast.ephemeris import closest_approach

    t0 = guess.timestamp()
    t, d, asc = closest_approach(
        sat, [lon], [lat], t0 - half_window_s, t0 + half_window_s, step_s=2.0
    )
    if direction and bool(asc[0]) != (direction == "ascending"):
        return None
    if not (t0 - half_window_s < t[0] < t0 + half_window_s):  # at the window edge
        return None
    return pd.Timestamp(t[0], unit="s", tz="UTC")


def refine_many(sat, lons, lats, guesses, directions):
    """Vectorised enough: one propagation per distinct guess hour."""
    out = []
    for lon, lat, g, d in zip(lons, lats, guesses, directions, strict=True):
        out.append(orbit_refine(sat, lon, lat, g, d))
    return out


def satrecs(elements: dict) -> dict:
    from swathcast.ephemeris import satrec

    out = {}
    for platform, omm in (elements or {}).items():
        try:
            out[platform] = satrec(omm)
        except (ValueError, KeyError) as exc:
            LOGGER.warning("no orbit for %s: %s", platform, exc)
    return out


def utc_window(days: int, now: datetime | None = None):
    now = now or datetime.now(UTC)
    start = pd.Timestamp(now).floor("D")
    return start, start + pd.Timedelta(days=days)


def lonlat_centre(ring) -> tuple[float, float]:
    a = np.asarray(ring, dtype=float)[:-1] if len(ring) > 1 else np.asarray(ring)
    lons = a[:, 0]
    if lons.max() - lons.min() > 180:
        lons = np.where(lons < 0, lons + 360, lons)
    return float(((lons.mean() + 180) % 360) - 180), float(a[:, 1].mean())


#: Poleward of this, WRS-2 scenes lie where the track turns (see landsat_scenes).
TURN_LAT = 78.0


# ---------------------------------------------------------------- builders
def landsat_scenes(
    cycles, cycle_path_row, wrs2, start, days, sats: dict
) -> pd.DataFrame:
    """One row per planned Landsat scene: platform, path, row, direction, time.

    Time: the pass on the calendar date (UTC, +/- 2 h) whose ground track
    comes closest to the WRS-2 scene centre (which lies on the nominal track).
    Checked against 4,707 USGS STAC scene times (2026-09-10/11/26/27): median
    0.3 s, p99 6 s; 6 were a day off where the calendar date and UTC date
    differ. Without elements: the 10:12 local-solar-time formula (median 35
    min off on the same scenes)."""
    from swathcast.ephemeris import nearest_pass

    cal = landsat_calendar(cycles, cycle_path_row, start.date(), days)
    cells = wrs2.set_index(["path", "row"])
    cal = cal[[k in cells.index for k in zip(cal["path"], cal["row"], strict=True)]]
    centre = {
        k: lonlat_centre(list(g.exterior.coords)) for k, g in cells.geometry.items()
    }
    cal["direction"] = [
        cells.at[(p, r), "direction"]
        for p, r in zip(cal["path"], cal["row"], strict=True)
    ]
    lon = np.array(
        [centre[(p, r)][0] for p, r in zip(cal["path"], cal["row"], strict=True)]
    )
    lat = np.array(
        [centre[(p, r)][1] for p, r in zip(cal["path"], cal["row"], strict=True)]
    )
    cal = cal.reset_index(drop=True)
    when = np.full(len(cal), np.nan)  # unix seconds
    timing = np.full(len(cal), "formula", dtype=object)
    track_km = np.full(len(cal), np.nan)
    for (platform, day, direction), idx in cal.groupby(
        ["platform", "day", "direction"]
    ).indices.items():
        sat = sats.get(platform)
        if sat is not None:
            d0 = pd.Timestamp(day).tz_localize("UTC").timestamp()
            t, dist = nearest_pass(
                sat,
                lon[idx],
                lat[idx],
                d0 - 7200,
                d0 + 93600,
                ascending=direction == "ascending",
            )
            good = np.isfinite(t) & (dist < 60)  # scene centres lie on the track
            when[idx[good]] = t[good]
            timing[idx[good]] = "orbit"
            track_km[idx] = dist
            # The rows where the track turns (122 at ~82 S, 246-248 near 81 N)
            # run neither north nor south, so the direction filter can drop
            # their pass (it took the formula, up to an orbit off, for ~540
            # scenes a month): look again in either direction. Only there:
            # elsewhere a pass the other way can cross a scene centre.
            polar = idx[~good & (np.abs(lat[idx]) > TURN_LAT)]
            if len(polar):
                t2, dist2 = nearest_pass(
                    sat, lon[polar], lat[polar], d0 - 7200, d0 + 93600
                )
                ok = np.isfinite(t2) & (dist2 < 60)
                when[polar[ok]] = t2[ok]
                timing[polar[ok]] = "orbit"
                track_km[polar] = dist2
        for k in idx[np.isnan(when[idx])]:
            when[k] = formula_time(day, lon[k], direction, LANDSAT_LST_H).timestamp()
    cal["time"] = pd.to_datetime(when, unit="s", utc=True)
    cal["timing"] = timing
    cal["track_km"] = track_km
    return cal.drop(columns=["day"]).reset_index(drop=True)


#: NISAR frame centres lie 561-579 km (p5-p95) left of the ground track:
#: 2,804 RSLC frames sensed 2026-09-26/27 (CMR), SGP4 with CelesTrak elements.
NISAR_FRAME_OFFSET_KM = 567.0


def nisar_times(df, sat) -> tuple[np.ndarray, np.ndarray]:
    """Abeam time of each planned frame: the pass on its (UTC) plan date that
    has the frame centre ~567 km to its left. Against those 2,804 frames:
    median 0.65 s, p95 14 s from the granule mid-time (the formula: 42 min).
    Returns unix seconds (NaN where no pass qualifies) and the distance."""
    from swathcast.ephemeris import nearest_pass

    lon = np.array([lonlat_centre(r)[0] for r in df["ring"]])
    lat = np.array([lonlat_centre(r)[1] for r in df["ring"]])
    when = np.full(len(df), np.nan)
    dist = np.full(len(df), np.nan)
    for (day, direction), idx in df.groupby(["day", "direction"]).indices.items():
        if direction not in ("ascending", "descending"):
            continue
        d0 = pd.Timestamp(day).timestamp()
        t, d = nearest_pass(
            sat,
            lon[idx],
            lat[idx],
            d0 - 7200,
            d0 + 93600,
            ascending=direction == "ascending",
            offset_km=NISAR_FRAME_OFFSET_KM,
            side="left",
        )
        good = np.isfinite(t) & (np.abs(d - NISAR_FRAME_OFFSET_KM) < 60)
        when[idx[good]] = t[good]
        dist[idx] = d
    return when, dist


def nisar_schedule(start, end, session, cache_dir: Path, sat=None):
    import geopandas as gpd
    from shapely.geometry import Polygon

    url = nisar_plan_url(get(session, NISAR_DATA_PAGE).text)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.rsplit("/", 1)[-1].split("?")[0])
    df = parse_nisar_kmz(cached(session, url, cache_dir / "nisar", name).read_bytes())
    df["day"] = pd.to_datetime(df["day"], utc=True)
    df = df[(df["day"] >= start.floor("D") - pd.Timedelta(days=1)) & (df["day"] < end)]
    df = df.reset_index(drop=True)
    centres = [lonlat_centre(r) for r in df["ring"]]
    formula = [
        formula_time(d, lon, dr if dr in NISAR_LST_H else "descending", NISAR_LST_H)
        for d, (lon, _), dr in zip(df["day"], centres, df["direction"], strict=True)
    ]
    orbit = nisar_times(df, sat)[0] if sat is not None else np.full(len(df), np.nan)
    times = [
        f if np.isnan(o) else pd.Timestamp(o, unit="s", tz="UTC")
        for f, o in zip(formula, orbit, strict=True)
    ]
    timing = np.where(np.isnan(orbit), "formula", "orbit")
    out = pd.DataFrame(
        {
            "sensor": "nisar",
            "platform": "NISAR",
            "mode": df["mode"].astype(str).to_numpy(),
            "orbit_direction": df["direction"].to_numpy(),
            "track": df["track"].to_numpy(),
            "begin_time": times,
            "end_time": times,
            "time_is_estimated": True,
            "frame": df["frame"].to_numpy(),
            "polarization": [nisar_polarization(m) for m in df["mode"]],
            "mode_source": df["mode_source"].to_numpy(),
            "datatake_id": None,
            "absolute_orbit": np.nan,
            "swath": None,
            "timing": timing,
        }
    )
    keep = ((out["begin_time"] >= start) & (out["begin_time"] < end)).to_numpy()
    out = out[keep]
    geoms = [Polygon(r) for r, k in zip(df["ring"], keep, strict=True) if k]
    LOGGER.info("NISAR: %d frame acquisitions from %s", len(out), name)
    return gpd.GeoDataFrame(
        out.reset_index(drop=True), geometry=geoms, crs="EPSG:4326"
    ), url


def s1_orbit_direction(gdf, sats: dict) -> int:
    """Pass direction of each S1 datatake from the orbit at its midpoint
    (latitude rising = ascending). Returns how many were set."""
    from swathcast.ephemeris import subpoints

    n = 0
    for platform, idx in (
        gdf[gdf["sensor"] == "sentinel-1"].groupby("platform").groups.items()
    ):
        sat = sats.get(platform)
        if sat is None:
            continue
        mid = (
            gdf.loc[idx, "begin_time"]
            + (gdf.loc[idx, "end_time"] - gdf.loc[idx, "begin_time"]) / 2
        )
        t = np.array([m.timestamp() for m in mid])
        _, lat0 = subpoints(sat, t - 5)
        _, lat1 = subpoints(sat, t + 5)
        gdf.loc[idx, "orbit_direction"] = np.where(
            lat1 > lat0, "ascending", "descending"
        )
        n += len(idx)
    return n
