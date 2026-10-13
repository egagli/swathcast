"""Source parsing against recorded API responses (offline)."""

from datetime import UTC, datetime, timedelta

import pytest

from conftest import FakeResponse, FakeSession, load_fixture
from swathcast.sources.asf import ASFGRD, ASFSLC
from swathcast.sources.base import SourceError
from swathcast.sources.cdse import CDSECatalog, acquisition_from_odata, parse_name
from swathcast.sources.opera import OperaRTC, parent_slc
from swathcast.sources.pc import PCGRD, PCRTC, pc_item_id
from swathcast.timeutil import parse_utc, to_odata

GRD = "S1C_IW_GRDH_1SDV_20260925T193709_20260925T193733_009608_0131FB_55E1"
SLC = "S1C_IW_SLC__1SDV_20260925T072131_20260925T072202_009600_0131B2_E986"


def test_parse_name():
    parts = parse_name(GRD)
    assert (
        parts["platform"] == "S1C" and parts["type"] == "GRDH" and parts["pol"] == "DV"
    )
    assert parse_name(SLC)["type"] == "SLC_"
    with pytest.raises(ValueError):
        parse_name(
            "S1C_EW_GRDM_1SDH_20260928T181245_20260928T181340_009651_01335E_45EC"
        )


def test_cdse_acquisition_fields():
    items = {i["Name"]: i for i in load_fixture("cdse_products.json")["value"]}
    acq = acquisition_from_odata(items[GRD + ".SAFE"])
    assert acq.granule_id == GRD
    assert acq.product_type == "GRD"
    assert acq.platform == "S1C"
    assert acq.relative_orbit == 60
    assert acq.orbit_direction == "descending"
    assert acq.provider_timestamp_field == "PublicationDate"
    assert acq.provider_timestamp == datetime(
        2026, 9, 25, 21, 29, 54, 693355, tzinfo=UTC
    )
    assert acq.sensing_start.tzinfo is UTC
    assert acq.anx_start_s is not None
    assert -180 <= acq.centroid_lon <= 180
    slc = acquisition_from_odata(items[SLC + ".SAFE"])
    assert slc.product_type == "SLC"


def count(n):
    return FakeResponse({"@odata.count": n, "value": []})


def test_cdse_listing_pages_to_the_end():
    page = load_fixture("cdse_products.json")
    n = len(page["value"])
    first = dict(page, **{"@odata.nextLink": "https://example/next"})
    session = FakeSession(count(2 * n), FakeResponse(first), FakeResponse(page))
    got = CDSECatalog(session=session).list_new(
        (datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC))
    )
    assert len(got) == 2 * n
    flt = session.calls[1][2]["params"]["$filter"]
    assert "PublicationDate gt 2026-09-25T00:00:00.000Z" in flt
    assert "not endswith(Name,'_COG.SAFE')" in flt
    assert session.calls[2][2]["params"] is None  # nextLink carries the query


def test_cdse_listing_bisects_windows_over_the_paging_cap():
    page = load_fixture("cdse_products.json")
    n = len(page["value"])
    cat = CDSECatalog(session=None)
    cat.chunk_limit = n  # force one split
    cat.session = FakeSession(
        count(2 * n), count(n), FakeResponse(page), count(n), FakeResponse(page)
    )
    got = cat.list_new(
        (datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC))
    )
    assert len(got) == 2 * n
    filters = [
        c[2]["params"]["$filter"] for c in cat.session.calls if c[2].get("params")
    ]
    assert any("PublicationDate le 2026-09-25T12:00:00.000Z" in f for f in filters)
    assert any("PublicationDate gt 2026-09-25T12:00:00.000Z" in f for f in filters)


def test_cdse_listing_refuses_silent_truncation():
    page = load_fixture("cdse_products.json")
    n = len(page["value"])
    # short by 5, and still short before the settling tail: a real truncation
    session = FakeSession(count(n + 5), FakeResponse(page), count(n + 5))
    with pytest.raises(SourceError, match="truncated"):
        CDSECatalog(session=session).list_new(
            (datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC))
        )


def test_cdse_listing_tolerates_unsettled_tail():
    # CDSE counts products published in the last minutes before it lists them
    # (2026-10-04: 344 of 356 listed). Accept the listing when everything older
    # than the settling tail is complete; the next run's overlap lists the rest.
    page = load_fixture("cdse_products.json")
    n = len(page["value"])
    end = max(parse_utc(r["PublicationDate"]) for r in page["value"]) + timedelta(
        minutes=1
    )
    cutoff = end - CDSECatalog.settle
    before = sum(1 for r in page["value"] if parse_utc(r["PublicationDate"]) <= cutoff)
    session = FakeSession(count(n + 3), FakeResponse(page), count(before))
    got = CDSECatalog(session=session).list_new((end - timedelta(days=1), end))
    assert len(got) == n
    assert (
        "PublicationDate le " + to_odata(cutoff)
        in session.calls[2][2]["params"]["$filter"]
    )


def test_cdse_http_error_raises():
    session = FakeSession(FakeResponse({"detail": "boom"}, status=503))
    with pytest.raises(SourceError):
        CDSECatalog(session=session).list_new(
            (datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC))
        )


def test_asf_ignores_metadata_granules():
    payload = load_fixture("asf_umm.json")
    assert any("METADATA" in i["umm"]["GranuleUR"] for i in payload["items"])
    headers = {"CMR-Hits": str(payload["hits"])}
    found = ASFGRD(session=FakeSession(FakeResponse(payload, headers=headers))).lookup(
        [GRD, "S1C_IW_GRDH_1SDV_20990101T000000_20990101T000025_000000_000000_0000"]
    )
    assert list(found) == [GRD]
    hit = found[GRD]
    assert hit.provider_id == GRD + "-GRD_HD"
    assert hit.provider_timestamp_field == "ProviderDates[Insert]"
    assert hit.provider_timestamp.tzinfo is UTC
    slc = ASFSLC(session=FakeSession(FakeResponse(payload, headers=headers))).lookup(
        [SLC]
    )
    assert slc[SLC].provider_id == SLC + "-SLC"


def test_pc_item_ids():
    assert pc_item_id(GRD, "sentinel-1-grd") == GRD[:-5]
    assert pc_item_id(GRD, "sentinel-1-rtc") == GRD[:-5] + "_rtc"


def test_pc_lookup_matches_cog_variant_by_crc_free_id():
    session = FakeSession(FakeResponse(load_fixture("pc_sentinel-1-grd.json")))
    found = PCGRD(session=session).lookup([GRD])
    assert GRD in found
    # PC ingests CDSE's _COG product, whose CRC differs from the SAFE's.
    assert found[GRD].notes["product_identifier_matches"] is False
    assert found[GRD].provider_timestamp is None  # first-seen only
    body = session.calls[0][2]["json"]
    assert body["ids"] == [GRD[:-5]] and body["collections"] == ["sentinel-1-grd"]
    rtc = PCRTC(
        session=FakeSession(FakeResponse(load_fixture("pc_sentinel-1-rtc.json")))
    ).lookup([GRD])
    assert set(rtc) <= {GRD}


def test_opera_stream_maps_bursts_to_parent_slc():
    payload = load_fixture("opera_rtc_umm.json")
    parent = payload["items"][0]["umm"]["InputGranules"][0]
    now = datetime(2026, 9, 29, 1, tzinfo=UTC)
    src = OperaRTC(
        session=FakeSession(
            FakeResponse(payload, headers={"CMR-Hits": str(len(payload["items"]))})
        ),
        now=now,
    )
    src.set_cursor(datetime(2026, 9, 29, 0, tzinfo=UTC))
    found = src.lookup([parent, SLC])
    assert list(found) == [parent]
    hit = found[parent]
    assert hit.n_items == len(
        {i["umm"]["GranuleUR"].split("_")[3] for i in payload["items"]}
    )
    inserts = [
        e["Date"]
        for i in payload["items"]
        for e in i["umm"]["ProviderDates"]
        if e["Type"] == "Insert"
    ]
    assert hit.provider_timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") == min(inserts)
    assert src.get_cursor() == now
    sent = dict(src.session.calls[0][2]["data"])
    assert sent["updated_since"] == "2026-09-28T21:00:00Z"  # cursor - 3 h overlap


def test_opera_cursor_not_advanced_on_failure():
    src = OperaRTC(
        session=FakeSession(FakeResponse({}, status=500)),
        now=datetime(2026, 9, 29, tzinfo=UTC),
    )
    cursor = datetime(2026, 9, 28, tzinfo=UTC)
    src.set_cursor(cursor)
    with pytest.raises(SourceError):
        src.lookup(["x"])
    assert src.get_cursor() == cursor


def test_parent_slc_strips_suffixes():
    assert parent_slc(SLC + ".SAFE") == SLC
    assert parent_slc(SLC + "-SLC") == SLC
