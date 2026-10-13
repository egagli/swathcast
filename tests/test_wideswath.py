"""MODIS, VIIRS and Sentinel-3: passes predicted from the orbit, and their
latency sources (offline, recorded fixtures)."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from conftest import FakeResponse, FakeSession, load_fixture
from swathcast import wideswath
from swathcast.sources import modis, s3

pytest.importorskip("sgp4")
pytest.importorskip("shapely")


@pytest.fixture(scope="module")
def sats():
    from swathcast.ephemeris import satrec

    omm = load_fixture("celestrak_omm.json")
    return {p: satrec(omm[p]) for ps in wideswath.PLATFORMS.values() for p in ps}


# ---------------------------------------------------------------- the grids
def test_sinusoidal_grid_is_the_modis_tiling():
    grid = wideswath.sin_grid()
    assert (
        len(grid) == wideswath.N_SIN_TILES
    )  # the 460 tiles that hold any of the Earth
    assert grid["tile"].is_unique
    t = grid.set_index("tile")
    # h08v05 (California, Nevada): 30-40 N
    assert 30 < t.at["h08v05", "lat"] < 40 and -130 < t.at["h08v05", "lon"] < -100
    assert t.at["h08v05", "geometry"].bounds[1] == pytest.approx(30, abs=1e-6)
    assert "h00v00" not in t.index  # outside the projection's world
    assert grid.geometry.bounds["minx"].min() >= -180 - 1e-6
    x, y = wideswath.lonlat_to_sin(-120.0, 35.0)
    assert np.allclose(wideswath.sin_to_lonlat(x, y), (-120.0, 35.0))


def test_view_zenith_angle():
    assert wideswath.view_zenith_deg(0, 705) == pytest.approx(0)
    # MODIS's swath edge: ~55 deg off nadir, ~65 deg at the ground
    assert 63 < wideswath.view_zenith_deg(1171, 705) < 67


# ---------------------------------------------------------------- passes
def test_a_modis_tile_pass_is_inside_the_real_granule(sats):
    """The LANCE granule MOD021KM.A2026286.0940 (09:40-09:45 UTC): its
    centre is imaged by Terra in that window, close to nadir."""
    item = load_fixture("cmr_wideswath_umm.json")["modis_nrt"]
    a = modis.acquisition(item, "L1B", "MODIS")
    t0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC).timestamp()
    p = wideswath.tile_passes(
        sats["Terra"], "Terra", [a.centroid_lon], [a.centroid_lat], t0, t0 + 7200
    )
    assert len(p) == 1
    when = pd.Timestamp(p["time"].iloc[0], unit="s", tz="UTC")
    assert a.sensing_start <= when <= a.sensing_stop
    assert abs(p["xtrack_km"].iloc[0]) < 300


def test_sentinel3_frames_fall_where_cdse_has_them(sats):
    """S3B relative orbit 304, frame 1620 on 2026-10-12: the SLSTR frame
    started at 04:26:28 (the OLCI frame later: OLCI images in daylight only)."""
    passes = wideswath.s3_frame_passes(
        sats,
        pd.Timestamp("2026-10-12T03:00Z"),
        pd.Timestamp("2026-10-12T06:00Z"),
    )
    hit = passes[
        (passes["platform"] == "S3B")
        & (passes["track"] == 304)
        & (passes["frame"] == 1620)
    ]
    assert len(hit) == 1
    real = pd.Timestamp("2026-10-12T04:26:28.0Z")
    assert abs((hit["time"].iloc[0] - real).total_seconds()) < 2
    # every frame of an orbit: 180 s apart from the node
    orbit = passes[(passes["platform"] == "S3B") & (passes["track"] == 304)]
    assert list(orbit["frame"][:3]) == [0, 180, 360]
    # relative orbits from the node longitude
    rel, off = wideswath.s3_relative_orbit("S3A", [wideswath.S3_ORBIT1_ANX_LON["S3A"]])
    assert rel[0] == 1 and off[0] == pytest.approx(0)
    rel, _ = wideswath.s3_relative_orbit(
        "S3A", [wideswath.S3_ORBIT1_ANX_LON["S3A"] - 2 * wideswath.S3_TRACK_STEP_DEG]
    )
    assert rel[0] == 3


def test_sentinel3_frame_outline_overlaps_the_real_footprint(sats):
    """The predicted outline of the SLSTR frame against the real RBT
    product's footprint (equal-area projection). The fixture keeps every 8th
    vertex of the footprint, so the bar is lower than the full check (median
    IoU 0.996 over 200 frames, docs/methodology.md)."""
    import geopandas as gpd
    from shapely.geometry import Polygon, shape

    item = next(
        v
        for v in load_fixture("cdse_s3_products.json")["value"]
        if "_SL_1_RBT_" in v["Name"]
    )
    t0 = pd.Timestamp(item["ContentDate"]["Start"]).timestamp()
    ring = wideswath.swath_polygon(sats["S3B"], t0, t0 + 180, wideswath.SWATH_KM["S3B"])
    real = shape(item["GeoFootprint"])
    crs = f"+proj=laea +lon_0={real.centroid.x} +lat_0={real.centroid.y}"
    a = gpd.GeoSeries([Polygon(ring)], crs=4326).to_crs(crs).iloc[0]
    b = gpd.GeoSeries([real], crs=4326).to_crs(crs).buffer(0).iloc[0]
    assert a.intersection(b).area / a.union(b).area > 0.85


# ---------------------------------------------------------------- MODIS/VIIRS sources
def test_modis_and_viirs_ids():
    fx = load_fixture("cmr_wideswath_umm.json")
    a = modis.acquisition(fx["modis_nrt"], "L1B", "MODIS")
    assert (a.granule_id, a.platform, a.datatake_id) == (
        "MOD021KM.A2026286.0940",
        "Terra",
        "A2026286.0940",
    )
    assert a.provider_timestamp_field == "ProviderDates.Insert"
    assert a.coverage_key.split("-")[0] in ("Day", "Night", "Both")
    v = modis.acquisition(fx["viirs_nrt"], "L1B", "VIIRS")
    assert v.granule_id == "VJ102MOD.A2026286.1418" and v.platform == "N20"
    assert modis.stamp_of("VJ110.A2026285.1530.002.2026285210943.nc") == "A2026285.1530"
    with pytest.raises(ValueError):
        modis.platform_of("XYZ021KM.A2026286.0940")


def test_cmr_window_lookup_matches_platform_and_stamp():
    fx = load_fixture("cmr_wideswath_umm.json")
    session = FakeSession(
        FakeResponse({"items": [fx["modis_c7"]]}, headers={"CMR-Hits": "1"})
    )
    src = modis.MODIS_SOURCES[2](session=session)
    assert src.key == "laads_modis_l1b_c7"
    found = src.lookup(["MOD021KM.A2026286.0940", "MOD021KM.A2026286.0945"])
    assert set(found) == {"MOD021KM.A2026286.0940"}
    assert found["MOD021KM.A2026286.0940"].provider_id.endswith(".007.2026286155158.nc")
    # one temporal query for the platform, spanning the ids
    body = dict(session.calls[0][2]["data"])
    assert body["collection_concept_id"] == modis.MODIS_L1B_C7["Terra"]
    assert body["temporal"].startswith("2026-10-13T09:39:30")


# ---------------------------------------------------------------- Sentinel-3 sources
def test_sentinel3_names_and_frame_keys():
    cdse = "S3A_OL_1_EFR____20261013T133354_20261013T133654_20261013T151726_0179_145_081_3420_PS1_O_NR_004.SEN3"
    p = s3.parse_name(cdse)
    assert (
        p["platform"],
        p["instrument"],
        p["type"],
        p["rel"],
        p["frame"],
        p["timeliness"],
    ) == (
        "S3A",
        "OL",
        "EFR",
        81,
        3420,
        "NR",
    )
    gid = s3.granule_id(p, "EFR")
    assert gid == "S3A_OL_1_EFR_20261013T133354_145_081_3420"
    assert s3.key_of_id(gid) == p["key"] == "S3A_OL_145_081_3420"
    pc = s3.parse_name("S3B_OL_2_LFR_20261012T115525_20261012T115707_0102_125_308_4320")
    assert pc["key"] == "S3B_OL_125_308_4320" and pc["level"] == "2"
    with pytest.raises(ValueError):
        s3.parse_name(
            "S3A_SL_2_AOD____20250107T110554_20250107T111052_20250107T132924_0299_121_137______MAR_O_NR_003.SEN3"
        )


def test_sentinel3_sources_match_on_the_frame():
    items = load_fixture("cdse_s3_products.json")["value"]
    efr = next(v for v in items if "_OL_1_EFR_" in v["Name"] and "_NR_" in v["Name"])
    a = s3.acquisition(efr)
    assert (a.product_type, a.mode, a.relative_orbit, a.coverage_key) == (
        "EFR",
        "OLCI",
        304,
        "304-1620",
    )
    assert a.orbit_direction in ("ascending", "descending")
    # the NTC product and EUMETSAT's copy (another centre's name) match the same frame
    ntc = s3.CDSES3Source(
        session=FakeSession(FakeResponse({"value": items})),
        product_type="OL_1_EFR",
        name_filter="contains(Name,'_NT_')",
    )
    hit = ntc.lookup([a.granule_id])[a.granule_id]
    assert "_OL_1_EFR_" in hit.provider_id
    eum = load_fixture("eumetsat_s3_efr.json")
    found = s3.EumetsatS3Source(session=FakeSession(FakeResponse(eum))).lookup(
        [a.granule_id]
    )
    assert "_MAR_" in found[a.granule_id].provider_id
    assert found[a.granule_id].provider_timestamp_field == "updated"
    # a frame dumped in two NRT pieces is two reference rows: both match
    twin = a.granule_id.replace("T042851", "T042900")
    both = s3.EumetsatS3Source(session=FakeSession(FakeResponse(eum))).lookup(
        [a.granule_id, twin]
    )
    assert set(both) == {a.granule_id, twin}


def test_new_missions_are_registered():
    from swathcast.missions import MISSIONS

    for key in ("modis", "viirs", "s3"):
        m = MISSIONS[key]
        refs = [s for s in m.sources if s.is_reference]
        assert refs and all(
            s.reference_product in ("L1B", "EFR", "RBT") for s in m.sources
        )
        # LANCE deletes NRT products after 7 days: those sources give up first
        for s in m.sources:
            if s.key.startswith("lance_") and not s.is_reference:
                assert m.timeouts_h[s.key] < 7 * 24


def test_sentinel3_tile_passes_carry_the_products_frame(sats):
    """A pass over the centre of the real RBT frame (S3B, relative orbit 304,
    frame 1620, 2026-10-12 04:26:28) is labelled with that frame."""
    from shapely.geometry import shape

    item = next(
        v
        for v in load_fixture("cdse_s3_products.json")["value"]
        if "_SL_1_RBT_" in v["Name"]
    )
    c = shape(item["GeoFootprint"]).centroid
    grid = pd.DataFrame({"tile": ["x"], "lon": [c.x], "lat": [c.y]})
    p = wideswath.sin_tile_passes(
        grid,
        {"S3B": sats["S3B"]},
        "sentinel-3",
        pd.Timestamp("2026-10-12T03:00Z"),
        pd.Timestamp("2026-10-12T06:00Z"),
    )
    hit = p[(p["time"] > "2026-10-12T04:26Z") & (p["time"] < "2026-10-12T04:30Z")]
    assert len(hit) == 1
    assert int(hit["track"].iloc[0]) == 304
    assert int(hit["anx_s"].iloc[0]) // 180 * 180 == 1620
