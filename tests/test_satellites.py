"""The per-satellite check (satellites.py): a satellite that stops is told
apart from a quiet data stream, a slow distributor, our own failing sources
and a stretch with nothing planned."""

import json
from datetime import UTC, datetime, timedelta

import pandas as pd

from swathcast import satellites
from swathcast.storage import Store, empty_frame

NOW = datetime(2026, 11, 9, 23, 0, tzinfo=UTC)


def _obs(platform, times, delay_h=2.0, prefix="g"):
    rows = []
    for i, t in enumerate(times):
        rows.append(
            {
                "granule_id": f"{prefix}{platform}{i}",
                "source": "ref",
                "platform": platform,
                "sensing_start": t,
                "status": "seen",
                "first_seen_utc": t + timedelta(hours=delay_h),
            }
        )
    df = empty_frame()
    df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    return df


def _write(store_root, df):
    st = Store(store_root)
    months = sorted({(t.year, t.month) for t in df["sensing_start"]})
    st.write(df, months)


def _hourly(start, end):
    return list(pd.date_range(start, end, freq="1h", tz="UTC").to_pydatetime())


def _health(path, failing_since=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sources": {"ref": {"failing_since_utc": failing_since}}})
    )


def test_a_silent_satellite_and_a_silent_stream(tmp_path):
    d = tmp_path / "data"
    # Sentinel-1 store: S1C current, S1D silent for 5 days
    s1 = pd.concat(
        [
            _obs("S1C", _hourly(NOW - timedelta(days=20), NOW - timedelta(hours=3))),
            _obs("S1D", _hourly(NOW - timedelta(days=20), NOW - timedelta(days=5))),
        ]
    )
    _write(d, s1)
    # NISAR: L-band (ASF) silent 17 days, S-band (ISRO) current
    _write(
        d / "missions" / "nisar",
        _obs(
            "NISAR",
            _hourly(NOW - timedelta(days=40), NOW - timedelta(days=17)),
            prefix="l",
        ),
    )
    _write(
        d / "missions" / "nisar_s",
        _obs(
            "NISAR",
            _hourly(NOW - timedelta(days=30), NOW - timedelta(hours=10)),
            prefix="s",
        ),
    )
    r = satellites.check(d, None, NOW)["platforms"]
    assert r["S1C"]["status"] == "ok"
    assert r["S1D"]["status"] == "silent"
    assert "No data from S1D" in r["S1D"]["note"]
    assert r["NISAR"]["status"] == "stream_silent"
    assert (
        "L-band products at ASF" in r["NISAR"]["note"]
        and "still arrive" in r["NISAR"]["note"]
    )
    marker, body = satellites.issue_body({"platforms": r})
    assert marker == "flagged: NISAR=stream_silent, S1D=silent"
    assert body.startswith(f"<!-- {marker} -->")


def test_slow_delivery_is_not_silence(tmp_path):
    """Landsat 8's public products appear 7-12 days after sensing."""
    d = tmp_path / "data"
    times = _hourly(NOW - timedelta(days=50), NOW - timedelta(days=6))
    _write(d / "missions" / "landsat", _obs("L8", times, delay_h=240))
    assert satellites.check(d, None, NOW)["platforms"]["L8"]["status"] == "ok"


def test_failing_sources_make_it_unknown(tmp_path):
    d = tmp_path / "data"
    _write(
        d / "missions" / "viirs",
        _obs("SNPP", _hourly(NOW - timedelta(days=20), NOW - timedelta(days=7))),
    )
    _health(
        d / "missions" / "viirs" / "health.json", failing_since="2026-11-02T00:00:00Z"
    )
    r = satellites.check(d, None, NOW)["platforms"]["SNPP"]
    assert r["status"] == "unknown" and "failing" in r["note"]


def test_nothing_planned_is_not_silence(tmp_path):
    """A satellite with no planned passes since its last data (the schedule
    covers the stretch) is quiet, not silent."""
    d = tmp_path / "data"
    _write(d, _obs("S1D", _hourly(NOW - timedelta(days=20), NOW - timedelta(days=4))))
    sched = tmp_path / "schedule"
    sched.mkdir()
    t = pd.to_datetime([NOW - timedelta(days=5), NOW + timedelta(days=1)], utc=True)
    pd.DataFrame({"platform": ["S1D", "S1D"], "begin_time": t}).to_parquet(
        sched / "upcoming_passes.parquet"
    )
    r = satellites.check(d, sched, NOW)["platforms"]["S1D"]
    assert r["status"] == "ok" and r["streams"]["s1"]["due_since"] == 0
    # with passes due, the same silence is flagged
    t = pd.to_datetime(
        [NOW - timedelta(days=5), NOW - timedelta(days=2), NOW - timedelta(days=1)],
        utc=True,
    )
    pd.DataFrame({"platform": ["S1D"] * 3, "begin_time": t}).to_parquet(
        sched / "upcoming_passes.parquet"
    )
    r = satellites.check(d, sched, NOW)["platforms"]["S1D"]
    assert r["status"] == "silent" and r["streams"]["s1"]["due_since"] == 2


def test_a_long_normal_gap_raises_the_bar(tmp_path):
    """S1D's normal gaps reach ~28 h: silence must exceed 1.5 x the longest."""
    d = tmp_path / "data"
    times = _hourly(NOW - timedelta(days=20), NOW - timedelta(days=12))
    times += _hourly(
        NOW - timedelta(days=8), NOW - timedelta(days=3, hours=1)
    )  # a 4-day gap before
    _write(d, _obs("S1D", times))
    r = satellites.check(d, None, NOW)["platforms"]["S1D"]["streams"]["s1"]
    assert r["limit_h"] >= 1.5 * 96 - 1 and r["state"] == "ok"
