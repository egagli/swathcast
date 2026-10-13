"""Exact extents of browse images that are public but whose grid is not.

Two browse images the map drapes have public pixels but their placement
behind an Earthdata login (checked 2026-10-06):

- NISAR RSLC ``_LATLON.png``: its extent is the ``LatLonQuad`` of the
  granule's ``_LATLON.kml``. The CMR footprint's box overshoots the image's
  data by 0-5 km on varying sides, and the extent moves up to ~5 km between
  repeat passes of a frame, so it is kept per granule.
- OPERA RTC-S1 ``_BROWSE*.png``: it spans the burst's UTM grid exactly,
  which is in the product's GeoTIFF header (and ``.iso.xml``). The grid is
  fixed per burst (the same 2016-2026), so it is kept per burst ID, read
  once from the first 16 KB of the burst's ``_mask.tif``.

``update`` runs after each poll with the repository's Earthdata login
(``EARTHDATA_USERNAME``/``EARTHDATA_PASSWORD``) and keeps both tables on the
data branch, under ``extents/``: the granules CMR added since the last run,
plus a backfill step (NISAR: 92 days, OPERA: 14), to a time budget; it never
fails the job. ``publish_extents`` writes the site's per-track shards.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

LOGGER = logging.getLogger(__name__)

EXTENTS_DIR = "extents"
NISAR_FILE = "nisar_rslc.parquet"
OPERA_FILE = "opera_bursts.parquet"
STATE_FILE = "state.json"
SITE_DIR = "browse_extents"

EDL_TOKEN = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
CMR_CSV = "https://cmr.earthdata.nasa.gov/search/granules.csv"
NISAR_PRODUCTS = "https://nisar.asf.earthdatacloud.nasa.gov/NISAR"
OPERA_PRODUCTS = "https://cumulus.asf.earthdatacloud.nasa.gov/OPERA/OPERA_L2_RTC-S1"
#: NISAR_L1_PR_RSLC_031_106_A_026_4005_DHDH_A_20260925T130400_...
RSLC_NAME = re.compile(
    r"^NISAR_L1_(PR|OP)_RSLC_\d{3}_(\d{3})_[AD]_\d{3}_.*_(\d{8})T\d{6}_"
)
NISAR_COLLECTION = {"PR": "NISAR_L1_RSLC_PROVISIONAL_V1"}
#: OPERA_L2_RTC-S1_T137-292398-IW2_20261001T020133Z_...
OPERA_NAME = re.compile(r"^OPERA_L2_RTC-S1_(T(\d{3})-\d{6}-IW\d)_")

NISAR_DAYS = 92  # the map shows each frame's last 90 days
NISAR_MAX_TRIES = 3
#: kind -> (CMR query, days to backfill, backfill step per run); every OPERA
#: burst is seen within a 12-day repeat
BACKFILL = {
    "nisar": (
        {"collection_concept_id": "C2853145197-ASF"},
        NISAR_DAYS,
        timedelta(days=2),
    ),
    "opera": ({"short_name": "OPERA_L2_RTC-S1_V1"}, 14, timedelta(hours=12)),
}


# ---------------------------------------------------------------- parsing
def kml_box(text: str) -> list[float]:
    """A ``_LATLON.kml``'s ``LatLonQuad`` (or ``LatLonBox``) -> [w, s, e, n]."""
    m = re.search(r"<coordinates>([^<]+)</coordinates>", text)
    if m:
        pts = [tuple(map(float, p.split(",")[:2])) for p in m.group(1).split()]
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        return [min(lons), min(lats), max(lons), max(lats)]
    box = {
        k: float(re.search(f"<{k}>([^<]+)</{k}>", text).group(1))
        for k in ("west", "south", "east", "north")
        if re.search(f"<{k}>", text)
    }
    if len(box) != 4:
        raise ValueError("no LatLonQuad or LatLonBox in the KML")
    return [box["west"], box["south"], box["east"], box["north"]]


def tiff_grid(head: bytes) -> tuple[int, float, float, float, float]:
    """A GeoTIFF's first bytes -> (EPSG, xmin, ymin, xmax, ymax): its size,
    ModelPixelScale, ModelTiepoint and ProjectedCSTypeGeoKey (classic TIFF,
    north-up)."""
    bo = {b"II": "<", b"MM": ">"}.get(head[:2])
    if bo is None or struct.unpack(bo + "H", head[2:4])[0] != 42:
        raise ValueError("not a classic TIFF")
    off = struct.unpack(bo + "I", head[4:8])[0]
    (n,) = struct.unpack(bo + "H", head[off : off + 2])
    types = {3: ("H", 2), 4: ("I", 4), 12: ("d", 8)}
    tags = {}
    for i in range(n):
        e = head[off + 2 + 12 * i : off + 14 + 12 * i]
        tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
        if tag not in (256, 257, 33550, 33922, 34735) or typ not in types:
            continue
        fmt, size = types[typ]
        if cnt * size <= 4:
            data = e[8 : 8 + cnt * size]
        else:
            p = struct.unpack(bo + "I", e[8:12])[0]
            data = head[p : p + cnt * size]
        if len(data) < cnt * size:
            raise ValueError(f"TIFF tag {tag} lies past the bytes read")
        tags[tag] = struct.unpack(bo + fmt * cnt, data)
    if not all(t in tags for t in (256, 257, 33550, 33922, 34735)):
        raise ValueError("not a georeferenced TIFF")
    keys, epsg = tags[34735], None
    for j in range(1, keys[3] + 1):
        key, loc, _, value = keys[4 * j : 4 * j + 4]
        if key == 3072 and loc == 0:
            epsg = value
    if not epsg:
        raise ValueError("no projected EPSG code")
    w, h = tags[256][0], tags[257][0]
    sx, sy = tags[33550][:2]
    x0, y1 = tags[33922][3], tags[33922][4]
    return int(epsg), x0, y1 - h * sy, x0 + w * sx, y1


def nisar_kml_url(granule: str) -> str | None:
    m = RSLC_NAME.match(granule)
    coll = m and NISAR_COLLECTION.get(m[1])
    return f"{NISAR_PRODUCTS}/{coll}/{granule}/{granule}_LATLON.kml" if coll else None


def opera_mask_url(granule: str) -> str:
    return f"{OPERA_PRODUCTS}/{granule}/{granule}_mask.tif"


# ---------------------------------------------------------------- tables
def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=columns)


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    os.replace(tmp, path)


NISAR_COLUMNS = [
    "granule",
    "track",
    "sensed",
    "west",
    "south",
    "east",
    "north",
    "tries",
]
OPERA_COLUMNS = ["burst", "track", "epsg", "xmin", "ymin", "xmax", "ymax"]


# ---------------------------------------------------------------- fetching
def edl_session(session=None):
    """A requests session carrying an Earthdata bearer token, or None when
    the login is not set (or refused)."""
    import requests

    user, password = (
        os.environ.get("EARTHDATA_USERNAME"),
        os.environ.get("EARTHDATA_PASSWORD"),
    )
    if not user or not password:
        return None
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", "swathcast (+github.com/egagli)")
    try:
        r = s.post(EDL_TOKEN, auth=(user, password), timeout=60)
        r.raise_for_status()
        token = r.json()["access_token"]
    except (OSError, ValueError, KeyError) as exc:
        LOGGER.warning("Earthdata login refused: %s", exc)
        return None
    # sent to ASF only: requests drops it on the redirect to the signed URL
    s.headers["Authorization"] = f"Bearer {token}"
    return s


def _get(session, url, **kw):
    r = session.get(url, timeout=60, **kw)
    if r.status_code not in (200, 206):
        raise OSError(f"HTTP {r.status_code}")
    return r


def cmr_names(session, params: dict, start: datetime, end: datetime) -> list[str]:
    """Granule names added to CMR in [start, end), newest sensed first (CSV:
    names and links only, ~2.5 KB a granule; the JSON is ~10x that)."""
    params = {
        **params,
        "created_at[]": f"{start:%Y-%m-%dT%H:%M:%SZ},{end:%Y-%m-%dT%H:%M:%SZ}",
        "page_size": 2000,
        "sort_key": "-start_date",
    }
    out, headers = [], {}
    for _ in range(100):
        r = session.get(CMR_CSV, params=params, headers=headers, timeout=180)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))[1:]
        out += [row[0] for row in rows if row]
        after = r.headers.get("CMR-Search-After")
        if len(rows) < params["page_size"] or not after:
            break
        headers = {"CMR-Search-After": after}
    return out


def listing(session, kind: str, now: datetime, state: dict) -> list[str]:
    """Names added since the last run, then one backfill step back to
    ``BACKFILL[kind]`` days (the cursors are kept in ``state``)."""
    params, days, step = BACKFILL[kind]
    after = pd.Timestamp(state.get(f"{kind}_after") or now - timedelta(hours=2))
    names = cmr_names(session, params, after.to_pydatetime(), now)
    state[f"{kind}_after"] = now.isoformat()
    floor = now - timedelta(days=days)
    before = pd.Timestamp(state.get(f"{kind}_backfill_before") or after).to_pydatetime()
    if before > floor:
        start = max(floor, before - step)
        names += cmr_names(session, params, start, before)
        state[f"{kind}_backfill_before"] = start.isoformat()
    return names


def _parallel(fn, items, deadline, workers):
    """fn over items until the deadline: [(item, result | exception)]."""
    out = []

    def run(item):
        if time.monotonic() > deadline:
            return item, None
        try:
            return item, fn(item)
        except (OSError, ValueError) as exc:
            return item, exc

    with ThreadPoolExecutor(workers) as pool:
        for item, res in pool.map(run, items):
            if res is not None:
                out.append((item, res))
    return out


def update_nisar(
    data_dir: Path, session, now: datetime, deadline, state: dict, workers=16
) -> dict:
    """KML extents of NISAR RSLC granules: those added to CMR since the last
    run, and a backfill of NISAR_DAYS; failures are retried NISAR_MAX_TRIES
    times (when listed again, or still to do)."""
    path = Path(data_dir) / EXTENTS_DIR / NISAR_FILE
    table = _read(path, NISAR_COLUMNS)
    done = table.set_index("granule")["tries"].to_dict() if len(table) else {}
    have = set(table.loc[table["west"].notna(), "granule"]) if len(table) else set()
    pending = list(table.loc[table["west"].isna(), "granule"]) if len(table) else []
    names = list(dict.fromkeys(listing(session, "nisar", now, state) + pending))
    todo = [
        g
        for g in names
        if g not in have and done.get(g, 0) < NISAR_MAX_TRIES and nisar_kml_url(g)
    ]

    def fetch(g):
        return kml_box(_get(session, nisar_kml_url(g)).text)

    results = _parallel(fetch, todo, deadline, workers)
    rows = []
    for g, res in results:
        m = RSLC_NAME.match(g)
        ok = not isinstance(res, Exception)
        rows.append(
            {
                "granule": g,
                "track": int(m[2]),
                "sensed": pd.Timestamp(m[3], tz="UTC"),
                **dict(
                    zip(
                        ["west", "south", "east", "north"],
                        res if ok else [float("nan")] * 4,
                        strict=True,
                    )
                ),
                "tries": 0 if ok else done.get(g, 0) + 1,
            }
        )
    if rows:
        new = pd.DataFrame(rows)
        table = pd.concat(
            [table[~table["granule"].isin(new["granule"])], new], ignore_index=True
        )
    if len(table):
        table["sensed"] = pd.to_datetime(table["sensed"], utc=True)
        table = table[table["sensed"] >= now - timedelta(days=NISAR_DAYS + 8)]
        table = table.astype({"track": "int16", "tries": "int8"})
        _write(table.sort_values(["track", "granule"]), path)
    failed = sum(isinstance(r, Exception) for _, r in results)
    return {
        "nisar_new": len(results) - failed,
        "nisar_failed": failed,
        "nisar_left": len(todo) - len(results),
        "nisar_known": int(table["west"].notna().sum()) if len(table) else 0,
    }


def update_opera(
    data_dir: Path, session, now: datetime, deadline, state: dict, workers=16
) -> dict:
    """Grids of OPERA RTC-S1 bursts not yet known: those added to CMR since
    the last run, and a backfill of two weeks."""
    path = Path(data_dir) / EXTENTS_DIR / OPERA_FILE
    table = _read(path, OPERA_COLUMNS)
    known = set(table["burst"]) if len(table) else set()
    seen = {}
    for g in listing(session, "opera", now, state):
        m = OPERA_NAME.match(g)
        if m:
            seen.setdefault(m[1], g)
    todo = [(b, g) for b, g in seen.items() if b not in known]

    def fetch(item):
        head = _get(
            session, opera_mask_url(item[1]), headers={"Range": "bytes=0-16383"}
        )
        return tiff_grid(head.content)

    results = _parallel(fetch, todo, deadline, workers)
    rows = [
        {
            "burst": b,
            "track": int(b[1:4]),
            **dict(zip(OPERA_COLUMNS[2:], res, strict=True)),
        }
        for (b, _), res in results
        if not isinstance(res, Exception)
    ]
    if rows:
        table = pd.concat([table, pd.DataFrame(rows)], ignore_index=True)
        table = table.astype({"track": "int16", "epsg": "int32"})
        _write(table.sort_values(["track", "burst"]), path)
    return {
        "opera_new": len(rows),
        "opera_failed": len(results) - len(rows),
        "opera_left": len(todo) - len(results),
        "opera_known": len(table),
    }


def update(data_dir: Path, now: datetime | None = None, budget_s: float = 480) -> dict:
    """Both tables, within ``budget_s`` (NISAR gets the first half)."""
    now = now or datetime.now(UTC)
    session = edl_session()
    if session is None:
        LOGGER.info(
            "extents: no Earthdata login (EARTHDATA_USERNAME/PASSWORD); skipped"
        )
        return {"skipped": "no Earthdata login"}
    state_path = Path(data_dir) / EXTENTS_DIR / STATE_FILE
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    t0 = time.monotonic()
    report = {}
    for name, fn in (
        (
            "nisar",
            lambda: update_nisar(data_dir, session, now, t0 + budget_s / 2, state),
        ),
        ("opera", lambda: update_opera(data_dir, session, now, t0 + budget_s, state)),
    ):
        try:
            report.update(fn())
        except Exception as exc:  # noqa: BLE001 - never fails the poll
            LOGGER.exception("extents: %s failed", name)
            report[f"{name}_error"] = str(exc)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1) + "\n")
    LOGGER.info("extents: %s", report)
    return report


# ---------------------------------------------------------------- the site
def publish_extents(data_dir: Path, out_dir: Path) -> dict:
    """``browse_extents/nisar/<track>.json`` ({granule: [w, s, e, n]}) and
    ``browse_extents/opera/<track>.json`` ({burst: [epsg, xmin, ymin, xmax,
    ymax]}): what the map reads to place those previews exactly."""
    src = Path(data_dir) / EXTENTS_DIR
    out = Path(out_dir) / SITE_DIR
    counts = {}
    for kind, name, value in (
        (
            "nisar",
            NISAR_FILE,
            lambda r: [round(r[c], 6) for c in ("west", "south", "east", "north")],
        ),
        (
            "opera",
            OPERA_FILE,
            lambda r: (
                [int(r["epsg"])]
                + [float(r[c]) for c in ("xmin", "ymin", "xmax", "ymax")]
            ),
        ),
    ):
        path = src / name
        if not path.exists():
            continue
        table = pd.read_parquet(path)
        if kind == "nisar":
            table = table[table["west"].notna()]
        key = "granule" if kind == "nisar" else "burst"
        folder = out / kind
        folder.mkdir(parents=True, exist_ok=True)
        for track, group in table.groupby("track"):
            shard = {r[key]: value(r) for r in group.to_dict("records")}
            (folder / f"{int(track):03d}.json").write_text(
                json.dumps(shard, separators=(",", ":"))
            )
        counts[kind] = len(table)
    if counts:
        LOGGER.info("published browse extents: %s", counts)
    return counts
