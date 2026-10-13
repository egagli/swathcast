"""Smoke tests against the real APIs. Run with ``pixi run test-live``."""

from datetime import timedelta

import pytest

from swathcast.sources import SOURCE_CLASSES, CDSECatalog
from swathcast.timeutil import utcnow

pytestmark = pytest.mark.live

# A granule known to exist at every GRD-based source (checked 2026-09-29).
GRD = "S1C_IW_GRDH_1SDV_20260925T193709_20260925T193733_009608_0131FB_55E1"


@pytest.fixture(scope="module")
def recent():
    now = utcnow()
    return CDSECatalog().list_new((now - timedelta(hours=6), now))


def test_cdse_lists_recent_iw_products(recent):
    assert recent, "CDSE published no IW GRDH/SLC in the last 6 h"
    assert {a.product_type for a in recent} <= {"GRD", "SLC"}
    assert all(a.provider_timestamp is not None for a in recent)
    assert all(not a.granule_id.endswith("_COG") for a in recent)


@pytest.mark.parametrize("key", ["asf_grd", "pc_grd", "pc_rtc", "cdse_grd"])
def test_known_grd_is_found(key):
    assert GRD in SOURCE_CLASSES[key]().lookup([GRD])


def test_opera_stream_reads(recent):
    src = SOURCE_CLASSES["opera_rtc"]()
    src.lookup([a.granule_id for a in recent if a.product_type == "SLC"])
    assert src.stats["stream_items"] >= 0
    assert src.get_cursor() is not None


# ---------------------------------------------------------------- MODIS, VIIRS, Sentinel-3
@pytest.mark.parametrize("mission", ["modis", "viirs", "s3"])
def test_wide_swath_reference_lists_and_its_granules_are_found(mission):
    """The reference listing of the last 3 h, then the same granules looked
    up at the reference again (every listed granule is found)."""
    from swathcast.missions import MISSIONS

    m = MISSIONS[mission]
    now = utcnow()
    listed = m.catalog().list_new((now - timedelta(hours=3), now))
    assert listed, f"{mission}: nothing listed in the last 3 h"
    ref = next(s for s in m.sources if s.is_reference)
    ids = [a.granule_id for a in listed if a.product_type == ref.reference_product][:40]
    found = ref().lookup(ids)
    assert set(found) == set(ids)


def test_standard_modis_granules_of_yesterday_are_at_laads():
    from swathcast.sources import modis

    day = (utcnow() - timedelta(days=2)).strftime("A%Y%j")
    ids = [f"MOD021KM.{day}.{h:02d}00" for h in range(0, 6)]
    assert set(modis.MODIS_SOURCES[2]().lookup(ids)) == set(ids)  # collection 7
