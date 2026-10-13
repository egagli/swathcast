"""Command line: ``swathcast {schedule,poll,extents,tle,summarize,viewer,check-site,watchdog}``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from swathcast.timeutil import parse_utc


def _cmd_schedule(args) -> int:
    from swathcast.schedule import build

    build(args.output, days=args.days, sensors=args.sensors)
    return 0


def _cmd_poll(args) -> int:
    from swathcast.missions import run_mission
    from swathcast.tracker import run_poll

    now = parse_utc(args.now) if args.now else None
    code = run_poll(args.data_dir, now=now).exit_code
    for key in filter(None, (args.missions or "").split(",")):
        # Sentinel-1 has already been written; a crash in another mission
        # must not cost its data.
        try:
            result = run_mission(key, args.data_dir, now=now)
            if result is not None:  # None: skipped (credentials not set)
                code = max(code, result.exit_code)
        except Exception:  # noqa: BLE001
            logging.getLogger("swathcast").exception("mission %s crashed", key)
            code = max(code, 1)
    return code


def _cmd_summarize(args) -> int:
    from swathcast.publish import copy_schedule, publish

    now = parse_utc(args.now) if args.now else None
    publish(args.data_dir, args.out, now=now)
    if args.schedule:
        copy_schedule(args.schedule, args.out)
    return 0


def _cmd_extents(args) -> int:
    from swathcast.extents import update

    # Never fails the job: without these the previews are placed approximately.
    print(json.dumps(update(args.data_dir, budget_s=args.budget_seconds)))
    return 0


def _cmd_tle(args) -> int:
    from swathcast.tle import update

    payload = update(args.output, max_age_h=args.max_age_hours)
    # Never fails the job: positions are a convenience, and old elements are kept.
    print(
        f"tle.json: {len(payload['satellites'])} satellites, failed: {payload['failed']}"
    )
    return 0


def _cmd_viewer(args) -> int:
    from swathcast.viewer import serve

    serve(
        args.schedule,
        args.latency,
        args.latest,
        args.health,
        args.port,
        not args.no_browser,
    )
    return 0


def _cmd_check_site(args) -> int:
    from swathcast.publish import check_site

    problems = check_site(args.site_dir)
    for problem in problems:
        print(f"contract: {problem}", file=sys.stderr)
    print("site data contract OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _cmd_watchdog(args) -> int:
    from swathcast.watchdog import check, issue_body

    report = check(args.data_dir)
    print(json.dumps(report, indent=2))
    if args.issue_body:
        Path(args.issue_body).write_text(issue_body(report))
    return 0 if report["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swathcast", description=__doc__)
    parser.add_argument(
        "-l",
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "schedule", help="build the upcoming-pass GeoParquet from the agencies' plans"
    )
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--output", type=Path, default=Path("upcoming_passes.parquet"))
    p.add_argument(
        "--sensors",
        nargs="+",
        default=None,
        help="default: sentinel-1 sentinel-2 landsat nisar modis viirs sentinel-3",
    )
    p.set_defaults(func=_cmd_schedule)

    p = sub.add_parser(
        "poll", help="run one latency-poller pass over the data directory"
    )
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument(
        "--now", help="override the current time (ISO 8601, for replays/tests)"
    )
    p.add_argument(
        "--missions",
        default="s2,landsat,nisar,nisar_s,modis,viirs,s3",
        help="other missions to poll after Sentinel-1 (comma-separated; '' for none)",
    )
    p.set_defaults(func=_cmd_poll)

    p = sub.add_parser(
        "summarize", help="write the site's latency.json / latest.json / health.json"
    )
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path, default=Path("site/data"))
    p.add_argument(
        "--schedule", type=Path, help="also copy this schedule parquet (+ sidecar)"
    )
    p.add_argument("--now")
    p.set_defaults(func=_cmd_summarize)

    p = sub.add_parser(
        "extents",
        help="exact extents of NISAR and OPERA browse images (Earthdata login)",
    )
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--budget-seconds", type=float, default=480)
    p.set_defaults(func=_cmd_extents)

    p = sub.add_parser("tle", help="refresh orbital elements for the map (CelesTrak)")
    p.add_argument("--output", type=Path, default=Path("data/tle.json"))
    p.add_argument("--max-age-hours", type=float, default=6.0)
    p.set_defaults(func=_cmd_tle)

    p = sub.add_parser("viewer", help="serve the site locally")
    p.add_argument("--schedule", type=Path, required=True)
    p.add_argument("--latency", type=Path)
    p.add_argument("--latest", type=Path)
    p.add_argument("--health", type=Path)
    p.add_argument("--port", type=int, default=8472)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=_cmd_viewer)

    p = sub.add_parser(
        "check-site", help="validate an assembled site against the data contract"
    )
    p.add_argument("site_dir", type=Path)
    p.set_defaults(func=_cmd_check_site)

    p = sub.add_parser(
        "watchdog", help="exit 1 if the data branch is stale or a source is failing"
    )
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--issue-body", type=Path, help="write a Markdown issue body here")
    p.set_defaults(func=_cmd_watchdog)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
