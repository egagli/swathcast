"""Projection past a plan's end (projection.py), the Landsat projection the
schedule publishes (schedule.landsat_projection), its index entry and
contract check (publish.py), and the hindcast's scoring (hindcast.py)."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from swathcast import hindcast, plans, projection, publish, schedule

T = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
DAY = pd.Timedelta(days=1)


def _plan(platform, times, dur_s=60):
    t = pd.to_datetime(times, utc=True)
    return pd.DataFrame(
        {"platform": platform, "begin": t, "end": t + pd.Timedelta(seconds=dur_s)}
    )


def test_project_repeat_shifts_the_last_cycle_by_whole_repeats():
    df = _plan("S1C", ["2026-01-01T10:00Z", "2026-01-08T10:00Z", "2026-01-12T05:00Z"])
    end = T("2026-01-13")  # base cycle: [Jan 1, Jan 13)
    out = projection.project_repeat(
        df,
        {"S1C": end},
        pd.Timedelta(days=12),
        T("2026-02-01"),
        time_cols=("begin", "end"),
    )
    assert (out["basis"] == "projected").all()
    assert out["begin"].min() >= end
    assert out["begin"].max() < T("2026-02-01")
    got = sorted(zip(out["begin"], out["cycles"], strict=True))
    assert got == [
        (T("2026-01-13T10:00Z"), 1),
        (T("2026-01-20T10:00Z"), 1),
        (T("2026-01-24T05:00Z"), 1),
        (T("2026-01-25T10:00Z"), 2),  # Feb 1 10:00 is past the cut-off
    ]
    # every row keeps its duration and lands a whole number of cycles later
    assert ((out["end"] - out["begin"]) == pd.Timedelta(seconds=60)).all()
    base = {t.time() for t in df["begin"]}
    assert {t.time() for t in out["begin"]} == base


def test_project_repeat_cycles_and_cutoff():
    df = _plan("L8", ["2026-01-01T18:00Z"])
    out = projection.project_repeat(
        df,
        {"L8": T("2026-01-02")},
        pd.Timedelta(days=16),
        T("2026-03-01"),
        time_cols=("begin", "end"),
    )
    # Jan 1 + 16 k days before Mar 1: Jan 17, Feb 2, Feb 18
    assert list(out["begin"]) == [
        T("2026-01-17T18:00Z"),
        T("2026-02-02T18:00Z"),
        T("2026-02-18T18:00Z"),
    ]
    assert list(out["cycles"]) == [1, 2, 3]


def test_project_repeat_only_projects_listed_platforms_and_handles_nothing():
    df = pd.concat(
        [_plan("S2A", ["2026-01-01T10:00Z"]), _plan("S2B", ["2026-01-01T11:00Z"])]
    )
    out = projection.project_repeat(
        df,
        {"S2B": T("2026-01-02")},
        pd.Timedelta(days=10),
        T("2026-01-20"),
        time_cols=("begin", "end"),
    )
    assert set(out["platform"]) == {"S2B"}
    empty = projection.project_repeat(
        df,
        {"S2B": T("2026-01-02")},
        pd.Timedelta(days=10),
        T("2026-01-02"),
        time_cols=("begin", "end"),
    )
    assert empty.empty and {"cycles", "basis"} <= set(empty.columns)


def test_repeat_of_uses_the_platforms_plan_period():
    assert projection.repeat_of("sentinel-2", "S2B") == pd.Timedelta(days=10)
    assert projection.repeat_of("sentinel-2", "S2A") == pd.Timedelta(days=20)
    assert projection.repeat_of("landsat", "L9") == pd.Timedelta(days=16)


def test_interval_overlap():
    a = pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T00:05Z"], utc=True)
    b = a + pd.Timedelta(minutes=2)
    union = projection.merge_intervals(
        pd.to_datetime(
            ["2026-01-01T00:01:00Z", "2026-01-01T00:01:30Z", "2026-01-01T00:10:00Z"],
            utc=True,
        ),
        pd.to_datetime(
            ["2026-01-01T00:01:45Z", "2026-01-01T00:03:00Z", "2026-01-01T00:11:00Z"],
            utc=True,
        ),
    )
    assert len(union[0]) == 2  # the first two merge
    got = projection.overlap_ns(a, b, union) / 1e9
    assert list(got) == [60.0, 0.0]


# ---------------------------------------------------------------- Landsat
def _calendar():
    """A USGS-shaped calendar: cycle day d+1 on 1/d+1/1970 (1970-01-01 is a
    Thursday; any permutation will do), path 10+d on cycle day d+1 for L8 and
    path 30+d for L9, rows 20 and 21."""
    perm = [(5 * d) % 16 + 1 for d in range(16)]
    cycles = {
        "landsat_8": {f"1/{d + 1}/1970": {"cycle": str(perm[d])} for d in range(16)}
    }
    cpr = {
        "landsat_8": {
            str(c): [{"path": 10 + c, "rows": [20, 21]}] for c in range(1, 17)
        },
        "landsat_9": {
            str(c): [{"path": 30 + c, "rows": [20, 21]}] for c in range(1, 17)
        },
    }
    return cycles, cpr


def test_repeating_the_last_cycle_reproduces_the_usgs_calendar():
    """The calendar repeats every 16 days, so the projection's dates are the
    calendar's: shifting a 30-day window's last cycle gives exactly the
    calendar's later days."""
    cycles, cpr = _calendar()
    start = date(2026, 11, 9)
    cal = plans.landsat_calendar(cycles, cpr, start, 30)
    df = cal.assign(
        begin=pd.to_datetime(cal["day"]).dt.tz_localize("UTC") + pd.Timedelta(hours=18)
    )
    end = T("2026-12-09")
    out = projection.project_repeat(
        df, {"L8": end, "L9": end}, pd.Timedelta(days=16), T("2027-05-08")
    )
    later = plans.landsat_calendar(cycles, cpr, date(2026, 12, 9), 150)
    want = {(p, d, pa, r) for p, d, pa, r in later.itertuples(index=False)}
    got = {
        (p, b.date(), pa, r)
        for p, b, pa, r in out[["platform", "begin", "path", "row"]].itertuples(
            index=False
        )
    }
    assert got == want


def test_spec_per_platform_with_one_horizon():
    start = T("2026-11-09")
    out = projection.spec(
        "sentinel-2",
        {"S2A": T("2026-11-16"), "S2B": T("2026-11-10"), "S2C": T("2028-01-01")},
        start,
    )
    assert out["to"] == (start + projection.HORIZON_DAYS * DAY).isoformat()
    # S2C's plan reaches past the horizon: nothing to project
    assert set(out["platforms"]) == {"S2A", "S2B"}
    a = out["platforms"]["S2A"]
    assert a["repeat_days"] == 20 and a["from"] == T("2026-11-16").isoformat()
    assert a["base_from"] == T("2026-10-27").isoformat()
    assert out["platforms"]["S2B"]["repeat_days"] == 10
    assert projection.spec("not-a-mission", {"X": start}, start) is None


def test_spec_leaves_out_platforms_that_do_not_repeat():
    end = T("2026-12-09")
    out = projection.spec("modis", {"Terra": end, "Aqua": end}, T("2026-11-09"))
    # neither repeats: nothing projected, but why is kept
    assert out["platforms"] == {} and set(out["not_projected"]) == {"Terra", "Aqua"}
    out = projection.spec("viirs", {"N21": end, "N20": end}, T("2026-11-09"))
    assert set(out["platforms"]) == {"N21", "N20"} and "not_projected" not in out
    mixed = projection.spec("modis", {"Terra": end, "X": end}, T("2026-11-09"))
    assert set(mixed["platforms"]) == {"X"} and "Terra" in mixed["not_projected"]


def test_schedule_projections_and_trim():
    gpd = pytest.importorskip("geopandas")
    start, end = T("2026-11-09"), T("2026-12-09")
    rows = pd.DataFrame(
        {
            "sensor": ["sentinel-1"] * 3 + ["nisar"] * 2,
            "platform": ["S1C", "S1C", "S1D", "NISAR", "NISAR"],
            "begin_time": pd.to_datetime(
                [
                    "2026-11-01T10:00Z",
                    "2026-11-27T10:00Z",
                    "2026-11-20T09:00Z",
                    "2026-12-20T06:00Z",
                    "2026-12-31T18:00Z",
                ],
                utc=True,
            ),
        }
    )
    rows["end_time"] = rows["begin_time"] + pd.Timedelta(minutes=2)
    gdf = gpd.GeoDataFrame(rows, geometry=[None] * len(rows))
    out = schedule.projections(
        gdf, start, end, ["sentinel-1", "nisar", "landsat"], {"landsat": {"L8": end}}
    )
    s1c = out["sentinel-1"]["platforms"]["S1C"]
    assert s1c["from"] == T("2026-11-27T10:02Z").isoformat()
    assert out["nisar"]["platforms"]["NISAR"]["from"] == T("2027-01-01").isoformat()
    assert out["landsat"]["platforms"]["L8"]["from"] == end.isoformat()
    # the extra lookback is dropped, but each platform's base cycle is kept
    kept = schedule.trim_to_base(
        gdf[gdf["sensor"] == "sentinel-1"],
        {"S1C": T("2026-11-15"), "S1D": T("2026-11-08")},
        T("2026-11-07"),
    )
    assert list(kept["platform"]) == ["S1C", "S1D"]


def test_repeat_times_shift_the_same_frame_by_whole_cycles():
    base = pd.DataFrame(
        {
            "track": [1, 1, 2],
            "frame": [10, 10, 20],
            "direction": ["ascending"] * 3,
            "day": pd.to_datetime(["2026-11-10", "2026-11-22", "2026-11-12"], utc=True),
            "time": pd.to_datetime(
                ["2026-11-10T06:01Z", "2026-11-22T06:02Z", "2026-11-12T06:30Z"],
                utc=True,
            ),
        }
    )
    later = pd.DataFrame(
        {
            "track": [1, 2, 3],
            "frame": [10, 20, 30],
            "direction": ["ascending"] * 3,
            "day": pd.to_datetime(["2026-12-16", "2026-12-13", "2026-12-16"], utc=True),
        }
    )
    t = plans.repeat_times(
        later, base, ["track", "frame", "direction"], pd.Timedelta(days=12)
    )
    assert t.iloc[0] == T("2026-12-16T06:02Z")  # from the latest, Nov 22 + 2 cycles
    assert pd.isna(t.iloc[1])  # Dec 13 is not a whole number of cycles after Nov 12
    assert pd.isna(t.iloc[2])  # a frame the window does not image


def _site(
    tmp_path,
    specs,
    scene_times,
    window=("2026-11-09T00:00:00+00:00", "2026-12-09T00:00:00+00:00"),
):
    d = tmp_path / "site"
    d.mkdir(exist_ok=True)
    pd.DataFrame(
        {
            "platform": "L8",
            "path": [1] * len(scene_times),
            "row": 1,
            "direction": "descending",
            "time": pd.to_datetime(scene_times, utc=True),
            "timing": "orbit",
        }
    ).to_parquet(d / "landsat_scenes.parquet")
    (d / "upcoming_passes.meta.json").write_text(
        json.dumps(
            {
                "window": list(window),
                "plan_pages": {"landsat": "https://landsat.usgs.gov/landsat_acq"},
                "postprocessing": {"projections": specs or {}},
            }
        )
    )
    (d / publish.SCHEDULE_INDEX).write_text(
        json.dumps({"projections": publish.projections_index(d), "sensors": {}})
    )
    return d


SPEC = {
    "landsat": {
        "to": "2027-11-09T00:00:00+00:00",
        "platforms": {
            "L8": {
                "from": "2026-12-09T00:00:00+00:00",
                "base_from": "2026-11-23T00:00:00+00:00",
                "repeat_days": 16,
            },
        },
    },
    "sentinel-3": {
        "to": "2027-11-09T00:00:00+00:00",
        "platforms": {
            "S3A": {
                "from": "2026-12-09T00:00:00+00:00",
                "base_from": "2026-11-12T00:00:00+00:00",
                "repeat_days": 27,
            },
        },
        "not_projected": {"S3X": "why"},
    },
}


def test_projection_index_and_contract(tmp_path):
    d = _site(tmp_path, SPEC, ["2026-10-29T18:00Z", "2026-12-08T18:00Z"])
    idx = json.loads((d / publish.SCHEDULE_INDEX).read_text())
    assert idx["projections"]["landsat"] == {
        "to": "2027-11-09T00:00:00Z",
        "platforms": {
            "L8": {
                "from": "2026-12-09T00:00:00Z",
                "base_from": "2026-11-23T00:00:00Z",
                "repeat_days": 16,
            }
        },
    }
    # by the map's mission key, with what is not projected and why
    assert idx["projections"]["s3"]["not_projected"] == {"S3X": "why"}
    assert publish.plan_pages_index(d) == {
        "landsat": "https://landsat.usgs.gov/landsat_acq"
    }
    assert publish._check_projections(d) == []
    (tmp_path / "b").mkdir()
    d2 = _site(tmp_path / "b", None, ["2026-12-08T18:00Z"])
    assert json.loads((d2 / publish.SCHEDULE_INDEX).read_text())["projections"] == {}
    assert publish._check_projections(d2) == []


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"base_from": "2026-11-20T00:00:00+00:00"}, "base is not one cycle"),
        (
            {
                "from": "2028-01-01T00:00:00+00:00",
                "base_from": "2027-12-16T00:00:00+00:00",
            },
            "after the horizon",
        ),
    ],
)
def test_projection_contract_catches_problems(tmp_path, change, problem):
    spec = json.loads(json.dumps(SPEC))
    spec["landsat"]["platforms"]["L8"].update(change)
    d = _site(tmp_path, spec, ["2026-10-29T18:00Z", "2026-12-08T18:00Z"])
    problems = publish._check_projections(d)
    assert any(problem in p for p in problems), problems


def test_contract_wants_one_horizon(tmp_path):
    spec = json.loads(json.dumps(SPEC))
    spec["sentinel-3"]["to"] = "2027-06-01T00:00:00+00:00"
    d = _site(tmp_path, spec, ["2026-12-08T18:00Z"])
    assert any("more than one horizon" in p for p in publish._check_projections(d))


def test_split_schedule_puts_rows_past_the_window_in_monthly_files(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    d = _site(tmp_path, SPEC, ["2026-12-08T18:00Z"])
    t = pd.to_datetime(
        [
            "2026-11-20T06:00Z",
            "2026-12-08T06:00Z",
            "2026-12-15T06:00Z",
            "2027-01-02T06:00Z",
            "2026-11-21T07:00Z",
        ],
        utc=True,
    )
    gdf = gpd.GeoDataFrame(
        {
            "sensor": ["nisar"] * 4 + ["sentinel-1"],
            "platform": ["NISAR"] * 4 + ["S1C"],
            "track": [1, 1, 1, 1, 5],
            "frame": [10, 10, 10, 10, None],
            "begin_time": t,
            "end_time": t,
            "polarization": ["HH"] * 5,
            "plan_url": ["https://x/plan.kmz"] * 4 + ["https://esa/s1c.kml"],
        },
        geometry=[box(0, 0, 1, 1)] * 5,
        crs="EPSG:4326",
    )
    gdf.to_parquet(d / "upcoming_passes.parquet")
    idx = publish.split_schedule(d)
    nisar = idx["sensors"]["nisar"]
    assert nisar["rows"] == 2
    assert [c["file"] for c in nisar["later"]] == [
        "passes_nisar_2026-12.parquet",
        "passes_nisar_2027-01.parquet",
    ]
    assert all((d / c["file"]).exists() for c in nisar["later"])
    # the plan's end counts every row; the schedule's end only the window's
    assert nisar["plan_ends"]["NISAR"] == "2027-01-02T06:00:00Z"
    assert idx["schedule_end"] == "2026-12-08T06:00:00Z"
    assert "later" not in idx["sensors"]["sentinel-1"]
    assert idx["plan_pages"] == {"landsat": "https://landsat.usgs.gov/landsat_acq"}
    assert publish._check_projections(d) == []


# ---------------------------------------------------------------- hindcast
def test_esa_scores_are_time_weighted():
    p = _plan("S1C", ["2026-01-01T00:00Z", "2026-01-01T01:00Z"], dur_s=100).assign(
        mode="IW", pol="DV"
    )
    t = _plan("S1C", ["2026-01-01T00:00:50Z"], dur_s=100).assign(mode="IW", pol="DH")
    # 50 s of the first projected take was later planned; none of the second
    assert hindcast._overlap_s(p, t, []) == pytest.approx(50)
    assert hindcast._overlap_s(t, p, []) == pytest.approx(50)
    # the same mode but not the same polarization: no match
    assert hindcast._overlap_s(p, t, ["mode", "pol"]) == 0


def test_summarize_esa_pools_by_lead():
    rows = []
    for base, (proj, hit) in enumerate([(100, 90), (100, 50)]):
        rows.append(
            {
                "sensor": "sentinel-1",
                "platform": "S1C",
                "base_file": f"f{base}",
                "base_end": T("2026-01-01"),
                "cycles": 1,
                "lead_days": 6.0,
                "projected_s": proj,
                "planned_s": 100.0,
                "hit_any_s": hit,
                "caught_any_s": hit,
                "hit_mode_s": hit,
                "caught_mode_s": hit,
            }
        )
    out = hindcast.summarize_esa(pd.DataFrame(rows), by_platform=False)
    r = out.iloc[0]
    assert r["lead"] == "(0, 30]" and r["bases"] == 2
    assert r["precision"] == pytest.approx(0.7) and r["recall"] == pytest.approx(0.7)
    assert r["precision_same_mode_p10"] == pytest.approx(0.54)


def test_summarize_landsat():
    act = pd.DataFrame(
        {
            "platform": "L8",
            "path": 1,
            "row": 1,
            "time": pd.to_datetime(["2026-01-01T18:00Z"], utc=True),
            "on_cal": [True],
            "on_cal_1d": [True],
        }
    )
    cal = pd.DataFrame({"platform": ["L8", "L8"], "acquired": [True, False]})
    shift = pd.DataFrame({"cycles": [1, 1, 3], "error_s": [2.0, -4.0, 30.0]})
    sg = pd.DataFrame({"lead_days": [-5.0, -200.0], "error_s": [1.0, np.nan]})
    out = hindcast.summarize_landsat(act, cal, shift, sg)
    assert out["acquired_on_calendar_date"] == 1.0
    assert out["calendar_days_acquired"] == 0.5
    one = next(r for r in out["repeat_shift"] if r["cycles"] == "(0, 1]")
    assert one["n"] == 2 and one["median_abs_s"] == pytest.approx(3.0)
    far = next(r for r in out["sgp4"] if r["days_from_epoch"] == "(120, 240]")
    assert far["found"] == 0.0
    assert "Landsat 8/9" in hindcast.markdown({"generated_at": "x", "landsat": out})


def test_summarize_wide_bins_by_lead():
    df = pd.DataFrame(
        {
            "mission": "viirs",
            "platform": "N21",
            "lead_days": [16, 16, 32, 352],
            "along_s": [3.0, -5.0, 8.0, 60.0],
            "cross_km": [1.0, 1.0, 2.0, 9.0],
        }
    )
    out = hindcast.summarize_wide(df)
    assert [r["lead"] for r in out] == ["(0, 30]", "(30, 60]", "(240, 400]"]
    assert out[0]["n"] == 2 and out[0]["median_abs_s"] == pytest.approx(4.0)
    md = hindcast.markdown({"generated_at": "x", "wide": out})
    assert "N21" in md


def test_nisar_hindcast_scores_the_plan_against_itself(monkeypatch):
    days = pd.date_range(T("2026-01-01"), T("2026-03-31"), freq="D")
    rows = []
    for d in days:
        # frame 1 every 12 days from Jan 1; frame 2 once, never again
        if (d - days[0]).days % 12 == 0:
            rows.append((1, 10, "ascending", d, "L_DH"))
    rows.append((2, 20, "ascending", T("2026-01-05"), "L_DH"))
    plan = pd.DataFrame(rows, columns=["track", "frame", "direction", "day", "mode"])
    monkeypatch.setattr(plans, "nisar_plan", lambda session, cache: (plan, "https://x"))
    out = hindcast.nisar_hindcast(None, None, max_lead_days=36)
    first = out[out["base_file"] == "2026-01-13"]
    one = first[first["cycles"] == 1].iloc[0]
    # the base (Jan 1-12) has frame 1 and frame 2; only frame 1 repeats
    assert one["projected_s"] == 2 and one["hit_any_s"] == 1 and one["planned_s"] == 1
    later = out[out["base_file"] == "2026-02-12"]
    assert (later["hit_any_s"] == later["projected_s"]).all()


def test_hindcast_json_has_no_nan(tmp_path, monkeypatch):
    """Browsers cannot parse NaN: one would make the whole file unreadable."""
    monkeypatch.setattr(
        hindcast,
        "s3_hindcast",
        lambda session: pd.DataFrame(
            {
                "mission": ["sentinel-3"],
                "platform": ["S3A"],
                "lead_days": [27],
                "along_s": [1.0],
                "cross_km": [np.nan],
            }
        ),
    )
    hindcast.run(tmp_path, sensors=("sentinel-3",))
    text = (tmp_path / "hindcast.json").read_text()
    assert "NaN" not in text and json.loads(text)["wide"][0]["median_cross_km"] is None
