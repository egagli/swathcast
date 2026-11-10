"""NISAR at ASF (via NASA CMR): L0B and RSLC as reference, GCOV as a source.

Verified 2026-09-29 and 2026-11-09 (docs/sources.md): searchable anonymously
(downloads need Earthdata login). ASF moved NISAR from its ``*_PROVISIONAL_V1``
collections to ``*_V1`` ones on 2026-10-21 (the last PROVISIONAL product was
sensed 20:37 UTC, the first V1 one 17:07 UTC that day): same granule names,
attributes and ProviderDates, a new collection id and download folder. Both
are listed and looked up (V1 first), so granules tracked before the move still
resolve and the latency history runs on across it.
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

#: product -> (V1, PROVISIONAL_V1) collection ids
COLLECTIONS = {
    "L0B": ("C3622265756-ASF", "C2853089814-ASF"),  # NISAR_L0B_RRSD_V1, _PROVISIONAL_V1
    "RSLC": ("C3622236985-ASF", "C2853145197-ASF"),  # NISAR_L1_RSLC_V1, _PROVISIONAL_V1
}
GCOV = ("C3622241360-ASF", "C2854338529-ASF")  # NISAR_L2_GCOV_V1, _PROVISIONAL_V1
#: NISAR_UR_L1: JPL's urgent-response pipeline (RSLC, RIFG, RUNW, ROFF made
#: for named requests, e.g. a cyclone): checked 2026-11-10, 402 of 413 UR
#: RSLCs sensed 2026-10-25..31 had a standard RSLC of the same frame and
#: start second, and the UR one was in CMR first for 94 % of them (median
#: 175 h earlier).
UR_L1 = "C2887499907-ASF"
#: the frame and start second shared by a standard RSLC and its UR twin (the
#: mode, polarization, end and processing letters can differ)
FRAME_START_RE = re.compile(
    r"^NISAR_L1_(?:PR|UR)_RSLC_(?P<cycle>\d{3})_(?P<track>\d{3})_(?P<dir>[AD])_(?P<frame>\d{3})_\w{4}_\w{4}_\w_(?P<start>\d{8}T\d{6})_"
)
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


def _center(umm: dict):
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
    lon, lat = _center(umm)
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
        for ptype, concepts in COLLECTIONS.items():
            params = [("collection_concept_id", c) for c in concepts] + [
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
            params += [
                ("collection_concept_id", c) for cs in COLLECTIONS.values() for c in cs
            ]
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
            params = [("collection_concept_id", c) for c in GCOV] + [
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


def ur_pattern(rslc: str) -> str | None:
    """CMR name pattern of a standard RSLC's urgent-response twin."""
    m = FRAME_START_RE.match(rslc)
    if not m:
        return None
    g = m.groupdict()
    return f"NISAR_L1_UR_RSLC_{g['cycle']}_{g['track']}_{g['dir']}_{g['frame']}_*_{g['start']}_*"


class NISARRSLCUrgent(Source):
    """The RSLC of the same frame from JPL's urgent-response pipeline
    (``NISAR_UR_L1``): made only for requested frames, so its coverage is
    learned; matched on frame and start second."""

    key = "asf_nisar_rslc_ur"
    label = "ASF NISAR RSLC, urgent response"
    distributor = "ASF DAAC (NASA CMR), JPL urgent-response pipeline"
    product = "RSLC (UR)"
    reference_product = "RSLC"
    coverage = "learned"
    batch_size = 100

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        by_key = {}
        for g in ids:
            m = FRAME_START_RE.match(g)
            if m:
                by_key.setdefault(m.groups(), []).append(g)
        found: dict[str, Found] = {}
        pats = sorted({ur_pattern(g) for g in ids if ur_pattern(g)})
        for batch in chunks(pats, self.batch_size):
            params = [
                ("collection_concept_id", UR_L1),
                ("options[readable_granule_name][pattern]", "true"),
            ] + [("readable_granule_name[]", p) for p in batch]
            for item in search_umm(self.session, params):
                umm = item.get("umm", {})
                m = FRAME_START_RE.match(umm.get("GranuleUR", ""))
                if not m:
                    continue
                insert = provider_insert_date(umm)
                ts = parse_utc(insert) if insert else None
                for gid in by_key.get(m.groups(), []):
                    prev = found.get(gid)
                    if prev is None or (
                        ts and prev.provider_timestamp and ts < prev.provider_timestamp
                    ):
                        found[gid] = Found(
                            provider_timestamp=ts,
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
    NISARRSLCUrgent,
]
CATALOG: type[ReferenceCatalog] = NISARCatalog
