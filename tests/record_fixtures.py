"""Re-record the offline API fixtures in tests/fixtures/ from the live APIs.

Run manually (``pixi run python tests/record_fixtures.py``); the unit tests
never touch the network. Responses are trimmed (footprints, URLs) to keep
the fixtures small, but field names and value formats are left as served.
"""

import json
from pathlib import Path

import requests

HERE = Path(__file__).parent / "fixtures"
GRD = "S1C_IW_GRDH_1SDV_20260925T193709_20260925T193733_009608_0131FB_55E1"
SLC = "S1C_IW_SLC__1SDV_20260925T072131_20260925T072202_009600_0131B2_E986"


def dump(name, payload):
    (HERE / name).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    print("wrote", name)


def trim_umm(item):
    umm = item["umm"]
    umm.pop("RelatedUrls", None)
    umm.pop("SpatialExtent", None)
    return item


def main():
    s = requests.Session()
    r = s.get(
        "https://catalogue.dataspace.copernicus.eu/odata/v1/Products",
        params={
            "$filter": f"Name eq '{GRD}.SAFE' or Name eq '{SLC}.SAFE'",
            "$expand": "Attributes",
        },
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    for v in payload["value"]:
        v.pop("Footprint", None)
    dump("cdse_products.json", payload)

    r = s.post(
        "https://cmr.earthdata.nasa.gov/search/granules.umm_json",
        data=[
            ("provider", "ASF"),
            ("readable_granule_name[]", GRD),
            ("readable_granule_name[]", SLC),
            ("page_size", "2000"),
        ],
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    payload["items"] = [trim_umm(i) for i in payload["items"]]
    dump("asf_umm.json", payload)

    ids = [GRD.rsplit("_", 1)[0]]
    for coll, suffix in (("sentinel-1-grd", ""), ("sentinel-1-rtc", "_rtc")):
        r = s.post(
            "https://planetarycomputer.microsoft.com/api/stac/v1/search",
            json={
                "collections": [coll],
                "ids": [i + suffix for i in ids],
                "limit": 10,
                "fields": {"include": ["id", "properties.s1:product_identifier"]},
            },
            timeout=60,
        )
        r.raise_for_status()
        dump(f"pc_{coll}.json", r.json())

    r = s.get(
        "https://cmr.earthdata.nasa.gov/search/granules.umm_json",
        params={
            "short_name": "OPERA_L2_RTC-S1_V1",
            "provider": "ASF",
            "page_size": 4,
            "sort_key": "-revision_date",
        },
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    payload["items"] = [trim_umm(i) for i in payload["items"]]
    dump("opera_rtc_umm.json", payload)


def wide_swath():
    """MODIS/VIIRS granules of one stamp at LANCE and LAADS (CMR), and one
    Sentinel-3 frame's products at CDSE (NRT, NTC, L2), EUMETSAT and
    Planetary Computer."""
    s = requests.Session()
    cmr = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
    out = {}
    for key, concept, temporal in (
        (
            "modis_nrt",
            "C1426414410-LANCEMODIS",
            "2026-10-13T09:45:00Z,2026-10-13T09:46:00Z",
        ),
        ("modis_c7", "C3861664481-LAADS", "2026-10-13T09:45:00Z,2026-10-13T09:46:00Z"),
        (
            "viirs_nrt",
            "C2208778455-LANCEMODIS",
            "2026-10-13T14:24:00Z,2026-10-13T14:25:00Z",
        ),
        (
            "viirs_snow",
            "C3173450415-NSIDC_CPRD",
            "2026-10-12T15:36:00Z,2026-10-12T15:37:00Z",
        ),
    ):
        r = s.get(
            cmr,
            params={
                "collection_concept_id": concept,
                "temporal": temporal,
                "page_size": 1,
            },
            timeout=60,
        )
        r.raise_for_status()
        item = r.json()["items"][0]
        item["umm"].pop("RelatedUrls", None)
        out[key] = item
    dump("cmr_wideswath_umm.json", out)

    frame = "_125_304_1620_"
    r = s.get(
        "https://catalogue.dataspace.copernicus.eu/odata/v1/Products",
        params={
            "$filter": "Collection/Name eq 'SENTINEL-3' and contains(Name,'"
            + frame
            + "') and ContentDate/Start ge 2026-10-12T04:00:00.000Z"
            " and ContentDate/Start le 2026-10-12T05:00:00.000Z",
            "$expand": "Attributes",
            "$top": 20,
        },
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    keep = ("_OL_1_EFR_", "_SL_1_RBT_", "_OL_2_LFR_", "_SL_2_LST_")
    payload["value"] = [
        v for v in payload["value"] if any(k in v["Name"] for k in keep)
    ]
    for v in payload["value"]:
        v.pop("Footprint", None)
        ring = v["GeoFootprint"]["coordinates"][0]
        v["GeoFootprint"]["coordinates"] = [ring[::8] + [ring[0]]]
    dump("cdse_s3_products.json", payload)
    r = s.get(
        "https://api.eumetsat.int/data/search-products/1.0.0/os",
        params={
            "format": "json",
            "pi": "EO:EUM:DAT:0409",
            "dtstart": "2026-10-12T04:28:00Z",
            "dtend": "2026-10-12T04:30:00Z",
            "c": 5,
        },
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    for f in payload.get("features", []):
        f.pop("geometry", None)
        f["properties"] = {
            k: f["properties"][k] for k in ("identifier", "updated", "date")
        }
    dump(
        "eumetsat_s3_efr.json",
        {"totalResults": payload["totalResults"], "features": payload["features"]},
    )


if __name__ == "__main__":
    main()
    wide_swath()
