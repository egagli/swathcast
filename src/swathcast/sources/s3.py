"""Sentinel-3 OLCI and SLSTR: CDSE (NRT reference, NTC, Level-2), EUMETSAT, Planetary Computer.

Verified 2026-10-13 (docs/sources.md):

* The universe is every near-real-time (``_NR_``) OLCI Level-1 full
  resolution (``OL_1_EFR``) and SLSTR Level-1 radiance (``SL_1_RBT``) frame
  CDSE publishes, by ``PublicationDate``: about 250 OLCI frames (daylight
  only) and 480 SLSTR frames a day per platform, published about 2 h after
  sensing.
* Products are cut in 3-minute frames on a fixed grid, named in every
  product of the frame: ``..._0179_145_081_3420_...`` is duration 179 s,
  cycle 145, relative orbit 081, frame 3420 (s after the ascending node).
  The same frame's NRT and NTC products, EUMETSAT's copy (processed by
  another centre, so named ``_MAR_`` instead of ``_PS1_``) and its Level-2
  products share platform, instrument, cycle, relative orbit and frame, so
  the granule id is ``S3A_OL_1_EFR_20261013T133354_145_081_3420`` (with the
  reference's start) and sources are matched on platform, instrument, cycle,
  relative orbit and frame, never on the full name (a partial frame's start
  can differ between processings).
* Sources are looked up by sensing day (every product of the day between
  the first and last open frame, paged to the end).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from swathcast.sources.base import (
    Acquisition,
    Found,
    Source,
    SourceError,
)
from swathcast.sources.cdse import CDSECatalog, _centroid
from swathcast.sources.s2 import _stac_post
from swathcast.timeutil import parse_utc, to_odata

LOGGER = logging.getLogger(__name__)

#: reference product type -> (instrument, name fragment)
REFERENCE = {"EFR": ("OL", "_OL_1_EFR____"), "RBT": ("SL", "_SL_1_RBT____")}
MODES = {"OL": "OLCI", "SL": "SLSTR"}
PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
EUMETSAT_SEARCH = "https://api.eumetsat.int/data/search-products/1.0.0/os"
EUMETSAT_EFR = "EO:EUM:DAT:0409"
_STAMP = re.compile(r"^\d{8}T\d{6}$")


def parse_name(name: str) -> dict:
    """Platform, instrument, level, type, start and frame key of any S3 name:
    CDSE 'S3A_OL_1_EFR____20261013T133354_20261013T133654_20261013T151726_0179_145_081_3420_PS1_O_NR_004.SEN3'
    or Planetary Computer 'S3B_OL_2_LFR_20261012T115525_20261012T115707_0102_125_308_4320'."""
    tok = [t for t in name.removesuffix(".SEN3").split("_") if t]
    try:
        plat, inst, level, typ = tok[:4]
        i = 4
        stamps = []
        while _STAMP.match(tok[i]):
            stamps.append(tok[i])
            i += 1
        dur, cycle, rel, frame = tok[i : i + 4]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"not a Sentinel-3 frame product: {name!r}") from exc
    if not (
        plat.startswith("S3")
        and frame.isdigit()
        and rel.isdigit()
        and cycle.isdigit()
        and stamps
    ):
        raise ValueError(f"not a Sentinel-3 frame product: {name!r}")
    rest = tok[i + 4 :]
    return {
        "platform": plat,
        "instrument": inst,
        "level": level,
        "type": typ,
        "start": datetime.strptime(stamps[0], "%Y%m%dT%H%M%S").replace(tzinfo=UTC),
        "duration_s": int(dur),
        "cycle": int(cycle),
        "rel": int(rel),
        "frame": int(frame),
        "timeliness": rest[2] if len(rest) >= 3 else None,
        "key": f"{plat}_{inst}_{int(cycle):03d}_{int(rel):03d}_{int(frame):04d}",
    }


def granule_id(p: dict, ref_type: str) -> str:
    """'S3A_OL_1_EFR_20261013T133354_145_081_3420' (a reference product's frame)."""
    return (
        f"{p['platform']}_{p['instrument']}_1_{ref_type}_{p['start']:%Y%m%dT%H%M%S}"
        f"_{p['cycle']:03d}_{p['rel']:03d}_{p['frame']:04d}"
    )


def key_of_id(gid: str) -> str:
    """'S3A_OL_1_EFR_20261013T133354_145_081_3420' -> 'S3A_OL_145_081_3420'."""
    t = gid.split("_")
    return "_".join([t[0], t[1], *t[5:]])


def sensing_start(gid: str) -> datetime:
    return datetime.strptime(gid.split("_")[4], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


def acquisition(item: dict) -> Acquisition:
    p = parse_name(item["Name"])
    ref = next(k for k, (_, frag) in REFERENCE.items() if frag in item["Name"])
    attrs = {a["Name"]: a.get("Value") for a in item.get("Attributes", [])}
    lon, lat = _centroid(item.get("GeoFootprint"))
    direction = attrs.get("orbitDirection")
    orbit = attrs.get("orbitNumber")
    return Acquisition(
        granule_id=granule_id(p, ref),
        product_type=ref,
        platform=p["platform"],
        mode=MODES[p["instrument"]],
        polarization=None,
        absolute_orbit=int(orbit) if orbit is not None else None,
        relative_orbit=p["rel"],
        orbit_direction=direction.lower() if direction else None,
        datatake_id=f"{p['frame']:04d}",
        anx_start_s=float(p["frame"]),
        sensing_start=parse_utc(item["ContentDate"]["Start"]),
        sensing_stop=parse_utc(item["ContentDate"]["End"]),
        centroid_lon=lon,
        centroid_lat=lat,
        timeliness=p["timeliness"],
        provider_timestamp=parse_utc(item["PublicationDate"]),
        provider_timestamp_field="PublicationDate",
        coverage_key=f"{p['rel']:03d}-{p['frame']:04d}",
    )


class CDSES3Catalog(CDSECatalog):
    """Newly published NRT OLCI EFR and SLSTR RBT frames."""

    key = "cdse_s3"
    label = "CDSE Sentinel-3"
    product = "OLCI EFR + SLSTR RBT (NRT)"
    max_products = 40000
    expand = "Attributes"

    def _filter(self, window) -> str:
        start, end = window
        names = " or ".join(
            f"contains(Name,'{frag}')" for _, frag in REFERENCE.values()
        )
        return (
            "Collection/Name eq 'SENTINEL-3'"
            f" and PublicationDate gt {to_odata(start)}"
            f" and PublicationDate le {to_odata(end)}"
            f" and ({names}) and contains(Name,'_NR_')"
        )

    def list_new(self, window) -> list[Acquisition]:
        out = []
        for item in self._list_items(*window, tail=window[1]):
            try:
                out.append(acquisition(item))
            except (KeyError, ValueError, StopIteration) as exc:
                LOGGER.warning("CDSE S3: skipping %s (%s)", item.get("Name"), exc)
        return out

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for ref, (inst, _) in REFERENCE.items():
            mine = [g for g in ids if g.split("_")[3] == ref]
            if mine:
                src = CDSES3Source(
                    session=self.session,
                    product_type=f"{inst}_1_{ref}",
                    name_filter="contains(Name,'_NR_')",
                )
                found.update(src.lookup(mine))
        return found


class S3Source(Source):
    """A Sentinel-3 product of the same frame as the reference (OLCI EFR or
    SLSTR RBT): every product sensed on the open frames' days (from the
    first to the last of each day), matched on the frame key."""

    reference_product = "EFR"
    product_type = ""  # e.g. "OL_1_EFR", "SL_2_LST"
    #: extra OData condition (timeliness), e.g. "contains(Name,'_NT_')"
    name_filter = ""
    pad = timedelta(minutes=10)

    def window_of(self, ids: list[str]) -> list[tuple[datetime, datetime, dict]]:
        """[(start, end, {key: [ids]})] per sensing day of the ids."""
        per_day: dict = {}
        for gid in ids:
            when = sensing_start(gid)
            entry = per_day.setdefault(when.date(), [when, when, {}])
            entry[0], entry[1] = min(entry[0], when), max(entry[1], when)
            # a frame dumped in two pieces is two reference products: both match
            entry[2].setdefault(key_of_id(gid), []).append(gid)
        return [(a - self.pad, b + self.pad, m) for a, b, m in per_day.values()]

    def products(
        self, start: datetime, end: datetime
    ) -> list[tuple[str, datetime | None, str | None]]:
        """[(name, provider timestamp, timestamp field)] sensed in [start, end]."""
        raise NotImplementedError

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for start, end, by_key in self.window_of(ids):
            for name, ts, field in self.products(start, end):
                try:
                    k = parse_name(name)["key"]
                except ValueError:
                    continue
                for gid in by_key.get(k, ()):
                    prev = found.get(gid)
                    if prev is None or (
                        ts and prev.provider_timestamp and ts < prev.provider_timestamp
                    ):
                        found[gid] = Found(
                            provider_timestamp=ts,
                            provider_timestamp_field=field,
                            provider_id=name,
                        )
        return found


class CDSES3Source(S3Source):
    distributor = "ESA Copernicus Data Space Ecosystem"
    product_type = ""

    def products(self, start, end):
        types = self.product_type or "OL_1_EFR"
        flt = (
            "Collection/Name eq 'SENTINEL-3'"
            f" and ContentDate/Start ge {to_odata(start)}"
            f" and ContentDate/Start le {to_odata(end)}"
            f" and contains(Name,'_{types}_')"
        )
        if self.name_filter:
            flt += f" and {self.name_filter}"
        rows = CDSECatalog(session=self.session)._get_all(
            {"$filter": flt, "$top": 1000, "$orderby": "ContentDate/Start asc"}
        )
        return [
            (r["Name"], parse_utc(r["PublicationDate"]), "PublicationDate")
            for r in rows
        ]


class PCS3Source(S3Source):
    distributor = "Microsoft Planetary Computer"
    collection = ""

    def products(self, start, end):
        body = {
            "collections": [self.collection],
            "datetime": f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}",
            "limit": 1000,
            "fields": {"include": ["id"]},
        }
        return [
            (f["id"], None, None) for f in _stac_post(self.session, PC_SEARCH, body)
        ]


class EumetsatS3Source(S3Source):
    """EUMETSAT Data Store (OpenSearch, anonymous search; downloads need an
    EUMETSAT account). ``updated`` is kept as the provider timestamp."""

    distributor = "EUMETSAT Data Store"
    collection = EUMETSAT_EFR
    page = 500
    max_items = 20000

    def products(self, start, end):
        out, si = [], 0
        while True:
            r = self.session.get(
                EUMETSAT_SEARCH,
                params={
                    "format": "json",
                    "pi": self.collection,
                    "dtstart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "dtend": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "si": si,
                    "c": self.page,
                },
                timeout=120,
            )
            if not r.ok:
                raise SourceError(f"EUMETSAT HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
            feats = payload.get("features") or []
            for f in feats:
                p = f.get("properties") or {}
                up = p.get("updated")
                out.append(
                    (
                        p.get("identifier") or f.get("id"),
                        parse_utc(up) if up else None,
                        "updated",
                    )
                )
            si += len(feats)
            if not feats or si >= int(payload.get("totalResults", 0)):
                break
            if si > self.max_items:
                raise SourceError(f"EUMETSAT search exceeded {self.max_items} items")
        return out


class CDSES3Product(Source):
    distributor = "ESA Copernicus Data Space Ecosystem"
    is_reference = True

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return CDSES3Catalog(session=self.session).lookup(ids)


def _ref(key, label, ref):
    return type(
        key,
        (CDSES3Product,),
        {
            "key": key,
            "label": label,
            "product": f"{label.split()[-2]} NRT",
            "reference_product": ref,
        },
    )


def _src(base, key, label, ref, product, coverage="full", **attrs):
    return type(
        key,
        (base,),
        {
            "key": key,
            "label": label,
            "reference_product": ref,
            "product": product,
            "coverage": coverage,
            **attrs,
        },
    )


SOURCES: list[type[Source]] = [
    _ref("cdse_s3_efr", "CDSE OLCI EFR (NRT)", "EFR"),
    _ref("cdse_s3_rbt", "CDSE SLSTR RBT (NRT)", "RBT"),
    _src(
        CDSES3Source,
        "cdse_s3_efr_ntc",
        "CDSE OLCI EFR (NTC)",
        "EFR",
        "OL_1_EFR NTC",
        product_type="OL_1_EFR",
        name_filter="contains(Name,'_NT_')",
    ),
    _src(
        CDSES3Source,
        "cdse_s3_rbt_ntc",
        "CDSE SLSTR RBT (NTC)",
        "RBT",
        "SL_1_RBT NTC",
        product_type="SL_1_RBT",
        name_filter="contains(Name,'_NT_')",
    ),
    _src(EumetsatS3Source, "eumetsat_s3_efr", "EUMETSAT OLCI EFR", "EFR", "OL_1_EFR"),
    _src(
        CDSES3Source,
        "cdse_s3_lfr",
        "CDSE OLCI L2 LFR",
        "EFR",
        "OL_2_LFR (land)",
        "learned",
        product_type="OL_2_LFR",
    ),
    _src(
        PCS3Source,
        "pc_s3_lfr",
        "Planetary Computer OLCI LFR",
        "EFR",
        "OL_2_LFR (land)",
        "learned",
        collection="sentinel-3-olci-lfr-l2-netcdf",
    ),
    _src(
        CDSES3Source,
        "cdse_s3_lst",
        "CDSE SLSTR L2 LST",
        "RBT",
        "SL_2_LST",
        "learned",
        product_type="SL_2_LST",
    ),
    _src(
        PCS3Source,
        "pc_s3_lst",
        "Planetary Computer SLSTR LST",
        "RBT",
        "SL_2_LST",
        "learned",
        collection="sentinel-3-slstr-lst-l2-netcdf",
    ),
]
CATALOG = CDSES3Catalog
