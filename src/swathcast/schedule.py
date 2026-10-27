"""Build the upcoming-pass schedule from the agencies' own plans.

Plans are read by :mod:`swathcast.plans` (ESA KMLs for Sentinel-1/2, the USGS
cycle calendar for Landsat, NASA's NISAR plan). This module assembles them and
applies documented post-processing:

1. **Orbit direction.** ESA's KMLs have no direction field. It is taken from
   the orbit at the datatake midpoint (SGP4, CelesTrak elements): 3,637 of
   3,653 CDSE products (2026-09-26..29) agreed, all 16 misses poleward of 70
   deg. Without elements: local solar time at the centroid (99.7 % on 5,854
   products, skipped poleward of 70 deg).
2. **Antimeridian.** Plan footprints with longitudes outside +/-180 are split
   into MultiPolygons so the browser's intersection test works.
3. **Size.** Non-Sentinel-1 footprints are simplified to ~1 km and the file
   is written with zstd; Sentinel-1 footprints are exact.

4. **Observed outlines.** NISAR's planned frames and Landsat's WRS-2 cells
   are replaced by the latest real product's footprint of the same frame or
   path/row where one was seen (:mod:`swathcast.outlines`).

Beside ``upcoming_passes.parquet`` it writes the footprint grids the map loads
lazily: ``wrs2_grid.parquet`` + ``landsat_scenes.parquet`` (per planned scene,
timed from the orbit), ``s2_grid.parquet`` + ``s2_tile_passes.parquet``, and,
for the wide-swath sensors that have no plan (:mod:`swathcast.wideswath`),
``sin_grid.parquet`` + ``modis_tile_passes.parquet`` / ``viirs_tile_passes.parquet``
/ ``s3_tile_passes.parquet``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

LOGGER = logging.getLogger(__name__)

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
]
DIRECTION_MAX_ABS_LAT = 70.0
#: Non-Sentinel-1 footprints (NISAR frames above all) are simplified to about
#: 1 km for the browser; this keeps the whole-schedule file under ~5 MB.
#: Sentinel-1 footprints are left exact.
SITE_SIMPLIFY_DEG = 0.01
#: Days of the recent past kept per sensor: a pass already imaged but not yet
#: in any catalogue is still shown (as planned, not delivered) until it is
#: delivered. About the p90 delivery time of the sensor's first source
#: (2026-10: Sentinel-1 CDSE 6 h, Sentinel-2 CDSE 7 h, NISAR L0B 9 h, Landsat
#: Level-1 at USGS 244 h), plus a day.
LOOKBACK_DAYS = {
    "sentinel-1": 2,
    "sentinel-2": 2,
    "nisar": 3,
    "landsat": 11,
    # no plans: passes predicted from the orbit (wideswath.py); LANCE NRT and
    # CDSE NRT deliver within hours
    "modis": 2,
    "viirs": 2,
    "sentinel-3": 2,
}


def solar_direction(when: datetime, lon: float, lat: float) -> str:
    """Sentinel-1 pass direction from local solar time; "" when ambiguous."""
    if abs(lat) > DIRECTION_MAX_ABS_LAT:
        return ""
    hours = when.hour + when.minute / 60 + when.second / 3600
    local_solar = (hours + lon / 15.0) % 24
    return "ascending" if 12 <= local_solar < 24 else "descending"


#: Plan footprints (ESA KML ``tessellate``, NISAR frames) have few vertices,
#: joined by great circles. Drawn as straight lines in lon/lat they bend
#: around the poles (a 5,000 km Arctic EW edge between two points at 80 N
#: 180 deg of longitude apart runs along 80 N instead of across the pole), so
#: long edges are densified along the great circle first.
GEODESIC_STEP_KM = 50.0
GEODESIC_TOL_DEG = 0.01
EARTH_RADIUS_KM = 6371.0


def _unit(lon, lat):
    lon, lat = np.radians(lon), np.radians(lat)
    return np.stack(
        [np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], -1
    )


def _slerp(a, b, t):
    ang = float(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0)))
    if ang < 1e-12:
        return a
    return (np.sin((1 - t) * ang) * a + np.sin(t * ang) * b) / np.sin(ang)


def _lonlat(w):
    return (
        float(np.degrees(np.arctan2(w[1], w[0]))),
        float(np.degrees(np.arcsin(np.clip(w[2], -1.0, 1.0)))),
    )


def _near(lon, ref):
    return ref + (lon - ref + 180) % 360 - 180


def _edge(a, b, lon_a, depth=0, tol_deg=GEODESIC_TOL_DEG, min_km=GEODESIC_STEP_KM):
    """Points after ``a`` up to and including ``b`` (continuous longitudes),
    bisecting the great circle while its midpoint is more than ``tol_deg``
    off the straight lon/lat segment."""
    lon_b, lat_b = _lonlat(b)
    lon_b = _near(lon_b, lon_a)
    ang = float(np.arccos(np.clip(np.dot(a, b), -1.0, 1.0)))
    if depth < 12 and ang * EARTH_RADIUS_KM > min_km:
        m = _slerp(a, b, 0.5)
        lon_m, lat_m = _lonlat(m)
        lon_m = _near(lon_m, lon_a)
        lat_a = _lonlat(a)[1]
        off = max(
            abs(lat_m - (lat_a + lat_b) / 2),
            abs(lon_m - (lon_a + lon_b) / 2) * np.cos(np.radians(lat_m)),
        )
        if off > tol_deg:
            left = _edge(a, m, lon_a, depth + 1, tol_deg, min_km)
            return left + _edge(m, b, left[-1][0], depth + 1, tol_deg, min_km)
    return [(lon_b, lat_b)]


def geodesic_ring(
    coords, step_km: float = GEODESIC_STEP_KM
) -> list[tuple[float, float]]:
    """Closed ring whose edges follow great circles to within
    GEODESIC_TOL_DEG (points added only where the straight lon/lat segment
    departs from it, i.e. long edges at high latitude), with *continuous*
    longitudes (each within 180 deg of the previous; they may leave
    [-180, 180]). A ring around a pole is closed through that pole."""
    pts = [tuple(map(float, c[:2])) for c in coords]
    if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    if len(pts) < 3:
        return pts
    v = [_unit(x, y) for x, y in pts]
    ring = [(pts[0][0], pts[0][1])]
    for i in range(len(pts)):
        ring += _edge(v[i], v[(i + 1) % len(pts)], ring[-1][0], min_km=step_km)
    # the last point is the first one again, possibly +/-360 around a pole
    winding = ring[-1][0] - ring[0][0]
    ring = ring[:-1]
    if abs(winding) > 180:
        pole = 90.0 if np.mean([y for _, y in ring]) > 0 else -90.0
        ring += [
            (ring[0][0] + winding, ring[0][1]),
            (ring[0][0] + winding, pole),
            (ring[0][0], pole),
        ]
    ring.append(ring[0])
    return ring


def geodesic_densify(geom, step_km: float = GEODESIC_STEP_KM):
    """Densify every polygon ring along great circles (see geodesic_ring),
    before wrap_antimeridian splits the result at +/-180."""
    from shapely.geometry import MultiPolygon, Polygon

    if geom is None or geom.is_empty:
        return geom
    if isinstance(geom, MultiPolygon):
        # a KML footprint already split at the antimeridian: rejoin by
        # densifying each part (its pieces then re-split cleanly)
        parts = [geodesic_densify(g, step_km) for g in geom.geoms]
        return MultiPolygon([q for g in parts for q in getattr(g, "geoms", [g])])
    if not isinstance(geom, Polygon):
        return geom
    return Polygon(geodesic_ring(list(geom.exterior.coords), step_km))


def wrap_antimeridian(geom):
    """Split a polygon with longitudes outside [-180, 180] into wrapped pieces."""
    from shapely import affinity, make_valid
    from shapely.geometry import MultiPolygon, Polygon, box
    from shapely.ops import unary_union

    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        geom = make_valid(geom)
    minx, _, maxx, _ = geom.bounds
    if minx >= -180 and maxx <= 180:
        if maxx - minx <= 180:
            return geom
        # Wrapped but jumping across the antimeridian: unwrap first.
        if isinstance(geom, Polygon):
            geom = Polygon(geodesic_ring(list(geom.exterior.coords), step_km=1e9))
        else:
            return geom
    pieces = []
    for shift in (-720, -360, 0, 360, 720):
        clip = geom.intersection(box(-180 - shift, -90, 180 - shift, 90))
        if not clip.is_empty:
            pieces.append(affinity.translate(clip, xoff=shift))
    merged = unary_union(pieces)
    polys = [g for g in getattr(merged, "geoms", [merged]) if g.geom_type == "Polygon"]
    return MultiPolygon(polys) if len(polys) > 1 else (polys[0] if polys else merged)


def wrs2_grid(outlines):
    """Landsat WRS-2 path/row cells for the grid view, simplified for the browser."""
    grid = outlines[["path", "row", "direction", "geometry"]].copy()
    grid["geometry"] = grid.geometry.simplify(0.005, preserve_topology=True)
    grid["path"] = grid["path"].astype("int16")
    grid["row"] = grid["row"].astype("int16")
    return grid.reset_index(drop=True)


def simplify_non_s1(gdf, tolerance: float) -> float:
    mask = gdf["sensor"] != "sentinel-1"
    if mask.any() and tolerance > 0:
        gdf.loc[mask, "geometry"] = gdf.loc[mask, "geometry"].simplify(
            tolerance, preserve_topology=True
        )
    return tolerance


def _centroid_lonlat(geom) -> tuple[float, float]:
    """Centroid of a footprint that may be unwrapped or jump the antimeridian."""
    from shapely.geometry import Polygon

    minx, _, maxx, _ = geom.bounds
    if isinstance(geom, Polygon) and minx >= -180 and maxx <= 180 and maxx - minx > 180:
        geom = Polygon([(x + 360 if x < 0 else x, y) for x, y in geom.exterior.coords])
    c = geom.centroid
    return ((c.x + 180) % 360) - 180, c.y


def postprocess(gdf):
    """Apply the documented fixes in place and return counts for the sidecar."""
    from shapely.errors import GEOSException

    s1 = (gdf["sensor"] == "sentinel-1") & (gdf["orbit_direction"].fillna("") == "")
    filled = 0
    for i in gdf.index[s1]:
        geom = gdf.at[i, "geometry"]
        if geom is None or geom.is_empty:
            continue
        begin, end = gdf.at[i, "begin_time"], gdf.at[i, "end_time"]
        lon, lat = _centroid_lonlat(geom)
        direction = solar_direction(
            (begin + (end - begin) / 2).to_pydatetime(), lon, lat
        )
        if direction:
            gdf.at[i, "orbit_direction"] = direction
            filled += 1
    fixed = failed = 0
    for i in gdf.index:
        geom = gdf.at[i, "geometry"]
        try:
            dense = geodesic_densify(geom)
            new = wrap_antimeridian(dense)
        except GEOSException as exc:
            LOGGER.warning("schedule row %s: geometry left as-is (%s)", i, exc)
            failed += 1
            continue
        gdf.at[i, "geometry"] = new
        if new is not dense:
            fixed += 1
    return {
        "s1_direction_filled": filled,
        "s1_direction_left_blank": int(s1.sum()) - filled,
        "geometry_fixed_or_split": fixed,
        "geometry_fix_failed": failed,
    }


def cache_dir() -> Path:
    return Path(os.environ.get("SWATHCAST_CACHE", Path.home() / ".cache" / "swathcast"))


def orbital_elements() -> dict:
    """Fresh CelesTrak elements for every platform ({platform: OMM}); {} if
    CelesTrak is unreachable (orbit timing then falls back per platform)."""
    from swathcast import tle

    with tempfile.TemporaryDirectory() as tmp:
        try:
            payload = tle.update(Path(tmp) / "tle.json")
        except OSError as exc:
            LOGGER.warning("orbital elements unavailable: %s", exc)
            return {}
    return {p: s["omm"] for p, s in payload["satellites"].items()}


ALL_SENSORS = [
    "sentinel-1",
    "sentinel-2",
    "landsat",
    "nisar",
    "modis",
    "viirs",
    "sentinel-3",
]
#: Wide-swath sensors without a plan (wideswath.py): the files written for them
WIDESWATH_FILES = {
    "modis": "modis_tile_passes.parquet",
    "viirs": "viirs_tile_passes.parquet",
    "sentinel-3": "s3_tile_passes.parquet",
}


def build_wideswath(out_dir: Path, sensors, sats: dict, start, end, days: int) -> dict:
    """The sinusoidal tiles and when MODIS, VIIRS and Sentinel-3 image each
    (predicted from the orbit; Sentinel-3 passes labelled with their frame),
    written beside the schedule.
    Returns counts for the sidecar; a mission whose platforms have no
    elements is left out (and said so)."""
    import pandas as pd

    from swathcast import wideswath

    fixes: dict = {}
    tiles = [m for m in ("modis", "viirs", "sentinel-3") if m in sensors]
    if tiles:
        grid = wideswath.sin_grid()
        wideswath.site_sin_grid(grid).to_parquet(
            out_dir / "sin_grid.parquet", index=False, compression="zstd"
        )
        fixes["sin_tiles"] = int(len(grid))
        for m in tiles:
            back = start - pd.Timedelta(days=LOOKBACK_DAYS[m])
            passes = wideswath.sin_tile_passes(grid, sats, m, back, end)
            if passes.empty:
                fixes[f"{m}_error"] = "no orbital elements for any platform"
                continue
            passes.to_parquet(
                out_dir / WIDESWATH_FILES[m], index=False, compression="zstd"
            )
            fixes[f"{m}_tile_passes"] = {
                str(k): int(v) for k, v in passes["platform"].value_counts().items()
            }
    return fixes


def build(output: Path, days: int = 12, sensors: list[str] | None = None) -> Path:
    """Fetch the plans and write ``output`` (+ ``.meta.json`` sidecar and grids)."""
    import geopandas as gpd
    import pandas as pd
    import requests

    from swathcast import outlines as outlines_mod
    from swathcast import plans, s2grid

    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sensors = sensors or list(ALL_SENSORS)
    session = requests.Session()
    session.headers["User-Agent"] = "swathcast (+github.com/egagli)"
    cache = cache_dir()
    start, end = plans.utc_window(days)
    elements = orbital_elements()
    sats = plans.satrecs(elements)
    parts, fixes, sources = [], {}, {}
    missing_plan_files: list[str] = []  # listed by ESA but a 404

    for sensor in ("sentinel-1", "sentinel-2"):
        if sensor in sensors:
            back = start - pd.Timedelta(days=LOOKBACK_DAYS[sensor])
            parts.append(
                plans.sentinel_schedule(
                    sensor, back, end, session, cache, missing_plan_files
                )
            )
            sources[sensor] = plans.ESA_PAGES[sensor]
    if "nisar" in sensors:
        try:
            nisar, url = plans.nisar_schedule(
                start - pd.Timedelta(days=LOOKBACK_DAYS["nisar"]),
                end,
                session,
                cache,
                sats.get("NISAR"),
            )
            parts.append(nisar)
            sources["nisar"] = url
        except RuntimeError as exc:
            LOGGER.warning("NISAR plan unavailable: %s", exc)
            fixes["nisar_error"] = str(exc)
    if not parts:  # only plan-less sensors asked for
        parts.append(
            gpd.GeoDataFrame(
                {c: [] for c in plans.SCHEDULE_COLUMNS if c != "geometry"},
                geometry=[],
                crs="EPSG:4326",
            )
        )
    gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    gdf = gdf[plans.SCHEDULE_COLUMNS].copy()
    if "nisar" in sensors and (gdf["sensor"] == "nisar").any():
        try:
            observed = outlines_mod.nisar_observed(session, datetime.now(UTC))
            fixes["nisar_observed_outlines"] = outlines_mod.apply_nisar(gdf, observed)
        except (OSError, ValueError) as exc:  # requests errors are OSErrors
            LOGGER.warning("NISAR observed outlines unavailable: %s", exc)
            fixes["nisar_observed_outlines_error"] = str(exc)
    if "sentinel-1" in sensors and not (gdf["sensor"] == "sentinel-1").any():
        raise RuntimeError("no Sentinel-1 passes in the ESA plans; refusing to publish")

    fixes["s1_direction_from_orbit"] = plans.s1_orbit_direction(gdf, sats)
    fixes.update(postprocess(gdf))
    s2 = None
    if "sentinel-2" in sensors:
        try:
            tiles = s2grid.load_tiles(cache)
            s2 = (
                s2grid.site_grid(tiles),
                s2grid.tile_passes(
                    tiles,
                    gdf,
                    {k: v for k, v in elements.items() if k.startswith("S2")},
                ),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            LOGGER.warning("Sentinel-2 tile grid unavailable: %s", exc)
    if "landsat" in sensors:
        try:
            cyc = plans.get(session, f"{plans.USGS_JSON}/cycles.json").json()
            cpr = plans.get(session, f"{plans.USGS_JSON}/cycle_path_row.json").json()
            outlines = plans.wrs2_outlines(session)
            try:
                observed = outlines_mod.landsat_observed(session, datetime.now(UTC))
                fixes["landsat_observed_outlines"] = outlines_mod.apply_landsat(
                    outlines, observed
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("Landsat observed outlines unavailable: %s", exc)
                fixes["landsat_observed_outlines_error"] = str(exc)
            back = LOOKBACK_DAYS["landsat"]
            first = start - pd.Timedelta(days=back)
            scenes = plans.landsat_scenes(cyc, cpr, outlines, first, days + back, sats)
            scenes = scenes[(scenes["time"] >= first) & (scenes["time"] < end)]
            scenes = scenes.assign(
                path=scenes["path"].astype("int16"),
                row=scenes["row"].astype("int16"),
                time=scenes["time"].astype("datetime64[ms, UTC]"),
                track_km=scenes["track_km"].round(1),
            )
            for c in ("platform", "direction", "timing"):
                scenes[c] = scenes[c].astype("category")
            wrs2_grid(outlines).to_parquet(
                output.parent / "wrs2_grid.parquet", index=False, compression="zstd"
            )
            scenes.to_parquet(
                output.parent / "landsat_scenes.parquet",
                index=False,
                compression="zstd",
            )
            fixes["landsat_scenes"] = int(len(scenes))
            fixes["landsat_timing"] = {
                str(k): int(v) for k, v in scenes["timing"].value_counts().items()
            }
            sources["landsat"] = plans.USGS_JSON
        except (RuntimeError, KeyError, ValueError) as exc:
            LOGGER.warning("Landsat plan unavailable: %s", exc)
            fixes["landsat_error"] = str(exc)

    try:
        fixes.update(build_wideswath(output.parent, sensors, sats, start, end, days))
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.warning("wide-swath passes unavailable: %s", exc)
        fixes["wideswath_error"] = str(exc)

    fixes["non_s1_simplified_deg"] = simplify_non_s1(gdf, SITE_SIMPLIFY_DEG)
    gdf.to_parquet(output, index=False, compression="zstd")
    if s2 is not None:
        grid, passes = s2
        grid.to_parquet(
            output.parent / "s2_grid.parquet",
            index=False,
            compression="zstd",
            compression_level=19,
        )
        passes.to_parquet(
            output.parent / "s2_tile_passes.parquet", index=False, compression="zstd"
        )
        fixes["s2_tiles"] = int(len(grid))
        fixes["s2_tile_passes"] = int(len(passes))
        fixes["s2_tile_timing"] = {
            str(k): int(v) for k, v in passes["timing"].value_counts().items()
        }
    nisar_rows = gdf[gdf["sensor"] == "nisar"]
    fixes["nisar_frames"] = int(len(nisar_rows))
    fixes["nisar_timing"] = {
        str(k): int(v) for k, v in nisar_rows["timing"].value_counts().items()
    }
    meta = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "days": days,
        "window": [start.isoformat(), end.isoformat()],
        "lookback_days": LOOKBACK_DAYS,
        "n_passes": int(len(gdf)),
        "sensors": {k: int(v) for k, v in gdf["sensor"].value_counts().items()},
        "platforms": {k: int(v) for k, v in gdf["platform"].value_counts().items()},
        "sources": sources,
        "orbital_elements": sorted(elements),
        "postprocessing": fixes,
        "requested_sensors": sensors,
        "missing_plan_files": missing_plan_files,
    }
    output.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    LOGGER.info("schedule: %s passes -> %s (%s)", len(gdf), output, fixes)
    return output
