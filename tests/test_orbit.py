"""Sentinel-1 orbit timing: ANX lattice fit, burst timing, plan matching."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import load_fixture
from swathcast import orbit

EPOCH = datetime(2026, 9, 20, 3, 0, 0, tzinfo=UTC)
PERIOD = 5924.5731
OFFSET = 42  # track of orbit k is ((k + 42) % 175) + 1


def synthetic_products(n=400, seed=0, jitter_s=0.1):
    """Slices on a known lattice, including slices just after an ANX whose
    startTimeFromAscendingNode is measured from the previous ANX (> 1 period)."""
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n):
        k = int(rng.integers(-80, 80))
        anx = float(rng.uniform(0, PERIOD))
        if rng.random() < 0.1:  # datatake began in orbit k-1, slice is in orbit k
            anx_reported, k_ref = anx + PERIOD, k - 1
        else:
            anx_reported, k_ref = anx, k
        start = EPOCH + timedelta(
            seconds=k_ref * PERIOD + anx_reported + rng.normal(0, jitter_s)
        )
        rows.append(
            {
                "sensing_start": start,
                "anx_start_s": anx_reported,
                "relative_orbit": (k + OFFSET) % 175 + 1,
            }
        )
    return pd.DataFrame(rows)


def test_fit_recovers_the_lattice():
    df = synthetic_products()
    m = orbit.fit(pd.Series(df.sensing_start), df.anx_start_s, df.relative_orbit, "S1X")
    assert m.period_s == pytest.approx(PERIOD, abs=1e-3)
    # The fitted epoch may be any lattice point: it must lie on the true
    # lattice and give the same track for the same orbit.
    shift = (m.epoch - EPOCH).total_seconds() / PERIOD
    assert abs(shift - round(shift)) * PERIOD < 0.1
    assert int(m.track(-round(shift))) == OFFSET + 1
    assert m.resid_max_s < 0.6


def test_fit_rejects_inconsistent_tracks():
    df = synthetic_products()
    df["relative_orbit"] = np.random.default_rng(1).integers(1, 176, len(df))
    with pytest.raises(ValueError, match="inconsistent"):
        orbit.fit(pd.Series(df.sensing_start), df.anx_start_s, df.relative_orbit, "S1X")


def model():
    return orbit.OrbitModel("S1X", EPOCH, PERIOD, OFFSET, 100, 0.1, 0.3)


def test_burst_times_repeat_every_175_orbits_on_the_right_track():
    m = model()
    track, tanx = 137, 1234.5
    times = orbit.burst_times(m, track, tanx, EPOCH, EPOCH + timedelta(days=24.5))
    assert len(times) == 2
    assert (times[1] - times[0]).total_seconds() == pytest.approx(175 * PERIOD)
    for t in times:
        k = round(((t - EPOCH).total_seconds() - tanx) / PERIOD)
        assert int(m.track(k)) == track


def test_burst_time_from_anx_matches_asf_to_the_millisecond():
    bursts = load_fixture("burst_map_attrs.json")["bursts"]
    for b in bursts:
        got = orbit.burst_time_from_anx(
            int(b["PATH_NUMBER"]), int(b["BURST_ID_RELATIVE"]), b["SUBSWATH_NAME"]
        )
        assert got == pytest.approx(float(b["TIME_FROM_ANX_SEC"]), abs=1e-3), b[
            "BURST_ID_FULL"
        ]
    assert {b["SUBSWATH_NAME"] for b in bursts} == {"IW1", "IW2", "IW3"}


def test_acquisitions_only_inside_planned_iw_datatakes():
    m = model()
    track, tanx = 50, 3000.0
    [when] = orbit.burst_times(m, track, tanx, EPOCH, EPOCH + timedelta(days=12))
    plan = pd.DataFrame(
        [
            {
                "platform": "S1X",
                "mode": "IW",
                "track": track,
                "begin_time": when - timedelta(seconds=40),
                "end_time": when + timedelta(seconds=90),
            },
            {
                "platform": "S1X",
                "mode": "EW",
                "track": track,
                "begin_time": when + timedelta(days=12) - timedelta(seconds=40),
                "end_time": when + timedelta(days=12, seconds=90),
            },
        ]
    )
    plan["begin_time"] = pd.to_datetime(plan["begin_time"], utc=True)
    plan["end_time"] = pd.to_datetime(plan["end_time"], utc=True)
    got = orbit.acquisitions(
        {"S1X": m}, plan, track, tanx, EPOCH, EPOCH + timedelta(days=30)
    )
    assert [a["time"] for a in got] == [when]  # the EW datatake does not count
    assert got[0]["datatake_track"] == track
    # a burst whose instant misses the window is not acquired
    assert (
        orbit.acquisitions(
            {"S1X": m}, plan, track, tanx + 200, EPOCH, EPOCH + timedelta(days=30)
        )
        == []
    )


def test_model_json_roundtrip():
    m = model()
    again = orbit.OrbitModel.from_dict(m.to_dict())
    assert (
        again.offset == m.offset and abs((again.epoch - m.epoch).total_seconds()) < 1e-3
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_implementation_matches_python(tmp_path):
    """site/footprints.js must time bursts exactly like orbit.py."""
    m = model()
    cases = [
        (t, tanx) for t in (1, 13, 88, 137, 175) for tanx in (5.0, 2176.28, 5900.0)
    ]
    start, end = EPOCH, EPOCH + timedelta(days=13)
    expected = [
        [x.timestamp() * 1000 for x in orbit.burst_times(m, t, tanx, start, end)]
        for t, tanx in cases
    ]
    bursts = load_fixture("burst_map_attrs.json")["bursts"]
    site = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "swathcast"
        / "site"
        / "footprints.js"
    )
    script = tmp_path / "parity.mjs"
    script.write_text(
        f"""
import {{ burstTimes, burstTimeFromAnx, parseModels }} from {json.dumps(site.as_uri())};
const models = parseModels({{ platforms: {{ S1X: {json.dumps(m.to_dict())} }} }});
const cases = {json.dumps(cases)};
const times = cases.map(([t, a]) => burstTimes(models.S1X, t, a, {start.timestamp() * 1000}, {end.timestamp() * 1000}));
const bursts = {json.dumps(bursts)};
const tanx = bursts.map((b) => burstTimeFromAnx(Number(b.PATH_NUMBER), Number(b.BURST_ID_RELATIVE), b.SUBSWATH_NAME));
console.log(JSON.stringify({{ times, tanx }}));
"""
    )
    out = json.loads(
        subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=True
        ).stdout
    )
    for got, want in zip(out["times"], expected, strict=True):
        assert np.allclose(got, want, atol=1.0)  # 1 ms
    assert np.allclose(
        out["tanx"],
        [
            orbit.burst_time_from_anx(
                int(b["PATH_NUMBER"]), int(b["BURST_ID_RELATIVE"]), b["SUBSWATH_NAME"]
            )
            for b in bursts
        ],
        atol=1e-6,
    )


def test_fit_is_not_fooled_by_a_median_between_lattice_points():
    """Regression, 2026-09-29: two equal groups one orbit apart put the median
    ANX time half-way between lattice points and split the track offset."""
    rng = np.random.default_rng(3)
    rows = []
    for k in (3, 4):
        for _ in range(20):
            anx = float(rng.uniform(0, PERIOD))
            start = EPOCH + timedelta(seconds=k * PERIOD + anx + rng.normal(0, 0.05))
            rows.append(
                {
                    "sensing_start": start,
                    "anx_start_s": anx,
                    "relative_orbit": (k + OFFSET) % 175 + 1,
                }
            )
    two = pd.DataFrame(rows)
    m = orbit.fit(
        pd.Series(two.sensing_start), two.anx_start_s, two.relative_orbit, "S1X"
    )
    shift = (m.epoch - EPOCH).total_seconds() / PERIOD
    assert abs(shift - round(shift)) * PERIOD < 0.2
    assert int(m.track(-round(shift))) == OFFSET + 1
