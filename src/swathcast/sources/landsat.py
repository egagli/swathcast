"""Landsat 8/9 Collection 2: USGS STAC reference, Earth Search, Planetary Computer, HLS L30.

Verified 2026-09-29 (docs/sources.md):

* The reference is USGS's public STAC (``landsatlook.usgs.gov/stac-server``),
  collections ``landsat-c2l1`` (L1) and ``landsat-c2l2-sr`` (L2 surface
  reflectance), listed by their ``created`` time. ``datetime`` (scene centre)
  is the sensing time. About 800 L1 and 650 L2 scenes a day.
* **Landsat 8 real-time (RT) products are not in the public STAC**: L8 scenes
  appear only as T1/T2, 7-12 days after sensing. So Landsat 8 latency here is
  the delay to a *tiered* product, which is what an anonymous user can find.
* Earth Search and Planetary Computer ``landsat-c2-l2`` item ids are the USGS
  L2 id without the processing date and the ``_SR`` suffix.
* HLS L30 granules name their source scene in ``LANDSAT_PRODUCT_ID`` (one
  scene gives several MGRS-tile granules); for Landsat 8 that is often the RT
  product, which is not in this universe.
"""

from __future__ import annotations

import logging

from swathcast.sources.base import (
    Acquisition,
    Found,
    ReferenceCatalog,
    Source,
    SourceError,
    chunks,
)
from swathcast.sources.cmr import (
    additional_attribute,
    provider_insert_date,
    search_umm,
)
from swathcast.sources.s2 import _stac_post
from swathcast.timeutil import parse_utc

LOGGER = logging.getLogger(__name__)

USGS_STAC = "https://landsatlook.usgs.gov/stac-server/search"
EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
HLS_L30 = "C2021957657-LPCLOUD"
COLLECTIONS = {"L1": "landsat-c2l1", "L2": "landsat-c2l2-sr"}
PLATFORMS = {"LANDSAT_8": "L8", "LANDSAT_9": "L9"}


def l2_mirror_id(usgs_id: str) -> str:
    """LC09_L2SR_084110_20260927_20260928_02_T2_SR -> LC09_L2SR_084110_20260927_02_T2"""
    parts = usgs_id.removesuffix("_SR").split("_")
    return "_".join(parts[:4] + parts[5:7])


def acquisition(feature: dict, product_type: str) -> Acquisition:
    p = feature["properties"]
    path, row = int(p["landsat:wrs_path"]), int(p["landsat:wrs_row"])
    bbox = feature.get("bbox") or [None] * 4
    lon = (bbox[0] + bbox[2]) / 2 if bbox[0] is not None else None
    lat = (bbox[1] + bbox[3]) / 2 if bbox[1] is not None else None
    t = parse_utc(p["datetime"])
    return Acquisition(
        granule_id=feature["id"],
        product_type=product_type,
        platform=PLATFORMS[p["platform"]],
        mode="OLI_TIRS",
        polarization=None,
        absolute_orbit=None,
        relative_orbit=path,
        orbit_direction="descending" if row <= 122 else "ascending",
        datatake_id=f"{path:03d}{row:03d}",
        anx_start_s=None,
        sensing_start=t,
        sensing_stop=t,
        centroid_lon=round(lon, 4) if lon is not None else None,
        centroid_lat=round(lat, 4) if lat is not None else None,
        timeliness=p.get("landsat:collection_category"),
        provider_timestamp=parse_utc(p["created"]) if p.get("created") else None,
        provider_timestamp_field="created",
        coverage_key=f"{path:03d}{row:03d}",
    )


class USGSLandsatCatalog(ReferenceCatalog):
    key = "usgs_landsat"
    label = "USGS STAC Landsat"
    distributor = "USGS EROS (LandsatLook STAC)"
    product = "L1+L2"
    max_products = 20000

    def _list(self, collection: str, window) -> list[dict]:
        """Items created in (start, end]: sorted newest first, paged until
        past the window start. (A ``created`` query returns nothing on
        ``landsat-c2l2-sr``, checked 2026-09-29, although sorting on it works.)"""
        start, end = window
        body: dict | None = {
            "collections": [collection],
            "sortby": [{"field": "properties.created", "direction": "desc"}],
            "limit": 500,
        }
        url, out = USGS_STAC, []
        while body is not None:
            response = self.session.post(url, json=body, timeout=180)
            if not response.ok:
                raise SourceError(
                    f"USGS STAC HTTP {response.status_code}: {response.text[:300]}"
                )
            payload = response.json()
            page = payload.get("features", [])
            done = False
            for f in page:
                created = parse_utc(f["properties"]["created"])
                if created <= start:
                    done = True
                    break
                if created <= end and f["properties"].get("platform") in PLATFORMS:
                    out.append(f)
            if done or not page:
                break
            if len(out) > self.max_products:
                raise SourceError(
                    f"USGS STAC listing exceeded {self.max_products} items"
                )
            body = None
            for link in payload.get("links", []):
                if link.get("rel") == "next" and link.get("method", "GET") == "POST":
                    url, body = link["href"], link.get("body")
        return out

    def list_new(self, window) -> list[Acquisition]:
        out = []
        for ptype, collection in COLLECTIONS.items():
            for f in self._list(collection, window):
                try:
                    out.append(acquisition(f, ptype))
                except (KeyError, ValueError) as exc:
                    LOGGER.warning("USGS STAC: skipping %s: %s", f.get("id"), exc)
        return out

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), 100):
            body = {
                "collections": list(COLLECTIONS.values()),
                "ids": batch,
                "limit": len(batch),
            }
            for f in _stac_post(self.session, USGS_STAC, body):
                created = (f.get("properties") or {}).get("created")
                found[f["id"]] = Found(
                    provider_timestamp=parse_utc(created) if created else None,
                    provider_timestamp_field="created",
                    provider_id=f["id"],
                )
        return found


class USGSLandsatProduct(Source):
    distributor = "USGS EROS (LandsatLook STAC)"
    is_reference = True

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return USGSLandsatCatalog(session=self.session).lookup(ids)


class LandsatL2Mirror(Source):
    """A STAC mirror of USGS Level-2 (Earth Search, Planetary Computer)."""

    reference_product = "L2"
    product = "L2 SR"
    url = ""
    collection = "landsat-c2-l2"
    batch_size = 100

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        by_item = {l2_mirror_id(g): g for g in ids}
        found: dict[str, Found] = {}
        for batch in chunks(sorted(by_item), self.batch_size):
            body = {"collections": [self.collection], "ids": batch, "limit": len(batch)}
            for f in _stac_post(self.session, self.url, body):
                gid = by_item.get(f.get("id"))
                if not gid:
                    continue
                created = (f.get("properties") or {}).get("created")
                found[gid] = Found(
                    provider_timestamp=parse_utc(created) if created else None,
                    provider_timestamp_field="created" if created else None,
                    provider_id=f["id"],
                )
        return found


class EarthSearchLandsat(LandsatL2Mirror):
    key = "es_landsat_l2"
    label = "Earth Search Landsat L2"
    distributor = "Element84 Earth Search (AWS)"
    url = EARTH_SEARCH


class PlanetaryLandsat(LandsatL2Mirror):
    key = "pc_landsat_l2"
    label = "Planetary Computer Landsat L2"
    distributor = "Microsoft Planetary Computer"
    url = PC_SEARCH


class HLSL30(Source):
    """NASA HLS L30, matched on ``LANDSAT_PRODUCT_ID`` = the L1 scene id.
    Seen when the first of the scene's MGRS-tile granules appears."""

    key = "hls_l30"
    label = "HLS L30 (LP DAAC)"
    distributor = "NASA LP DAAC (HLS)"
    product = "HLS L30"
    reference_product = "L1"
    coverage = "learned"
    batch_size = 50

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), self.batch_size):
            params = [
                ("collection_concept_id", HLS_L30),
                ("options[attribute][or]", "true"),
            ]
            params += [("attribute[]", f"string,LANDSAT_PRODUCT_ID,{g}") for g in batch]
            for item in search_umm(self.session, params):
                umm = item.get("umm", {})
                for gid in additional_attribute(umm, "LANDSAT_PRODUCT_ID"):
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


def _ref(key: str, label: str, ptype: str) -> type[Source]:
    return type(
        key,
        (USGSLandsatProduct,),
        {"key": key, "label": label, "product": ptype, "reference_product": ptype},
    )


SOURCES: list[type[Source]] = [
    _ref("usgs_landsat_l1", "USGS Landsat L1", "L1"),
    _ref("usgs_landsat_l2", "USGS Landsat L2 SR", "L2"),
    EarthSearchLandsat,
    PlanetaryLandsat,
    HLSL30,
]
CATALOG: type[ReferenceCatalog] = USGSLandsatCatalog
