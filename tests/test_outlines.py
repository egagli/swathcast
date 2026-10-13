"""Observed outlines (swathcast.outlines): NISAR frames from CMR's RSLC
footprints, Landsat cells from USGS's Level-1 footprints, both with offline
fake catalogues."""

from datetime import UTC, datetime

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon, box

from swathcast import outlines

NOW = datetime(2026, 10, 6, tzinfo=UTC)


class Resp:
    def __init__(self, payload, headers=None):
        self.payload, self.headers = payload, headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeCMR:
    """Two pages (search-after), oldest first."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(headers or {})
        ring = lambda w: f"45 {w} 45 {w + 3} 47 {w + 3} 47 {w} 45 {w}"  # noqa: E731
        name = "NISAR_L1_PR_RSLC_031_{t:03d}_{d}_{f:03d}_4005_DHDH_A_x"
        if not headers:
            entries = [
                {"title": name.format(t=170, d="D", f=66), "polygons": [[ring(-75)]]},
                {"title": "not an RSLC", "polygons": [[ring(0)]]},
            ] + [{"title": "x"}] * 1998  # a full page: there is a next one
            return Resp({"feed": {"entry": entries}}, {"CMR-Search-After": "k"})
        entries = [
            # the same frame, later: wins
            {"title": name.format(t=170, d="D", f=66), "polygons": [[ring(-74)]]},
            {"title": name.format(t=12, d="A", f=10), "polygons": [[ring(10)]]},
            # across 180: left to the plan
            {
                "title": name.format(t=13, d="A", f=11),
                "polygons": [["10 179 10 -179 12 -179 12 179 10 179"]],
            },
        ]
        return Resp({"feed": {"entry": entries}})


def test_nisar_observed_latest_per_frame():
    s = FakeCMR()
    got = outlines.nisar_observed(s, NOW)
    assert s.calls[1] == {"CMR-Search-After": "k"}
    assert set(got) == {(170, 66, "descending"), (12, 10, "ascending")}
    assert got[(170, 66, "descending")].bounds == (-74, 45, -71, 47)  # lon, lat


def test_cmr_polygon_is_lat_lon():
    p = outlines.cmr_polygon("45.9 -74.9 43.8 -75.9 43.0 -72.9 45.9 -74.9")
    assert p.exterior.coords[0] == (-74.9, 45.9)
    assert outlines.cmr_polygon("1 2 3") is None


class FakeSTAC:
    def __init__(self):
        self.bodies = []

    def post(self, url, json=None, timeout=None):
        self.bodies.append((url, json))
        feat = lambda p, r, g: {  # noqa: E731
            "geometry": g.__geo_interface__,
            "properties": {
                "landsat:wrs_path": f"{p:03d}",
                "landsat:wrs_row": f"{r:03d}",
            },
        }
        if len(self.bodies) == 1:
            return Resp(
                {
                    "features": [feat(46, 27, box(-124, 46, -121, 48))],
                    "links": [
                        {
                            "rel": "next",
                            "method": "POST",
                            "href": "https://next",
                            "body": {"next": "t"},
                        }
                    ],
                }
            )
        return Resp(
            {
                "features": [
                    feat(46, 27, box(-124.1, 46, -121, 48)),
                    {"geometry": None, "properties": {}},
                ],
                "links": [],
            }
        )


def test_landsat_observed_pages_and_keeps_latest():
    s = FakeSTAC()
    got = outlines.landsat_observed(s, NOW)
    assert s.bodies[1] == ("https://next", {"next": "t"})
    assert "assets" in s.bodies[0][1]["fields"]["exclude"]
    assert list(got) == [(46, 27)] and got[(46, 27)].bounds[0] == -124.1


def test_apply_replaces_where_seen_and_keeps_the_plan_elsewhere():
    plan = gpd.GeoDataFrame(
        pd.DataFrame(
            {
                "sensor": ["nisar", "nisar", "sentinel-1"],
                "track": [170, 171, 170],
                "frame": [66, 66, None],
                "orbit_direction": ["descending", "descending", "descending"],
            }
        ),
        geometry=[box(0, 0, 1, 1)] * 3,
        crs="EPSG:4326",
    )
    seen = {(170, 66, "descending"): box(5, 5, 6, 6)}
    assert outlines.apply_nisar(plan, seen) == 1
    assert plan.geometry[0].bounds == (5, 5, 6, 6)
    assert plan.geometry[1].bounds == (0, 0, 1, 1) == plan.geometry[2].bounds
    cells = gpd.GeoDataFrame(
        {"path": [46, 47], "row": [27, 27]}, geometry=[box(0, 0, 1, 1)] * 2
    )
    assert outlines.apply_landsat(cells, {(47, 27): Polygon(box(2, 2, 3, 3))}) == 1
    assert cells.geometry[1].bounds == (2, 2, 3, 3)
    assert outlines.apply_nisar(plan, {}) == 0


@pytest.mark.parametrize("w,e,ok", [(-10, 10, True), (-179, 179, False)])
def test_usable(w, e, ok):
    assert outlines._usable(box(w, 0, e, 1)) is ok
