"""ASF DAAC Sentinel-1 GRD and SLC, looked up in CMR by scene name."""

from __future__ import annotations

from swathcast.sources.base import Found, Source, chunks
from swathcast.sources.cmr import provider_insert_date, search_umm
from swathcast.timeutil import parse_utc

#: ASF's GranuleUR is the ESA name plus a product suffix. The same scene
#: name also matches a "-METADATA_..." granule in the META collections;
#: those are not the downloadable product and are ignored.
GRANULE_UR_SUFFIX = {"GRD": "-GRD_HD", "SLC": "-SLC"}


class ASFSource(Source):
    distributor = "ASF DAAC (NASA)"
    provider = "ASF"
    batch_size = 200

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        suffix = GRANULE_UR_SUFFIX[self.reference_product]
        wanted = set(ids)
        found: dict[str, Found] = {}
        for batch in chunks(sorted(wanted), self.batch_size):
            params = [("provider", self.provider)] + [
                ("readable_granule_name[]", gid) for gid in batch
            ]
            for item in search_umm(self.session, params):
                umm = item.get("umm", {})
                ur = umm.get("GranuleUR", "")
                if not ur.endswith(suffix):
                    continue
                gid = ur[: -len(suffix)]
                if gid not in wanted:
                    continue
                insert = provider_insert_date(umm)
                found[gid] = Found(
                    provider_timestamp=parse_utc(insert) if insert else None,
                    provider_timestamp_field="ProviderDates[Insert]"
                    if insert
                    else None,
                    provider_id=ur,
                )
        return found


class ASFGRD(ASFSource):
    key = "asf_grd"
    label = "ASF GRD"
    product = "GRD"
    reference_product = "GRD"


class ASFSLC(ASFSource):
    key = "asf_slc"
    label = "ASF SLC"
    product = "SLC"
    reference_product = "SLC"
