"""The platform registry (platforms.py, site/platforms.json): its checks, and
that a retired platform is not predicted, projected or flagged as silent."""

import json
from datetime import UTC, datetime

import pandas as pd

from swathcast import platforms, projection

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _reg(tmp_path, state="decommissioned", **extra):
    reg = {
        "reviewed": "2026-11-10",
        "missions": [
            {
                "key": "viirs",
                "label": "VIIRS",
                "platforms": [
                    {
                        "code": "SNPP",
                        "name": "Suomi NPP",
                        "launch": {
                            "date": "2011-10-28",
                            "source": "https://example.org/a",
                        },
                        "status": {
                            "state": state,
                            "since": "2026-11-02",
                            "by": "NOAA",
                            "summary": "Data delivery ended.",
                            "source": "https://example.org/b",
                        },
                        "notices": [
                            {
                                "date": "2026-10-19",
                                "title": "Cessation",
                                "url": "https://example.org/c",
                            }
                        ],
                        **extra,
                    },
                    {
                        "code": "N21",
                        "name": "NOAA-21",
                        "status": {
                            "state": "operational",
                            "source": "https://example.org/d",
                        },
                    },
                ],
            }
        ],
    }
    p = tmp_path / "platforms.json"
    p.write_text(json.dumps(reg))
    platforms.registry.cache_clear()
    return p


def test_registry_retired_and_notes(tmp_path):
    p = _reg(tmp_path)
    assert set(platforms.platforms(p)) == {"SNPP", "N21"}
    assert set(platforms.retired(p)) == {"SNPP"}
    assert platforms.is_retired("SNPP", p) and not platforms.is_retired("N21", p)
    assert (
        platforms.retired_note("SNPP", p)
        == "decommissioned: data ended 2026-11-02 (NOAA)"
    )
    assert platforms.retired_note("N21", p) == ""
    assert platforms.check(p) == []


def test_registry_check_catches_problems(tmp_path):
    p = _reg(tmp_path, state="asleep", notices=[{"date": "soon", "url": "http://x"}])
    problems = platforms.check(p)
    assert any("unknown state" in x for x in problems)
    assert any("notice without a date or https URL" in x for x in problems)


def test_the_shipped_registry_is_valid():
    platforms.registry.cache_clear()
    assert platforms.check() == []
    reg = platforms.platforms()
    # every platform the schedule and the poller know is in it
    from swathcast.tle import NORAD

    assert set(NORAD) - {"S1A"} <= set(reg)


def test_a_retired_platform_is_not_projected(monkeypatch, tmp_path):
    p = _reg(tmp_path)
    monkeypatch.setattr(platforms, "REGISTRY_PATH", p)
    monkeypatch.setattr(platforms.retired_note, "__defaults__", (p,))
    end = pd.Timestamp("2026-12-09", tz="UTC")
    out = projection.spec(
        "viirs", {"SNPP": end, "N21": end}, pd.Timestamp("2026-11-09", tz="UTC")
    )
    assert set(out["platforms"]) == {"N21"}
    assert out["not_projected"]["SNPP"].startswith("decommissioned")


def test_a_retired_platform_is_not_silent(monkeypatch, tmp_path):
    from swathcast import satellites
    from test_satellites import _hourly, _obs, _write

    p = _reg(tmp_path)
    monkeypatch.setattr(platforms.retired_note, "__defaults__", (p,))
    d = tmp_path / "data"
    _write(
        d / "missions" / "viirs",
        _obs(
            "SNPP",
            _hourly(
                datetime(2026, 10, 20, tzinfo=UTC),
                datetime(2026, 11, 2, 15, tzinfo=UTC),
            ),
        ),
    )
    r = satellites.check(d, None, NOW)["platforms"]["SNPP"]
    assert r["status"] == "retired" and "data ended 2026-11-02" in r["note"]
    marker, _ = satellites.issue_body({"platforms": {"SNPP": r}})
    assert marker == "flagged: "


def test_retired_platforms_are_not_predicted(monkeypatch, tmp_path):
    from swathcast import wideswath

    p = _reg(tmp_path)
    monkeypatch.setattr(platforms.is_retired, "__defaults__", (p,))
    called = []
    monkeypatch.setattr(
        wideswath,
        "tile_passes",
        lambda sat, platform, *a, **k: (
            called.append(platform)
            or pd.DataFrame({"i": [], "time": [], "ascending": [], "xtrack_km": []})
        ),
    )
    grid = pd.DataFrame({"tile": [], "lon": [], "lat": []})
    wideswath.sin_tile_passes(
        grid,
        {"SNPP": object(), "N21": object(), "N20": object()},
        "viirs",
        pd.Timestamp("2026-11-10", tz="UTC"),
        pd.Timestamp("2026-11-11", tz="UTC"),
    )
    assert "SNPP" not in called and "N21" in called
