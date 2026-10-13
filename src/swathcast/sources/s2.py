"""Sentinel-2: CDSE reference (L1C, L2A), Earth Search, Planetary Computer, HLS S30.

Verified 2026-09-29 (docs/sources.md):

* CDSE lists ~15,000 L1C and ~15,400 L2A products a day. The tracking
  universe is a fixed **1-in-8 sample of MGRS tiles** (``crc32(tile) % 8 ==
  0``), so the hourly job stays small. It is a spatially uniform sample
  fixed in advance, not the "first N results": every product of a sampled
  tile is tracked.
* Products are keyed on the full ESA name (without ``.SAFE``). About 450 a day
  per level share platform, sensing time and tile and differ only in the
  processing time: both are real products (Earth Search keeps both too).
* ``ContentDate/Start`` is the datatake start, the same for every tile of
  a datatake; the latency is measured from it (the per-tile sensing time is
  seconds to 30 min later, so tile-level latency is at most that much less).
"""

from __future__ import annotations

import logging
import re
import zlib

from swathcast.sources.base import (
    Acquisition,
    Found,
    ReferenceCatalog,
    Source,
    SourceError,
    chunks,
)
from swathcast.sources.cdse import CDSECatalog, _centroid, strip_safe
from swathcast.sources.cmr import (
    additional_attribute,
    provider_insert_date,
    search_umm,
)
from swathcast.timeutil import parse_utc, to_odata

LOGGER = logging.getLogger(__name__)

#: S2B_MSIL1C_20260928T035539_N0513_R004_T52XEM_20260928T054827
NAME_RE = re.compile(
    r"^(?P<platform>S2[A-Z])_MSI(?P<level>L1C|L2A)_(?P<start>\d{8}T\d{6})_N(?P<baseline>\d{4})_"
    r"R(?P<orbit>\d{3})_T(?P<tile>\d{2}[A-Z]{3})_(?P<proc>\d{8}T\d{6})$"
)
PRODUCT_TYPES = {"L1C": "S2MSI1C", "L2A": "S2MSI2A"}
SAMPLE_MODULO = 8
EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
HLS_S30 = "C2021957295-LPCLOUD"


def in_sample(tile: str) -> bool:
    return zlib.crc32(tile.encode()) % SAMPLE_MODULO == 0


def parse_name(name: str) -> dict:
    m = NAME_RE.match(strip_safe(name))
    if not m:
        raise ValueError(f"not a Sentinel-2 L1C/L2A product name: {name!r}")
    return m.groupdict()


def acquisition(item: dict) -> Acquisition:
    gid = strip_safe(item["Name"])
    p = parse_name(gid)
    attrs = {a["Name"]: a.get("Value") for a in item.get("Attributes", [])}
    lon, lat = _centroid(item.get("GeoFootprint"))
    orbit = attrs.get("orbitNumber")
    return Acquisition(
        granule_id=gid,
        product_type=p["level"],
        platform=p["platform"],
        mode="MSI",
        polarization=None,
        absolute_orbit=int(orbit) if orbit is not None else None,
        relative_orbit=int(p["orbit"]),
        orbit_direction="descending",
        datatake_id=p["tile"],
        anx_start_s=None,
        sensing_start=parse_utc(item["ContentDate"]["Start"]),
        sensing_stop=parse_utc(item["ContentDate"]["End"]),
        centroid_lon=lon,
        centroid_lat=lat,
        timeliness=f"N{p['baseline']}",
        provider_timestamp=parse_utc(item["PublicationDate"]),
        provider_timestamp_field="PublicationDate",
        coverage_key=p["tile"],
    )


class CDSES2Catalog(CDSECatalog):
    """Newly published Sentinel-2 L1C and L2A products of the sampled tiles."""

    key = "cdse_s2"
    label = "CDSE Sentinel-2"
    product = "L1C+L2A"
    max_products = 60000
    #: ~1,300 products an hour: skip the attributes (orbit number unused)
    expand = None

    def _filter(self, window) -> str:
        start, end = window
        types = " or ".join(
            "Attributes/OData.CSC.StringAttribute/any(a:a/Name eq 'productType'"
            f" and a/OData.CSC.StringAttribute/Value eq '{t}')"
            for t in PRODUCT_TYPES.values()
        )
        return (
            "Collection/Name eq 'SENTINEL-2'"
            f" and PublicationDate gt {to_odata(start)}"
            f" and PublicationDate le {to_odata(end)}"
            f" and ({types})"
        )

    def list_new(self, window) -> list[Acquisition]:
        out = []
        for item in self._list_items(*window, tail=window[1]):
            try:
                p = parse_name(item["Name"])
            except ValueError:
                continue
            if in_sample(p["tile"]):
                out.append(acquisition(item))
        return out

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), 20):
            names = " or ".join(f"Name eq '{gid}.SAFE'" for gid in batch)
            rows = self._get_all(
                {
                    "$filter": f"Collection/Name eq 'SENTINEL-2' and ({names})",
                    "$top": 100,
                }
            )
            for item in rows:
                found[strip_safe(item["Name"])] = Found(
                    provider_timestamp=parse_utc(item["PublicationDate"]),
                    provider_timestamp_field="PublicationDate",
                    provider_id=item["Name"],
                )
        return found


class CDSES2Product(Source):
    distributor = "ESA Copernicus Data Space Ecosystem"
    is_reference = True

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return CDSES2Catalog(session=self.session).lookup(ids)


def _stac_post(session, url: str, body: dict) -> list[dict]:
    """POST a STAC search and follow POST ``next`` links to the end."""
    features: list[dict] = []
    while body is not None:
        response = session.post(url, json=body, timeout=180)
        if not response.ok:
            raise SourceError(
                f"STAC {url} HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        features.extend(payload.get("features", []))
        body = None
        for link in payload.get("links", []):
            if link.get("rel") == "next":
                if link.get("method", "GET") != "POST":
                    raise SourceError(f"STAC {url}: unexpected GET paging")
                url, body = link["href"], link.get("body")
    return features


class EarthSearchS2(Source):
    """Element84 Earth Search, looked up by ``s2:product_uri`` (the ESA name).

    ``created`` is recorded as the provider timestamp (equal to ``updated`` on
    every item checked; not validated as a first-publication time)."""

    distributor = "Element84 Earth Search (AWS)"
    collection = ""
    batch_size = 200

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), self.batch_size):
            body = {
                "collections": [self.collection],
                "query": {"s2:product_uri": {"in": [f"{g}.SAFE" for g in batch]}},
                "limit": 250,
                "fields": {
                    "include": ["id", "properties.s2:product_uri", "properties.created"]
                },
            }
            for f in _stac_post(self.session, EARTH_SEARCH, body):
                props = f.get("properties") or {}
                gid = strip_safe(props.get("s2:product_uri") or "")
                if gid in batch:
                    created = props.get("created")
                    found[gid] = Found(
                        provider_timestamp=parse_utc(created) if created else None,
                        provider_timestamp_field="created" if created else None,
                        provider_id=f["id"],
                    )
        return found


def pc_s2_id(gid: str) -> str:
    """PC item id: the ESA name without the ``_Nxxxx`` baseline."""
    return re.sub(r"_N\d{4}_", "_", gid, count=1)


class PlanetaryS2L2A(Source):
    """Planetary Computer ``sentinel-2-l2a`` by item id. No ingest timestamp:
    first-seen only. On 2026-09-29 it was missing every product CDSE
    published on 2026-09-28 00:00-04:30 UTC (not back-filled 42 h later)."""

    key = "pc_s2_l2a"
    label = "Planetary Computer S2 L2A"
    distributor = "Microsoft Planetary Computer"
    product = "L2A"
    reference_product = "L2A"
    batch_size = 100

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        by_item = {pc_s2_id(g): g for g in ids}
        found: dict[str, Found] = {}
        for batch in chunks(sorted(by_item), self.batch_size):
            body = {
                "collections": ["sentinel-2-l2a"],
                "ids": batch,
                "limit": len(batch),
                "fields": {"include": ["id", "properties.s2:product_uri"]},
            }
            for f in _stac_post(self.session, PC_SEARCH, body):
                gid = by_item.get(f.get("id"))
                if gid:
                    found[gid] = Found(provider_id=f["id"])
        return found


class HLSS30(Source):
    """NASA HLS S30 (LP DAAC, CMR), matched on its ``PRODUCT_URI`` attribute
    (the exact ESA L1C name). Provider timestamp: CMR ProviderDates Insert.
    HLS covers land tiles only, so coverage is learned per tile."""

    key = "hls_s30"
    label = "HLS S30 (LP DAAC)"
    distributor = "NASA LP DAAC (HLS)"
    product = "HLS S30"
    reference_product = "L1C"
    coverage = "learned"
    batch_size = 50

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), self.batch_size):
            params = [
                ("collection_concept_id", HLS_S30),
                ("options[attribute][or]", "true"),
            ]
            params += [("attribute[]", f"string,PRODUCT_URI,{g}.SAFE") for g in batch]
            for item in search_umm(self.session, params):
                umm = item.get("umm", {})
                for uri in additional_attribute(umm, "PRODUCT_URI"):
                    gid = strip_safe(uri)
                    if gid not in batch:
                        continue
                    insert = provider_insert_date(umm)
                    ts = parse_utc(insert) if insert else None
                    prev = found.get(gid)
                    if prev is None or (
                        ts and prev.provider_timestamp and ts < prev.provider_timestamp
                    ):
                        found[gid] = Found(
                            provider_timestamp=ts,
                            provider_timestamp_field="ProviderDates.Insert"
                            if ts
                            else None,
                            provider_id=umm.get("GranuleUR"),
                        )
        return found


def _ref(key: str, label: str, level: str) -> type[Source]:
    return type(
        key,
        (CDSES2Product,),
        {"key": key, "label": label, "product": level, "reference_product": level},
    )


def _es(key: str, label: str, collection: str, level: str) -> type[Source]:
    return type(
        key,
        (EarthSearchS2,),
        {
            "key": key,
            "label": label,
            "collection": collection,
            "product": level,
            "reference_product": level,
        },
    )


SOURCES: list[type[Source]] = [
    _ref("cdse_s2_l1c", "CDSE S2 L1C", "L1C"),
    _ref("cdse_s2_l2a", "CDSE S2 L2A", "L2A"),
    _es("es_s2_l1c", "Earth Search S2 L1C", "sentinel-2-l1c", "L1C"),
    _es("es_s2_l2a", "Earth Search S2 L2A", "sentinel-2-l2a", "L2A"),
    PlanetaryS2L2A,
    HLSS30,
]
CATALOG: type[ReferenceCatalog] = CDSES2Catalog
