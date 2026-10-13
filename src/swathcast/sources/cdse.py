"""ESA Copernicus Data Space Ecosystem (CDSE) OData catalogue.

CDSE is the reference catalog: the tracking universe is every IW GRDH / IW
SLC product it publishes. Verified field notes are in docs/sources.md.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from swathcast.sources.base import (
    Acquisition,
    Found,
    ReferenceCatalog,
    Source,
    SourceError,
    chunks,
)
from swathcast.timeutil import parse_utc, to_odata

LOGGER = logging.getLogger(__name__)

ODATA_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

#: ESA product-name layout, e.g.
#: S1C_IW_GRDH_1SDV_20260928T215218_20260928T215232_009653_01336F_1287
NAME_RE = re.compile(
    r"^(?P<platform>S1[A-Z])_(?P<mode>IW)_(?P<type>GRDH|SLC_)_1S(?P<pol>DV|DH|SV|SH|HH|VV|HV|VH)_"
    r"(?P<start>\d{8}T\d{6})_(?P<stop>\d{8}T\d{6})_(?P<orbit>\d{6})_(?P<datatake>[0-9A-F]{6})_(?P<crc>[0-9A-F]{4})$"
)

#: name fragment -> product type used throughout the package
PRODUCT_FILTERS = {"GRD": "_IW_GRDH_1S", "SLC": "_IW_SLC__1S"}


def parse_name(granule_id: str) -> dict:
    match = NAME_RE.match(granule_id)
    if not match:
        raise ValueError(f"not an IW GRDH/SLC product name: {granule_id!r}")
    return match.groupdict()


def strip_safe(name: str) -> str:
    return name[:-5] if name.endswith(".SAFE") else name


def _centroid(footprint: dict | None) -> tuple[float | None, float | None]:
    try:
        ring = footprint["coordinates"][0]
        if footprint["type"] == "MultiPolygon":
            ring = footprint["coordinates"][0][0]
    except (KeyError, IndexError, TypeError):
        return None, None
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if not pts:
        return None, None
    lons = [p[0] for p in pts]
    # Footprints across the antimeridian: unwrap before averaging.
    if max(lons) - min(lons) > 180:
        lons = [lon + 360 if lon < 0 else lon for lon in lons]
    lon = sum(lons) / len(lons)
    if lon > 180:
        lon -= 360
    return round(lon, 4), round(sum(p[1] for p in pts) / len(pts), 4)


def acquisition_from_odata(item: dict) -> Acquisition:
    granule_id = strip_safe(item["Name"])
    parts = parse_name(granule_id)
    attrs = {a["Name"]: a.get("Value") for a in item.get("Attributes", [])}
    product_type = "GRD" if parts["type"] == "GRDH" else "SLC"
    absolute_orbit = int(parts["orbit"])
    relative = attrs.get("relativeOrbitNumber")
    relative = int(relative) if relative is not None else None
    direction = attrs.get("orbitDirection")
    anx_ms = attrs.get("startTimeFromAscendingNode")
    lon, lat = _centroid(item.get("GeoFootprint"))
    return Acquisition(
        granule_id=granule_id,
        product_type=product_type,
        platform=parts["platform"],
        mode=parts["mode"],
        polarization=parts["pol"],
        absolute_orbit=absolute_orbit,
        relative_orbit=relative,
        orbit_direction=direction.lower() if direction else None,
        datatake_id=parts["datatake"],
        anx_start_s=float(anx_ms) / 1000.0 if anx_ms is not None else None,
        sensing_start=parse_utc(item["ContentDate"]["Start"]),
        sensing_stop=parse_utc(item["ContentDate"]["End"]),
        centroid_lon=lon,
        centroid_lat=lat,
        timeliness=attrs.get("timeliness"),
        provider_timestamp=parse_utc(item["PublicationDate"]),
        provider_timestamp_field="PublicationDate",
    )


class CDSECatalog(ReferenceCatalog):
    """Lists newly *published* IW GRDH and SLC products (``_COG`` excluded)."""

    key = "cdse"
    label = "CDSE"
    distributor = "ESA Copernicus Data Space Ecosystem"
    product = "GRD+SLC"
    page_size = 1000
    #: windows with more products than this are split (CDSE pages stop ~11,000)
    chunk_limit = 5000
    #: safety valve: a single listing that pages beyond this is an error
    max_products = 20000
    #: OData $expand (Sentinel-1 needs the orbit attributes)
    expand: str | None = "Attributes"
    #: products this new may be counted before they can be listed (seen
    #: 2026-10-04: 12 of 356 missing, all published in the last ~3 min)
    settle = timedelta(minutes=15)

    def _filter(self, window: tuple[datetime, datetime]) -> str:
        start, end = window
        names = " or ".join(
            f"contains(Name,'{frag}')" for frag in PRODUCT_FILTERS.values()
        )
        return (
            "Collection/Name eq 'SENTINEL-1'"
            f" and PublicationDate gt {to_odata(start)}"
            f" and PublicationDate le {to_odata(end)}"
            f" and ({names})"
            " and not endswith(Name,'_COG.SAFE')"
        )

    def _get_all(self, params: dict) -> list[dict]:
        rows: list[dict] = []
        url: str | None = ODATA_URL
        while url:
            response = self.session.get(url, params=params, timeout=120)
            if not response.ok:
                raise SourceError(
                    f"CDSE OData HTTP {response.status_code}: {response.text[:300]}"
                )
            payload = response.json()
            rows.extend(payload.get("value", []))
            url = payload.get("@odata.nextLink")
            params = None  # nextLink already carries the query
            if len(rows) > self.max_products:
                raise SourceError(f"CDSE listing exceeded {self.max_products} products")
        return rows

    def _count(self, flt: str) -> int:
        response = self.session.get(
            ODATA_URL, params={"$filter": flt, "$top": 1, "$count": "true"}, timeout=120
        )
        if not response.ok:
            raise SourceError(
                f"CDSE OData count HTTP {response.status_code}: {response.text[:300]}"
            )
        return int(response.json()["@odata.count"])

    def _list_items(
        self,
        start: datetime,
        end: datetime,
        depth: int = 0,
        tail: datetime | None = None,
    ) -> list[dict]:
        """All products in (start, end], splitting the window below the paging cap.

        CDSE's OData stops returning ``@odata.nextLink`` after ~11,000 rows of
        one query (verified 2026-09-29: 26,662 matches, 11,000 returned, no
        error). So count first, bisect windows that are too large, and refuse
        to return fewer rows than the count.

        One exception: products published in the last ``settle`` before the
        listing's end (``tail``, the poller's "now") can be counted before they
        are listed. A short listing is accepted only when everything published
        before ``tail - settle`` is complete (counted again); the rest is listed
        again by the next run, whose window overlaps this one by hours.
        """
        flt = self._filter((start, end))
        expected = self._count(flt)
        if expected == 0:
            return []
        if (
            expected > self.chunk_limit
            and (end - start) > timedelta(minutes=1)
            and depth < 20
        ):
            mid = start + (end - start) / 2
            return self._list_items(start, mid, depth + 1, tail) + self._list_items(
                mid, end, depth + 1, tail
            )
        params = {
            "$filter": flt,
            "$top": self.page_size,
            "$orderby": "PublicationDate asc",
        }
        if self.expand:
            params["$expand"] = self.expand
        rows = self._get_all(params)
        if len(rows) < expected and self._settled(rows, start, end, tail):
            LOGGER.warning(
                "CDSE: %d of %d products listed for %s..%s; the missing ones were"
                " published in the last %s and will be listed by the next run",
                len(rows),
                expected,
                start,
                end,
                self.settle,
            )
            return rows
        if len(rows) < expected:
            raise SourceError(
                f"CDSE listing truncated: {len(rows)} of {expected} products for {start}..{end}"
            )
        return rows

    def _settled(
        self, rows: list[dict], start: datetime, end: datetime, tail: datetime | None
    ) -> bool:
        """True when every product published before ``tail - settle`` is in ``rows``."""
        if tail is None or end != tail:
            return False
        cutoff = tail - self.settle
        if cutoff <= start:
            return True
        before = sum(1 for r in rows if parse_utc(r["PublicationDate"]) <= cutoff)
        return before >= self._count(self._filter((start, cutoff)))

    def list_new(self, window: tuple[datetime, datetime]) -> list[Acquisition]:
        acquisitions = []
        for item in self._list_items(*window, tail=window[1]):
            try:
                acquisitions.append(acquisition_from_odata(item))
            except (ValueError, KeyError) as exc:
                LOGGER.warning(
                    "CDSE: skipping unparseable product %s: %s", item.get("Name"), exc
                )
        return acquisitions

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        found: dict[str, Found] = {}
        for batch in chunks(sorted(set(ids)), 20):
            names = " or ".join(f"Name eq '{gid}.SAFE'" for gid in batch)
            rows = self._get_all(
                {
                    "$filter": f"Collection/Name eq 'SENTINEL-1' and ({names})",
                    "$top": 100,
                }
            )
            for item in rows:
                gid = strip_safe(item["Name"])
                found[gid] = Found(
                    provider_timestamp=parse_utc(item["PublicationDate"]),
                    provider_timestamp_field="PublicationDate",
                    provider_id=item["Name"],
                )
        return found


class CDSEProduct(Source):
    """CDSE as a *source*: its rows are created already seen by the listing.

    ``lookup`` exists for completeness (and live smoke tests); the tracker
    never needs to call it, because a product enters the universe exactly
    when CDSE lists it.
    """

    distributor = "ESA Copernicus Data Space Ecosystem"
    is_reference = True
    provider_timestamp_validated = False

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        return CDSECatalog(session=self.session).lookup(ids)
