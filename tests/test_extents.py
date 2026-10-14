"""Exact browse extents (swathcast.extents): the parsers, the listing
cursors, the update with a fake Earthdata session, and the site's shards."""

import json
import struct
from datetime import UTC, datetime

import pandas as pd

from swathcast import extents as x

NOW = datetime(2026, 10, 6, 18, tzinfo=UTC)
GCOV = "NISAR_L2_PR_GCOV_031_106_A_026_4005_DHDH_A_20260925T130400_20260925T130436_P05023_N_F_J_001"
OPERA = "OPERA_L2_RTC-S1_T137-292398-IW2_20261001T020133Z_20261001T044458Z_S1C_30_v1.0"
#: a GCOV's _NATIVE.kml (UTM grid, live 2026-10-14): corners from the
#: bottom-left, counter-clockwise
KML = """<kml><Document><GroundOverlay><ns0:LatLonQuad><coordinates>-81.00957297644737,-12.536319822837351 -77.93625335824206,-12.518855410856494 -77.96662459240734,-9.580320872532512 -81.00947790011288,-9.59359972346223</coordinates></ns0:LatLonQuad></GroundOverlay></Document></kml>"""
#: the same corners from the top-left, clockwise, rounded as published
QUAD = [
    -81.009478,
    -9.5936,
    -77.966625,
    -9.580321,
    -77.936253,
    -12.518855,
    -81.009573,
    -12.53632,
]


def geotiff(w=3439, h=1563, x0=477180.0, y1=5286150.0, epsg=32610):
    """The first bytes of a classic little-endian GeoTIFF, as OPERA writes them."""
    entries, extra = [], b""
    base = 8 + 2 + 12 * 5 + 4  # header, count, 5 entries, next-IFD offset

    def add(tag, typ, values, fmt):
        nonlocal extra
        data = struct.pack("<" + fmt * len(values), *values)
        if len(data) <= 4:
            entries.append(
                struct.pack("<HHI", tag, typ, len(values)) + data.ljust(4, b"\0")
            )
        else:
            entries.append(
                struct.pack("<HHII", tag, typ, len(values), base + len(extra))
            )
            extra += data

    add(256, 3, [w], "H")
    add(257, 3, [h], "H")
    add(33550, 12, [30.0, 30.0, 0.0], "d")
    add(33922, 12, [0, 0, 0, x0, y1, 0], "d")
    add(34735, 3, [1, 1, 0, 2, 1024, 0, 1, 1, 3072, 0, 1, epsg], "H")
    return (
        b"II"
        + struct.pack("<HI", 42, 8)
        + struct.pack("<H", 5)
        + b"".join(entries)
        + b"\0" * 4
        + extra
    )


def test_parsers():
    assert [round(v, 6) for v in x.kml_quad(KML)] == QUAD
    box = "<LatLonBox><north>2</north><south>1</south><east>4</east><west>3</west></LatLonBox>"
    assert x.kml_quad(box) == [3, 2, 4, 2, 4, 1, 3, 1]
    assert x.tiff_grid(geotiff()) == (32610, 477180.0, 5239260.0, 580350.0, 5286150.0)
    assert x.nisar_kml_url(GCOV).endswith(
        f"NISAR_L2_GCOV_PROVISIONAL_V1/{GCOV}/{GCOV}_NATIVE.kml"
    )
    assert x.nisar_kml_url("NISAR_L1_PR_RSLC_x") is None
    assert x.OPERA_NAME.match(OPERA)[1] == "T137-292398-IW2"


class Resp:
    def __init__(self, status=200, text="", content=b"", headers=None):
        self.status_code, self.text, self.content, self.headers = (
            status,
            text,
            content,
            headers or {},
        )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(self.status_code)


class FakeEDL:
    """CMR's CSV listing (one page) and the files behind the login."""

    def __init__(self):
        self.listed = []

    def get(self, url, params=None, headers=None, timeout=None):
        if url == x.CMR_CSV:
            self.listed.append(params["created_at[]"])
            name = GCOV if "collection_concept_id" in params else OPERA
            return Resp(text=f"Granule UR,Producer Granule ID\n{name},{name}\n")
        if url.endswith("_NATIVE.kml"):
            return Resp(text=KML)
        if url.endswith("_mask.tif"):
            assert headers == {"Range": "bytes=0-16383"}
            return Resp(206, content=geotiff())
        return Resp(404)


def test_update_lists_fetches_and_backfills(tmp_path, monkeypatch):
    s = FakeEDL()
    monkeypatch.setattr(x, "edl_session", lambda session=None: s)
    report = x.update(tmp_path, now=NOW, budget_s=60)
    assert report["nisar_new"] == 1 and report["opera_new"] == 1
    # new since the last run (2 h on the first), then one backfill step each
    assert s.listed == [
        "2026-10-06T16:00:00Z,2026-10-06T18:00:00Z",
        "2026-10-04T16:00:00Z,2026-10-06T16:00:00Z",
        "2026-10-06T16:00:00Z,2026-10-06T18:00:00Z",
        "2026-10-06T04:00:00Z,2026-10-06T16:00:00Z",
    ]
    state = json.loads((tmp_path / "extents" / "state.json").read_text())
    assert state["opera_backfill_before"].startswith("2026-10-06T04:00")
    # a second run lists from the cursor and fetches nothing it knows
    s.listed.clear()
    report = x.update(tmp_path, now=NOW.replace(hour=19), budget_s=60)
    assert report["nisar_new"] == 0 and report["opera_new"] == 0
    assert s.listed[0] == "2026-10-06T18:00:00Z,2026-10-06T19:00:00Z"
    # the site's shards
    out = tmp_path / "site"
    assert x.publish_extents(tmp_path, out) == {"nisar_gcov": 1, "opera": 1}
    assert json.loads((out / "browse_extents/nisar_gcov/106.json").read_text()) == {
        GCOV: QUAD
    }
    assert json.loads((out / "browse_extents/opera/137.json").read_text()) == {
        "T137-292398-IW2": [32610, 477180.0, 5239260.0, 580350.0, 5286150.0]
    }


def test_failures_are_retried_then_given_up(tmp_path, monkeypatch):
    class NoKML(FakeEDL):
        def get(self, url, **kw):
            return Resp(404) if url.endswith(".kml") else super().get(url, **kw)

    s = NoKML()
    monkeypatch.setattr(x, "edl_session", lambda session=None: s)
    for h in range(18, 18 + x.NISAR_MAX_TRIES + 1):
        report = x.update(tmp_path, now=NOW.replace(hour=h), budget_s=60)
    t = pd.read_parquet(tmp_path / "extents" / x.NISAR_FILE)
    assert t["tries"].tolist() == [x.NISAR_MAX_TRIES] and t["lon0"].isna().all()
    assert report["nisar_failed"] == 0  # given up: not tried again


def test_no_login_skips(tmp_path, monkeypatch):
    monkeypatch.delenv("EARTHDATA_USERNAME", raising=False)
    assert x.update(tmp_path, now=NOW) == {"skipped": "no Earthdata login"}
    assert x.publish_extents(tmp_path, tmp_path / "site") == {}
