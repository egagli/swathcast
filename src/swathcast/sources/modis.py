"""MODIS (Terra, Aqua) and VIIRS (Suomi NPP, NOAA-20, NOAA-21), all via NASA CMR.

Verified 2026-10-13 (docs/sources.md):

* The universe is every Level-1B 1 km (MODIS ``MOD021KM``/``MYD021KM``) and
  750 m (VIIRS ``VNP02MOD``/``VJ102MOD``/``VJ202MOD``) swath granule that
  LANCE, NASA's near-real-time system, lists in CMR, by CMR ``created_at``:
  5-minute MODIS granules (288 a day per platform) and 6-minute VIIRS ones
  (240), day and night. LANCE products appear about 40 min (MODIS) to 2 h
  (VIIRS) after sensing and are deleted after 7 days (CMR ``Delete`` date).
* LANCE ``GranuleUR`` is an opaque number (``LANCEMODIS:3067030029``); the
  product name is the ``ProducerGranuleId``
  (``MOD021KM.A2026286.1525.061.2026286160422.NRT.hdf``). Every product of
  one granule (Level-1B, near-real-time or standard, snow cover, any
  collection) shares the platform and the ``AYYYYDDD.HHMM`` start, so the
  granule id here is ``<reference product>.AYYYYDDD.HHMM`` and other
  sources are matched on platform + that stamp.
* Sources are looked up by temporal window (every granule of the platform
  between the first and last open granule, paged to the end) rather than by
  name: CMR cannot search LANCE's producer ids without wildcards.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from swathcast.sources.base import (
    Acquisition,
    Found,
    ReferenceCatalog,
    Source,
)
from swathcast.sources.cmr import provider_insert_date, search_umm
from swathcast.timeutil import parse_utc, to_iso

LOGGER = logging.getLogger(__name__)

#: product short-name prefix -> platform code (as in tle.NORAD)
PLATFORM_OF_PREFIX = {
    "MOD": "Terra",
    "MYD": "Aqua",
    "VNP": "SNPP",
    "VJ1": "N20",
    "VJ2": "N21",
}
STAMP_RE = re.compile(r"\.(A\d{7}\.\d{4})\.")

# CMR collections, per platform (concept ids checked 2026-10-13)
MODIS_L1B_NRT = {"Terra": "C1426414410-LANCEMODIS", "Aqua": "C1426616847-LANCEMODIS"}
MODIS_L1B_C61 = {"Terra": "C1378227407-LAADS", "Aqua": "C1379758607-LAADS"}
MODIS_L1B_C7 = {"Terra": "C3861664481-LAADS", "Aqua": "C3861713185-LAADS"}
MODIS_SNOW_NRT = {"Terra": "C2007659515-LANCEMODIS", "Aqua": "C2007631329-LANCEMODIS"}
MODIS_SNOW = {"Terra": "C3024162910-NSIDC_CPRD", "Aqua": "C3090964320-NSIDC_CPRD"}
VIIRS_L1B_NRT = {
    "SNPP": "C2185497928-LANCEMODIS",
    "N20": "C2208778455-LANCEMODIS",
    "N21": "C3383723948-LANCEMODIS",
}
VIIRS_L1B = {
    "SNPP": "C2105087643-LAADS",
    "N20": "C2103811746-LAADS",
    "N21": "C3478403600-LAADS",
}
VIIRS_SNOW_NRT = {
    "SNPP": "C2780575359-LANCEMODIS",
    "N20": "C2781269342-LANCEMODIS",
    "N21": "C4232151476-LANCEMODIS",
}
VIIRS_SNOW = {
    "SNPP": "C3173404788-NSIDC_CPRD",
    "N20": "C3173450415-NSIDC_CPRD",
    "N21": "C3824913163-NSIDC_CPRD",
}
#: learned-coverage cell: day/night flag and a 10-degree box of the granule
#: centre (snow products are made for daytime land granules only)
CELL_DEG = 10


def producer_id(umm: dict) -> str:
    for ident in (umm.get("DataGranule") or {}).get("Identifiers") or []:
        if ident.get("IdentifierType") == "ProducerGranuleId":
            return ident["Identifier"]
    return umm.get("GranuleUR", "")


def stamp_of(name: str) -> str | None:
    """'MOD021KM.A2026286.1525.061.2026286160422.NRT.hdf' -> 'A2026286.1525'."""
    m = STAMP_RE.search(f".{name}.")
    return m[1] if m else None


def granule_id(name: str) -> str:
    """'MOD021KM.A2026286.1525.061.....hdf' -> 'MOD021KM.A2026286.1525'
    (LANCE's VIIRS names carry ``_NRT``: 'VNP02MOD_NRT.A...' -> 'VNP02MOD.A...')."""
    stamp = stamp_of(name)
    if not stamp:
        raise ValueError(f"no AYYYYDDD.HHMM stamp in {name!r}")
    return f"{name.split('.')[0].removesuffix('_NRT')}.{stamp}"


def platform_of(gid: str) -> str:
    try:
        return PLATFORM_OF_PREFIX[gid[:3]]
    except KeyError as exc:
        raise ValueError(f"unknown MODIS/VIIRS product {gid!r}") from exc


def _centre(umm: dict):
    try:
        pts = umm["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]["GPolygons"][
            0
        ]["Boundary"]["Points"][:-1]
    except (KeyError, IndexError, TypeError):
        return None, None
    lons = [p["Longitude"] for p in pts]
    if max(lons) - min(lons) > 180:
        lons = [x + 360 if x < 0 else x for x in lons]
    lon = ((sum(lons) / len(lons) + 180) % 360) - 180
    return round(lon, 4), round(sum(p["Latitude"] for p in pts) / len(pts), 4)


def acquisition(item: dict, product_type: str, mode: str) -> Acquisition:
    umm = item["umm"]
    gid = granule_id(producer_id(umm))
    rng = umm["TemporalExtent"]["RangeDateTime"]
    lon, lat = _centre(umm)
    flag = (umm.get("DataGranule") or {}).get("DayNightFlag") or "Unspecified"
    insert = provider_insert_date(umm)
    cell = (
        f"{flag}-{int((lat + 90) // CELL_DEG):02d}-{int((lon + 180) // CELL_DEG):02d}"
        if lat is not None
        else None
    )
    return Acquisition(
        granule_id=gid,
        product_type=product_type,
        platform=platform_of(gid),
        mode=mode,
        polarization=None,
        absolute_orbit=None,
        relative_orbit=None,
        orbit_direction=None,
        datatake_id=stamp_of(gid + "."),
        anx_start_s=None,
        sensing_start=parse_utc(rng["BeginningDateTime"]),
        sensing_stop=parse_utc(rng["EndingDateTime"]),
        centroid_lon=lon,
        centroid_lat=lat,
        timeliness=flag,
        provider_timestamp=parse_utc(insert) if insert else None,
        provider_timestamp_field="ProviderDates.Insert",
        coverage_key=cell,
    )


class LanceCatalog(ReferenceCatalog):
    """Every Level-1B granule LANCE lists, by CMR ``created_at``."""

    distributor = "NASA LANCE (CMR)"
    product = "L1B NRT"
    collections: dict[str, str] = {}
    mode = ""

    def list_new(self, window) -> list[Acquisition]:
        start, end = window
        out = []
        for concept in self.collections.values():
            params = [
                ("collection_concept_id", concept),
                ("created_at", f"{to_iso(start)},{to_iso(end)}"),
            ]
            for item in search_umm(self.session, params):
                try:
                    out.append(acquisition(item, "L1B", self.mode))
                except (KeyError, ValueError) as exc:
                    LOGGER.warning("%s: skipping %s", self.key, exc)
        return out

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return lookup_window(self.session, self.collections, ids)


def _window(ids: list[str]) -> dict[str, tuple[datetime, datetime, dict[str, str]]]:
    """Per platform: the span of the ids' start times and {stamp: id}."""
    out: dict[str, list] = {}
    for gid in ids:
        stamp = stamp_of(gid + ".")
        if not stamp:
            continue
        when = datetime.strptime(stamp, "A%Y%j.%H%M").replace(tzinfo=UTC)
        entry = out.setdefault(platform_of(gid), [when, when, {}])
        entry[0], entry[1] = min(entry[0], when), max(entry[1], when)
        entry[2][stamp] = gid
    return {p: (a, b, m) for p, (a, b, m) in out.items()}


def lookup_window(
    session, collections: dict[str, str], ids: list[str]
) -> dict[str, Found]:
    """Which of ``ids`` the platform's collection holds: every granule that
    starts between the first and the last of them, matched on the stamp."""
    found: dict[str, Found] = {}
    for platform, (first, last, by_stamp) in _window(ids).items():
        concept = collections.get(platform)
        if not concept:
            continue
        params = [
            ("collection_concept_id", concept),
            (
                "temporal",
                f"{to_iso(first - timedelta(seconds=30))},{to_iso(last + timedelta(seconds=30))}",
            ),
        ]
        for item in search_umm(session, params):
            umm = item.get("umm", {})
            name = producer_id(umm)
            gid = by_stamp.get(stamp_of(name) or "")
            if not gid:
                continue
            insert = provider_insert_date(umm)
            ts = parse_utc(insert) if insert else None
            prev = found.get(gid)
            if prev is None or (
                ts and prev.provider_timestamp and ts < prev.provider_timestamp
            ):
                found[gid] = Found(
                    provider_timestamp=ts,
                    provider_timestamp_field="ProviderDates.Insert" if ts else None,
                    provider_id=name,
                )
    return found


class LanceProduct(Source):
    """The reference listing as a source (rows created seen)."""

    is_reference = True
    reference_product = "L1B"
    collections: dict[str, str] = {}

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return lookup_window(self.session, self.collections, ids)


class CmrWindowSource(Source):
    """A CMR collection per platform, matched on platform + start stamp."""

    reference_product = "L1B"
    collections: dict[str, str] = {}

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return lookup_window(self.session, self.collections, ids)


def _source(
    key, label, distributor, product, collections, coverage="full", base=CmrWindowSource
):
    return type(
        key,
        (base,),
        {
            "key": key,
            "label": label,
            "distributor": distributor,
            "product": product,
            "collections": collections,
            "coverage": coverage,
        },
    )


class ModisCatalog(LanceCatalog):
    key = "lance_modis"
    label = "LANCE MODIS"
    collections = MODIS_L1B_NRT
    mode = "MODIS"


class ViirsCatalog(LanceCatalog):
    key = "lance_viirs"
    label = "LANCE VIIRS"
    collections = VIIRS_L1B_NRT
    mode = "VIIRS"


MODIS_SOURCES: list[type[Source]] = [
    _source(
        "lance_modis_l1b",
        "LANCE MODIS L1B (NRT)",
        "NASA LANCE",
        "MOD021KM/MYD021KM NRT",
        MODIS_L1B_NRT,
        base=LanceProduct,
    ),
    _source(
        "laads_modis_l1b",
        "LAADS MODIS L1B C6.1",
        "NASA LAADS DAAC",
        "MOD021KM/MYD021KM v6.1",
        MODIS_L1B_C61,
    ),
    _source(
        "laads_modis_l1b_c7",
        "LAADS MODIS L1B C7",
        "NASA LAADS DAAC",
        "MOD021KM/MYD021KM v7",
        MODIS_L1B_C7,
    ),
    _source(
        "lance_modis_snow",
        "LANCE MODIS snow (NRT)",
        "NASA LANCE",
        "MOD10_L2/MYD10_L2 NRT",
        MODIS_SNOW_NRT,
        "learned",
    ),
    _source(
        "nsidc_modis_snow",
        "NSIDC MODIS snow",
        "NSIDC DAAC",
        "MOD10_L2/MYD10_L2 v61",
        MODIS_SNOW,
        "learned",
    ),
]
VIIRS_SOURCES: list[type[Source]] = [
    _source(
        "lance_viirs_l1b",
        "LANCE VIIRS L1B (NRT)",
        "NASA LANCE",
        "VNP02MOD/VJ102MOD/VJ202MOD NRT",
        VIIRS_L1B_NRT,
        base=LanceProduct,
    ),
    _source(
        "laads_viirs_l1b",
        "LAADS VIIRS L1B",
        "NASA LAADS DAAC",
        "VNP02MOD/VJ102MOD/VJ202MOD",
        VIIRS_L1B,
    ),
    _source(
        "lance_viirs_snow",
        "LANCE VIIRS snow (NRT)",
        "NASA LANCE",
        "VNP10/VJ110/VJ210 NRT",
        VIIRS_SNOW_NRT,
        "learned",
    ),
    _source(
        "nsidc_viirs_snow",
        "NSIDC VIIRS snow",
        "NSIDC DAAC",
        "VNP10/VJ110/VJ210 v2",
        VIIRS_SNOW,
        "learned",
    ),
]
