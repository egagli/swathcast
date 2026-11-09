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


def test_landsat_projection_spec():
    t = pd.date_range(T("2026-10-29"), T("2026-12-08T23:00"), freq="6h")
    scenes = pd.DataFrame({"platform": "L8", "path": 1, "row": 1, "time": t})
    start, end = T("2026-11-09"), T("2026-12-09")
    spec = schedule.landsat_projection(scenes, start, end)
    days = projection.PROJECTION_DAYS["landsat"]
    assert spec["from"] == end.isoformat()
    assert spec["to"] == (start + days * DAY).isoformat()
    assert spec["repeat_days"] == 16
    assert spec["base_from"] == (end - 16 * DAY).isoformat()
    assert spec["base_scenes"] == 16 * 4
    # nothing to project: the window already reaches the horizon, or no scenes
    assert schedule.landsat_projection(scenes, start, start + (days + 1) * DAY) is None
    assert schedule.landsat_projection(scenes.iloc[0:0], start, end) is None


def _site(tmp_path, spec, scene_times):
    d = tmp_path / "site"
    d.mkdir()
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
        json.dumps({"postprocessing": {"landsat_projected": spec} if spec else {}})
    )
    (d / publish.SCHEDULE_INDEX).write_text(
        json.dumps({"projections": publish.projections_index(d)})
    )
    return d


SPEC = {
    "from": "2026-12-09T00:00:00+00:00",
    "to": "2027-11-09T00:00:00+00:00",
    "repeat_days": 16,
    "base_from": "2026-11-23T00:00:00+00:00",
    "base_scenes": 2,
}


def test_projection_index_and_contract(tmp_path):
    d = _site(tmp_path, SPEC, ["2026-10-29T18:00Z", "2026-12-08T18:00Z"])
    idx = json.loads((d / publish.SCHEDULE_INDEX).read_text())
    assert idx["projections"]["landsat"] == {
        "from": "2026-12-09T00:00:00Z",
        "to": "2027-11-09T00:00:00Z",
        "repeat_days": 16,
        "base_from": "2026-11-23T00:00:00Z",
    }
    assert publish._check_landsat_projected(d) == []
    # no projection: no entry, nothing to check
    (tmp_path / "b").mkdir()
    d2 = _site(tmp_path / "b", None, ["2026-12-08T18:00Z"])
    assert json.loads((d2 / publish.SCHEDULE_INDEX).read_text())["projections"] == {}
    assert publish._check_landsat_projected(d2) == []


@pytest.mark.parametrize(
    "change, scenes, problem",
    [
        ({"to": "2026-12-01T00:00:00+00:00"}, None, "ends before it starts"),
        ({"base_from": "2026-11-20T00:00:00+00:00"}, None, "base is not one cycle"),
        ({}, ["2026-12-01T18:00Z"], "does not cover"),
    ],
)
def test_projection_contract_catches_problems(tmp_path, change, scenes, problem):
    d = _site(
        tmp_path,
        {**SPEC, **change},
        scenes or ["2026-10-29T18:00Z", "2026-12-08T18:00Z"],
    )
    problems = publish._check_landsat_projected(d)
    assert any(problem in p for p in problems), problems


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
