"""Assemble a site from synthetic data and hold it to the data contract."""

import json
import shutil
import struct
from importlib.resources import files

import pyarrow as pa
import pyarrow.parquet as pq

from swathcast.config import TrackerConfig
from swathcast.publish import check_site, publish, split_schedule
from swathcast.storage import Store
from test_summary import NOW, synthetic


def wkb_polygon(ring):
    body = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring))
    return body + b"".join(struct.pack("<dd", x, y) for x, y in ring)


def write_schedule(path):
    ring = [
        (-124.5, 47.0),
        (-122.5, 47.0),
        (-122.5, 49.0),
        (-124.5, 49.0),
        (-124.5, 47.0),
    ]
    t = pa.table(
        {
            "sensor": ["sentinel-1", "sentinel-2"],
            "platform": ["S1D", "S2A"],
            "mode": ["IW", "INS-NOBS"],
            "orbit_direction": ["descending", ""],
            "track": pa.array([137, 13], pa.int32()),
            "begin_time": pa.array(
                [1_790_000_000_000, 1_790_050_000_000], pa.timestamp("ms", tz="UTC")
            ),
            "end_time": pa.array(
                [1_790_000_060_000, 1_790_050_060_000], pa.timestamp("ms", tz="UTC")
            ),
            "time_is_estimated": [False, False],
            "geometry": [wkb_polygon(ring), wkb_polygon(ring)],
        }
    )
    pq.write_table(t, path)
    path.with_suffix(".meta.json").write_text(
        json.dumps({"generated_at": "2026-10-19T03:41:00+00:00"})
    )


def build_site(tmp_path):
    data = tmp_path / "data"
    Store(data).write(synthetic(300), [(2026, 9), (2026, 10)])
    Store(data).write_json(
        "health.json",
        {"sources": {"cdse": {"last_success_utc": "2026-10-19T23:23:00Z"}}},
    )
    site = tmp_path / "site"
    site.mkdir()
    for asset in (files("swathcast") / "site").iterdir():
        shutil.copyfile(asset, site / asset.name)
    # the synthetic table holds these three sources only
    config = TrackerConfig(source_options={"cdse_grd": {}, "asf_grd": {}, "pc_rtc": {}})
    publish(data, site / "data", now=NOW, config=config)
    write_schedule(site / "data" / "upcoming_passes.parquet")
    split_schedule(site / "data")
    orbits = {
        "platforms": {
            "S1D": {
                "platform": "S1D",
                "epoch": "2026-10-01T00:00:00.000000Z",
                "period_s": 5924.5714,
                "offset": 3,
                "n": 500,
                "resid_std_s": 0.1,
                "resid_max_s": 0.3,
            }
        }
    }
    (site / "data" / "orbits.json").write_text(json.dumps(orbits))
    return site


def test_assembled_site_meets_contract(tmp_path):
    site = build_site(tmp_path)
    assert check_site(site) == []
    summary = json.loads((site / "data" / "latency.json").read_text())
    assert summary["data_as_of"] == "2026-10-19T23:23:00Z"


def test_contract_catches_problems(tmp_path):
    site = build_site(tmp_path)
    (site / "dashboard.js").unlink()
    summary = json.loads((site / "data" / "latency.json").read_text())
    summary["overall"]["pc_rtc"]["measured"] = False
    summary["overall"]["pc_rtc"]["median_h"] = 72.0  # an invented number
    summary["audit"]["missing_rows"] = 3
    del summary["trend"]
    (site / "data" / "latency.json").write_text(json.dumps(summary))
    (site / "data" / "latest.json").write_text('{"granules": [{"dir": NaN}]}')
    (site / "data" / "orbits.json").write_text(json.dumps({"platforms": {}}))
    problems = check_site(site)
    assert any("latest.json is not strict JSON" in p for p in problems)
    assert any("dashboard.js" in p for p in problems)
    assert any("no timing model for S1D" in p for p in problems)
    assert any("pc_rtc" in p and "not measured" in p for p in problems)
    assert any("missing_rows = 3" in p for p in problems)
    assert any("trend" in p for p in problems)


def test_schedule_split_per_sensor(tmp_path):
    site = build_site(tmp_path)
    index = json.loads((site / "data" / "passes_index.json").read_text())
    assert set(index["sensors"]) == {"sentinel-1", "sentinel-2"}
    s1 = index["sensors"]["sentinel-1"]
    assert s1["file"] == "passes_sentinel-1.parquet" and s1["rows"] == 1
    assert list(s1["plan_ends"]) == ["S1D"]
    assert index["schedule_end"] == index["sensors"]["sentinel-2"]["plan_ends"]["S2A"]
    rows = pq.read_table(site / "data" / s1["file"]).to_pylist()
    assert [r["platform"] for r in rows] == ["S1D"]
    (site / "data" / "passes_sentinel-2.parquet").unlink()
    assert any("sentinel-2 file" in p for p in check_site(site))
