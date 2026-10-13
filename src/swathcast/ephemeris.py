"""Sub-satellite points from CelesTrak orbital elements (SGP4).

Mirrors what the map does in the browser with satellite.js: SGP4 in TEME,
rotated to Earth-fixed by Greenwich mean sidereal time, then WGS84 geodetic
latitude. Used to time Sentinel-2 tiles: a tile is imaged when the
ground track passes closest to it.

Checked on 2026-09-29: for 91 Sentinel-2 tiles (7 datatakes, 2026-09-26..27)
the closest-approach time matched the granule ``SENSING_TIME`` with median
0.4 s and max 8.6 s (elements propagated 2-3 days back from their epoch); for
1,683 Sentinel-1 GRD products the footprint centre sat 488-500 km (p5-p95)
right of the ground track, as IW's right-looking geometry predicts.
"""

from __future__ import annotations

import numpy as np

EARTH_R_KM = 6371.0
WGS84_A_KM = 6378.137
WGS84_F = 1 / 298.26  # as satellite.js
UNIX_EPOCH_JD = 2440587.5


def satrec(omm: dict):
    from sgp4 import omm as omm_io
    from sgp4.api import Satrec

    sat = Satrec()
    omm_io.initialize(sat, {k: str(v) for k, v in omm.items()})
    return sat


def subpoints(sat, unix_s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Geodetic lon, lat (degrees) of the sub-satellite point at ``unix_s``."""
    from sgp4.propagation import gstime

    unix_s = np.asarray(unix_s, dtype=float)
    jd = np.floor(unix_s / 86400) + UNIX_EPOCH_JD
    fr = (unix_s % 86400) / 86400
    err, r, _ = sat.sgp4_array(jd, fr)
    if np.any(err):
        raise ValueError(f"SGP4 error codes {sorted(set(err[err != 0]))}")
    x, y, z = r[:, 0], r[:, 1], r[:, 2]
    gmst = np.array([gstime(a + b) for a, b in zip(jd, fr, strict=True)])
    lon = np.degrees((np.arctan2(y, x) - gmst + np.pi) % (2 * np.pi) - np.pi)
    # WGS84 geodetic latitude, iterated as satellite.js does
    e2 = WGS84_F * (2 - WGS84_F)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p)
    for _ in range(20):
        c = 1 / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        lat = np.arctan2(z + WGS84_A_KM * c * e2 * np.sin(lat), p)
    return lon, np.degrees(lat)


def _haversine_km(lon1, lat1, lon2, lat2):
    lon1, lat1, lon2, lat2 = map(np.radians, (lon1, lat1, lon2, lat2))
    a = (
        np.sin((lat2 - lat1) / 2) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def closest_approach(
    sat, lon, lat, t0_unix: float, t1_unix: float, step_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For points (lon, lat): time (unix s) the ground track passes closest in
    [t0, t1], the distance (km), and whether the satellite was ascending."""
    lon = np.atleast_1d(np.asarray(lon, dtype=float))
    lat = np.atleast_1d(np.asarray(lat, dtype=float))
    ts = np.arange(t0_unix, t1_unix + step_s, step_s)
    slon, slat = subpoints(sat, ts)
    d = _haversine_km(lon[:, None], lat[:, None], slon[None, :], slat[None, :])
    i = np.argmin(d, axis=1)
    # parabolic refinement of the minimum between neighbouring steps
    j = np.clip(i, 1, len(ts) - 2)
    d0, d1, d2 = (d[np.arange(len(j)), j + k] ** 2 for k in (-1, 0, 1))
    denom = d0 - 2 * d1 + d2
    shift = np.where(denom > 0, 0.5 * (d0 - d2) / np.where(denom > 0, denom, 1), 0)
    interior = (i > 0) & (i < len(ts) - 1)
    t = ts[i] + np.where(interior, np.clip(shift, -1, 1) * step_s, 0)
    ascending = slat[np.minimum(j + 1, len(ts) - 1)] > slat[j - 1]
    return t, d[np.arange(len(i)), i], ascending


def nearest_pass(
    sat,
    lon,
    lat,
    t0_unix: float,
    t1_unix: float,
    *,
    ascending=None,
    offset_km: float = 0.0,
    side: str | None = None,
    coarse_s: float = 10.0,
):
    """For each point, the pass in [t0, t1] whose ground track passes it at the
    distance nearest ``offset_km`` (0: straight over it; NISAR frames sit
    ~567 km left of the track), optionally only in one direction and with the
    point on one ``side`` ("left"/"right" of the direction of travel). Refined
    to ~0.1 s. Returns (time unix s, distance km); NaN where no pass qualifies.

    One propagation over the window serves every point, so call it once per
    satellite and day rather than per point.
    """
    lon = np.atleast_1d(np.asarray(lon, dtype=float))
    lat = np.atleast_1d(np.asarray(lat, dtype=float))
    ts = np.arange(t0_unix, t1_unix + coarse_s, coarse_s)
    slon, slat = subpoints(sat, ts)
    rising = np.gradient(slat) > 0
    coslat = np.cos(np.radians(slat))
    vx = np.gradient(np.unwrap(np.radians(slon))) * coslat
    vy = np.gradient(np.radians(slat))
    times = np.full(len(lon), np.nan)
    dists = np.full(len(lon), np.nan)
    for start in range(0, len(lon), 256):
        sl = slice(start, start + 256)
        d = _haversine_km(lon[sl, None], lat[sl, None], slon[None, :], slat[None, :])
        # one candidate per pass: local minima of the distance
        cand = np.zeros_like(d, dtype=bool)
        cand[:, 1:-1] = (d[:, 1:-1] <= d[:, :-2]) & (d[:, 1:-1] < d[:, 2:])
        if ascending is not None:
            cand &= rising[None, :] == ascending
        if side is not None:
            fx = (
                np.radians((lon[sl, None] - slon[None, :] + 540) % 360 - 180)
                * coslat[None, :]
            )
            fy = np.radians(lat[sl, None] - slat[None, :])
            left = (vx[None, :] * fy - vy[None, :] * fx) > 0
            cand &= left if side == "left" else ~left
        score = np.where(cand, np.abs(d - offset_km), np.inf)
        i = np.argmin(score, axis=1)
        ok = np.isfinite(score[np.arange(len(i)), i])
        times[sl] = np.where(ok, ts[i], np.nan)
    for k in np.flatnonzero(np.isfinite(times)):
        t, dk, _ = closest_approach(
            sat, lon[k], lat[k], times[k] - 2 * coarse_s, times[k] + 2 * coarse_s, 0.5
        )
        times[k], dists[k] = t[0], dk[0]
    return times, dists
