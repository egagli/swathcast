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


if __name__ == "__main__":
    main()
