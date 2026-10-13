"""publish_landsat_l1: recent USGS Level-1 scenes, one file per WRS-2 path."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pyarrow.parquet as pq

from swathcast.missions import MISSIONS
from swathcast.publish import (
    LANDSAT_L1_COLUMNS,
    _check_landsat_l1,
    landsat_l1_scenes,
    publish_landsat_l1,
)
from swathcast.storage import normalize

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def tracking_rows():
    """Two L1 scenes (one tracked by two sources), one L2, one too old."""
    rows = []

    def add(gid, ptype, path_row, days_ago, source, platform="L9"):
        t = NOW - timedelta(days=days_ago)
        rows.append(
            dict(
                granule_id=gid,
                source=source,
                product_type=ptype,
                platform=platform,
                mode="OLI_TIRS",
                relative_orbit=int(path_row[:3]),
                orbit_direction="ascending"
                if int(path_row[3:]) > 122
                else "descending",
                datatake_id=path_row,
                coverage_cell=path_row,
                sensing_start=t,
                sensing_stop=t,
                timeliness="T2",
                tracking_started_utc=t + timedelta(hours=1),
                status="seen",
                n_checks=1,
            )
        )

    add(
        "LC09_L1GT_139217_20260929_20260929_02_T2", "L1", "139217", 7, "usgs_landsat_l1"
    )
    add("LC09_L1GT_139217_20260929_20260929_02_T2", "L1", "139217", 7, "hls_l30")
    add(
        "LC08_L1TP_047027_20260916_20260917_02_T1",
        "L1",
        "047027",
        20,
        "usgs_landsat_l1",
        "L8",
    )
    add(
        "LC08_L2SP_047027_20260916_20260918_02_T1_SR",
        "L2",
        "047027",
        20,
        "usgs_landsat_l2",
        "L8",
    )
    add(
        "LC08_L1TP_047027_20260601_20260602_02_T1",
        "L1",
        "047027",
        127,
        "usgs_landsat_l1",
        "L8",
    )
    return normalize(pd.DataFrame(rows))


def test_scenes_are_l1_recent_and_unique():
    scenes = landsat_l1_scenes(tracking_rows(), NOW)
    assert list(scenes.columns) == LANDSAT_L1_COLUMNS
    assert sorted(scenes["id"]) == [
        "LC08_L1TP_047027_20260916_20260917_02_T1",
        "LC09_L1GT_139217_20260929_20260929_02_T2",
    ]
    night = scenes[scenes["path"] == 139].iloc[0]
    assert (night["row"], night["platform"], night["category"]) == (217, "L9", "T2")


def test_published_one_file_per_path(tmp_path):
    data, out = tmp_path / "data", tmp_path / "site"
    MISSIONS["landsat"].store(data).write(tracking_rows(), [(2026, 6), (2026, 9)])
    assert publish_landsat_l1(data, out, NOW) == 2
    files = sorted(p.name for p in (out / "landsat_l1").iterdir())
    assert files == ["047.parquet", "139.parquet"]
    t = pq.read_table(out / "landsat_l1" / "139.parquet").to_pandas()
    assert t["row"].tolist() == [217]
    assert _check_landsat_l1(out) == []
    # a file holding another path is caught
    (out / "landsat_l1" / "047.parquet").rename(out / "landsat_l1" / "048.parquet")
    assert _check_landsat_l1(out) == ["landsat_l1/048.parquet holds paths [47]"]


def test_no_landsat_table_publishes_nothing(tmp_path):
    assert publish_landsat_l1(tmp_path / "data", tmp_path / "site", NOW) == 0
