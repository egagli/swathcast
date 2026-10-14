"""NISAR S-band at ISRO's Bhoonidhi (STAC API; a Bhoonidhi login is required).

Written to the published API specification (https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/,
read 2026-10-01) and checked against the live API with a login (2026-10-01):

* ``POST /auth/token`` with ``{"userId", "password", "grant_type": "password"}``
  returns ``access_token`` (sent as ``Authorization: Bearer``) and
  ``refresh_token``; ``POST /auth/logout`` with the refresh token ends the
  session. Sessions are limited ("403 Max sessions already active"), so each
  run logs in once and logs out at the end (:func:`close`).
* ``POST /data/search`` takes STAC search JSON (collections, datetime, ids,
  limit <= 500) and pages through ``links`` ``rel: next``.
* NISAR S-band collections: ``NISAR_SSAR_RSLC``, ``..._GSLC``, ``..._GCOV``,
  ``..._RIFG``, ``..._RUNW``, ``..._GUNW``, ``..._ROFF``, ``..._GOFF``.

What the live items look like (2026-10-01):

* names ``NISAR_S1_PR_RSLC_<cycle>_<track>_<A|D>_<frame>_...`` and
  ``NISAR_S2_PR_GCOV_...`` with the same tail (ISRO's S1/S2 where JPL writes
  L1/L2), so an RSLC's GCOV name is known;
* properties ``Track``, ``Frame``, ``Node`` (Ascending/Descending),
  ``Polarization`` (e.g. ``CP``, compact pol), ``Coverage`` (F/P),
  ``CycleNumber``, ``RCID``, ``GenerationTime`` (UTC, no zone) and ``updated``
  (``YYYY-MM-DD HH:MM:SS`` in India time, UTC+05:30: an item sensed 12:44 UTC,
  generated 17:04 UTC, reads ``updated 22:56`` = 17:26 UTC);
* an empty search body is a 502; a search over many days is slow (~30 s for
  500 items) and once returned 500s while paging, so :class:`BhoonidhiCatalog`
  asks one sensing day at a time (~70 RSLC a day, under one page).

Credentials come from ``BHOONIDHI_USER_ID`` and ``BHOONIDHI_PASSWORD``; without
them the mission is skipped (missions.py).

The map's "Recent acquisitions" cannot search Bhoonidhi (login), so each run
also keeps a history table (:func:`update_history`,
``missions/nisar_s/history.parquet``): the items the run listed, plus a
backfill of :data:`HISTORY_DAYS`, a few sensing days per run. publish.py writes
it per track for the site. Each row keeps the item's browse image link, if it
has one (any asset named or with a role like browse, quicklook or thumbnail;
its query string, which could hold a token, dropped). Every item has a public
``thumbnail`` JPEG (no login, no CORS: the map reads it through an image proxy),
one channel, the product decimated 20x. To place it, each row also keeps

* ``corners``: the data's four corners (``ImgLatLon``, the same in an RSLC and
  its GCOV), from the image's top-left, clockwise: the RSLC's image is in radar
  geometry, so these place it approximately;
* ``grid``: the GCOV's grid corners (``ProdLatLon``, its UTM grid's extent,
  which the GCOV's thumbnail fills exactly), from the GCOV item; looked up by
  name (:func:`fill_grids`, at most :data:`GRID_LOOKUPS_PER_RUN` a run, each
  row every run while it is under two days old, then daily, until its GCOV
  is out).

Both are ``"lon,lat,lon,lat,..."`` strings (8 numbers, 5 decimals).

The universe is every ``NISAR_SSAR_RSLC`` item whose sensing time falls in the
last :data:`LOOKBACK_DAYS`. ``updated`` is the provider timestamp, and a
sighting is prompt when it comes within the tracker's tolerance of it, as for
other catalogues. Items without one fall back to the listing gap: prompt only
when the previous listing ran within :data:`PROMPT_GAP_HOURS` (the first run and
the run after an outage list a backlog, which is excluded from the statistics).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from swathcast.sources.base import (
    Acquisition,
    Found,
    ReferenceCatalog,
    Source,
    SourceError,
    chunks,
)
from swathcast.timeutil import parse_utc, to_iso, utcnow

LOGGER = logging.getLogger(__name__)

API = "https://bhoonidhi-api.nrsc.gov.in"
COLLECTIONS = {"RSLC": "NISAR_SSAR_RSLC", "GCOV": "NISAR_SSAR_GCOV"}
ENV_USER, ENV_PASSWORD = "BHOONIDHI_USER_ID", "BHOONIDHI_PASSWORD"
LOOKBACK_DAYS = 10
#: a listing within this long after the previous one sees new items promptly
#: (the tracker re-lists from its cursor minus the overlap)
PROMPT_GAP_HOURS = 3.0
MAX_PAGES = 60
#: Description Bhoonidhi gives, with HTTP 404, for a search that matches nothing
NO_RESULTS = "NO RESULTS FOUND"
#: ``updated`` is written in India Standard Time without a zone
IST = timedelta(hours=5, minutes=30)
#: NISAR product names: ..._<cycle>_<track>_<A|D>_<frame>_...
NAME_RE = re.compile(
    r"_(?P<cycle>\d{3})_(?P<track>\d{3})_(?P<dir>[AD])_(?P<frame>\d{3})_"
)


def configured() -> bool:
    return bool(os.environ.get(ENV_USER) and os.environ.get(ENV_PASSWORD))


def _auth(session) -> dict:
    """The run's login (cached on the session)."""
    state = getattr(session, "_bhoonidhi", None)
    if state:
        return state
    if not configured():
        raise SourceError(f"Bhoonidhi credentials not set ({ENV_USER}, {ENV_PASSWORD})")
    r = session.post(
        f"{API}/auth/token",
        json={
            "userId": os.environ[ENV_USER],
            "password": os.environ[ENV_PASSWORD],
            "grant_type": "password",
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise SourceError(f"Bhoonidhi login HTTP {r.status_code}: {_error(r)}")
    body = r.json()
    if not body.get("access_token"):
        raise SourceError(f"Bhoonidhi login returned no access token: {_error(r)}")
    session._bhoonidhi = body
    return body


def _error(r) -> str:
    try:
        j = r.json()
        return f"{j.get('ErrorCode') or j.get('errorCode')}: {j.get('Description') or j.get('description')}"[
            :300
        ]
    except ValueError:
        return r.text[:300]


def _no_results(r) -> bool:
    """Bhoonidhi answers a search that matches nothing with HTTP 404 "NO RESULTS
    FOUND" rather than 200 and an empty ``features``. Any other 404 is an error."""
    return r.status_code == 404 and NO_RESULTS in _error(r).upper()


def close(session) -> None:
    """Log out (Bhoonidhi limits concurrent sessions). Never raises."""
    state = getattr(session, "_bhoonidhi", None)
    if not state:
        return
    try:
        session.post(
            f"{API}/auth/logout",
            headers={"Authorization": f"Bearer {state.get('refresh_token', '')}"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - best effort
        LOGGER.warning("Bhoonidhi logout failed: %s", exc)
    session._bhoonidhi = None


def search(session, body: dict) -> list[dict]:
    """POST a STAC search and follow ``rel: next`` links."""
    headers = {"Authorization": f"Bearer {_auth(session)['access_token']}"}
    url, method, features = f"{API}/data/search", "POST", []
    for _ in range(MAX_PAGES):
        try:
            if method == "POST":
                r = session.post(url, json=body, headers=headers, timeout=180)
            else:
                r = session.get(url, headers=headers, timeout=180)
        except requests.RequestException as exc:
            raise SourceError(f"Bhoonidhi search failed: {exc}") from exc
        if _no_results(r):
            return features
        if r.status_code != 200:
            raise SourceError(f"Bhoonidhi search HTTP {r.status_code}: {_error(r)}")
        page = r.json()
        features.extend(page.get("features") or [])
        nxt = next(
            (ln for ln in page.get("links") or [] if ln.get("rel") == "next"), None
        )
        if not nxt or not page.get("features"):
            return features
        url = nxt["href"]
        method = (nxt.get("method") or ("POST" if nxt.get("body") else "GET")).upper()
        body = nxt.get("body") or body
    raise SourceError(f"Bhoonidhi search: more than {MAX_PAGES} pages")


def _prop(p: dict, *names):
    lower = {k.lower(): v for k, v in p.items()}
    for n in names:
        if n in p and p[n] not in (None, ""):
            return p[n]
        if n.lower() in lower and lower[n.lower()] not in (None, ""):
            return lower[n.lower()]
    return None


def provider_time(p: dict) -> tuple[datetime | None, str | None]:
    """``updated`` (India time) as UTC, else ``GenerationTime`` (UTC)."""
    for field, offset in (("updated", IST), ("GenerationTime", timedelta(0))):
        v = p.get(field)
        if not v:
            continue
        try:
            t = parse_utc(v, assume_utc=True)
        except (TypeError, ValueError):
            continue
        if not re.search(r"(Z|[+-]\d\d:?\d\d)$", str(v).strip()):
            t = t - offset  # a zoneless time: read in its own zone
        return t, field
    return None, None


def acquisition(item: dict, product_type: str, promptly: bool | None) -> Acquisition:
    p = item.get("properties") or {}
    gid = item["id"]
    start = parse_utc(_prop(p, "start_datetime", "datetime"))
    stop = parse_utc(_prop(p, "end_datetime") or _prop(p, "start_datetime", "datetime"))
    m = NAME_RE.search(gid)
    track = _int(_prop(p, "Track")) or (int(m["track"]) if m else None)
    frame = _int(_prop(p, "Frame")) or (int(m["frame"]) if m else None)
    node = str(_prop(p, "Node") or "").lower()
    direction = (
        node
        if node in ("ascending", "descending")
        else ({"A": "ascending", "D": "descending"}[m["dir"]] if m else None)
    )
    cycle = _int(_prop(p, "CycleNumber")) or (int(m["cycle"]) if m else None)
    lon, lat = _prop(p, "SceneCenterLon"), _prop(p, "SceneCenterLat")
    if lon is None or lat is None:
        b = item.get("bbox") or [None] * 4
        if b[0] is not None:
            lon, lat = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    published, field = provider_time(p)
    pol = _prop(p, "Polarization", "sar:polarizations")
    return Acquisition(
        granule_id=gid,
        product_type=product_type,
        platform="NISAR",
        mode="S-band",
        polarization="+".join(pol) if isinstance(pol, list) else pol,
        absolute_orbit=_int(_prop(p, "Imaging_Orbit")),
        relative_orbit=track,
        orbit_direction=direction,
        datatake_id=f"{frame:03d}" if frame is not None else None,
        anx_start_s=None,
        sensing_start=start,
        sensing_stop=stop,
        centroid_lon=round(float(lon), 4) if lon is not None else None,
        centroid_lat=round(float(lat), 4) if lat is not None else None,
        timeliness=f"cycle {cycle}" if cycle is not None else None,
        provider_timestamp=published,
        provider_timestamp_field=f"{field} (IST)" if field == "updated" else field,
        coverage_key=f"{track:03d}-{frame:03d}"
        if track is not None and frame is not None
        else gid,
        # with a publication time the tracker judges promptness itself
        listed_promptly=None if published is not None else promptly,
    )


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def day_windows(end: datetime, days: int) -> list[tuple[datetime, datetime]]:
    """``days`` consecutive one-day windows ending at ``end``, newest first."""
    return [
        (end - timedelta(days=i + 1), end - timedelta(days=i) - timedelta(seconds=1))
        for i in range(days)
    ]


class BhoonidhiCatalog(ReferenceCatalog):
    key = "bhoonidhi_nisar_s"
    label = "Bhoonidhi NISAR S-band"
    distributor = "ISRO NRSC Bhoonidhi"
    product = "S-band RSLC"
    #: the tracker's re-listing overlap (TrackerConfig.reference_overlap_hours)
    overlap_hours = 3.0

    def list_new(self, window) -> list[Acquisition]:
        start, end = window
        promptly = (end - start) <= timedelta(
            hours=self.overlap_hours + PROMPT_GAP_HOURS
        )
        out, seen = [], set()
        for t0, t1 in day_windows(end, LOOKBACK_DAYS):
            body = {
                "collections": [COLLECTIONS["RSLC"]],
                "datetime": f"{to_iso(t0)}/{to_iso(t1)}",
                "limit": 500,
            }
            items = search(self.session, body)
            _remember(self.session, items)
            for item in items:
                if item.get("id") in seen:
                    continue
                seen.add(item.get("id"))
                try:
                    out.append(acquisition(item, "RSLC", promptly))
                except (KeyError, TypeError, ValueError) as exc:
                    LOGGER.warning(
                        "Bhoonidhi: skipping item %s: %s", item.get("id"), exc
                    )
        return out

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return _lookup(self.session, COLLECTIONS["RSLC"], {g: g for g in ids})


def _lookup(session, collection: str, by_name: dict[str, str]) -> dict[str, Found]:
    found: dict[str, Found] = {}
    names = sorted(by_name)
    for batch in chunks(names, 100):
        body = {"collections": [collection], "ids": batch, "limit": len(batch)}
        for item in search(session, body):
            gid = by_name.get(item.get("id"))
            if not gid:
                continue
            published, field = provider_time(item.get("properties") or {})
            found[gid] = Found(
                provider_timestamp=published,
                provider_timestamp_field=f"{field} (IST)"
                if field == "updated"
                else field,
                provider_id=item["id"],
            )
    return found


# ---------------------------------------------------------------- history
HISTORY_FILE = "history.parquet"
HISTORY_STATE = "history_state.json"
#: the map shows a frame's last 90 days
HISTORY_DAYS = 92
#: sensing days backfilled per run (one search each, ~3-30 s)
BACKFILL_DAYS_PER_RUN = 3
HISTORY_COLUMNS = [
    "id",
    "track",
    "frame",
    "direction",
    "time",
    "pol",
    "partial",
    "browse",
    "corners",
    "grid",
    "grid_checked",
]
#: GCOV items looked up (by name) per run for their grids, newest rows first
GRID_LOOKUPS_PER_RUN = 500
#: a row whose GCOV was not found is asked again after this long: every run
#: while it is young (a GCOV follows its RSLC by minutes to hours), then daily
GRID_RETRY_YOUNG, GRID_RETRY = timedelta(minutes=50), timedelta(days=1)
GRID_YOUNG = timedelta(days=2)
#: Bhoonidhi's polarization codes as the plan names the channel sets
#: (seen 2026-10: DH, DV, HH, CP)
POL = {
    "SH": "HH",
    "HH": "HH",
    "SV": "VV",
    "VV": "VV",
    "DH": "HH+HV",
    "DV": "VV+VH",
    "QP": "HH+HV+VH+VV",
    "QD": "HH+VV",
    "CP": "RH+RV",
}
BROWSE_ASSET = re.compile(r"browse|quick.?look|thumbnail|overview|preview", re.I)


def browse_href(item: dict) -> str | None:
    """The item's browse image link, without its query string, or None."""
    cands = [
        (k, a)
        for k, a in (item.get("assets") or {}).items()
        if isinstance(a, dict) and a.get("href")
    ] + [
        (ln.get("rel", ""), ln)
        for ln in item.get("links") or []
        if isinstance(ln, dict) and ln.get("href")
    ]
    for name, a in cands:
        if BROWSE_ASSET.search(f"{name} {' '.join(a.get('roles') or [])}"):
            return str(a["href"]).split("?", 1)[0]
    return None


def quad(item: dict, field: str) -> str | None:
    """A ``[[lon, lat], ...]`` property (``ImgLatLon``, ``ProdLatLon``: the
    four corners from the image's top-left, clockwise, then the first again)
    as ``"lon,lat,..."``, or None."""
    pts = (item.get("properties") or {}).get(field)
    try:
        xy = [(float(p[0]), float(p[1])) for p in pts[:4]]
    except (TypeError, ValueError, IndexError):
        return None
    if len(xy) != 4 or not all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in xy):
        return None
    return ",".join(f"{v:.5f}" for p in xy for v in p)


def history_row(item: dict) -> dict:
    a = acquisition(item, "RSLC", None)
    p = item.get("properties") or {}
    code = str(a.polarization or "")
    cov = str(_prop(p, "Coverage") or "")
    if not cov:
        m = re.search(r"_([FP])_[A-Z]_\d{3}$", item["id"])
        cov = m[1] if m else ""
    return {
        "id": item["id"],
        "track": a.relative_orbit,
        "frame": int(a.datatake_id) if a.datatake_id else None,
        "direction": a.orbit_direction,
        "time": a.sensing_start,
        "pol": POL.get(code.upper(), code) or None,
        "partial": cov.upper() == "P",
        "browse": browse_href(item),
        "corners": quad(item, "ImgLatLon"),
        "grid": None,
        "grid_checked": None,
    }


def _remember(session, items: list[dict]) -> None:
    """Keep the run's listed items on the session for update_history."""
    rows = getattr(session, "_bhoonidhi_rows", None)
    if rows is None:
        rows = session._bhoonidhi_rows = []
    if items and not rows:
        # what an item links to (for previews), once a run, without query strings
        it = items[0]
        LOGGER.info(
            "Bhoonidhi item %s: assets %s; links %s",
            it.get("id"),
            {
                k: [a.get("type"), a.get("roles"), str(a.get("href", "")).split("?")[0]]
                for k, a in (it.get("assets") or {}).items()
                if isinstance(a, dict)
            },
            [
                [ln.get("rel"), str(ln.get("href", "")).split("?")[0]]
                for ln in it.get("links") or []
                if isinstance(ln, dict)
            ],
        )
    for it in items:
        try:
            rows.append(history_row(it))
        except (KeyError, TypeError, ValueError):
            pass


def update_history(session, data_dir: Path, now: datetime | None = None) -> dict:
    """``missions/nisar_s/history.parquet``: this run's listed items, and up to
    BACKFILL_DAYS_PER_RUN older sensing days until HISTORY_DAYS are covered
    (the cursor is in ``history_state.json``). A failed backfill search ends
    the step; it is retried next run."""
    now = now or utcnow()
    root = Path(data_dir) / "missions" / "nisar_s"
    path, state_path = root / HISTORY_FILE, root / HISTORY_STATE
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    rows = list(getattr(session, "_bhoonidhi_rows", None) or [])
    floor = now - timedelta(days=HISTORY_DAYS)
    before = (
        parse_utc(state["backfill_before"])
        if state.get("backfill_before")
        else (now - timedelta(days=LOOKBACK_DAYS))
    )
    filled, error = 0, None
    while before > floor and filled < BACKFILL_DAYS_PER_RUN:
        start = max(floor, before - timedelta(days=1))
        body = {
            "collections": [COLLECTIONS["RSLC"]],
            "datetime": f"{to_iso(start)}/{to_iso(before - timedelta(seconds=1))}",
            "limit": 500,
        }
        try:
            items = search(session, body)
        except SourceError as exc:
            error = str(exc)
            break
        for it in items:
            try:
                rows.append(history_row(it))
            except (KeyError, TypeError, ValueError):
                pass
        before, filled = start, filled + 1
    state["backfill_before"] = to_iso(before)
    table = (
        pd.read_parquet(path)
        if path.exists()
        else pd.DataFrame(columns=HISTORY_COLUMNS)
    )
    table = table.reindex(columns=HISTORY_COLUMNS)  # files from before a column
    if rows:
        new = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
        # a row listed again keeps the grid (and data corners) found earlier
        old = table.drop_duplicates("id", keep="last").set_index("id")
        for col in ("corners", "grid", "grid_checked"):
            new[col] = new[col].where(new[col].notna(), new["id"].map(old[col]))
        table = pd.concat([table, new], ignore_index=True) if len(table) else new
    if len(table):
        table["time"] = pd.to_datetime(table["time"], utc=True)
        table = table.dropna(subset=["track", "frame", "time"])
        table = table[table["time"] >= floor].drop_duplicates("id", keep="last")
        table = table.astype({"track": "int16", "frame": "int16", "partial": "bool"})
        table["grid_checked"] = pd.to_datetime(table["grid_checked"], utc=True)
        if not error:
            error = fill_grids(session, table, now)
        # strings even when all empty (a column of nulls has no type)
        table = table.astype({c: "string" for c in ("browse", "corners", "grid")})
        root.mkdir(parents=True, exist_ok=True)
        table.sort_values(["track", "frame", "time"]).to_parquet(
            path, index=False, compression="zstd"
        )
    root.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1) + "\n")
    count = lambda c: int(table[c].notna().sum()) if len(table) else 0  # noqa: E731
    report = {
        "history_rows": len(table),
        "history_backfill_before": state["backfill_before"],
        "history_with_browse": count("browse"),
        "history_with_grid": count("grid"),
    }
    if error:
        report["history_error"] = error
    LOGGER.info("Bhoonidhi history: %s", report)
    return report


def fill_grids(session, table: pd.DataFrame, now: datetime) -> str | None:
    """Sets ``grid`` (and ``corners``, if missing) on rows without a grid from
    their GCOV items, newest rows first, at most GRID_LOOKUPS_PER_RUN, each
    asked at most once per GRID_RETRY (GRID_RETRY_YOUNG while younger than
    GRID_YOUNG). In place; returns an error or None."""
    young = table["time"] > now - GRID_YOUNG
    since = now - table["grid_checked"]
    due = table["grid"].isna() & (
        table["grid_checked"].isna()
        | (since > GRID_RETRY)
        | (young & (since > GRID_RETRY_YOUNG))
    )
    todo = table[due].sort_values("time", ascending=False).head(GRID_LOOKUPS_PER_RUN)
    if todo.empty:
        return None
    by_name = {gcov_name(i): i for i in todo["id"]}
    names = sorted(by_name)
    found: dict[str, dict] = {}
    try:
        for batch in chunks(names, 100):
            body = {
                "collections": [COLLECTIONS["GCOV"]],
                "ids": batch,
                "limit": len(batch),
            }
            for it in search(session, body):
                if it.get("id") in by_name:
                    found[by_name[it["id"]]] = it
    except SourceError as exc:
        return str(exc)
    idx = todo.index
    table.loc[idx, "grid_checked"] = pd.Timestamp(now)
    for i, rid in zip(idx, todo["id"], strict=True):
        it = found.get(rid)
        if it is None:
            continue
        table.at[i, "grid"] = quad(it, "ProdLatLon")
        if pd.isna(table.at[i, "corners"]):
            table.at[i, "corners"] = quad(it, "ImgLatLon")
    return None


def gcov_name(rslc: str) -> str:
    """The GCOV of an RSLC: ``NISAR_S1_PR_RSLC_...`` -> ``NISAR_S2_PR_GCOV_...``
    (same tail; seen on the live catalogue 2026-10-01)."""
    return re.sub(r"^NISAR_S1_", "NISAR_S2_", rslc).replace("_RSLC_", "_GCOV_", 1)


class BhoonidhiRSLC(Source):
    key = "bhoonidhi_nisar_s_rslc"
    label = "Bhoonidhi NISAR S-band RSLC"
    distributor = "ISRO NRSC Bhoonidhi"
    product = "RSLC"
    reference_product = "RSLC"
    is_reference = True

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return _lookup(self.session, COLLECTIONS["RSLC"], {g: g for g in ids})


class BhoonidhiGCOV(Source):
    key = "bhoonidhi_nisar_s_gcov"
    label = "Bhoonidhi NISAR S-band GCOV"
    distributor = "ISRO NRSC Bhoonidhi"
    product = "GCOV"
    reference_product = "RSLC"

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return _lookup(
            self.session, COLLECTIONS["GCOV"], {gcov_name(g): g for g in ids}
        )


SOURCES: list[type[Source]] = [BhoonidhiRSLC, BhoonidhiGCOV]
CATALOG: type[ReferenceCatalog] = BhoonidhiCatalog
