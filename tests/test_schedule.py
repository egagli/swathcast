from datetime import UTC, datetime

import pytest

shapely = pytest.importorskip("shapely")
from shapely.geometry import Polygon  # noqa: E402

from swathcast.schedule import solar_direction, wrap_antimeridian  # noqa: E402


def test_solar_direction():
    # Olympic Mountains (lon -123.5): ascending pass ~02:00 UTC (18:00 LST)
    assert (
        solar_direction(datetime(2026, 10, 1, 2, 0, tzinfo=UTC), -123.5, 47.8)
        == "ascending"
    )
    # descending ~14:00 UTC (06:00 LST)
    assert (
        solar_direction(datetime(2026, 10, 1, 14, 10, tzinfo=UTC), -123.5, 47.8)
        == "descending"
    )
    # wraps across midnight
    assert (
        solar_direction(datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 150.0, 0.0)
        == "descending"
    )
    # ambiguous near the poles
    assert solar_direction(datetime(2026, 10, 1, 2, 0, tzinfo=UTC), 0.0, -75.0) == ""


def test_wrap_antimeridian_splits_unwrapped_polygon():
    poly = Polygon([(-170, 60), (-190, 62), (-200, 70), (-175, 72), (-170, 60)])
    out = wrap_antimeridian(poly)
    assert out.geom_type == "MultiPolygon"
    minx, _, maxx, _ = out.bounds
    assert minx >= -180 and maxx <= 180
    assert out.area == pytest.approx(poly.area, rel=1e-9)


def test_wrap_antimeridian_leaves_normal_polygons():
    poly = Polygon([(-124, 47), (-123, 47), (-123, 48), (-124, 48), (-124, 47)])
    assert wrap_antimeridian(poly) is poly


def test_wrap_antimeridian_wrapped_jump():
    poly = Polygon([(175, 10), (-175, 10), (-175, 12), (175, 12), (175, 10)])
    out = wrap_antimeridian(poly)
    assert out.geom_type == "MultiPolygon"
    assert out.area == pytest.approx(20.0)


def test_wrs2_grid_keeps_path_row_direction():
    gpd = pytest.importorskip("geopandas")
    from swathcast.schedule import wrs2_grid

    ring = [(-124 + i * 0.001, 47 + (i % 2) * 0.0005) for i in range(50)] + [
        (-122, 49),
        (-124, 49),
    ]
    outlines = gpd.GeoDataFrame(
        {"path": [46], "row": [27], "direction": ["descending"]},
        geometry=[Polygon(ring)],
        crs="EPSG:4326",
    )
    grid = wrs2_grid(outlines)
    assert list(grid.columns) == ["path", "row", "direction", "geometry"]
    assert grid.iloc[0].geometry.exterior.coords.__len__() < 50


def test_geodesic_edge_crosses_the_pole_not_the_parallel():
    """An edge between two points 180 deg apart at 80 N runs over the pole
    (great circle), not along 80 N as a straight lon/lat segment would."""
    from swathcast.schedule import geodesic_ring

    ring = geodesic_ring([(-90, 80), (90, 80), (90, 79), (-90, 79), (-90, 80)])
    assert max(y for _, y in ring) > 89.5  # 50 km steps
    lons = [x for x, _ in ring]
    assert all(abs(b - a) <= 180 for a, b in zip(lons, lons[1:], strict=False))


def test_geodesic_ring_around_a_pole_is_closed_through_it():
    from shapely.geometry import Point, Polygon

    from swathcast.schedule import geodesic_ring

    # a cap of four points at 85 N, one per quadrant: contains the pole
    ring = geodesic_ring([(0, 85), (90, 85), (180, 85), (-90, 85), (0, 85)])
    poly = Polygon(ring)
    assert poly.is_valid
    assert any(abs(y - 90) < 1e-9 for _, y in ring)
    assert poly.buffer(0).contains(Point(ring[0][0] + 45, 89))


def test_densify_then_wrap_a_polar_swath():
    from shapely.geometry import Polygon

    from swathcast.schedule import geodesic_densify, wrap_antimeridian

    # an Arctic swath crossing the antimeridian with sparse vertices
    swath = Polygon([(150, 70), (-150, 82), (-140, 80), (160, 68), (150, 70)])
    out = wrap_antimeridian(geodesic_densify(swath))
    minx, miny, maxx, maxy = out.bounds
    assert -180 <= minx and maxx <= 180 and out.geom_type == "MultiPolygon"
    assert maxy > 82  # the great circle bulges poleward of the vertices
    assert len(out.geoms[0].exterior.coords) > 10
