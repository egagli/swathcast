"""Sentinel-2 tile grid parsing and per-tile timing."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from conftest import FIXTURES, load_fixture
from swathcast import s2grid


def sample_tiles():
    return s2grid.parse_kml((FIXTURES / "s2_tiles_sample.kml").read_text())


def test_parse_kml_reads_tiles_and_unwraps_antimeridian_corners():
    tiles = sample_tiles()
    assert list(tiles["tile"]) == ["01CCV", "10TET", "33UVP"]
    ccv = tiles.iloc[0]
    # split into two parts at +/-180 for intersection tests ...
    assert ccv.geometry.geom_type == "MultiPolygon"
    # ... but a single quadrilateral (lon past 180) for drawing
    assert ccv["corners"].shape == (4, 2)
    assert ccv["corners"][:, 0].max() > 180
    assert np.ptp(ccv["corners"][:, 0]) < 10
    tet = tiles.iloc[1]
    assert tet.geometry.geom_type == "Polygon"
    assert tet.geometry.contains(box(-122.9, 46.9, -122.8, 47.0))  # 10TET: WA


def test_site_grid_roundtrips_corners_to_1e4_deg():
    tiles = sample_tiles()
    grid = s2grid.site_grid(tiles)
    for i, c in enumerate(tiles["corners"]):
        x = grid.at[i, "x0"] + np.array([0, *[grid.at[i, f"dx{k}"] for k in (1, 2, 3)]])
        y = grid.at[i, "y0"] + np.array([0, *[grid.at[i, f"dy{k}"] for k in (1, 2, 3)]])
        assert np.allclose(x / s2grid.SCALE, c[:, 0], atol=1e-4)
        assert np.allclose(y / s2grid.SCALE, c[:, 1], atol=1e-4)


def test_tile_times_match_measured_sensing_times():
    """91 tiles of 7 real datatakes: the estimate is within 40 s of the
    granule SENSING_TIME, where the datatake begin is up to 32 min off."""
    errs, naive = [], []
    for dt in load_fixture("s2_tile_sensing.json")["datatakes"]:
        begin, end = pd.Timestamp(dt["begin"]), pd.Timestamp(dt["end"])
        lat = [t["lat"] for t in dt["tiles"]]
        truth = pd.to_datetime([t["sensing_time"] for t in dt["tiles"]], utc=True)
        got = s2grid.tile_times(lat, begin, end, dt["miny"], ascending=False)
        errs.extend((pd.DatetimeIndex(got) - truth).total_seconds())
        naive.extend((begin - truth).total_seconds())
    errs = np.abs(errs)
    assert len(errs) == 91
    assert errs.max() < 40
    assert np.abs(naive).max() > 1800


def test_tile_passes_joins_tiles_to_nominal_datatakes():
    import geopandas as gpd

    tiles = sample_tiles()
    t0 = pd.Timestamp("2026-10-01T18:50:00Z")
    sched = gpd.GeoDataFrame(
        {
            "sensor": ["sentinel-2", "sentinel-2", "sentinel-1"],
            "platform": ["S2B", "S2C", "S1C"],
            "mode": ["NOBS", "DARK-O", "IW"],
            "track": [13, 13, 13],
            "begin_time": [t0, t0, t0],
            "end_time": [t0 + pd.Timedelta(minutes=10)] * 3,
        },
        geometry=[box(-125, 40, -120, 50)] * 3,
        crs="EPSG:4326",
    )
    got = s2grid.tile_passes(tiles, sched)
    assert list(got["tile"]) == ["10TET"]  # the dark take and S1 are ignored
    row = got.iloc[0]
    assert row["platform"] == "S2B" and row["track"] == 13
    assert row["direction"] == "descending"
    assert t0 <= row["time"] <= t0 + pd.Timedelta(minutes=10)
    assert row["coverage"] == pytest.approx(1.0)  # the box covers the whole tile
    # a swath clipping only the tile's western strip
    clip = sched.iloc[:1].copy()
    lon0 = tiles.set_index("tile").loc["10TET"].geometry.bounds[0]
    clip["geometry"] = [box(lon0 - 5, 40, lon0 + 0.2, 50)]
    part = s2grid.tile_passes(tiles, gpd.GeoDataFrame(clip, crs="EPSG:4326"))
    assert 0.05 < part.iloc[0]["coverage"] < 0.4


@pytest.mark.parametrize("ascending", [False, True])
def test_tile_times_are_clamped_to_the_datatake(ascending):
    b, e = pd.Timestamp("2026-10-01T10:00Z"), pd.Timestamp("2026-10-01T10:05Z")
    got = s2grid.tile_times([80.0, -80.0], b, e, 0.0, ascending)
    assert all(b <= t <= e for t in pd.DatetimeIndex(got))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_decodes_the_grid_and_parses_filters(tmp_path):
    """site/footprints.js must decode s2_grid rows back to the ESA corners."""
    tiles = sample_tiles()
    rows = s2grid.site_grid(tiles).to_dict(orient="records")
    site = Path(__file__).resolve().parents[1] / "src" / "swathcast" / "site"
    script = tmp_path / "s2.mjs"
    script.write_text(
        f"""
import {{ s2Tile, parseRanges, inRanges, tileFilter }} from {json.dumps((site / "footprints.js").as_uri())};
const rows = {json.dumps(rows, default=int)};
const tiles = rows.map(s2Tile);
const f = tileFilter("10T, 1C");
console.log(JSON.stringify({{
  rings: tiles.map((t) => t.geojson.coordinates[0]),
  ranges: parseRanges("10-20, 35"),
  inside: [inRanges(parseRanges("10-20"), 15), inRanges(parseRanges("10-20"), 21), inRanges(null, 3)],
  tiles: ["10TET", "11TET", "01CCV"].map(f),
}}));
"""
    )
    out = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    for ring, corners in zip(out["rings"], tiles["corners"], strict=True):
        assert np.allclose(np.array(ring[:4]), corners, atol=1e-4)
        assert ring[0] == ring[-1]
    assert out["ranges"] == [[10, 20], [35, 35]]
    assert out["inside"] == [True, False, True]
    assert out["tiles"] == [True, False, True]


def test_orbit_timing_matches_measured_sensing_times():
    """SGP4 closest approach (elements of 2026-09-29, 2-3 days after the
    sensing): within 10 s of the granule SENSING_TIME for all 91 tiles."""
    from swathcast import ephemeris

    omm = load_fixture("celestrak_omm.json")
    errs = []
    for dt in load_fixture("s2_tile_sensing.json")["datatakes"]:
        sat = ephemeris.satrec(omm[dt["platform"]])
        begin, end = pd.Timestamp(dt["begin"]), pd.Timestamp(dt["end"])
        lon = [t["lon"] for t in dt["tiles"]]
        lat = [t["lat"] for t in dt["tiles"]]
        truth = pd.to_datetime([t["sensing_time"] for t in dt["tiles"]], utc=True)
        got, direction = s2grid.orbit_times(sat, lon, lat, begin, end)
        assert direction == "descending"
        errs.extend((pd.DatetimeIndex(got) - truth).total_seconds())
    errs = np.abs(errs)
    assert len(errs) == 91
    assert errs.max() < 10
    assert np.median(errs) < 1


def test_tile_passes_uses_the_orbit_when_elements_are_given():
    import geopandas as gpd

    tiles = sample_tiles()
    omm = load_fixture("celestrak_omm.json")
    t0 = pd.Timestamp("2026-10-01T18:50:00Z")
    sched = gpd.GeoDataFrame(
        {
            "sensor": ["sentinel-2"] * 2,
            "platform": ["S2B", "S2C"],
            "mode": ["NOBS"] * 2,
            "track": [13, 13],
            "begin_time": [t0, t0],
            "end_time": [t0 + pd.Timedelta(minutes=10)] * 2,
        },
        geometry=[box(-125, 40, -120, 50)] * 2,
        crs="EPSG:4326",
    )
    got = s2grid.tile_passes(tiles, sched, {"S2B": omm["S2B"]})
    assert dict(zip(got["platform"], got["timing"], strict=True)) == {
        "S2B": "orbit",
        "S2C": "latitude",
    }
