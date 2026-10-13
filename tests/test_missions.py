"""Latency tracking for Sentinel-2, Landsat and NISAR (sources and a tracker run)."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from conftest import load_fixture
from swathcast.missions import MISSIONS
from swathcast.sources import landsat, nisar, s2
from swathcast.sources.base import Found, ReferenceCatalog, Source
from swathcast.storage import Store
from swathcast.tracker import Tracker


# ---------------------------------------------------------------- Sentinel-2
def test_s2_names_sampling_and_ids():
    items = load_fixture("cdse_s2_products.json")["value"]
    a = s2.acquisition(items[0])
    assert (
        a.granule_id == "S2B_MSIL2A_20260928T021559_N0513_R003_T50PQQ_20260928T053233"
    )
    assert (a.product_type, a.platform, a.relative_orbit, a.coverage_key) == (
        "L2A",
        "S2B",
        3,
        "50PQQ",
    )
    assert a.sensing_start == pd.Timestamp(items[0]["ContentDate"]["Start"])
    assert a.provider_timestamp_field == "PublicationDate"
    assert (
        s2.pc_s2_id(a.granule_id)
        == "S2B_MSIL2A_20260928T021559_R003_T50PQQ_20260928T053233"
    )
    # the sample is a fixed 1-in-8 of tiles, the same on every run
    tiles = [
        f"{z:02d}{b}{c}{d}"
        for z in range(1, 61)
        for b in "CT"
        for c in "AB"
        for d in "QR"
    ]
    share = sum(s2.in_sample(t) for t in tiles) / len(tiles)
    assert 0.08 < share < 0.18
    assert s2.in_sample("50PQQ") == s2.in_sample("50PQQ")
    with pytest.raises(ValueError):
        s2.parse_name(
            "S1C_IW_GRDH_1SDV_20260928T215218_20260928T215232_009653_01336F_1287"
        )


class FakeResponse:
    def __init__(self, payload, ok=True):
        self.payload, self.ok, self.status_code, self.text = payload, ok, 200, ""
        self.headers = {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload, self.bodies = payload, []

    def post(self, url, json=None, timeout=None, **kw):
        self.bodies.append(json)
        return FakeResponse(self.payload)


def test_earth_search_matches_on_product_uri_and_keeps_created():
    payload = load_fixture("earthsearch_s2_l2a.json")
    ids = [
        s2.strip_safe(f["properties"]["s2:product_uri"]) for f in payload["features"]
    ]
    src = s2.SOURCES[3](session=FakeSession(payload))
    assert src.key == "es_s2_l2a"
    found = src.lookup(
        ids + ["S2B_MSIL2A_20260928T021559_N0513_R003_T99XXX_20260928T053233"]
    )
    assert set(found) == set(ids)
    hit = found[ids[0]]
    assert (
        hit.provider_timestamp_field == "created" and hit.provider_timestamp is not None
    )
    body = src.session.bodies[0]
    assert all(u.endswith(".SAFE") for u in body["query"]["s2:product_uri"]["in"])


# ---------------------------------------------------------------- Landsat
def test_landsat_items_and_mirror_ids():
    feats = load_fixture("usgs_landsat_items.json")["features"]
    l1 = landsat.acquisition(feats[0], "L1")
    assert (l1.platform, l1.relative_orbit, l1.coverage_key, l1.orbit_direction) == (
        "L9",
        66,
        "066018",
        "descending",
    )
    assert l1.provider_timestamp_field == "created" and l1.timeliness == "T1"
    l2 = landsat.acquisition(feats[1], "L2")
    assert landsat.l2_mirror_id(l2.granule_id) == "LC09_L2SR_084110_20260927_02_T2"


# ---------------------------------------------------------------- NISAR
def test_nisar_names_and_gcov_mapping():
    fx = load_fixture("cmr_nisar_umm.json")
    rslc = nisar.acquisition(fx["RSLC"], "RSLC")
    assert (
        rslc.relative_orbit,
        rslc.orbit_direction,
        rslc.datatake_id,
        rslc.polarization,
    ) == (134, "descending", "059", "DH")
    assert rslc.coverage_key == "134-059"
    assert nisar.gcov_name(rslc.granule_id) == fx["GCOV"]["umm"]["GranuleUR"]
    assert fx["GCOV"]["umm"]["InputGranules"][0].startswith(rslc.granule_id)
    l0b = nisar.acquisition(fx["L0B"], "L0B")
    assert (l0b.product_type, l0b.datatake_id) == ("L0B", "148S")
    assert fx["RSLC"]["umm"]["InputGranules"][0].startswith(l0b.granule_id)
    assert rslc.centroid_lat is not None and -90 <= rslc.centroid_lat <= 90


# ---------------------------------------------------------------- tracker
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _acq(i, ptype):
    t = NOW - timedelta(hours=3)
    from swathcast.sources.base import Acquisition

    return Acquisition(
        granule_id=f"G{i}_{ptype}",
        product_type=ptype,
        platform="S2B",
        mode="MSI",
        polarization=None,
        absolute_orbit=None,
        relative_orbit=3,
        orbit_direction="descending",
        datatake_id="50PQQ",
        anx_start_s=None,
        sensing_start=t,
        sensing_stop=t,
        centroid_lon=1.0,
        centroid_lat=2.0,
        timeliness="N0513",
        provider_timestamp=NOW - timedelta(hours=1),
        provider_timestamp_field="PublicationDate",
        coverage_key="50PQQ",
    )


class Catalog(ReferenceCatalog):
    key = "cdse_s2"
    label = distributor = product = "x"

    def list_new(self, window):
        return [_acq(1, "L1C"), _acq(2, "L2A")]

    def lookup(self, ids):
        return {}


class Mirror(Source):
    key, label, distributor, product, reference_product = (
        "es_s2_l2a",
        "ES",
        "ES",
        "L2A",
        "L2A",
    )

    def lookup(self, ids):
        return {g: Found(provider_id=g) for g in ids}


def test_a_mission_tracks_like_sentinel_1(tmp_path):
    ref_l1c = type(
        "R1",
        (s2.CDSES2Product,),
        {
            "key": "cdse_s2_l1c",
            "label": "L1C",
            "product": "L1C",
            "reference_product": "L1C",
        },
    )
    ref_l2a = type(
        "R2",
        (s2.CDSES2Product,),
        {
            "key": "cdse_s2_l2a",
            "label": "L2A",
            "product": "L2A",
            "reference_product": "L2A",
        },
    )
    sources = {
        "cdse_s2_l1c": ref_l1c(),
        "cdse_s2_l2a": ref_l2a(),
        "es_s2_l2a": Mirror(),
    }
    store = Store(tmp_path)
    r = Tracker(
        store, MISSIONS["s2"].config(), now=NOW, catalog=Catalog(), sources=sources
    ).run()
    assert r.new_acquisitions == 2 and r.sightings == {"es_s2_l2a": 1}
    df = store.read_all()
    assert len(df) == 3  # L1C: reference; L2A: reference + mirror
    assert set(df["coverage_cell"]) == {"50PQQ"}
    assert store.read_cursors()["cdse_s2"] == NOW  # the catalog's own cursor key
    assert "cdse_s2" in store.read_json("health.json")["sources"]


def test_every_mission_source_has_a_reference_product():
    for m in MISSIONS.values():
        refs = {c.reference_product for c in m.sources if c.is_reference}
        assert refs and all(c.reference_product in refs for c in m.sources), m.key
        keys = [c.key for c in m.sources]
        assert len(keys) == len(set(keys))
