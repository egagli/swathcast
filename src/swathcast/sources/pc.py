"""Microsoft Planetary Computer STAC: ``sentinel-1-grd`` and ``sentinel-1-rtc``.

Item ids are derived from the ESA name: the GRD item id drops the 4-hex CRC
suffix, the RTC item id additionally appends ``_rtc``. The item's
``s1:product_identifier`` carries the full ESA name and is used to confirm
the match. PC items expose no publication/ingest timestamp (the only
candidate, ``s1:processing_datetime``, is ESA's processing time), so these
sources are first-seen only. See docs/sources.md.
"""

from __future__ import annotations

from swathcast.sources.base import Found, Source, SourceError, chunks

STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"


def pc_item_id(granule_id: str, collection: str) -> str:
    base = granule_id.rsplit("_", 1)[0]  # drop the CRC
    return f"{base}_rtc" if collection == "sentinel-1-rtc" else base


class PlanetaryComputerSource(Source):
    distributor = "Microsoft Planetary Computer"
    reference_product = "GRD"
    coverage = "learned"
    collection = ""
    batch_size = 100

    def _search(self, item_ids: list[str]) -> list[dict]:
        features: list[dict] = []
        body: dict | None = {
            "collections": [self.collection],
            "ids": item_ids,
            "limit": len(item_ids),
            "fields": {"include": ["id", "properties.s1:product_identifier"]},
        }
        url = STAC_SEARCH
        while body is not None:
            response = self.session.post(url, json=body, timeout=120)
            if not response.ok:
                raise SourceError(
                    f"PC STAC HTTP {response.status_code}: {response.text[:300]}"
                )
            payload = response.json()
            features.extend(payload.get("features", []))
            body = None
            for link in payload.get("links", []):
                if link.get("rel") == "next" and link.get("method", "GET") == "POST":
                    url, body = link["href"], link.get("body")
        return features

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        by_item = {pc_item_id(gid, self.collection): gid for gid in ids}
        found: dict[str, Found] = {}
        for batch in chunks(sorted(by_item), self.batch_size):
            for feature in self._search(batch):
                gid = by_item.get(feature.get("id"))
                if gid is None:
                    continue
                pid = (feature.get("properties") or {}).get("s1:product_identifier")
                found[gid] = Found(
                    provider_id=feature["id"],
                    notes={"product_identifier_matches": pid == gid if pid else None},
                )
        return found


class PCGRD(PlanetaryComputerSource):
    key = "pc_grd"
    label = "Planetary Computer GRD"
    product = "GRD"
    collection = "sentinel-1-grd"


class PCRTC(PlanetaryComputerSource):
    key = "pc_rtc"
    label = "Planetary Computer RTC"
    product = "RTC"
    collection = "sentinel-1-rtc"
