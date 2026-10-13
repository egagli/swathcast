import json
from datetime import UTC, datetime

from swathcast.watchdog import check, issue_body

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def write(tmp_path, schedule_at, latency_at, failing_since=None):
    (tmp_path / "schedule").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schedule" / "upcoming_passes.meta.json").write_text(
        json.dumps({"generated_at": schedule_at})
    )
    sources = {"cdse": {"last_success_utc": latency_at, "failing_since_utc": None}}
    if failing_since:
        sources["asf_grd"] = {
            "failing_since_utc": failing_since,
            "last_error": "HTTPError: 503",
        }
    (tmp_path / "health.json").write_text(json.dumps({"sources": sources}))


def test_fresh(tmp_path):
    write(tmp_path, "2026-10-05T03:41:00+00:00", "2026-10-05T11:23:00Z")
    report = check(tmp_path, now=NOW)
    assert report["ok"] and report["stale"] == []


def test_stale_latency_and_schedule(tmp_path):
    write(tmp_path, "2026-10-02T03:41:00+00:00", "2026-10-05T09:00:00Z")
    report = check(tmp_path, now=NOW)
    assert set(report["stale"]) == {"schedule", "latency"}
    assert not report["ok"]
    assert "| latency | 3.0 | yes |" in issue_body(report)


def test_missing_data_is_stale(tmp_path):
    report = check(tmp_path, now=NOW)
    assert set(report["stale"]) == {"schedule", "latency"}


def test_source_failing_over_24h(tmp_path):
    write(
        tmp_path,
        "2026-10-05T03:41:00+00:00",
        "2026-10-05T11:23:00Z",
        failing_since="2026-10-04T08:00:00Z",
    )
    report = check(tmp_path, now=NOW)
    assert not report["ok"] and report["stale"] == []
    assert "asf_grd" in issue_body(report)


def test_source_failing_briefly_is_reported_but_ok(tmp_path):
    write(
        tmp_path,
        "2026-10-05T03:41:00+00:00",
        "2026-10-05T11:23:00Z",
        failing_since="2026-10-05T10:00:00Z",
    )
    report = check(tmp_path, now=NOW)
    assert report["ok"] and report["failing_sources"]
