"""Staleness check used by the weekly watchdog workflow (and by humans)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from swathcast.timeutil import parse_utc, to_iso, utcnow

#: expected cadence of each product, hours; stale = older than 2x cadence
CADENCE_HOURS = {"schedule": 24.0, "latency": 1.0}


def _age_hours(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    return (now - parse_utc(stamp)).total_seconds() / 3600.0


def check(data_dir: Path, now: datetime | None = None, factor: float = 2.0) -> dict:
    """Inspect the data branch checkout and report staleness and failing sources."""
    now = now or utcnow()
    data_dir = Path(data_dir)
    report: dict = {
        "checked_at": to_iso(now),
        "stale": [],
        "failing_sources": [],
        "ages_h": {},
    }

    meta_path = data_dir / "schedule" / "upcoming_passes.meta.json"
    schedule_stamp = (
        json.loads(meta_path.read_text()).get("generated_at")
        if meta_path.exists()
        else None
    )
    health_path = data_dir / "health.json"
    health = json.loads(health_path.read_text()) if health_path.exists() else {}
    latency_stamp = (health.get("sources", {}).get("cdse") or {}).get(
        "last_success_utc"
    )

    for name, stamp in (("schedule", schedule_stamp), ("latency", latency_stamp)):
        age = _age_hours(stamp, now)
        report["ages_h"][name] = None if age is None else round(age, 2)
        if age is None or age > factor * CADENCE_HOURS[name]:
            report["stale"].append(name)

    for key, entry in sorted((health.get("sources") or {}).items()):
        since = entry.get("failing_since_utc")
        if since:
            report["failing_sources"].append(
                {
                    "source": key,
                    "failing_since": since,
                    "last_error": entry.get("last_error"),
                }
            )
    report["ok"] = not report["stale"] and not any(
        now - parse_utc(f["failing_since"]) > timedelta(hours=24)
        for f in report["failing_sources"]
    )
    return report


def issue_body(
    report: dict, repo_url: str = "https://github.com/egagli/swathcast"
) -> str:
    lines = [
        "The watchdog found stale or failing data.",
        "",
        f"Checked at {report['checked_at']}.",
        "",
        "| product | age (h) | stale |",
        "|---|---|---|",
    ]
    for name, age in report["ages_h"].items():
        lines.append(
            f"| {name} | {age if age is not None else 'missing'} | {'yes' if name in report['stale'] else 'no'} |"
        )
    if report["failing_sources"]:
        lines += ["", "Failing sources:", ""]
        for f in report["failing_sources"]:
            lines.append(
                f"- `{f['source']}` since {f['failing_since']}: {f['last_error']}"
            )
    lines += [
        "",
        f"Check the [Actions tab]({repo_url}/actions). If the scheduled workflows were disabled "
        "for inactivity, re-enable them there (the watchdog also tries to).",
    ]
    return "\n".join(lines)
