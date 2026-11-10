"""Is each satellite still delivering? A per-satellite check from data this
site already has.

The poller's health (``health.json``) says whether each *source* answers; it
cannot tell that a source answers with nothing new because a satellite has
stopped (or because a distributor moved its products elsewhere). So, for
each platform and each of its data streams (the mission stores the poller
writes: one reference catalogue per mission; NISAR has two, L-band at ASF and
S-band at ISRO):

- **last seen**: the newest sensing time any of the stream's granules has;
- **expected delay**: that stream's p95 delivery time (first seen at any
  source minus sensing time) over the lookback, because Landsat 8's public
  products appear 7-12 days after sensing and NISAR's in about two;
- **overdue**: now minus last seen minus that delay;
- **due since**: the platform's planned (or, for MODIS/VIIRS/Sentinel-3,
  predicted) acquisitions between last seen and now minus the delay, counted
  from the schedule, when it covers that stretch.

A stream is *silent* when it is overdue by more than ``max(MIN_SILENT_H,
GAP_FACTOR x`` the longest gap between its acquisitions over the lookback)``,
acquisitions were due meanwhile (or the schedule cannot say), and the
poller's sources for it are not failing (then the answer is *unknown*: our
side, not the satellite's). A platform is:

- ``silent`` when every one of its streams is silent: nothing from the
  satellite anywhere. Its projection is paused on the map and an issue opens;
- ``stream_silent`` when some stream is silent while another still delivers
  (NISAR's L-band at ASF, while its S-band arrives from ISRO): a distributor
  problem, or a product moved; an issue opens, the map only notes it;
- ``ok``; ``unknown`` (our sources failing, or nothing to compare with).

Thresholds are set from 30 days of the site's own data (2026-11-09: the
longest normal gap was S1D's 27.8 h).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from swathcast.timeutil import parse_utc, to_iso, utcnow

LOGGER = logging.getLogger(__name__)

LOOKBACK_DAYS = 60
#: a stream is silent past the longer of this and GAP_FACTOR x its longest gap
MIN_SILENT_H = 48.0
GAP_FACTOR = 1.5
#: a source failing for this long makes its streams' status unknown
SOURCE_FAILING_H = 6.0

#: stream (mission store) -> (label, what it measures)
STREAMS = {
    "s1": ("Sentinel-1", "IW/EW products at CDSE"),
    "s2": ("Sentinel-2", "Level-1C products at CDSE (1-in-8 tile sample)"),
    "landsat": ("Landsat 8/9", "Level-1 scenes at USGS"),
    "nisar": ("NISAR L-band", "L-band products at ASF"),
    "nisar_s": ("NISAR S-band", "S-band products at ISRO (Bhoonidhi)"),
    "modis": ("MODIS", "Level-1B granules at NASA LANCE/LAADS"),
    "viirs": ("VIIRS", "Level-1B granules at NASA LANCE/LAADS"),
    "s3": ("Sentinel-3", "OLCI/SLSTR frames at CDSE"),
}


def _stores(data_dir: Path) -> dict:
    from swathcast.missions import MISSIONS
    from swathcast.storage import Store

    out = {"s1": (Store(data_dir), Path(data_dir) / "health.json")}
    for key, m in MISSIONS.items():
        st = m.store(data_dir)
        out[key] = (st, st.root / "health.json")
    return out


def stream_stats(df: pd.DataFrame, now: datetime) -> dict:
    """Per platform of one stream: last sensing time, the longest gap between
    acquisitions before it (h), the p95 delivery time (h), granule count."""
    if df.empty:
        return {}
    first = (
        df[df["status"] == "seen"].groupby("granule_id")["first_seen_utc"].min()
        if "status" in df
        else pd.Series(dtype="datetime64[ns, UTC]")
    )
    g = df.sort_values("sensing_start").drop_duplicates("granule_id")
    out = {}
    for platform, gg in g.groupby("platform"):
        t = gg["sensing_start"].dropna().sort_values()
        t = t[t <= now]
        if t.empty:
            continue
        gaps = t.diff().dt.total_seconds().dropna() / 3600
        delay = (
            first.reindex(gg["granule_id"]).to_numpy() - gg["sensing_start"].to_numpy()
        )
        delay = pd.to_timedelta(pd.Series(delay)).dt.total_seconds().dropna() / 3600
        out[str(platform)] = {
            "last_seen": t.iloc[-1],
            "max_gap_h": float(gaps.max()) if len(gaps) else 0.0,
            "delivery_p95_h": float(delay.quantile(0.95)) if len(delay) else 0.0,
            "granules": int(len(gg)),
        }
    return out


def sources_failing(health_path: Path, now: datetime) -> bool:
    """True when every source of a stream has been failing for a while (the
    check then cannot tell anything about the satellite)."""
    if not health_path.exists():
        return False
    try:
        sources = json.loads(health_path.read_text()).get("sources", {})
    except ValueError:
        return False
    if not sources:
        return False
    failing = 0
    for s in sources.values():
        since = s.get("failing_since_utc") or s.get("failing_since")
        if since and (now - parse_utc(since)).total_seconds() / 3600 > SOURCE_FAILING_H:
            failing += 1
    return failing == len(sources)


def schedule_due(schedule_dir: Path | None) -> pd.DataFrame:
    """Planned or predicted acquisitions in the schedule's files, one row per
    (platform, time): Sentinel-1/2/NISAR datatakes or frames, Landsat
    scenes, MODIS/VIIRS/Sentinel-3 tile passes (each orbit counted once an
    hour). Empty without a schedule."""
    cols = ["platform", "time"]
    if schedule_dir is None or not Path(schedule_dir).exists():
        return pd.DataFrame(columns=cols)
    d = Path(schedule_dir)
    parts = []
    up = d / "upcoming_passes.parquet"
    if up.exists():
        t = pd.read_parquet(up, columns=["platform", "begin_time"])
        parts.append(t.rename(columns={"begin_time": "time"}))
    for name in (
        "landsat_scenes.parquet",
        "modis_tile_passes.parquet",
        "viirs_tile_passes.parquet",
        "s3_tile_passes.parquet",
    ):
        p = d / name
        if p.exists():
            t = pd.read_parquet(p, columns=["platform", "time"])
            t["time"] = pd.to_datetime(t["time"], utc=True).dt.floor("h")
            parts.append(t.drop_duplicates())
    if not parts:
        return pd.DataFrame(columns=cols)
    out = pd.concat(parts, ignore_index=True)
    out["platform"] = out["platform"].astype(str)
    out["time"] = pd.to_datetime(out["time"], utc=True)
    return out


def check(
    data_dir: Path, schedule_dir: Path | None = None, now: datetime | None = None
) -> dict:
    """The status of every platform (see the module docstring)."""
    now = now or utcnow()
    data_dir = Path(data_dir)
    due = schedule_due(schedule_dir)
    sched_from = due["time"].min() if len(due) else None
    streams: dict[str, dict] = {}
    for key, (store, health) in _stores(data_dir).items():
        if not (store.root / "observations").exists():
            continue
        df = store.read_window(now - timedelta(days=LOOKBACK_DAYS), now)
        if df.empty:
            continue
        failing = sources_failing(health, now)
        for platform, s in stream_stats(df, now).items():
            delay = timedelta(hours=s["delivery_p95_h"])
            overdue_h = (now - s["last_seen"] - delay).total_seconds() / 3600
            limit_h = max(MIN_SILENT_H, GAP_FACTOR * s["max_gap_h"])
            mine = due[due["platform"] == platform]
            covered = (
                sched_from is not None
                and len(mine)
                and sched_from <= s["last_seen"] + delay
            )
            n_due = (
                int(
                    (
                        (mine["time"] > s["last_seen"]) & (mine["time"] <= now - delay)
                    ).sum()
                )
                if len(mine)
                else None
            )
            if failing:
                state = "unknown"
            elif overdue_h > limit_h and (n_due is None or n_due > 0 or not covered):
                state = "silent"
            else:
                state = "ok"
            streams.setdefault(platform, {})[key] = {
                "state": state,
                "last_seen": to_iso(s["last_seen"].to_pydatetime()),
                "overdue_h": round(overdue_h, 1),
                "limit_h": round(limit_h, 1),
                "delivery_p95_h": round(s["delivery_p95_h"], 1),
                "max_gap_h": round(s["max_gap_h"], 1),
                "due_since": n_due,
                "sources_failing": failing,
                "what": STREAMS.get(key, (key, key))[1],
            }
    platforms = {}
    for platform, by in sorted(streams.items()):
        states = {v["state"] for v in by.values()}
        if states == {"silent"}:
            status = "silent"
        elif "silent" in states and "ok" in states:
            status = "stream_silent"
        elif states == {"ok"} or (states <= {"ok", "unknown"} and "ok" in states):
            status = "ok"
        else:
            status = "unknown"
        last = max(parse_utc(v["last_seen"]) for v in by.values())
        platforms[platform] = {
            "status": status,
            "last_seen": to_iso(last),
            "note": note(platform, status, by),
            "streams": by,
        }
    return {"checked_at": to_iso(now), "platforms": platforms}


def _ago(stamp: str, now: datetime | None = None) -> str:
    h = ((now or utcnow()) - parse_utc(stamp)).total_seconds() / 3600
    return f"{h / 24:.1f} days" if h >= 48 else f"{h:.0f} h"


def note(platform: str, status: str, by: dict) -> str:
    """One sentence for the map and the issue."""
    silent = {k: v for k, v in by.items() if v["state"] == "silent"}
    alive = {k: v for k, v in by.items() if v["state"] == "ok"}
    if status == "silent":
        k, v = next(iter(silent.items()))
        due = (
            f", while {v['due_since']} of its passes were planned or predicted"
            if v["due_since"]
            else ""
        )
        return (
            f"No data from {platform} since {v['last_seen'][:16].replace('T', ' ')} UTC "
            f"({_ago(v['last_seen'])} ago; {v['what']}{due}): it may have stopped acquiring."
        )
    if status == "stream_silent":
        k, v = next(iter(silent.items()))
        a = next(iter(alive.values()))
        return (
            f"No new {v['what']} for {platform} since {v['last_seen'][:16].replace('T', ' ')} UTC "
            f"({_ago(v['last_seen'])} ago), while its {a['what']} still arrive: the satellite is "
            "acquiring; the distributor may have stopped or moved these products."
        )
    if status == "unknown":
        return f"{platform}: this site's sources for it are failing, so its status is unknown."
    return ""


def issue_body(report: dict, run_url: str = "") -> tuple[str, str]:
    """(marker, markdown) for the satellite-status issue: the marker names the
    flagged platforms and states, so the workflow comments only on a change."""
    flagged = {
        p: v
        for p, v in report["platforms"].items()
        if v["status"] in ("silent", "stream_silent")
    }
    marker = "flagged: " + ", ".join(
        f"{p}={v['status']}" for p, v in sorted(flagged.items())
    )
    lines = [
        f"<!-- {marker} -->",
        "Swathcast's per-satellite check (`swathcast satellites`, hourly) finds:",
        "",
        "| Platform | Status | Last data | Stream | Overdue (h) | Silent past (h) | Due since |",
        "|---|---|---|---|---|---|---|",
    ]
    for p, v in sorted(flagged.items()):
        for _key, s in sorted(v["streams"].items()):
            lines.append(
                f"| {p} | {v['status'] if s['state'] == 'silent' else '(still arriving)'} | {s['last_seen']} | "
                f"{s['what']} | {s['overdue_h']} | {s['limit_h']} | {s['due_since'] if s['due_since'] is not None else '?'} |"
            )
    lines += [""] + [f"- **{p}**: {v['note']}" for p, v in sorted(flagged.items())]
    lines += [
        "",
        "`silent`: no data from any of the platform's streams; the map pauses its projection and warns on "
        "its passes. `stream_silent`: one stream silent while another still delivers (a distributor or "
        "product change, not the satellite); the map notes it. This issue updates itself and closes when "
        f"every platform is back. {run_url}",
    ]
    return marker, "\n".join(lines) + "\n"
