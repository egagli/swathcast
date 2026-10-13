"""Orbital elements for the map's live satellite positions.

CelesTrak's GP API in OMM JSON form (``FORMAT=json``), one request per NORAD
catalogue number. The browser propagates them with SGP4 (satellite.js
``json2satrec``); this module only fetches and checks them. A satellite whose
fetch fails keeps its previous elements, so one CelesTrak hiccup does not
blank the map.

NORAD numbers checked against CelesTrak's own records on 2026-09-29.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

GP_URL = "https://celestrak.org/NORAD/elements/gp.php"
#: schedule platform code -> NORAD catalogue number
NORAD = {
    "S1A": 39634,
    "S1C": 62261,
    "S1D": 66315,
    "S2A": 40697,
    "S2B": 42063,
    "S2C": 60989,
    "L8": 39084,
    "L9": 49260,
    "NISAR": 65053,
}
#: OMM fields SGP4 needs (json2satrec)
OMM_FIELDS = (
    "OBJECT_NAME",
    "OBJECT_ID",
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "NORAD_CAT_ID",
    "BSTAR",
    "MEAN_MOTION_DOT",
    "MEAN_MOTION_DDOT",
    "EPHEMERIS_TYPE",
    "CLASSIFICATION_TYPE",
    "ELEMENT_SET_NO",
    "REV_AT_EPOCH",
)


def check_omm(omm: dict, norad: int) -> dict:
    missing = [f for f in OMM_FIELDS if f not in omm]
    if missing:
        raise ValueError(f"NORAD {norad}: OMM missing {missing}")
    if int(omm["NORAD_CAT_ID"]) != norad:
        raise ValueError(f"asked for NORAD {norad}, got {omm['NORAD_CAT_ID']}")
    return {f: omm[f] for f in OMM_FIELDS}


def fetch_one(session, norad: int, attempts: int = 4) -> dict:
    last = None
    for i in range(attempts):
        try:
            r = session.get(
                GP_URL, params={"CATNR": norad, "FORMAT": "json"}, timeout=60
            )
            r.raise_for_status()
            rows = r.json()
            if len(rows) != 1:
                raise ValueError(f"NORAD {norad}: {len(rows)} element sets")
            return check_omm(rows[0], norad)
        except (OSError, ValueError) as exc:  # requests errors are OSErrors
            last = exc
            time.sleep(2 ** (i + 1))
    raise RuntimeError(f"NORAD {norad}: {last}")


def update(
    path: Path, session=None, now: datetime | None = None, max_age_h: float = 0
) -> dict:
    """Refresh ``path`` (tle.json); satellites that fail keep their old elements.

    With ``max_age_h``, a file generated less than that long ago is left alone
    (CelesTrak asks clients not to refetch unchanged elements often).
    """
    import requests

    now = now or datetime.now(UTC)
    path = Path(path)
    if max_age_h and path.exists():
        try:
            current = json.loads(path.read_text())
            age = now - datetime.fromisoformat(current["generated_at"])
            if age.total_seconds() < max_age_h * 3600 and not current.get("failed"):
                LOGGER.info("tle.json is %s old; not refetching", age)
                return current
        except (ValueError, KeyError):
            pass
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "swathcast (+github.com/egagli)"
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text()).get("satellites", {})
        except ValueError:
            old = {}
    sats, failed = {}, []
    for platform, norad in NORAD.items():
        try:
            sats[platform] = {
                "norad": norad,
                "omm": fetch_one(session, norad),
                "fetched_at": now.isoformat(timespec="seconds"),
            }
        except RuntimeError as exc:
            LOGGER.warning("TLE %s: %s", platform, exc)
            failed.append(platform)
            if platform in old:
                sats[platform] = old[platform]
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": GP_URL,
        "failed": failed,
        "satellites": sats,
    }
    path.write_text(json.dumps(payload, indent=1) + "\n")
    return payload
