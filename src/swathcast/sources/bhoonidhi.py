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

The universe is every ``NISAR_SSAR_RSLC`` item whose sensing time falls in the
last :data:`LOOKBACK_DAYS`. ``updated`` is the provider timestamp, and a
sighting is prompt when it comes within the tracker's tolerance of it, as for
other catalogues. Items without one fall back to the listing gap: prompt only
when the previous listing ran within :data:`PROMPT_GAP_HOURS` (the first run and
the run after an outage list a backlog, which is excluded from the statistics).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

import requests

from swathcast.sources.base import (
    Acquisition,
    Found,
    ReferenceCatalog,
    Source,
    SourceError,
    chunks,
)
from swathcast.timeutil import parse_utc, to_iso

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
            for item in search(self.session, body):
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
