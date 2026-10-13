"""NISAR at ASF (via NASA CMR): L0B and RSLC as reference, GCOV as a source.

Verified 2026-09-29 (docs/sources.md): the current science collections are
PROVISIONAL_V1 and searchable anonymously (downloads need Earthdata login).
The universe is every L0B RRSD (the first product, per datatake segment) and
every L1 RSLC (per frame) that CMR lists by ``created_at``. A GCOV frame has
the same name as its RSLC after the product prefix
(``NISAR_L1_PR_RSLC_<rest>`` -> ``NISAR_L2_PR_GCOV_<rest>``) and names the
RSLC in ``InputGranules``. Provider timestamp: ProviderDates Insert.
"""

from __future__ import annotations

import logging
import re

from swathcast.sources.base import Acquisition, Found, ReferenceCatalog, Source, chunks
from swathcast.sources.cmr import provider_insert_date, search_umm
from swathcast.timeutil import parse_utc, to_iso

LOGGER = logging.getLogger(__name__)

COLLECTIONS = {"L0B": "C2853089814-ASF", "RSLC": "C2853145197-ASF"}
GCOV = "C2854338529-ASF"
#: NISAR_L1_PR_RSLC_031_134_D_059_2005_DHDH_A_20260927T115943_20260927T120019_P05023_N_F_J_001
RSLC_RE = re.compile(
    r"^NISAR_L1_PR_RSLC_(?P<cycle>\d{3})_(?P<track>\d{3})_(?P<dir>[AD])_(?P<frame>\d{3})_(?P<mode>\d{4})_(?P<pol>[A-Z]{4})_"
)
#: NISAR_L0_PR_RRSD_031_134_D_148S_20260927T115720_20260927T120818_P05023_F_J_001
L0B_RE = re.compile(
    r"^NISAR_L0_PR_RRSD_(?P<cycle>\d{3})_(?P<track>\d{3})_(?P<dir>[AD])_(?P<seg>\w+?)_\d{8}T"
)


def gcov_name(rslc: str) -> str:
    return rslc.replace("NISAR_L1_PR_RSLC_", "NISAR_L2_PR_GCOV_", 1)


def _centre(umm: dict):
    try:
        poly = umm["SpatialExtent"]["HorizontalSpatialDomain"]["Geometry"]["GPolygons"][
            0
        ]
        pts = poly["Boundary"]["Points"][:-1]
    except (KeyError, IndexError, TypeError):
        return None, None
    lons = [p["Longitude"] for p in pts]
    if max(lons) - min(lons) > 180:
        lons = [x + 360 if x < 0 else x for x in lons]
    lon = sum(lons) / len(lons)
    return round(((lon + 180) % 360) - 180, 4), round(
        sum(p["Latitude"] for p in pts) / len(pts), 4
    )


def acquisition(item: dict, ptype: str) -> Acquisition:
    umm = item["umm"]
    gid = umm["GranuleUR"]
    m = (RSLC_RE if ptype == "RSLC" else L0B_RE).match(gid)
    if not m:
        raise ValueError(f"unexpected NISAR {ptype} name {gid!r}")
    g = m.groupdict()
    rng = umm["TemporalExtent"]["RangeDateTime"]
    lon, lat = _centre(umm)
    insert = provider_insert_date(umm)
    frame = g.get("frame") or g.get("seg")
    return Acquisition(
        granule_id=gid,
        product_type=ptype,
        platform="NISAR",
        mode=g.get("mode") or "L0B",
        polarization=(g.get("pol") or "")[:2] or None,
        absolute_orbit=None,
        relative_orbit=int(g["track"]),
        orbit_direction="ascending" if g["dir"] == "A" else "descending",
        datatake_id=frame,
        anx_start_s=None,
        sensing_start=parse_utc(rng["BeginningDateTime"]),
        sensing_stop=parse_utc(rng["EndingDateTime"]),
        centroid_lon=lon,
        centroid_lat=lat,
        timeliness=f"cycle {g['cycle']}",
        provider_timestamp=parse_utc(insert) if insert else None,
        provider_timestamp_field="ProviderDates.Insert",
        coverage_key=f"{int(g['track']):03d}-{frame}",
    )


class NISARCatalog(ReferenceCatalog):
    key = "asf_nisar"
    label = "ASF NISAR"
    distributor = "ASF DAAC (NASA CMR)"
    product = "L0B+RSLC"

    def list_new(self, window) -> list[Acquisition]:
        start, end = window
        out = []
        for ptype, concept in COLLECTIONS.items():
            params = [
                ("collection_concept_id", concept),
                ("created_at", f"{to_iso(start)},{to_iso(end)}"),
            ]
            for item in search_umm(self.session, params):
                try:
                    out.append(acquisition(item, ptype))
                except (KeyError, ValueError) as exc:
                    LOGGER.warning("NISAR: skipping %s", exc)
        return out

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), 100):
            params = [("readable_granule_name[]", g) for g in batch]
            params += [("collection_concept_id", c) for c in COLLECTIONS.values()]
            for item in search_umm(self.session, params):
                umm = item.get("umm", {})
                insert = provider_insert_date(umm)
                found[umm["GranuleUR"]] = Found(
                    provider_timestamp=parse_utc(insert) if insert else None,
                    provider_timestamp_field="ProviderDates.Insert",
                    provider_id=umm["GranuleUR"],
                )
        return found


class NISARProduct(Source):
    distributor = "ASF DAAC (NASA CMR)"
    is_reference = True

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return NISARCatalog(session=self.session).lookup(ids)


class NISARGCOV(Source):
    key = "asf_nisar_gcov"
    label = "ASF NISAR GCOV"
    distributor = "ASF DAAC (NASA CMR)"
    product = "GCOV"
    reference_product = "RSLC"
    batch_size = 100

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        by_name = {gcov_name(g): g for g in ids}
        found: dict[str, Found] = {}
        for batch in chunks(sorted(by_name), self.batch_size):
            params = [("collection_concept_id", GCOV)] + [
                ("readable_granule_name[]", g) for g in batch
            ]
            for item in search_umm(self.session, params):
                umm = item.get("umm", {})
                gid = by_name.get(umm.get("GranuleUR"))
                if gid:
                    insert = provider_insert_date(umm)
                    found[gid] = Found(
                        provider_timestamp=parse_utc(insert) if insert else None,
                        provider_timestamp_field="ProviderDates.Insert",
                        provider_id=umm["GranuleUR"],
                    )
        return found


def _ref(key: str, label: str, ptype: str) -> type[Source]:
    return type(
        key,
        (NISARProduct,),
        {"key": key, "label": label, "product": ptype, "reference_product": ptype},
    )


SOURCES: list[type[Source]] = [
    _ref("asf_nisar_l0b", "ASF NISAR L0B", "L0B"),
    _ref("asf_nisar_rslc", "ASF NISAR RSLC", "RSLC"),
    NISARGCOV,
]
CATALOG: type[ReferenceCatalog] = NISARCatalog
