"""Acquisition plans read from the agencies (plans.py) and orbit timing."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from conftest import FIXTURES, load_fixture
from swathcast import ephemeris, plans


def test_esa_plan_files_take_the_platform_from_the_file_name():
    html = (FIXTURES / "esa_s1_page_snippet.html").read_text()
    files = plans.esa_plan_files("sentinel-1", html)
    assert files and all(f.url.startswith(plans.ESA) for f in files)
    names = {f.name: f for f in files}
    # ESA listed an S1D file in the Sentinel-1C section on 2026-09-29
    misfiled = [f for n, f in names.items() if n.startswith("s1d")]
    assert misfiled and all(f.platform == "S1D" for f in misfiled)
    f = names["s1d_mp_user_20260928t180643_20261004t195100"]
    assert f.start == pd.Timestamp("2026-09-28T18:06:43Z")
    assert f.end == pd.Timestamp("2026-10-04T19:51:00Z")
    assert plans.esa_plan_files("sentinel-2", html) == []


def test_parse_esa_kml_keeps_every_attribute():
    df = plans.parse_esa_kml(FIXTURES / "esa_s1c_plan_excerpt.kml")
    assert len(df) == 3
    row = df.iloc[2]
    assert (row["Mode"], row["Polarisation"], row["DatatakeId"]) == (
        "IW",
        "DV",
        "133DC",
    )
    assert int(row["OrbitRelative"]) == 117 and int(row["OrbitAbsolute"]) == 9665
    assert row["begin"] == pd.Timestamp("2026-09-29T18:32:24Z")
    assert len(row["ring"]) >= 4


def _plan(start, end, begins):
    f = plans.PlanFile("S1C", pd.Timestamp(start), pd.Timestamp(end), "u", start)
    b = pd.to_datetime(begins, utc=True)
    return f, pd.DataFrame(
        {"begin": b, "end": b + pd.Timedelta(minutes=1), "Mode": "IW"}
    )


def test_newest_plan_wins_over_its_window():
    old = _plan(
        "2026-09-20T00:00Z",
        "2026-10-10T00:00Z",
        ["2026-09-25T01:00Z", "2026-10-01T01:00Z", "2026-10-08T01:00Z"],
    )
    new = _plan("2026-09-28T00:00Z", "2026-10-05T00:00Z", ["2026-10-01T01:05Z"])
    got = plans.newest_wins([old, new])
    assert list(got["begin"].dt.strftime("%m-%d %H:%M")) == [
        "10-01 01:05",  # the newest file's version of the re-planned datatake
        "09-25 01:00",  # before the newest file starts: old file
        "10-08 01:00",  # after the newest file ends: old file again
    ]
    assert "10-01 01:00" not in set(got["begin"].dt.strftime("%m-%d %H:%M"))  # dropped


def test_landsat_calendar_lists_paths_and_rows():
    cycles = load_fixture("usgs_cycles.json")
    cpr = load_fixture("usgs_cycle_path_row_excerpt.json")
    cal = plans.landsat_calendar(cycles, cpr, date(2026, 9, 11), 1)
    assert set(cal["platform"]) == {"L8", "L9"}
    first = cpr["landsat_9"][next(iter(cpr["landsat_9"]))][0]
    got = cal[(cal["platform"] == "L9") & (cal["path"] == first["path"])]
    assert sorted(got["row"]) == sorted(first["rows"])
    assert (cal["day"] == date(2026, 9, 11)).all()
    # 16 days later the same cycle day comes back
    later = plans.landsat_calendar(cycles, cpr, date(2026, 9, 27), 1)
    assert len(later) == len(cal)
    assert plans.landsat_calendar(cycles, cpr, date(2026, 9, 12), 1).empty


def test_nisar_kmz_frames_dates_and_polarization():
    df = plans.parse_nisar_kmz((FIXTURES / "nisar_plan_excerpt.kmz").read_bytes())
    assert len(df) > 10 and df["track"].notna().all() and df["frame"].notna().all()
    assert set(df["direction"]) <= {"ascending", "descending"}
    assert set(df["mode_source"]) <= {"plan", "earlier cycle", "none"}
    assert not df.duplicated(["track", "frame", "day"]).any()
    # codes -> real channels (checked against ASF RSLC attributes)
    assert plans.nisar_polarization("L:SCI:DH:20M+05N:FS:B4:D09") == "L HH+HV"
    assert (
        plans.nisar_polarization("L:SCI:QD:05W+---:FS:B4:D09  S:DB:CP:25W:B4")
        == "L HH+VV, S RH+RV"
    )
    assert (
        plans.nisar_polarization(
            "L:SCI:DH:20M+05N:FS:B4:D09, L:SCI:SV:05W+---:FS:B4:D09"
        )
        == "L VV, L HH+HV"
    )
    assert plans.nisar_polarization("S:DB:SH:37W:B4:D10") == "S HH"
    assert (
        plans.nisar_polarization("L:SCI:SH:40M+05N:FS:B4:D02  S:--:SH:37W:B4")
        == "L HH, S HH"
    )
    assert plans.nisar_polarization("") is None
    assert [plans.true_polarization(c) for c in ("DV", "DH", "SV", "SH", None)] == [
        "VV+VH",
        "HH+HV",
        "VV",
        "HH",
        None,
    ]


def test_nisar_dates_merged_and_blank_modes_carried():
    rows = [
        # the plan lists each date twice, possibly with different modes
        (1, 11, "ascending", "2026-05-09", "L:SCI:DH:20M+05N:FS:B4:D09"),
        (1, 11, "ascending", "2026-05-09", "L:SCI:SV:05W+---:FS:B4:D09"),
        (1, 11, "ascending", "2026-05-21", ""),
        (1, 11, "ascending", "2026-05-21", ""),
        (2, 5, "descending", "2026-05-21", ""),
    ]
    df = plans.merge_nisar_dates(
        pd.DataFrame(
            rows, columns=["track", "frame", "direction", "day", "mode"]
        ).assign(ring=None)
    )
    assert len(df) == 3
    got = {(r.track, r.day): (r.mode, r.mode_source) for r in df.itertuples()}
    both = "L:SCI:DH:20M+05N:FS:B4:D09, L:SCI:SV:05W+---:FS:B4:D09"
    assert got[(1, "2026-05-09")] == (both, "plan")
    assert got[(1, "2026-05-21")] == (both, "earlier cycle")
    assert got[(2, "2026-05-21")] == ("", "none")
    html = '<a href="https://assets.science.nasa.gov/content/dam/science/missions/nisar/kmz/NISAR_ROP358_TFDB_ObservationPlan_CY2026-20260305.kmz?emrc=1">'
    assert plans.nisar_plan_url(html).endswith("ObservationPlan_CY2026-20260305.kmz")


@pytest.fixture(scope="module")
def truth():
    return load_fixture("orbit_timing_truth.json")


@pytest.fixture(scope="module")
def omm():
    return load_fixture("celestrak_omm.json")


def test_landsat_scene_times_from_the_orbit(truth, omm):
    """40 USGS STAC scenes: the pass nearest the WRS-2 centre on the scene's
    date is within 10 s of the scene time (the 10:12 formula: tens of min)."""
    errs, formula = [], []
    df = pd.DataFrame(truth["landsat"])
    df["t"] = pd.to_datetime(df["datetime"], utc=True)
    for (platform, day, direction), g in df.groupby(
        [df.platform, df.t.dt.floor("D"), df.direction]
    ):
        sat = ephemeris.satrec(omm[platform])
        d0 = day.timestamp()
        t, dist = ephemeris.nearest_pass(
            sat, g.lon, g.lat, d0 - 7200, d0 + 93600, ascending=direction == "ascending"
        )
        errs.extend(t - g.t.map(pd.Timestamp.timestamp))
        assert (dist < 60).all()
        formula.extend(
            (
                plans.formula_time(day, lon, direction, plans.LANDSAT_LST_H) - tt
            ).total_seconds()
            for lon, tt in zip(g.lon, g.t, strict=True)
        )
    assert len(errs) == 40
    assert np.abs(errs).max() < 10
    assert np.median(np.abs(formula)) > 600


def test_nisar_frame_times_from_the_orbit(truth, omm):
    """40 CMR RSLC frames: the pass with the frame ~567 km to its left on the
    plan date is within 30 s of the granule mid-time."""
    df = pd.DataFrame(truth["nisar"])
    df["t"] = pd.to_datetime(df["mid"], utc=True)
    df["day"] = df.t.dt.floor("D")
    df["ring"] = [[(lon, lat)] * 2 for lon, lat in zip(df.lon, df.lat, strict=True)]
    sat = ephemeris.satrec(omm["NISAR"])
    when, dist = plans.nisar_times(df, sat)
    err = when - df.t.map(pd.Timestamp.timestamp).to_numpy()
    assert np.isfinite(when).all()
    assert np.abs(err).max() < 30
    assert (
        np.abs(dist - plans.NISAR_FRAME_OFFSET_KM) < 60
    ).all()  # as accepted in nisar_times


def test_nisar_antimeridian_pieces_joined():
    # ROP358 T136_F39: a 4-point sliver east of 180 and the main piece west of it
    east = [(-180.0, 71.875), (-180.0, 72.1115), (-179.619, 71.995), (-180.0, 71.875)]
    west = [
        (180.0, 72.1115),
        (170.0, 73.5),
        (170.0, 71.0),
        (180.0, 71.875),
        (180.0, 72.1115),
    ]
    rows = [(136, 39, "ascending", "2026-10-02", "", r) for r in (east, west)]
    df = plans.merge_nisar_dates(
        pd.DataFrame(
            rows, columns=["track", "frame", "direction", "day", "mode", "ring"]
        )
    )
    assert len(df) == 1
    lons = [x for x, _ in df.iloc[0]["ring"]]
    assert min(lons) == pytest.approx(170.0) and max(lons) == pytest.approx(
        180.381, abs=1e-3
    )


def test_landsat_turnaround_rows_timed_from_the_orbit(omm):
    """Row 246 (~82 N, where the track turns): the direction filter dropped
    L9's pass here and the scene took the formula, an orbit off. Three USGS
    scenes (Earth Search landsat-c2-l2, 2026-08-25/26), elements a month
    later: each within a minute, from the orbit."""
    import geopandas as gpd
    from shapely.geometry import box

    truth = [  # platform, path, cycle day, WRS-2 centre lon, scene time
        ("L8", 181, 2, 88.7477, "2026-08-26T10:17:07.548Z"),
        ("L9", 173, 2, 101.1082, "2026-08-26T09:27:45.354Z"),
        ("L8", 69, 1, -98.2052, "2026-08-25T22:44:56.100Z"),
    ]
    lat = 81.8108
    cpr = {"landsat_8": {}, "landsat_9": {}}
    for platform, path, cycle, _, _ in truth:
        mission = "landsat_8" if platform == "L8" else "landsat_9"
        cpr[mission].setdefault(str(cycle), []).append({"path": path, "rows": [246]})
    wrs2 = gpd.GeoDataFrame(
        {
            "path": [p for _, p, _, _, _ in truth],
            "row": [246] * 3,
            "direction": ["ascending"] * 3,
        },
        geometry=[
            box(x - 0.01, lat - 0.01, x + 0.01, lat + 0.01) for *_, x, _ in truth
        ],
        crs=4326,
    )
    sats = {p: ephemeris.satrec(omm[p]) for p in ("L8", "L9")}
    got = plans.landsat_scenes(
        load_fixture("usgs_cycles.json"),
        cpr,
        wrs2,
        pd.Timestamp("2026-08-25", tz="UTC"),
        2,
        sats,
    )
    assert len(got) == 3
    assert (got["timing"] == "orbit").all()
    for platform, path, _, _, when in truth:
        row = got[(got["platform"] == platform) & (got["path"] == path)].iloc[0]
        assert abs((row["time"] - pd.Timestamp(when)).total_seconds()) < 60


class _EsaSession:
    """The plans page, then each plan file: 404 for names in `missing`."""

    def __init__(self, names, missing):
        self.names, self.missing, self.calls = names, set(missing), []

    def get(self, url, **kw):
        import requests

        self.calls.append(url)
        r = requests.Response()
        r.url, r.status_code = url, 200
        if url == plans.ESA_PAGES["sentinel-1"]:
            r._content = "".join(
                f'<a href="/documents/d/sentinel/{n}">{n}</a>' for n in self.names
            ).encode()
        elif url.rsplit("/", 1)[1] in self.missing:
            r.status_code = 404
        else:
            r._content = (FIXTURES / "esa_s1c_plan_excerpt.kml").read_bytes()
        return r


def test_sentinel_plans_skip_a_listed_file_that_is_a_404(tmp_path):
    old = "s1c_mp_user_20260920t000000_20261010t000000"
    new = "s1c_mp_user_20260928t000000_20261020t000000"
    s = _EsaSession([old, new], missing=[new])
    df = plans.sentinel_plans(
        "sentinel-1",
        pd.Timestamp("2026-09-29T00:00Z"),
        pd.Timestamp("2026-09-30T00:00Z"),
        s,
        tmp_path,
    )
    assert len(df) == 3 and set(df["plan_file"]) == {old}
    assert s.calls.count(f"{plans.ESA}/documents/d/sentinel/{new}") == 1  # no retries
    assert not (tmp_path / "esa" / f"{new}.kml").exists()  # retried next run

    with pytest.raises(RuntimeError, match="none of the current plan files"):
        plans.sentinel_plans(
            "sentinel-1",
            pd.Timestamp("2026-09-29T00:00Z"),
            pd.Timestamp("2026-09-30T00:00Z"),
            _EsaSession([new], missing=[new]),
            tmp_path,
        )
