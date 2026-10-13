"""Observed outlines: footprints from real products, for the plans' cells.

The plans' own footprints are off: NISAR's planned frames by 7-11 km from
the RSLC frames ASF serves (IoU ~0.9, 2026-10), and the WRS-2 cells by ~2 km
from USGS's Level-1 scenes, which are also ~14 % larger (IoU 0.86). Frames
and scenes repeat (NISAR every 12 days, Landsat every 16), so the latest
product of the same frame or path/row is the better outline for the next one:

- NISAR: the RSLC footprints in NASA CMR over the last ``NISAR_DAYS``, by
  (track, frame, direction);
- Landsat: the Level-1 footprints in USGS's STAC over the last
  ``LANDSAT_DAYS``, by (path, row).

Each falls back to the planned footprint where no product was seen (or the
catalogue failed); the schedule's meta records how many were replaced.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

LOGGER = logging.getLogger(__name__)

CMR_JSON = "https://cmr.earthdata.nasa.gov/search/granules.json"
NISAR_RSLC = "C2853145197-ASF"
USGS_STAC = "https://landsatlook.usgs.gov/stac-server/search"
NISAR_DAYS = 13
LANDSAT_DAYS = 17
#: NISAR_L1_PR_RSLC_031_170_D_066_4005_DHDH_A_...
RSLC_KEY = re.compile(r"^NISAR_L1_PR_RSLC_\d{3}_(\d{3})_([AD])_(\d{3})_")
MAX_PAGES = 60


def _call(fn, *args, attempts: int = 4, **kw):
    """One catalogue request, retried (USGS's STAC answers 502 now and then)."""
    for i in range(attempts):
        try:
            r = fn(*args, **kw)
            r.raise_for_status()
            return r
        except OSError:  # requests errors are OSErrors
            if i == attempts - 1:
                raise
            time.sleep(2 ** (i + 1))


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _usable(poly) -> bool:
    """A footprint the map can draw as it is: valid, and not across 180 deg
    (catalogue rings there jump by ~360 deg; the plan's own outline is kept)."""
    if poly is None or poly.is_empty or not poly.is_valid:
        return False
    w, _, e, _ = poly.bounds
    return e - w < 180


def cmr_polygon(text: str):
    """CMR's "lat lon lat lon ..." ring -> a shapely Polygon (lon, lat)."""
    from shapely.geometry import Polygon

    v = [float(x) for x in text.split()]
    if len(v) < 8 or len(v) % 2:
        return None
    return Polygon(list(zip(v[1::2], v[0::2], strict=True)))


def nisar_observed(session, now: datetime, days: int = NISAR_DAYS) -> dict:
    """{(track, frame, "ascending"|"descending"): Polygon}, the latest RSLC of each."""
    params = {
        "collection_concept_id": NISAR_RSLC,
        "temporal": f"{_iso(now - timedelta(days=days))},{_iso(now)}",
        "page_size": 2000,
        "sort_key": "start_date",
    }
    headers, out = {}, {}
    for _ in range(MAX_PAGES):
        r = _call(session.get, CMR_JSON, params=params, headers=headers, timeout=180)
        entries = r.json().get("feed", {}).get("entry", [])
        for e in entries:  # oldest first: later ones overwrite
            m = RSLC_KEY.match(e.get("title", ""))
            rings = e.get("polygons") or []
            if not m or not rings or not rings[0]:
                continue
            poly = cmr_polygon(rings[0][0])
            if _usable(poly):
                d = "ascending" if m[2] == "A" else "descending"
                out[(int(m[1]), int(m[3]), d)] = poly
        after = r.headers.get("CMR-Search-After")
        if not entries or not after or len(entries) < params["page_size"]:
            break
        headers = {"CMR-Search-After": after}
    LOGGER.info("NISAR: %d observed frame outlines", len(out))
    return out


def landsat_observed(session, now: datetime, days: int = LANDSAT_DAYS) -> dict:
    """{(path, row): Polygon}, the latest Level-1 scene of each."""
    from shapely.geometry import shape

    body = {
        "collections": ["landsat-c2l1"],
        "datetime": f"{_iso(now - timedelta(days=days))}/{_iso(now)}",
        "limit": 500,
        "sortby": [{"field": "properties.datetime", "direction": "asc"}],
        "fields": {
            "include": [
                "id",
                "geometry",
                "properties.datetime",
                "properties.landsat:wrs_path",
                "properties.landsat:wrs_row",
            ],
            "exclude": ["assets", "links"],
        },
    }
    url, out = USGS_STAC, {}
    for _ in range(MAX_PAGES):
        r = _call(session.post, url, json=body, timeout=180)
        payload = r.json()
        for f in payload.get("features", []):
            p = f.get("properties", {})
            try:
                key = (int(p["landsat:wrs_path"]), int(p["landsat:wrs_row"]))
                poly = shape(f["geometry"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if poly.geom_type == "Polygon" and _usable(poly):
                out[key] = poly
        nxt = next(
            (
                link
                for link in payload.get("links", [])
                if link.get("rel") == "next" and link.get("method", "GET") == "POST"
            ),
            None,
        )
        if nxt is None or not payload.get("features"):
            break
        url, body = nxt["href"], nxt.get("body", body)
    LOGGER.info("Landsat: %d observed scene outlines", len(out))
    return out


def apply_nisar(gdf, observed: dict) -> int:
    """NISAR rows' planned frames -> the observed ones (in place); the count."""
    if not observed:
        return 0
    rows = gdf.index[gdf["sensor"] == "nisar"]
    n = 0
    for i in rows:
        key = (
            int(gdf.at[i, "track"]),
            int(gdf.at[i, "frame"]),
            str(gdf.at[i, "orbit_direction"]),
        )
        poly = observed.get(key)
        if poly is not None:
            gdf.at[i, "geometry"] = poly
            n += 1
    return n


def apply_landsat(outlines, observed: dict) -> int:
    """WRS-2 cells -> the observed scenes' footprints (in place); the count."""
    if not observed:
        return 0
    n = 0
    for i, path, row in zip(
        outlines.index, outlines["path"], outlines["row"], strict=True
    ):
        poly = observed.get((int(path), int(row)))
        if poly is not None:
            outlines.at[i, "geometry"] = poly
            n += 1
    return n
