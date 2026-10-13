"""Sentinel-2 tiles (MGRS) and when each is next imaged.

The tile grid is ESA's official Sentinel-2 tiling grid KML (56,686 tiles).
The acquisition plan (ESA KMLs, :mod:`swathcast.plans`) gives datatakes, not tiles, so a tile
is taken to be acquired by every nominal (``NOBS``) datatake whose footprint
intersects it. Checked on 2026-09-26..27 against Planetary Computer L2A items
of three long datatakes: every produced tile intersects its datatake, and 98 %
of intersecting tiles were produced (the rest overlap it by under 2 %).

A datatake's begin/end apply to its whole swath, which can run pole to pole
(32 min), and every tile of it carries that begin time in STAC ``datetime``.
So the plan says *whether* a tile is imaged and the orbit says *when*:

* ``timing="orbit"`` (normal): the time the satellite's ground track passes
  closest to the tile centre, from CelesTrak elements propagated with SGP4
  (:mod:`swathcast.ephemeris`), clamped to the datatake. Against the granule
  ``SENSING_TIME`` of 91 tiles in 7 datatakes (2026-09-26..27): median 0.4 s,
  max 8.6 s.
* ``timing="latitude"`` (fallback when a platform's elements are
  unavailable): the nadir latitude fixes the argument of latitude ``u``
  (``sin(lat) = sin(i) sin(u)``), ``u`` advances uniformly in time, anchored
  on the datatake's end at its southern (descending) or northern (ascending)
  edge. Same 91 tiles: max 35 s (95 % within 29 s).

Using the datatake begin instead is up to 32 min off. See docs/methodology.md.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

TILING_KML_URL = (
    "https://sentinels.copernicus.eu/documents/247904/1955685/"
    "S2A_OPER_GIP_TILPAR_MPC__20151209T095117_V20150622T000000_"
    "21000101T000000_B00.kml"
)
N_TILES = 56686
INCLINATION_DEG = 98.62
ORBIT_PERIOD_S = 10 * 86400 / 143  # 143 orbits in the 10-day repeat
#: Plan modes that produce user tiles (the rest are calibration/dark takes).
TILE_MODES = ("NOBS",)
#: Corners are stored in 1e-4 degree integers (about 10 m).
SCALE = 1e4

_PLACEMARK = re.compile(r"<Placemark>\s*<name>([^<]+)</name>(.*?)</Placemark>", re.S)
_LL_WKT = re.compile(r"LL_WKT</b>.*?MULTIPOLYGON\(\(\(([^)]*)\)\)\)", re.S)
_RING = re.compile(r"<LinearRing>\s*<coordinates>\s*(.*?)\s*</coordinates>", re.S)


def download_kml(cache_dir: Path, session=None) -> Path:
    """The tiling grid KML (~109 MB), downloaded once into ``cache_dir``."""
    import requests

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / Path(TILING_KML_URL).name
    if path.exists() and path.stat().st_size > 100_000_000:
        return path
    session = session or requests.Session()
    tmp = path.with_suffix(".part")
    with session.get(TILING_KML_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    tmp.replace(path)
    return path


def parse_kml(text: str):
    """Tiles as a GeoDataFrame: ``tile``, ``corners`` (4x2, lon unwrapped past
    180 for antimeridian tiles) and ``geometry`` (split at +/-180, from the KML)."""
    import geopandas as gpd
    from shapely.geometry import MultiPolygon, Polygon

    names, corners, geoms = [], [], []
    for m in _PLACEMARK.finditer(text):
        name, body = m.group(1).strip(), m.group(2)
        wkt = _LL_WKT.search(body)
        if not wkt:
            raise ValueError(f"tile {name}: no LL_WKT")
        c = np.array([[float(v) for v in p.split()] for p in wkt.group(1).split(",")])
        if len(c) != 5 or not np.allclose(c[0], c[-1]):
            raise ValueError(f"tile {name}: expected a closed 4-corner ring")
        c = c[:4]
        if c[:, 0].max() - c[:, 0].min() > 180:
            c[:, 0] = np.where(c[:, 0] < 0, c[:, 0] + 360, c[:, 0])
        rings = [
            Polygon([tuple(map(float, p.split(",")[:2])) for p in ring.split()])
            for ring in _RING.findall(body)
        ]
        names.append(name)
        corners.append(c)
        geoms.append(rings[0] if len(rings) == 1 else MultiPolygon(rings))
    return gpd.GeoDataFrame(
        {"tile": names, "corners": corners}, geometry=geoms, crs="EPSG:4326"
    )


def load_tiles(cache_dir: Path, session=None):
    tiles = parse_kml(download_kml(cache_dir, session).read_text())
    if len(tiles) != N_TILES or not tiles["tile"].is_unique:
        raise RuntimeError(
            f"S2 tiling grid: expected {N_TILES} unique tiles, got {len(tiles)}"
        )
    return tiles


def site_grid(tiles) -> pd.DataFrame:
    """Compact browser grid: corner 0 in 1e-4 deg, corners 1-3 as offsets."""
    c = np.round(np.stack(tiles["corners"].to_numpy()) * SCALE).astype(np.int64)
    out = {"tile": tiles["tile"].to_numpy()}
    out["x0"], out["y0"] = c[:, 0, 0].astype("int32"), c[:, 0, 1].astype("int32")
    for k in (1, 2, 3):
        out[f"dx{k}"] = (c[:, k, 0] - c[:, 0, 0]).astype("int32")
        out[f"dy{k}"] = (c[:, k, 1] - c[:, 0, 1]).astype("int32")
    return pd.DataFrame(out)


def _u(lat_deg, ascending: bool):
    """Argument of latitude (rad) of the nadir point at ``lat_deg``."""
    s = np.clip(
        np.sin(np.radians(lat_deg)) / np.sin(np.radians(INCLINATION_DEG)), -1, 1
    )
    a = np.arcsin(s)
    return a if ascending else np.pi - a


def tile_times(lat, begin, end, end_lat: float, ascending: bool):
    """Estimated imaging time of tiles centred at ``lat`` within one datatake."""
    lat = np.asarray(lat, dtype=float)
    dt = (_u(end_lat, ascending) - _u(lat, ascending)) / (2 * np.pi) * ORBIT_PERIOD_S
    when = end - pd.to_timedelta(dt, unit="s")
    return np.clip(when, begin, end)


def tile_centres(tiles) -> pd.DataFrame:
    """Mean of each tile's 4 corners (lon wrapped to [-180, 180))."""
    c = np.stack(tiles["corners"].to_numpy())
    lon = (c[:, :, 0].mean(axis=1) + 180) % 360 - 180
    return pd.DataFrame(
        {"lon": lon, "lat": c[:, :, 1].mean(axis=1)}, index=tiles["tile"].to_numpy()
    )


#: Real tiles were sensed up to 16 s before their datatake's planned start
#: (91-tile check), so the plan window is widened by this much for clamping.
PLAN_SLACK_S = 30.0


def orbit_times(sat, lon, lat, begin, end, pad_s: float = 90.0):
    """Closest-approach times (clamped to the datatake +/- ``PLAN_SLACK_S``)
    and the pass direction."""
    from swathcast.ephemeris import closest_approach

    b, e = begin.timestamp(), end.timestamp()
    t, _, ascending = closest_approach(sat, lon, lat, b - pad_s, e + pad_s)
    when = pd.to_datetime(
        np.clip(t, b - PLAN_SLACK_S, e + PLAN_SLACK_S), unit="s", utc=True
    )
    return when, "ascending" if ascending.mean() > 0.5 else "descending"


def tile_passes(tiles, schedule, elements: dict | None = None) -> pd.DataFrame:
    """One row per (tile, planned S2 datatake): platform, track, time, begin.

    ``elements``: {platform: OMM dict} (CelesTrak); platforms without elements
    fall back to the latitude method. The ``timing`` column says which.
    """
    import geopandas as gpd

    from swathcast.schedule import _centroid_lonlat, solar_direction

    s2 = schedule[
        (schedule["sensor"] == "sentinel-2") & schedule["mode"].isin(TILE_MODES)
    ]
    s2 = s2[s2.geometry.notna() & ~s2.geometry.is_empty]
    cols = [
        "tile",
        "platform",
        "track",
        "direction",
        "time",
        "begin",
        "timing",
        "coverage",
    ]
    if s2.empty:
        return pd.DataFrame(columns=cols)
    centres = tile_centres(tiles)
    sats = {}
    for platform, omm in (elements or {}).items():
        from swathcast.ephemeris import satrec

        try:
            sats[platform] = satrec(omm)
        except (ValueError, KeyError) as exc:
            LOGGER.warning("S2 timing: no orbit for %s (%s)", platform, exc)
    pairs = gpd.sjoin(
        tiles[["tile", "geometry"]],
        gpd.GeoDataFrame(s2[["geometry"]].reset_index(names="dt"), crs=tiles.crs),
        predicate="intersects",
    )
    # How much of each tile the datatake covers: a tile gets a product when any
    # of it is imaged (a sliver of a tile is a real, mostly-empty product).
    import shapely

    tile_geom = tiles.geometry.loc[pairs.index].to_numpy()
    dt_geom = s2.geometry.loc[pairs["dt"]].to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        cover = shapely.area(shapely.intersection(tile_geom, dt_geom)) / shapely.area(
            tile_geom
        )
    pairs = pairs.assign(coverage=np.clip(np.nan_to_num(cover, nan=1.0), 0, 1).round(2))
    parts = []
    for dt, grp in pairs.groupby("dt"):
        row = s2.loc[dt]
        begin, end = row["begin_time"], row["end_time"]
        where = centres.loc[grp["tile"]]
        sat = sats.get(row["platform"])
        if sat is not None:
            times, direction = orbit_times(
                sat, where["lon"].to_numpy(), where["lat"].to_numpy(), begin, end
            )
            timing = "orbit"
        else:
            lon, clat = _centroid_lonlat(row.geometry)
            # S2 is a 10:30 descending-node orbit: day side descends.
            direction = (
                solar_direction((begin + (end - begin) / 2).to_pydatetime(), lon, clat)
                or "descending"
            )
            ascending = direction == "ascending"
            miny, maxy = row.geometry.bounds[1], row.geometry.bounds[3]
            times = tile_times(
                where["lat"].to_numpy(),
                begin,
                end,
                maxy if ascending else miny,
                ascending,
            )
            timing = "latitude"
        parts.append(
            pd.DataFrame(
                {
                    "tile": grp["tile"].to_numpy(),
                    "platform": row["platform"],
                    "track": int(row["track"]),
                    "direction": direction,
                    "time": times,
                    "begin": begin,
                    "timing": timing,
                    "coverage": grp["coverage"].to_numpy(),
                }
            )
        )
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["tile", "time"]).drop_duplicates(
        ["tile", "platform", "begin"]
    )
    out["track"] = out["track"].astype("int16")
    out["coverage"] = out["coverage"].astype("float32")
    # millisecond timestamps: the browser's parquet reader turns them into Dates
    for c in ("time", "begin"):
        out[c] = pd.to_datetime(out[c], utc=True).astype("datetime64[ms, UTC]")
    for c in ("platform", "direction", "timing"):
        out[c] = out[c].astype("category")
    return out.reset_index(drop=True)[cols]
