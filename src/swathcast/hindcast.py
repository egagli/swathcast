"""Hindcast: how well a plan carried forward by its repeat cycle predicts what
actually came next (:mod:`swathcast.projection`).

Two checks, one per kind of plan:

* **Sentinel-1 and Sentinel-2** (ESA's plans, re-issued every few days).
  ESA keeps months of old plan files on its acquisition-plans pages. Each old
  file is projected past its own end by whole repeat cycles, and compared with
  what ESA *later* planned for those days (the newest of a chain of later
  files, which is what the map shows as "planned"). Scores, per number of
  cycles projected, pooled over every base file:

  - *precision*: the share of projected acquisition time that ESA later
    planned (same platform; "same mode" also asks for the same mode and,
    for Sentinel-1, polarization). On a repeat orbit, time along a pass is
    position along the ground track, so time overlap is footprint overlap.
  - *recall*: the share of ESA's later-planned acquisition time that the
    projection covered.

* **Landsat 8/9** (USGS's calendar, a fixed 16-day cycle). The dates are the
  calendar's, so the check is against what was *acquired*: USGS's Level-1
  STAC (``landsat-c2l1``) over a year, for a seeded random sample of daytime
  WRS-2 path/rows. Scores:

  - *calendar*: the share of acquired scenes whose (platform, path, date) is
    on the calendar, and the share of calendar days on which the scene was
    acquired.
  - *timing*: the error of a scene time predicted by shifting an earlier
    scene of the same path/row by whole 16-day cycles, by number of cycles;
    and the error of SGP4 with today's elements, by how far the scene is from
    the elements' epoch (propagated *back*, as a stand-in for ahead).

Writes ``hindcast.json`` and ``hindcast.md`` (``swathcast hindcast``).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from swathcast import plans
from swathcast.projection import merge_intervals, overlap_ns, project_repeat, repeat_of

LOGGER = logging.getLogger(__name__)

#: How far past each base file's end to project and score (days).
MAX_LEAD_DAYS = 120
#: S1 base and truth files: one per platform every this many days (S1 files
#: are ~3 MB and one is issued most days; every S2 file is used).
S1_CHAIN_DAYS = 7


# ------------------------------------------------------------------ ESA plans
def _plan_table(sensor: str, f, session, cache: Path) -> pd.DataFrame:
    """A plan file's datatakes, parsed once and kept as Parquet in the cache."""
    pq = cache / "esa_parsed" / f"{f.name}.parquet"
    if pq.exists():
        return pd.read_parquet(pq)
    path = plans.cached(session, f.url, cache / "esa", f"{f.name}.kml")
    df = plans.parse_esa_kml(path)
    s1 = sensor == "sentinel-1"
    out = pd.DataFrame(
        {
            "begin": df["begin"],
            "end": df["end"],
            "mode": df["Mode"].astype(str),
            "pol": df["Polarisation"].astype(str) if s1 else "",
            "track": pd.to_numeric(df.get("OrbitRelative"), errors="coerce"),
        }
    )
    pq.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(pq, index=False)
    return out


def _chain(files: list, every_days: float) -> list:
    """Per platform, the first file, then the first one at least ``every_days``
    after the last one taken."""
    out = []
    for platform in sorted({f.platform for f in files}):
        last = None
        for f in sorted(
            (f for f in files if f.platform == platform), key=lambda f: f.start
        ):
            if last is None or f.start - last.start >= pd.Timedelta(days=every_days):
                out.append(f)
                last = f
    return out


def _keep(sensor: str, df: pd.DataFrame) -> pd.DataFrame:
    """The acquisitions the map shows: Sentinel-2's nominal (tiled) datatakes;
    Sentinel-1 without wave mode (ocean vignettes, not imaged swaths)."""
    if sensor == "sentinel-2":
        return df[df["mode"] == "NOBS"]
    return df[df["mode"] != "WV"]


def esa_hindcast(sensor: str, session, cache: Path, since=None) -> pd.DataFrame:
    """One row per (base file, cycles): projected/planned/overlap seconds."""
    html = plans.get(session, plans.ESA_PAGES[sensor]).text
    files = plans.esa_plan_files(sensor, html)
    if since is not None:
        files = [f for f in files if f.start >= pd.Timestamp(since)]
    every = S1_CHAIN_DAYS if sensor == "sentinel-1" else 0
    chain = _chain(files, every) if every else files
    LOGGER.info("%s: %d plan files, %d used", sensor, len(files), len(chain))
    tables = {}
    for f in chain:
        try:
            tables[f.name] = _keep(sensor, _plan_table(sensor, f, session, cache))
        except plans.NotFound:
            LOGGER.warning("%s: %s listed but missing", sensor, f.name)
    rows = []
    for platform in sorted({f.platform for f in chain}):
        repeat = repeat_of(sensor, platform)
        pf = [f for f in chain if f.platform == platform and f.name in tables]
        truth_end = max(f.end for f in pf)
        for i, base in enumerate(pf):
            later = pf[i + 1 :]
            if not later or base.end + repeat > truth_end:
                continue
            truth = plans.newest_wins([(f, tables[f.name]) for f in later])
            truth = truth[truth["begin"] >= base.end]
            if truth.empty:
                continue
            df = tables[base.name].assign(platform=platform)
            df = df[(df["begin"] >= base.start) & (df["begin"] < base.end)]
            n = int(MAX_LEAD_DAYS // repeat.days)
            until = min(base.end + n * repeat, truth_end)
            proj = project_repeat(
                df, {platform: base.end}, repeat, until, time_cols=("begin", "end")
            )
            for k in range(1, n + 1):
                lo, hi = base.end + (k - 1) * repeat, base.end + k * repeat
                if hi > truth_end:
                    break
                p = proj[proj["cycles"] == k]
                t = truth[(truth["begin"] >= lo) & (truth["begin"] < hi)]
                row = {
                    "sensor": sensor,
                    "platform": platform,
                    "base_file": base.name,
                    "base_end": base.end,
                    "cycles": k,
                    "lead_days": (k - 0.5) * repeat.days,
                    "projected_s": _len_s(p),
                    "planned_s": _len_s(t),
                }
                # any mode, then same mode (+ polarization for S1)
                for tag, groups in (("any", []), ("mode", ["mode", "pol"])):
                    row[f"hit_{tag}_s"] = _overlap_s(p, t, groups)
                    row[f"caught_{tag}_s"] = _overlap_s(t, p, groups)
                rows.append(row)
    return pd.DataFrame(rows)


def _len_s(df) -> float:
    return float((df["end"] - df["begin"]).dt.total_seconds().sum()) if len(df) else 0.0


def _overlap_s(a: pd.DataFrame, b: pd.DataFrame, groups: list[str]) -> float:
    """Seconds of ``a`` covered by ``b`` (within each group, if any)."""
    if a.empty or b.empty:
        return 0.0
    if not groups:
        return (
            overlap_ns(
                a["begin"], a["end"], merge_intervals(b["begin"], b["end"])
            ).sum()
            / 1e9
        )
    total = 0.0
    for key, ga in a.groupby(groups):
        gb = b
        for c, v in zip(groups, key if isinstance(key, tuple) else (key,), strict=True):
            gb = gb[gb[c] == v]
        if len(gb):
            total += (
                overlap_ns(
                    ga["begin"], ga["end"], merge_intervals(gb["begin"], gb["end"])
                ).sum()
                / 1e9
            )
    return total


LEAD_BINS = [0, 30, 60, 90, 120]


def summarize_esa(df: pd.DataFrame, by_platform: bool = True) -> pd.DataFrame:
    """Pooled precision/recall per sensor (and platform) and 30-day lead bin
    (days after the base file's end), and the spread over base files."""
    if df.empty:
        return df
    df = df.assign(lead=pd.cut(df["lead_days"], LEAD_BINS).astype(str))
    keys = ["sensor", "platform", "lead"] if by_platform else ["sensor", "lead"]
    g = df.groupby(keys)
    cols = [
        "projected_s",
        "planned_s",
        "hit_any_s",
        "caught_any_s",
        "hit_mode_s",
        "caught_mode_s",
    ]
    out = g[cols].sum()
    out["bases"] = g["base_file"].nunique()
    out["precision"] = out["hit_any_s"] / out["projected_s"]
    out["recall"] = out["caught_any_s"] / out["planned_s"]
    out["precision_same_mode"] = out["hit_mode_s"] / out["projected_s"]
    out["recall_same_mode"] = out["caught_mode_s"] / out["planned_s"]
    per = df.groupby(keys + ["base_file"])[["hit_mode_s", "projected_s"]].sum()
    per = per["hit_mode_s"] / per["projected_s"].where(per["projected_s"] > 0)
    out["precision_same_mode_p10"] = per.groupby(level=keys).quantile(0.1)
    return out.reset_index().drop(columns=cols)


# -------------------------------------------------------------------- Landsat
USGS_STAC = "https://landsatlook.usgs.gov/stac-server/search"


def landsat_scenes_actual(session, path: int, row: int, start, end) -> pd.DataFrame:
    """Level-1 scenes of one path/row from USGS's STAC: platform, time, centre."""
    body = {
        "collections": ["landsat-c2l1"],
        "datetime": f"{pd.Timestamp(start).isoformat()}/{pd.Timestamp(end).isoformat()}",
        "limit": 200,
        "query": {
            "landsat:wrs_path": {"eq": f"{path:03d}"},
            "landsat:wrs_row": {"eq": f"{row:03d}"},
            "platform": {"in": ["LANDSAT_8", "LANDSAT_9"]},
        },
        "fields": {"include": ["properties.datetime", "properties.platform", "bbox"]},
    }
    out = []
    url = USGS_STAC
    while url:
        r = session.post(url, json=body, timeout=120)
        r.raise_for_status()
        j = r.json()
        for f in j.get("features", []):
            b = f.get("bbox") or [np.nan] * 4
            out.append(
                (
                    {"LANDSAT_8": "L8", "LANDSAT_9": "L9"}[f["properties"]["platform"]],
                    pd.Timestamp(f["properties"]["datetime"]),
                    (b[0] + b[2]) / 2,
                    (b[1] + b[3]) / 2,
                )
            )
        nxt = [link for link in j.get("links", []) if link.get("rel") == "next"]
        url, body = (nxt[0]["href"], nxt[0].get("body", body)) if nxt else (None, None)
    df = pd.DataFrame(out, columns=["platform", "time", "lon", "lat"])
    df["path"], df["row"] = path, row
    return df.drop_duplicates(["platform", "time"]).sort_values("time")


def landsat_hindcast(
    session, n_cells: int = 40, days: int = 365, seed: int = 1, now=None, cache=None
):
    """Calendar and timing scores on a seeded sample of daytime path/rows."""
    from swathcast import schedule
    from swathcast.ephemeris import nearest_pass

    now = pd.Timestamp(now or datetime.now(UTC)).floor("D")
    start = now - pd.Timedelta(days=days)
    pq = (
        Path(cache) / f"landsat_actual_{now.date()}_{days}_{n_cells}_{seed}.parquet"
        if cache
        else None
    )
    if pq is not None and pq.exists():
        actual = pd.read_parquet(pq)
    else:
        rng = np.random.default_rng(seed)
        cells = []
        tried = 0
        while len(cells) < n_cells and tried < n_cells * 6:
            tried += 1
            path, row = int(rng.integers(1, 234)), int(rng.integers(15, 105))
            df = landsat_scenes_actual(session, path, row, start, now)
            if len(df) >= 10:  # land, imaged
                cells.append(df)
        actual = pd.concat(cells, ignore_index=True)
        if pq is not None:
            pq.parent.mkdir(parents=True, exist_ok=True)
            actual.to_parquet(pq, index=False)
    LOGGER.info(
        "landsat: %d scenes on %d path/rows",
        len(actual),
        len(actual[["path", "row"]].drop_duplicates()),
    )

    cyc = plans.get(session, f"{plans.USGS_JSON}/cycles.json").json()
    cpr = plans.get(session, f"{plans.USGS_JSON}/cycle_path_row.json").json()
    cal = plans.landsat_calendar(cyc, cpr, start.date(), days)
    keys = actual[["path", "row"]].drop_duplicates()
    cal = cal.merge(keys, on=["path", "row"])
    cal_set = {(p, d, pa, r) for p, d, pa, r in cal.itertuples(index=False)}

    def on_cal(p, t, pa, r, slack):
        return any(
            (p, (t + pd.Timedelta(days=s)).date(), pa, r) in cal_set
            for s in range(-slack, slack + 1)
        )

    act = actual.assign(
        on_cal=[
            on_cal(p, t, pa, r, 0)
            for p, t, pa, r in actual[["platform", "time", "path", "row"]].itertuples(
                index=False
            )
        ],
        on_cal_1d=[
            on_cal(p, t, pa, r, 1)
            for p, t, pa, r in actual[["platform", "time", "path", "row"]].itertuples(
                index=False
            )
        ],
    )
    got = {
        (p, t.date(), pa, r)
        for p, t, pa, r in actual[["platform", "time", "path", "row"]].itertuples(
            index=False
        )
    }
    got |= {
        (p, (t - pd.Timedelta(days=1)).date(), pa, r)
        for p, t, pa, r in actual[["platform", "time", "path", "row"]].itertuples(
            index=False
        )
    }
    cal = cal.assign(
        acquired=[k in got for k in cal.itertuples(index=False, name=None)]
    )

    # timing by repeat shift: every later scene of the same platform & cell
    shift = []
    for _, g in actual.groupby(["platform", "path", "row"]):
        t = np.sort(np.array([x.timestamp() for x in g["time"]]))
        for i in range(len(t)):
            dt = t[i + 1 :] - t[i]
            k = np.rint(dt / (16 * 86400)).astype(int)
            ok = (k >= 1) & (np.abs(dt - k * 16 * 86400) < 3600)
            for kk, e in zip(k[ok], dt[ok] - k[ok] * 16 * 86400, strict=True):
                shift.append((int(kk), float(e)))
    shift = pd.DataFrame(shift, columns=["cycles", "error_s"])

    # timing by SGP4 with today's elements
    sats = plans.satrecs(schedule.orbital_elements())
    sg = []
    for platform, sat in sats.items():
        if platform not in ("L8", "L9"):
            continue
        epoch = pd.Timestamp(
            sat.jdsatepoch + sat.jdsatepochF - 2440587.5, unit="D", tz="UTC"
        )
        a = actual[actual["platform"] == platform]
        for t, lon, lat in a[["time", "lon", "lat"]].itertuples(index=False):
            ts = t.timestamp()
            tp, dist = nearest_pass(
                sat, [lon], [lat], ts - 1800, ts + 1800, ascending=False
            )
            if np.isfinite(tp[0]) and dist[0] < 60:
                sg.append(((epoch - t).total_seconds() / 86400, float(tp[0] - ts)))
            else:
                sg.append(((epoch - t).total_seconds() / 86400, np.nan))
    sg = pd.DataFrame(sg, columns=["lead_days", "error_s"])
    return act, cal, shift, sg


def _q(s, q):
    s = pd.Series(s).dropna().abs()
    return float(s.quantile(q)) if len(s) else float("nan")


def summarize_landsat(act, cal, shift, sg) -> dict:
    out = {
        "scenes": int(len(act)),
        "path_rows": int(len(act[["path", "row"]].drop_duplicates())),
        "window": [str(act["time"].min()), str(act["time"].max())],
        "acquired_on_calendar_date": float(act["on_cal"].mean()),
        "acquired_on_calendar_within_1d": float(act["on_cal_1d"].mean()),
        "calendar_days_acquired": float(cal["acquired"].mean()),
        "calendar_days_acquired_by_platform": cal.groupby("platform")["acquired"]
        .mean()
        .round(4)
        .to_dict(),
    }
    bins = [0, 1, 2, 4, 8, 12, 16, 23]
    sh = shift.assign(bin=pd.cut(shift["cycles"], bins, right=True))
    out["repeat_shift"] = [
        {
            "cycles": str(b),
            "days": f"{int(b.left) * 16 + 16}-{int(b.right) * 16}",
            "n": int(len(g)),
            "median_abs_s": _q(g["error_s"], 0.5),
            "p90_abs_s": _q(g["error_s"], 0.9),
            "p99_abs_s": _q(g["error_s"], 0.99),
        }
        for b, g in sh.groupby("bin", observed=True)
    ]
    lb = [0, 15, 30, 60, 120, 240, 400]
    s = sg.assign(bin=pd.cut(sg["lead_days"].abs(), lb))
    out["sgp4"] = [
        {
            "days_from_epoch": str(b),
            "n": int(len(g)),
            "found": float(g["error_s"].notna().mean()),
            "median_abs_s": _q(g["error_s"], 0.5),
            "p90_abs_s": _q(g["error_s"], 0.9),
        }
        for b, g in s.groupby("bin", observed=True)
    ]
    return out


# ---------------------------------------------------------------------- NISAR
NISAR_BASES_EVERY_DAYS = 30


def nisar_hindcast(
    session, cache: Path, max_lead_days: int = MAX_LEAD_DAYS
) -> pd.DataFrame:
    """NASA's plan against itself: does it repeat every 12 days? For a base
    date every month of the plan, the 12 days before it are repeated forward
    and compared, as (day, track, frame, direction) [+ mode], with what the
    plan has on those days. One row per (base, cycles), like esa_hindcast,
    counting frames instead of seconds. (Only one plan version is published,
    so it is the plan's own consistency, not a later re-plan.)"""
    plan, _ = plans.nisar_plan(session, cache)
    key = ["track", "frame", "direction"]
    plan = plan[key + ["day", "mode"]].drop_duplicates(key + ["day"])
    repeat = pd.Timedelta(days=12)
    first, last = plan["day"].min() + repeat, plan["day"].max()
    rows = []
    t = first
    while t + repeat <= last:
        base = plan[(plan["day"] >= t - repeat) & (plan["day"] < t)]
        n = int(min(max_lead_days, (last - t).days + 1) // 12)
        for k in range(1, n + 1):
            lo, hi = t + (k - 1) * repeat, t + k * repeat
            proj = base.assign(day=base["day"] + k * repeat)
            truth = plan[(plan["day"] >= lo) & (plan["day"] < hi)]
            hit = proj.merge(truth, on=key + ["day"], suffixes=("", "_t"))
            rows.append(
                {
                    "sensor": "nisar",
                    "platform": "NISAR",
                    "base_file": str(t.date()),
                    "base_end": t,
                    "cycles": k,
                    "lead_days": (k - 0.5) * 12,
                    "projected_s": float(len(proj)),
                    "planned_s": float(len(truth)),
                    "hit_any_s": float(len(hit)),
                    "caught_any_s": float(len(hit)),
                    "hit_mode_s": float((hit["mode"] == hit["mode_t"]).sum()),
                    "caught_mode_s": float((hit["mode"] == hit["mode_t"]).sum()),
                }
            )
        t += pd.Timedelta(days=NISAR_BASES_EVERY_DAYS)
    return pd.DataFrame(rows)


# ----------------------------------------------------------- MODIS, VIIRS, S3
#: Archive Level-1B collections (CMR): granules on a fixed UTC grid (MODIS
#: 5 min, VIIRS 6 min), so the same granule start one repeat later images the
#: same ground if the orbit repeats.
WIDE_COLLECTIONS = {
    "Terra": ("modis", "C1378227407-LAADS"),
    "Aqua": ("modis", "C1379758607-LAADS"),
    "SNPP": ("viirs", "C2105087643-LAADS"),
    "N20": ("viirs", "C2103811746-LAADS"),
    "N21": ("viirs", "C3478403600-LAADS"),
}
CMR_GRANULES = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
CDSE_ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
#: ground speed of the sub-satellite point (km/s), to turn an along-track
#: offset into seconds
GROUND_KM_S = {"modis": 6.75, "viirs": 6.6}


def _unit(lon, lat):
    lon, lat = np.radians(lon), np.radians(lat)
    return np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def _centroid(points) -> np.ndarray:
    v = np.mean([_unit(p["Longitude"], p["Latitude"]) for p in points], axis=0)
    return v / np.linalg.norm(v)


def _cmr_granules(session, collection: str, start, end) -> dict:
    """{begin time of day (s): footprint centroid (unit vector)} in [start, end)."""
    r = session.get(
        CMR_GRANULES,
        params={
            "collection_concept_id": collection,
            "temporal": f"{pd.Timestamp(start).isoformat()},{pd.Timestamp(end).isoformat()}",
            "page_size": 100,
        },
        timeout=120,
    )
    r.raise_for_status()
    out = {}
    for it in r.json().get("items", []):
        u = it["umm"]
        b = pd.Timestamp(u["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"])
        if b < pd.Timestamp(start) or b >= pd.Timestamp(end):
            continue
        try:
            pts = u["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"][
                "GPolygons"
            ][0]["Boundary"]["Points"]
        except (KeyError, IndexError, TypeError):
            continue
        out[(b - b.floor("D")).total_seconds()] = _centroid(pts)
    return out


def wide_hindcast(
    session, days: int = 368, hours: float = 1.0, now=None
) -> pd.DataFrame:
    """MODIS and VIIRS: the same granule start (UTC time of day) every 16 days
    for a year; between every pair of those days, how far the footprint moved,
    split along track (as seconds) and across. One row per (platform, pair)."""
    now = pd.Timestamp(now or datetime.now(UTC)).floor("D") - pd.Timedelta(days=3)
    from swathcast.platforms import is_retired

    rows = []
    for platform, (mission, coll) in WIDE_COLLECTIONS.items():
        if is_retired(platform):  # not projected: no point measuring it
            continue
        rep = pd.Timedelta(days=16)
        n = int(days // 16)
        d0 = now - n * rep
        got = []
        for k in range(n + 1):
            day = d0 + k * rep + pd.Timedelta(hours=12)
            try:
                got.append(
                    _cmr_granules(session, coll, day, day + pd.Timedelta(hours=hours))
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("%s %s: %s", platform, day.date(), exc)
                got.append({})
        for i in range(len(got)):
            for j in range(i + 1, len(got)):
                for tod, a in got[i].items():
                    b = got[j].get(tod)
                    nxt = got[i].get(min((x for x in got[i] if x > tod), default=-1))
                    if b is None or nxt is None:
                        continue
                    along = nxt - a
                    along -= np.dot(along, a) * a
                    along /= np.linalg.norm(along)
                    d = b - a
                    d -= np.dot(d, a) * a  # in the tangent plane at a
                    along_km = float(np.dot(d, along) * 6371.0)
                    cross_km = float(
                        np.linalg.norm(d - np.dot(d, along) * along) * 6371.0
                    )
                    rows.append(
                        {
                            "mission": mission,
                            "platform": platform,
                            "lead_days": (j - i) * 16,
                            "along_s": along_km / GROUND_KM_S[mission],
                            "cross_km": cross_km,
                        }
                    )
    return pd.DataFrame(rows)


def _s3_frames(session, platform: str, start, end) -> dict:
    """{(relative orbit, frame): sensing start} of SLSTR RBT frames from CDSE."""
    flt = (
        f"startswith(Name,'{platform}_SL_1_RBT') "
        f"and ContentDate/Start ge {pd.Timestamp(start).strftime('%Y-%m-%dT%H:%M:%S.000Z')} "
        f"and ContentDate/Start lt {pd.Timestamp(end).strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
    )
    r = session.get(CDSE_ODATA, params={"$filter": flt, "$top": 200}, timeout=120)
    r.raise_for_status()
    out = {}
    for p in r.json().get("value", []):
        tok = [t for t in p["Name"].removesuffix(".SEN3").split("_") if t]
        try:
            rel, frame = int(tok[9]), int(tok[10])
            t = pd.Timestamp(tok[4], tz="UTC")
        except (IndexError, ValueError):
            continue
        out.setdefault((rel, frame), t)
    return out


def s3_hindcast(session, days: int = 351, hours: float = 1.0, now=None) -> pd.DataFrame:
    """Sentinel-3: the same frame (relative orbit, seconds after the node) every
    27 days for a year; the error of its sensing start shifted by whole cycles."""
    now = pd.Timestamp(now or datetime.now(UTC)).floor("D") - pd.Timedelta(days=3)
    rows = []
    for platform in ("S3A", "S3B"):
        rep = pd.Timedelta(days=27)
        n = int(days // 27)
        d0 = now - n * rep
        got = []
        for k in range(n + 1):
            day = d0 + k * rep + pd.Timedelta(hours=12)
            try:
                got.append(
                    _s3_frames(session, platform, day, day + pd.Timedelta(hours=hours))
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("%s %s: %s", platform, day.date(), exc)
                got.append({})
        for i in range(len(got)):
            for j in range(i + 1, len(got)):
                for key, t in got[i].items():
                    u = got[j].get(key)
                    if u is None:
                        continue
                    err = (u - t - (j - i) * rep).total_seconds()
                    if abs(err) < 600:
                        rows.append(
                            {
                                "mission": "sentinel-3",
                                "platform": platform,
                                "lead_days": (j - i) * 27,
                                "along_s": err,
                                "cross_km": np.nan,
                            }
                        )
    return pd.DataFrame(rows)


WIDE_BINS = [0, 30, 60, 120, 240, 400]


def summarize_wide(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    df = df.assign(lead=pd.cut(df["lead_days"], WIDE_BINS).astype(str))
    out = []
    for (platform, lead), g in df.groupby(["platform", "lead"], sort=False):
        out.append(
            {
                "mission": g["mission"].iloc[0],
                "platform": platform,
                "lead": lead,
                "n": int(len(g)),
                "median_abs_s": _q(g["along_s"], 0.5),
                "p90_abs_s": _q(g["along_s"], 0.9),
                "median_cross_km": _q(g["cross_km"], 0.5),
                "p90_cross_km": _q(g["cross_km"], 0.9),
            }
        )
    order = list(WIDE_COLLECTIONS) + ["S3A", "S3B"]
    return sorted(
        out,
        key=lambda r: (
            order.index(r["platform"]) if r["platform"] in order else 99,
            WIDE_BINS.index(int(float(r["lead"].split(",")[0][1:]))),
        ),
    )


# --------------------------------------------------------------------- driver
def _finite(x):
    """NaN and infinities as null: hindcast.json is read by browsers, whose
    JSON has no NaN (one would make the whole file unreadable)."""
    if isinstance(x, float) and not np.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_finite(v) for v in x]
    return x


ALL = ("landsat", "sentinel-1", "sentinel-2", "nisar", "modis", "viirs", "sentinel-3")


def run(out_dir: Path, sensors=ALL, since=None) -> dict:
    import requests

    from swathcast.schedule import cache_dir

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "swathcast (+github.com/egagli)"
    result: dict = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    esa = []
    for sensor in ("sentinel-1", "sentinel-2"):
        if sensor in sensors:
            esa.append(esa_hindcast(sensor, session, cache_dir(), since))
    if "nisar" in sensors:
        esa.append(nisar_hindcast(session, cache_dir()))
    if esa:
        detail = pd.concat(esa, ignore_index=True)
        detail.to_parquet(out_dir / "hindcast_esa_detail.parquet", index=False)
        result["esa"] = json.loads(
            summarize_esa(detail, False).to_json(orient="records")
        )
        result["esa_by_platform"] = json.loads(
            summarize_esa(detail).to_json(orient="records")
        )
    if "landsat" in sensors:
        act, cal, shift, sg = landsat_hindcast(session, cache=cache_dir() / "hindcast")
        result["landsat"] = summarize_landsat(act, cal, shift, sg)
    wide = []
    if "modis" in sensors or "viirs" in sensors:
        w = wide_hindcast(session)
        wide.append(
            w[w["mission"].isin([m for m in ("modis", "viirs") if m in sensors])]
        )
    if "sentinel-3" in sensors:
        wide.append(s3_hindcast(session))
    if wide:
        result["wide"] = summarize_wide(pd.concat(wide, ignore_index=True))
    (out_dir / "hindcast.json").write_text(
        json.dumps(_finite(result), indent=2, default=str, allow_nan=False) + "\n"
    )
    (out_dir / "hindcast.md").write_text(markdown(result))
    return result


def markdown(result: dict) -> str:
    lines = [f"# Projection hindcast ({result['generated_at']})", ""]
    for key, title in (("esa", "by sensor"), ("esa_by_platform", "by platform")):
        if key not in result:
            continue
        lines += [
            f"## Sentinel-1 / Sentinel-2 / NISAR, {title}: projected vs later planned (S1/S2 time-weighted; NISAR frames, the plan against itself)",
            "",
            "Lead: days after the base plan's end. Same mode: also the same mode (S1: and polarization).",
            "",
            "| sensor | platform | lead (d) | bases | precision | recall | precision, same mode | recall, same mode | p10 over bases (same mode) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in result[key]:
            lines.append(
                f"| {r['sensor']} | {r.get('platform', 'all')} | {r['lead']} | {r['bases']} | "
                f"{r['precision']:.1%} | {r['recall']:.1%} | {r['precision_same_mode']:.1%} | "
                f"{r['recall_same_mode']:.1%} | {r['precision_same_mode_p10']:.1%} |"
            )
        lines.append("")
    if result.get("wide"):
        lines += [
            "## MODIS, VIIRS, Sentinel-3: the same granule or frame whole repeats later",
            "",
            "MODIS/VIIRS: granules start on a fixed UTC grid, so the same start 16 k days later images the same ground if the orbit repeats; the footprint's shift, along track as seconds and across track in km. Sentinel-3: the same frame (relative orbit, seconds after the node) 27 k days later; its sensing start's error.",
            "",
            "| platform | lead (d) | n | along track, median abs (s) | p90 (s) | across track, median (km) | p90 (km) |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in result["wide"]:
            lines.append(
                f"| {r['platform']} | {r['lead']} | {r['n']} | {r['median_abs_s']:.1f} | {r['p90_abs_s']:.1f} | "
                f"{r['median_cross_km']:.1f} | {r['p90_cross_km']:.1f} |"
            )
        lines.append("")
    if "landsat" in result:
        ls = result["landsat"]
        lines += [
            "## Landsat 8/9: USGS calendar and timing vs acquired Level-1 scenes",
            "",
            f"{ls['scenes']} scenes on {ls['path_rows']} daytime path/rows, {ls['window'][0]} to {ls['window'][1]}.",
            "",
            f"- acquired on a calendar date: {ls['acquired_on_calendar_date']:.1%} (within 1 day: {ls['acquired_on_calendar_within_1d']:.1%})",
            f"- calendar days on which the scene was acquired: {ls['calendar_days_acquired']:.1%} {ls['calendar_days_acquired_by_platform']}",
            "",
            "| repeat shift, cycles | days | n | median abs (s) | p90 abs (s) | p99 abs (s) |",
            "|---|---|---|---|---|---|",
        ]
        for r in ls["repeat_shift"]:
            lines.append(
                f"| {r['cycles']} | {r['days']} | {r['n']} | {r['median_abs_s']:.1f} | {r['p90_abs_s']:.1f} | {r['p99_abs_s']:.1f} |"
            )
        lines += [
            "",
            "| SGP4, days from elements' epoch | n | found | median abs (s) | p90 abs (s) |",
            "|---|---|---|---|---|",
        ]
        for r in ls["sgp4"]:
            lines.append(
                f"| {r['days_from_epoch']} | {r['n']} | {r['found']:.1%} | {r['median_abs_s']:.1f} | {r['p90_abs_s']:.1f} |"
            )
        lines.append("")
    return "\n".join(lines)
