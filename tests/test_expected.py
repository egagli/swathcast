from datetime import UTC, datetime, timedelta

from swathcast.stats import expected_availability

BEGIN = datetime(2026, 10, 6, 14, 2, tzinfo=UTC)
ENTRY = {
    "all": {
        "stratum": "all",
        "measured": True,
        "median_h": 2.0,
        "p90_h": 6.0,
        "n_events": 500,
    },
    "S1D": {
        "stratum": "S1D",
        "measured": True,
        "median_h": 1.5,
        "p90_h": 5.0,
        "n_events": 200,
    },
    "S1C": {
        "stratum": "S1C",
        "measured": False,
        "median_h": None,
        "p90_h": None,
        "n_events": 3,
    },
}


def test_uses_platform_stratum_when_measured():
    out = expected_availability(BEGIN, "S1D", ENTRY)
    assert out["stratum"] == "S1D"
    assert out["median_at"] == BEGIN + timedelta(hours=1.5)
    assert out["p90_at"] == BEGIN + timedelta(hours=5)


def test_falls_back_to_all_platforms():
    for platform in ("S1C", "S1E"):
        out = expected_availability(BEGIN, platform, ENTRY)
        assert out["stratum"] == "all"
        assert out["median_at"] == datetime(2026, 10, 6, 16, 2, tzinfo=UTC)
        assert out["p90_at"] == datetime(2026, 10, 6, 20, 2, tzinfo=UTC)


def test_not_measured_returns_none():
    assert (
        expected_availability(
            BEGIN,
            "S1D",
            {
                "all": {
                    "stratum": "all",
                    "measured": False,
                    "median_h": None,
                    "p90_h": None,
                    "n_events": 0,
                }
            },
        )
        is None
    )
    assert expected_availability(BEGIN, "S1D", {}) is None


def test_p90_may_be_unknown_while_median_is_known():
    entry = {
        "all": {
            "stratum": "all",
            "measured": True,
            "median_h": 30.0,
            "p90_h": None,
            "n_events": 40,
        }
    }
    out = expected_availability(BEGIN, "S1C", entry)
    assert out["median_at"] == BEGIN + timedelta(hours=30)
    assert out["p90_at"] is None
