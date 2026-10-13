"""Wide-swath sensors imaging continuously: MODIS, VIIRS, Sentinel-3.

None of these has an acquisition plan to read: MODIS (Terra, Aqua), VIIRS
(Suomi NPP, NOAA-20, NOAA-21) and Sentinel-3's SLSTR image on every orbit,
day and night (OLCI in daylight). So their passes are predicted from the
orbit alone: CelesTrak elements propagated with SGP4
(:mod:`swathcast.ephemeris`), as for the other missions' times.

* **MODIS and VIIRS: sinusoidal tiles.** Their gridded land products
  (MOD09GA, MOD10A1, VNP10A1, VJ110A1, ...) come on the MODIS sinusoidal grid
  of 10-degree tiles (h00-35, v00-17), 460 of which hold land or sea.
  A tile is imaged by a pass whose swath covers the tile centre: the time is
  when the ground track passes closest to it, and the signed cross-track
  distance there (``xtrack_km``, + to the right of the direction of travel)
  gives the view angle.
* **Sentinel-3: frames.** Sentinel-3 products are cut in 3-minute frames on
  a fixed grid: relative orbit (1-385, a 27-day exact repeat) and the frame's
  start in seconds after the ascending node (0, 180, ... 5940), as in
  ``S3A_OL_1_EFR____..._0179_145_081_3420_...``. Checked on 1,450 CDSE
  products of 2026-10-12: the frame number matched the start minus the SGP4
  ascending-node time to a median of 0.04 s. A frame's outline is the SLSTR
  nadir swath (the widest of the instruments) over its 180 s.

Swath edges were measured on real products (2026-10-13), as the signed
cross-track distance of footprint vertices from the SGP4 ground track at the
same along-track position (medians; 150 granules per sensor, 400 S3 frames):
MODIS +/-1,171-1,178 km, VIIRS +/-1,518-1,548 km; Sentinel-3 OLCI -336 to
+975 km and SLSTR nadir -500 to +996 km (both looking away from the sun, to
the right of travel).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------- platforms
#: mission -> its platforms (schedule platform codes, as in tle.NORAD)
PLATFORMS = {
    "modis": ("Terra", "Aqua"),
    "viirs": ("SNPP", "N20", "N21"),
    "sentinel-3": ("S3A", "S3B"),
}
#: Signed cross-track extent of the imaged strip, km (+ = right of travel),
#: measured on real footprints (see the module docstring).
SWATH_KM = {
    "Terra": (-1178.0, 1178.0),
    "Aqua": (-1171.0, 1171.0),
    "SNPP": (-1548.0, 1548.0),
    "N20": (-1526.0, 1526.0),
    "N21": (-1518.0, 1518.0),
    "S3A": (-500.0, 996.0),  # SLSTR nadir view; OLCI is S3_OLCI_KM
    "S3B": (-500.0, 996.0),
}
S3_OLCI_KM = (-336.0, 975.0)
#: Orbital altitudes (km) for the view zenith angle at a cross-track distance
ALTITUDE_KM = {"Terra": 705, "Aqua": 705, "SNPP": 834, "N20": 834, "N21": 834}
EARTH_R_KM = 6371.0

# ---------------------------------------------------------------- sinusoidal grid
SIN_R = 6371007.181
SIN_TILE_M = 1111950.5197665233
SIN_X0 = -20015109.355798
SIN_Y0 = 10007554.677899
N_SIN_TILES = 460


def sin_to_lonlat(x, y):
    lat = np.asarray(y, dtype=float) / SIN_R
    with np.errstate(divide="ignore", invalid="ignore"):
        lon = np.asarray(x, dtype=float) / (SIN_R * np.cos(lat))
    return np.degrees(lon), np.degrees(lat)


def _to_lonlat_clipped(x, y):
    lon, lat = sin_to_lonlat(x, y)
    return np.clip(lon, -180, 180), lat


def lonlat_to_sin(lon, lat):
    lat = np.radians(np.asarray(lat, dtype=float))
    return SIN_R * np.radians(np.asarray(lon, dtype=float)) * np.cos(lat), SIN_R * lat


def tile_name(h: int, v: int) -> str:
    return f"h{int(h):02d}v{int(v):02d}"


def sin_grid():
    """The MODIS sinusoidal tiles that hold any of the Earth (460), as a
    GeoDataFrame: ``tile`` ("h08v05"), ``h``, ``v``, centre ``lon``/``lat``
    and the lon/lat outline (the tile's part inside the projection's
    world outline, with curved east/west edges)."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import Polygon, box

    lat = np.linspace(-np.pi / 2, np.pi / 2, 721)
    edge = np.pi * SIN_R * np.cos(lat)
    world = Polygon(
        list(zip(edge, SIN_R * lat, strict=True))
        + list(zip(-edge[::-1], SIN_R * lat[::-1], strict=True))
    )
    rows = []
    for v in range(18):
        for h in range(36):
            x0, y1 = SIN_X0 + h * SIN_TILE_M, SIN_Y0 - v * SIN_TILE_M
            cell = box(x0, y1 - SIN_TILE_M, x0 + SIN_TILE_M, y1)
            part = cell.intersection(world)
            if part.is_empty or part.area < 1e6:
                continue
            part = shapely.segmentize(part, 25_000)
            cx, cy = part.centroid.x, part.centroid.y
            clon, clat = sin_to_lonlat(cx, cy)
            # (near the poles, rounding puts the world edge a hair past 180)
            geom = shapely.transform(part, _to_lonlat_clipped, interleaved=False)
            rows.append(
                {
                    "tile": tile_name(h, v),
                    "h": h,
                    "v": v,
                    "lon": float(clon),
                    "lat": float(clat),
                    "geometry": geom.buffer(0) if not geom.is_valid else geom,
                }
            )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def site_sin_grid(grid):
    """The grid for the browser: outlines simplified to ~1 km."""
    out = grid[["tile", "h", "v", "geometry"]].copy()
    out["geometry"] = out.geometry.simplify(0.01, preserve_topology=True)
    out["h"] = out["h"].astype("int8")
    out["v"] = out["v"].astype("int8")
    return out.reset_index(drop=True)


# ---------------------------------------------------------------- orbit helpers
def _track(sat, t0: float, t1: float, step_s: float):
    from swathcast.ephemeris import subpoints

    ts = np.arange(t0, t1 + step_s, step_s)
    lon, lat = subpoints(sat, ts)
    return ts, lon, lat


def view_zenith_deg(xtrack_km, altitude_km: float):
    """View zenith angle at the ground for a point ``xtrack_km`` from the
    ground track, seen from ``altitude_km`` (spherical Earth)."""
    g = np.abs(np.asarray(xtrack_km, dtype=float)) / EARTH_R_KM
    r = EARTH_R_KM + altitude_km
    # look angle from nadir, then the zenith angle at the ground point
    los = np.arctan2(np.sin(g) * EARTH_R_KM, r - EARTH_R_KM * np.cos(g))
    return np.degrees(los + g)


def tile_passes(
    sat, platform: str, lon, lat, t0: float, t1: float, step_s: float = 10.0
) -> pd.DataFrame:
    """Every pass of one satellite in [t0, t1] (unix s) whose swath covers
    each point (lon, lat): index of the point, time of closest approach
    (refined to ~0.1 s), signed cross-track distance (km, + right of travel)
    and direction."""
    from swathcast.ephemeris import _haversine_km

    lo, hi = SWATH_KM[platform]
    reach = max(-lo, hi) + 5
    ts, slon, slat = _track(sat, t0 - step_s, t1 + step_s, step_s)
    coslat = np.cos(np.radians(slat))
    vx = np.gradient(np.unwrap(np.radians(slon))) * coslat
    vy = np.gradient(np.radians(slat))
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    out = []
    for start in range(0, len(lon), 16):
        sl = slice(start, start + 16)
        d = _haversine_km(
            lon[sl, None], lat[sl, None], slon[None, :], slat[None, :]
        ).astype(np.float32)
        cand = np.zeros_like(d, dtype=bool)
        cand[:, 1:-1] = (d[:, 1:-1] <= d[:, :-2]) & (d[:, 1:-1] < d[:, 2:])
        cand &= d < reach
        pi, ti = np.nonzero(cand)
        if not len(pi):
            continue
        d0, d1, d2 = (d[pi, ti + k].astype(float) ** 2 for k in (-1, 0, 1))
        den = d0 - 2 * d1 + d2
        shift = np.where(den > 0, 0.5 * (d0 - d2) / np.where(den > 0, den, 1), 0.0)
        shift = np.clip(shift, -1, 1)
        when = ts[ti] + shift * step_s
        dist = np.sqrt(np.maximum(0, d1 - 0.25 * (d0 - d2) * shift))
        fx = np.radians((lon[sl][pi] - slon[ti] + 540) % 360 - 180) * coslat[ti]
        fy = np.radians(lat[sl][pi] - slat[ti])
        left = (vx[ti] * fy - vy[ti] * fx) > 0
        signed = np.where(left, -dist, dist)
        keep = (signed >= lo) & (signed <= hi) & (when >= t0) & (when <= t1)
        out.append(
            pd.DataFrame(
                {
                    "i": pi[keep] + start,
                    "time": when[keep],
                    "xtrack_km": signed[keep],
                    "ascending": vy[ti][keep] > 0,
                }
            )
        )
    if not out:
        return pd.DataFrame(columns=["i", "time", "xtrack_km", "ascending"])
    return pd.concat(out, ignore_index=True)


def sin_tile_passes(grid, sats: dict, mission: str, start, end) -> pd.DataFrame:
    """One row per (tile, pass) of the mission's platforms over [start, end):
    tile, platform, time, direction, xtrack_km."""
    parts = []
    for platform in PLATFORMS[mission]:
        sat = sats.get(platform)
        if sat is None:
            LOGGER.warning("%s: no orbital elements for %s", mission, platform)
            continue
        p = tile_passes(
            sat,
            platform,
            grid["lon"].to_numpy(),
            grid["lat"].to_numpy(),
            start.timestamp(),
            end.timestamp(),
        )
        parts.append(
            pd.DataFrame(
                {
                    "tile": grid["tile"].to_numpy()[p["i"].to_numpy(dtype=int)],
                    "platform": platform,
                    "time": pd.to_datetime(
                        p["time"].to_numpy(dtype=float), unit="s", utc=True
                    ),
                    "direction": np.where(
                        p["ascending"].to_numpy(dtype=bool), "ascending", "descending"
                    ),
                    "xtrack_km": np.round(p["xtrack_km"].to_numpy(dtype=float)).astype(
                        "int16"
                    ),
                }
            )
        )
        LOGGER.info("%s %s: %d tile passes", mission, platform, len(p))
    cols = ["tile", "platform", "time", "direction", "xtrack_km"]
    if not parts:
        return pd.DataFrame(columns=cols)
    out = pd.concat(parts, ignore_index=True).sort_values(["tile", "time"])
    out["time"] = out["time"].astype("datetime64[ms, UTC]")
    for c in ("tile", "platform", "direction"):
        out[c] = out[c].astype("category")
    return out.reset_index(drop=True)[cols]


# ---------------------------------------------------------------- Sentinel-3 frames
S3_TRACKS = 385
#: ground-track shift between consecutive orbits: 27 days / 385 orbits
S3_TRACK_STEP_DEG = 360.0 * 27 / S3_TRACKS
#: Longitude of the ascending node of relative orbit 1, fitted on 15
#: relative orbits per platform of CDSE products sensed 2026-10-12 (std
#: 0.0001 deg). Each platform numbers its own orbits; S3B's tracks lie
#: between S3A's.
S3_ORBIT1_ANX_LON = {"S3A": 0.6219, "S3B": 0.1564}
S3_FRAME_S = 180


def anx_crossings(sat, t0: float, t1: float):
    """Ascending-node times (unix s) and longitudes in [t0, t1]."""
    ts, lon, lat = _track(sat, t0, t1, 5.0)
    i = np.flatnonzero((lat[:-1] < 0) & (lat[1:] >= 0))
    f = -lat[i] / (lat[i + 1] - lat[i])
    dlon = (lon[i + 1] - lon[i] + 540) % 360 - 180
    return ts[i] + f * 5.0, (lon[i] + f * dlon + 180) % 360 - 180


def s3_relative_orbit(platform: str, anx_lon):
    """Relative orbit (1-385) of an orbit from its ascending-node longitude:
    track r crosses at L1 - (r - 1) * 25.2468 deg."""
    ref = (S3_ORBIT1_ANX_LON[platform] - np.arange(S3_TRACKS) * S3_TRACK_STEP_DEG) % 360
    diff = (np.asarray(anx_lon, dtype=float)[:, None] - ref[None, :] + 540) % 360 - 180
    return np.argmin(np.abs(diff), axis=1) + 1, np.min(np.abs(diff), axis=1)


def s3_frame_passes(sats: dict, start, end) -> pd.DataFrame:
    """One row per Sentinel-3 frame imaged in [start, end): platform, track
    (relative orbit), frame (s after the node), time (frame start), end,
    direction (at mid-frame)."""
    from swathcast.ephemeris import subpoints

    rows = []
    t0, t1 = start.timestamp(), end.timestamp()
    for platform in PLATFORMS["sentinel-3"]:
        sat = sats.get(platform)
        if sat is None:
            LOGGER.warning("sentinel-3: no orbital elements for %s", platform)
            continue
        anx, lons = anx_crossings(sat, t0 - 7000, t1)
        rel, off = s3_relative_orbit(platform, lons)
        if np.max(off) > 0.2:
            LOGGER.warning("%s: ANX up to %.2f deg off its track", platform, off.max())
        for k in range(len(anx) - 1):
            period = anx[k + 1] - anx[k]
            frames = np.arange(0, period, S3_FRAME_S)
            begin = anx[k] + frames
            stop = np.minimum(begin + S3_FRAME_S, anx[k + 1])
            mid = (begin + stop) / 2
            _, la = subpoints(sat, mid - 5)
            _, lb = subpoints(sat, mid + 5)
            keep = (stop > t0) & (begin < t1)
            for f, b, s, asc in zip(
                frames[keep], begin[keep], stop[keep], (lb > la)[keep], strict=True
            ):
                rows.append((platform, int(rel[k]), int(f), b, s, asc))
    df = pd.DataFrame(rows, columns=["platform", "track", "frame", "t", "t_end", "asc"])
    out = pd.DataFrame(
        {
            "platform": df["platform"].astype("category"),
            "track": df["track"].astype("int16"),
            "frame": df["frame"].astype("int16"),
            "time": pd.to_datetime(df["t"], unit="s", utc=True).astype(
                "datetime64[ms, UTC]"
            ),
            "end": pd.to_datetime(df["t_end"], unit="s", utc=True).astype(
                "datetime64[ms, UTC]"
            ),
            "direction": pd.Categorical(np.where(df["asc"], "ascending", "descending")),
        }
    )
    return out.sort_values(["track", "frame", "time"]).reset_index(drop=True)


def swath_polygon(sat, t0: float, t1: float, swath_km, step_s: float = 15.0):
    """The strip imaged between t0 and t1 (unix s), as a lon/lat ring with
    continuous longitudes: the right edge forward, the left edge back."""
    from swathcast.ephemeris import subpoints

    n = max(2, int(np.ceil((t1 - t0) / step_s)) + 1)
    ts = np.linspace(t0, t1, n)
    lon, lat = subpoints(sat, ts)
    lon2, lat2 = subpoints(sat, ts + 0.5)
    heading = _bearing(lon, lat, lon2, lat2)
    lo, hi = swath_km
    right = [
        _dest(x, y, h + 90 if hi >= 0 else h - 90, abs(hi))
        for x, y, h in zip(lon, lat, heading, strict=True)
    ]
    left = [
        _dest(x, y, h - 90 if lo <= 0 else h + 90, abs(lo))
        for x, y, h in zip(lon, lat, heading, strict=True)
    ]
    ring = right + left[::-1]
    out, prev = [], None
    for x, y in ring:
        if prev is not None:
            x += 360 * round((prev - x) / 360)
        out.append((x, y))
        prev = x
    out.append(out[0])
    return out


def _bearing(lon1, lat1, lon2, lat2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(lon2 - lon1)
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return np.degrees(np.arctan2(y, x))


def _dest(lon, lat, brg, dist_km):
    d = dist_km / EARTH_R_KM
    b, p1, l1 = np.radians(brg), np.radians(lat), np.radians(lon)
    p2 = np.arcsin(np.sin(p1) * np.cos(d) + np.cos(p1) * np.sin(d) * np.cos(b))
    l2 = l1 + np.arctan2(
        np.sin(b) * np.sin(d) * np.cos(p1), np.cos(d) - np.sin(p1) * np.sin(p2)
    )
    return float(np.degrees(l2)), float(np.degrees(p2))


def s3_frame_grid(sats: dict, passes: pd.DataFrame):
    """The outline of every (track, frame) in ``passes``, from its first
    occurrence: the SLSTR nadir swath over the frame's time, densified along
    great circles and split at +/-180 (as the plans' footprints are)."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import Polygon

    from swathcast.schedule import geodesic_ring, wrap_antimeridian

    first = passes.drop_duplicates(["track", "frame"])
    rows = []
    for r in first.itertuples(index=False):
        sat = sats[str(r.platform)]
        ring = swath_polygon(
            sat, r.time.timestamp(), r.end.timestamp(), SWATH_KM[str(r.platform)]
        )
        try:
            geom = wrap_antimeridian(Polygon(geodesic_ring(ring)))
        except Exception as exc:  # noqa: BLE001 - shapely raises several kinds
            LOGGER.warning("S3 frame %s/%s: %s", r.track, r.frame, exc)
            continue
        rows.append(
            {
                "track": int(r.track),
                "frame": int(r.frame),
                "direction": str(r.direction),
                "geometry": geom,
            }
        )
    grid = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    # ~2 km and 0.001 deg: 1.4 MB for the 13,090 frames instead of 4.2 MB
    grid["geometry"] = shapely.set_precision(
        grid.geometry.simplify(0.02, preserve_topology=True).values, 0.001
    )
    grid["track"] = grid["track"].astype("int16")
    grid["frame"] = grid["frame"].astype("int16")
    return grid
