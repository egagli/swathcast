"""Build the site's data files from the tracking table, and check them.

Outputs (all small JSON; the site never reads the raw table):

* ``latency.json`` - summary curves/quantiles, see :func:`stats.build_summary`
* ``latest.json``  - granules with any sighting in the last 24 h
* ``health.json``  - copied from the data branch

The data contract the browser relies on is checked by :func:`check_site`,
which CI runs against a fixture and the Pages job runs against real data.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from swathcast.config import TrackerConfig
from swathcast.sources import SOURCE_CLASSES
from swathcast.stats import build_summary
from swathcast.storage import Store
from swathcast.timeutil import parse_utc, to_iso, utcnow

LOGGER = logging.getLogger(__name__)


def sources_meta(config: TrackerConfig) -> dict[str, dict]:
    meta = {}
    for key in config.enabled_sources:
        source = SOURCE_CLASSES[key](**config.source_options.get(key, {}))
        meta[key] = source.describe()
    return meta


def latest_granules(df: pd.DataFrame, now: datetime, hours: float = 24.0) -> dict:
    cut = now - timedelta(hours=hours)
    recent_ids = df.loc[df["first_seen_utc"] >= cut, "granule_id"].unique()
    sub = df[df["granule_id"].isin(recent_ids)].copy()
    sources = sorted(sub["source"].unique())
    rows = []
    for gid, grp in sub.sort_values("sensing_start", ascending=False).groupby(
        "granule_id", sort=False
    ):
        first = grp.iloc[0]
        cells = {}
        for _, r in grp.iterrows():
            if r["status"] == "seen":
                cells[r["source"]] = round(
                    (r["first_seen_utc"] - r["sensing_start"]).total_seconds() / 3600, 2
                )
            elif r["status"] == "timed_out":
                cells[r["source"]] = "timeout"
            else:
                cells[r["source"]] = None
        rows.append(
            {
                "id": gid,
                "type": first["product_type"],
                "platform": first["platform"],
                "track": None
                if pd.isna(first["relative_orbit"])
                else int(first["relative_orbit"]),
                "dir": first["orbit_direction"]
                if isinstance(first["orbit_direction"], str)
                else None,
                "sensing": to_iso(first["sensing_start"]),
                "lon": None
                if pd.isna(first["centroid_lon"])
                else round(float(first["centroid_lon"]), 2),
                "lat": None
                if pd.isna(first["centroid_lat"])
                else round(float(first["centroid_lat"]), 2),
                "h": cells,
            }
        )
    return {
        "generated_at": to_iso(now),
        "window_hours": hours,
        "sources": sources,
        "granules": rows,
    }


def publish(
    data_dir: Path,
    out_dir: Path,
    now: datetime | None = None,
    config: TrackerConfig | None = None,
) -> dict:
    config = config or TrackerConfig()
    now = now or utcnow()
    store = Store(data_dir)
    df = store.read_all()
    health = store.read_json("health.json", default={}) or {}
    summary = build_summary(
        df, sources_meta(config), now=now, config=config, health=health
    )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latency.json").write_text(_dumps(summary))
    (out_dir / "latest.json").write_text(_dumps(latest_granules(df, now)))
    (out_dir / "orbits.json").write_text(_dumps(orbit_models(df, now)))
    (out_dir / "health.json").write_text(json.dumps(health, indent=1, default=str))
    for key, mission_summary in publish_missions(data_dir, now).items():
        (out_dir / f"latency_{key}.json").write_text(_dumps(mission_summary))
    publish_landsat_l1(data_dir, out_dir, now)
    publish_nisar_s(data_dir, out_dir, now)
    from swathcast.extents import publish_extents

    publish_extents(data_dir, out_dir)
    tle = Path(data_dir) / "tle.json"
    if tle.exists():
        shutil.copyfile(tle, out_dir / "tle.json")
    LOGGER.info("published site data for %d rows to %s", len(df), out_dir)
    return summary


def publish_missions(data_dir: Path, now: datetime) -> dict[str, dict]:
    """Latency summaries for the missions that have a tracking table."""
    from swathcast.missions import MISSIONS
    from swathcast.missions import sources_meta as mission_sources

    out = {}
    for key, m in MISSIONS.items():
        store = m.store(data_dir)
        if not (store.root / "observations").exists():
            continue
        df = store.read_all()
        health = store.read_json("health.json", default={}) or {}
        summary = build_summary(
            df,
            mission_sources(key),
            now=now,
            config=m.config(),
            health=health,
            reference=m.catalog.key,
        )
        summary["mission"] = {"key": key, "label": m.label}
        summary["health"] = health
        out[key] = summary
    return out


#: How far back the site's Landsat Level-1 shards reach (a footprint's popup
#: shows its last 90 days).
LANDSAT_L1_DAYS = 90
LANDSAT_L1_DIR = "landsat_l1"
LANDSAT_L1_COLUMNS = ["path", "row", "time", "id", "platform", "category"]


def landsat_l1_scenes(df: pd.DataFrame, now: datetime, days: float = LANDSAT_L1_DAYS):
    """USGS Level-1 scenes sensed in the last ``days``, one row each, from the
    Landsat tracking table (every scene the poller listed in USGS's STAC).

    The browser cannot search USGS's STAC (its CORS header names its own
    origin), and Level-2 copies exist for daytime scenes only, so this is how
    the site shows night scenes, and scenes before their Level-2 is made."""
    if df.empty:
        return pd.DataFrame(columns=LANDSAT_L1_COLUMNS)
    l1 = df[
        (df["product_type"] == "L1")
        & (df["sensing_start"] >= now - timedelta(days=days))
    ]
    l1 = l1[l1["datatake_id"].astype(str).str.fullmatch(r"\d{6}")]
    l1 = l1.sort_values("sensing_start").drop_duplicates("granule_id")
    return pd.DataFrame(
        {
            "path": l1["datatake_id"].str[:3].astype("int16"),
            "row": l1["datatake_id"].str[3:].astype("int16"),
            "time": l1["sensing_start"],
            "id": l1["granule_id"].astype(str),
            "platform": l1["platform"].astype(str),
            "category": l1["timeliness"].fillna("").astype(str),
        }
    ).reset_index(drop=True)


def publish_landsat_l1(data_dir: Path, out_dir: Path, now: datetime) -> int:
    """``landsat_l1/<path>.parquet``: the last 90 days of Level-1 scenes,
    one file per WRS-2 path (a footprint's popup reads its path's file only)."""
    from swathcast.missions import MISSIONS

    store = MISSIONS["landsat"].store(data_dir)
    if not (store.root / "observations").exists():
        return 0
    scenes = landsat_l1_scenes(
        store.read_window(now - timedelta(days=LANDSAT_L1_DAYS), now), now
    )
    folder = Path(out_dir) / LANDSAT_L1_DIR
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for path, group in scenes.groupby("path"):
        group.sort_values(["row", "time"]).to_parquet(
            folder / f"{int(path):03d}.parquet", index=False, compression="zstd"
        )
    LOGGER.info("published %d Landsat L1 scenes in %s", len(scenes), folder)
    return len(scenes)


#: NISAR S-band acquisitions of the last 90 days, one file per track (the
#: poller's Bhoonidhi history: the site cannot search Bhoonidhi, a login)
NISAR_S_DAYS = 90
NISAR_S_DIR = "nisar_s"
NISAR_S_COLUMNS = [
    "track",
    "frame",
    "direction",
    "time",
    "id",
    "pol",
    "partial",
    "browse",
]


def publish_nisar_s(data_dir: Path, out_dir: Path, now: datetime) -> int:
    """``nisar_s/<track>.parquet``: S-band RSLC items sensed in the last 90
    days (``missions/nisar_s/history.parquet``, sources/bhoonidhi.py)."""
    from swathcast.sources.bhoonidhi import HISTORY_FILE

    path = Path(data_dir) / "missions" / "nisar_s" / HISTORY_FILE
    folder = Path(out_dir) / NISAR_S_DIR
    if folder.exists():
        shutil.rmtree(folder)
    if not path.exists():
        return 0
    t = pd.read_parquet(path)
    t["time"] = pd.to_datetime(t["time"], utc=True)
    t = t[(t["time"] >= now - timedelta(days=NISAR_S_DAYS)) & (t["time"] <= now)]
    folder.mkdir(parents=True)
    for track, group in t.groupby("track"):
        group[NISAR_S_COLUMNS].sort_values(["frame", "time"]).to_parquet(
            folder / f"{int(track):03d}.parquet", index=False, compression="zstd"
        )
    LOGGER.info("published %d NISAR S-band items in %s", len(t), folder)
    return len(t)


def orbit_models(df: pd.DataFrame, now: datetime, days: float = 14.0) -> dict:
    """Per-platform ANX lattice fitted on the last ``days`` of CDSE GRD rows."""
    from swathcast.orbit import fit_from_table, models_to_json

    recent = df[df["sensing_start"] >= now - timedelta(days=days)]
    return models_to_json(fit_from_table(recent), now)


def _clean(value):
    """Replace NaN/inf with None so the output is strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _dumps(payload) -> str:
    return json.dumps(
        _clean(payload), separators=(",", ":"), default=str, allow_nan=False
    )


def copy_schedule(schedule: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schedule, out_dir / "upcoming_passes.parquet")
    sidecar = schedule.with_suffix(".meta.json")
    if sidecar.exists():
        shutil.copyfile(sidecar, out_dir / "upcoming_passes.meta.json")
    for name in GRID_FILES:
        path = schedule.parent / name
        if path.exists():
            shutil.copyfile(path, out_dir / name)
    split_schedule(out_dir)


#: The map's index of the per-sensor schedule files (see split_schedule).
SCHEDULE_INDEX = "passes_index.json"


def split_schedule(out_dir: Path) -> dict:
    """Write ``upcoming_passes.parquet`` again as one file per sensor
    (``passes_<sensor>.parquet``), plus ``passes_index.json``: for each sensor
    its file, row count, where each platform's plan ends, and its distinct
    polarization values. The map loads the index and the Sentinel-1/2 files
    first and NISAR's (most of the rows) only when NISAR is selected; the index
    gives it the filters and plan ends of every sensor up front."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    out_dir = Path(out_dir)
    table = pq.read_table(out_dir / "upcoming_passes.parquet")
    begin = pc.cast(table["begin_time"], pa.timestamp("us", tz="UTC"), safe=False)
    index = {"schedule_end": None, "sensors": {}}
    for sensor in sorted(set(table["sensor"].to_pylist())):
        mask = pc.equal(table["sensor"], sensor)
        # by track and frame: a frame's repeated outline then compresses away
        part = table.filter(mask).sort_by(
            [
                (c, "ascending")
                for c in ("track", "frame", "begin_time")
                if c in table.column_names
            ]
        )
        name = f"passes_{sensor}.parquet"
        pq.write_table(part, out_dir / name, compression="zstd", compression_level=19)
        df = pd.DataFrame(
            {
                "platform": part["platform"].to_pylist(),
                "begin": pc.filter(begin, mask).to_pylist(),
            }
        )
        pols = (
            part["polarization"].to_pylist()
            if "polarization" in part.column_names
            else []
        )
        index["sensors"][sensor] = {
            "file": name,
            "rows": part.num_rows,
            "plan_ends": {
                str(p): to_iso(t)
                for p, t in df.groupby("platform")["begin"].max().items()
            },
            "polarizations": sorted({str(p) for p in pols if p}),
        }
    if table.num_rows:
        index["schedule_end"] = to_iso(pc.max(begin).as_py())
    (out_dir / SCHEDULE_INDEX).write_text(_dumps(index))
    return index


#: Lazily loaded footprint grids written next to the schedule.
GRID_FILES = (
    "wrs2_grid.parquet",
    "landsat_scenes.parquet",
    "s2_grid.parquet",
    "s2_tile_passes.parquet",
    # wide-swath sensors without a plan, predicted from the orbit (wideswath.py)
    "sin_grid.parquet",
    "modis_tile_passes.parquet",
    "viirs_tile_passes.parquet",
    "s3_tile_passes.parquet",
)


# ---------------------------------------------------------------------------
# Data contract check
# ---------------------------------------------------------------------------

REQUIRED_SUMMARY_KEYS = {
    "schema_version",
    "generated_at",
    "data_as_of",
    "poll_interval_hours",
    "sources",
    "overall",
    "expected",
    "curves",
    "table",
    "trend",
    "provider_vs_first_seen",
}
REQUIRED_SCHEDULE_COLUMNS = [
    "sensor",
    "platform",
    "mode",
    "orbit_direction",
    "track",
    "begin_time",
    "end_time",
    "time_is_estimated",
    "geometry",
]
MAX_INITIAL_BYTES = 5 * 1024 * 1024


def check_site(site_dir: Path) -> list[str]:
    """Return a list of contract violations (empty = OK)."""
    site_dir = Path(site_dir)
    problems: list[str] = []
    for page in ("index.html", "dashboard.html"):
        if not (site_dir / page).exists():
            problems.append(f"missing page {page}")
    for page in site_dir.glob("*.html"):
        text = page.read_text()
        for ref in _local_refs(text):
            if not (site_dir / ref).exists():
                problems.append(f"{page.name} links to missing {ref}")

    data = site_dir / "data"
    names = ["latency.json", "latest.json", "health.json", "orbits.json", "tle.json"]
    names += sorted(p.name for p in data.glob("latency_*.json"))
    for name in names:
        path = data / name
        if path.exists():
            try:
                json.loads(path.read_text(), parse_constant=_reject_constant)
            except ValueError as exc:
                problems.append(
                    f"{name} is not strict JSON (browsers reject it): {exc}"
                )
    latency_path = data / "latency.json"
    if not latency_path.exists():
        problems.append("missing data/latency.json")
    else:
        summary = json.loads(latency_path.read_text())
        missing = REQUIRED_SUMMARY_KEYS - set(summary)
        if missing:
            problems.append(f"latency.json missing keys {sorted(missing)}")
        for key in ("generated_at", "data_as_of"):
            try:
                parse_utc(summary.get(key) or "")
            except (ValueError, TypeError):
                problems.append(f"latency.json {key} is not a UTC timestamp")
        for key, stats in (summary.get("overall") or {}).items():
            if not stats.get("measured"):
                for q in ("median_h", "p90_h", "p99_h"):
                    if stats.get(q) is not None:
                        problems.append(f"{key}: {q} present although not measured")
        au = summary.get("audit") or {}
        for field in ("missing_rows", "duplicate_rows", "bad_status"):
            if au.get(field):
                problems.append(f"tracking-table audit: {field} = {au[field]}")
        for key, entry in (summary.get("expected") or {}).items():
            if key not in (summary.get("sources") or {}):
                problems.append(f"expected[] has unknown source {key}")
            if "all" not in entry:
                problems.append(f"expected[{key}] has no 'all' stratum")

    orbits_path = data / "orbits.json"
    if not orbits_path.exists():
        problems.append("missing data/orbits.json")
    else:
        orbits = json.loads(orbits_path.read_text())
        for platform, m in (orbits.get("platforms") or {}).items():
            if m.get("resid_max_s", 99) > 2.0:
                problems.append(
                    f"orbits.json: {platform} fit residual {m.get('resid_max_s')} s > 2 s"
                )
    schedule = data / "upcoming_passes.parquet"
    if not schedule.exists():
        problems.append("missing data/upcoming_passes.parquet")
    else:
        import pyarrow.parquet as pq

        names = pq.read_schema(schedule).names
        for col in REQUIRED_SCHEDULE_COLUMNS:
            if col not in names:
                problems.append(f"schedule missing column {col}")
        if orbits_path.exists():
            cols = pq.read_table(schedule, columns=["sensor", "platform"]).to_pandas()
            s1 = set(cols.loc[cols["sensor"] == "sentinel-1", "platform"].dropna())
            fitted = set(json.loads(orbits_path.read_text()).get("platforms") or {})
            for platform in sorted(s1 - fitted):
                problems.append(
                    f"orbits.json has no timing model for {platform}: its bursts cannot be timed"
                )
        index_path = data / SCHEDULE_INDEX
        if not index_path.exists():
            problems.append(f"missing data/{SCHEDULE_INDEX}")
        else:
            index = json.loads(index_path.read_text())
            for sensor, entry in (index.get("sensors") or {}).items():
                if not (data / str(entry.get("file"))).exists():
                    problems.append(
                        f"{SCHEDULE_INDEX}: {sensor} file {entry.get('file')} missing"
                    )
        meta = schedule.with_suffix(".meta.json")
        if not meta.exists():
            problems.append("missing data/upcoming_passes.meta.json")
        else:
            try:
                parse_utc(json.loads(meta.read_text()).get("generated_at") or "")
            except (ValueError, TypeError):
                problems.append("schedule meta generated_at is not a timestamp")

    problems += _check_s2(data)
    problems += _check_landsat(data)
    problems += _check_landsat_l1(data)
    problems += _check_nisar_s(data)
    problems += _check_wideswath(data)

    # what the map loads before the first draw: the index, the Sentinel-1/2
    # schedule files and the small JSONs (NISAR's file waits until selected)
    initial = sum(
        (data / f).stat().st_size
        for f in (
            SCHEDULE_INDEX,
            "passes_sentinel-1.parquet",
            "passes_sentinel-2.parquet",
            "upcoming_passes.meta.json",
            "latency.json",
            "health.json",
        )
        if (data / f).exists()
    )
    if initial > MAX_INITIAL_BYTES:
        problems.append(f"initial map payload is {initial / 1e6:.1f} MB (> 5 MB)")
    return problems


def _check_s2(data: Path) -> list[str]:
    """The optional Sentinel-2 tile files: complete grid, passes on known tiles."""
    import pyarrow.parquet as pq

    from swathcast.s2grid import N_TILES

    grid_path, passes_path = data / "s2_grid.parquet", data / "s2_tile_passes.parquet"
    if not grid_path.exists() and not passes_path.exists():
        return []
    if not (grid_path.exists() and passes_path.exists()):
        return ["s2_grid.parquet and s2_tile_passes.parquet must be published together"]
    problems = []
    grid = pq.read_table(grid_path).to_pandas()
    want = ["tile", "x0", "y0", "dx1", "dy1", "dx2", "dy2", "dx3", "dy3"]
    if list(grid.columns) != want:
        problems.append(f"s2_grid.parquet columns {list(grid.columns)} != {want}")
    elif len(grid) != N_TILES or not grid["tile"].is_unique:
        problems.append(f"s2_grid.parquet has {len(grid)} tiles, expected {N_TILES}")
    passes = pq.read_table(passes_path).to_pandas()
    for col in ("tile", "platform", "track", "direction", "time", "begin"):
        if col not in passes.columns:
            problems.append(f"s2_tile_passes.parquet missing column {col}")
    if "tile" in passes.columns and "tile" in grid.columns:
        unknown = set(passes["tile"]) - set(grid["tile"])
        if unknown:
            problems.append(f"s2_tile_passes.parquet has {len(unknown)} unknown tiles")
    from swathcast.s2grid import PLAN_SLACK_S

    slack = pd.Timedelta(seconds=PLAN_SLACK_S + 1)
    if {"time", "begin"} <= set(passes.columns) and (
        passes["time"] < passes["begin"] - slack
    ).any():
        problems.append("s2_tile_passes.parquet has tile times before their datatake")
    return problems


def _check_landsat(data: Path) -> list[str]:
    """Landsat scenes must come with the WRS-2 grid and name cells in it."""
    import pyarrow.parquet as pq

    scenes_path, grid_path = data / "landsat_scenes.parquet", data / "wrs2_grid.parquet"
    if not scenes_path.exists():
        return []
    if not grid_path.exists():
        return ["landsat_scenes.parquet published without wrs2_grid.parquet"]
    scenes = pq.read_table(scenes_path).to_pandas()
    want = {"platform", "path", "row", "direction", "time", "timing"}
    if want - set(scenes.columns):
        return [
            f"landsat_scenes.parquet missing columns {sorted(want - set(scenes.columns))}"
        ]
    grid = pq.read_table(grid_path, columns=["path", "row"]).to_pandas()
    cells = set(zip(grid["path"], grid["row"], strict=True))
    unknown = sum(
        (p, r) not in cells for p, r in zip(scenes["path"], scenes["row"], strict=True)
    )
    return (
        [f"landsat_scenes.parquet has {unknown} scenes not in the WRS-2 grid"]
        if unknown
        else []
    )


def _check_landsat_l1(data: Path) -> list[str]:
    """landsat_l1/<path>.parquet (optional): the columns the map reads, and
    one path per file, named for it."""
    import pyarrow.parquet as pq

    folder = data / LANDSAT_L1_DIR
    if not folder.exists():
        return []
    problems = []
    for f in sorted(folder.glob("*.parquet")):
        t = pq.read_table(f)
        missing = set(LANDSAT_L1_COLUMNS) - set(t.column_names)
        if missing:
            problems.append(
                f"{LANDSAT_L1_DIR}/{f.name} missing columns {sorted(missing)}"
            )
            continue
        paths = set(t.column("path").to_pylist())
        if paths != {int(f.stem)}:
            problems.append(f"{LANDSAT_L1_DIR}/{f.name} holds paths {sorted(paths)}")
    return problems


def _check_nisar_s(data: Path) -> list[str]:
    """nisar_s/<track>.parquet (optional): the columns the map reads, and one
    track per file, named for it."""
    import pyarrow.parquet as pq

    folder = data / NISAR_S_DIR
    if not folder.exists():
        return []
    problems = []
    for f in sorted(folder.glob("*.parquet")):
        t = pq.read_table(f)
        missing = set(NISAR_S_COLUMNS) - set(t.column_names)
        if missing:
            problems.append(f"{NISAR_S_DIR}/{f.name} missing columns {sorted(missing)}")
            continue
        tracks = set(t.column("track").to_pylist())
        if tracks != {int(f.stem)}:
            problems.append(f"{NISAR_S_DIR}/{f.name} holds tracks {sorted(tracks)}")
    return problems


def _check_wideswath(data: Path) -> list[str]:
    """MODIS/VIIRS/Sentinel-3 tile passes (optional): the columns the map
    reads, on tiles of sin_grid.parquet."""
    import pyarrow.parquet as pq

    problems = []
    grid_path = data / "sin_grid.parquet"
    for name in (
        "modis_tile_passes.parquet",
        "viirs_tile_passes.parquet",
        "s3_tile_passes.parquet",
    ):
        path = data / name
        if not path.exists():
            continue
        if not grid_path.exists():
            problems.append(f"{name} published without sin_grid.parquet")
            continue
        t = pq.read_table(path)
        want = {"tile", "platform", "time", "direction", "xtrack_km"}
        if name.startswith("s3_"):
            want |= {"track", "anx_s"}
        if want - set(t.column_names):
            problems.append(
                f"{name} missing columns {sorted(want - set(t.column_names))}"
            )
            continue
        tiles = set(
            pq.read_table(grid_path, columns=["tile"]).column("tile").to_pylist()
        )
        unknown = {str(x) for x in t.column("tile").to_pylist()} - tiles
        if unknown:
            problems.append(f"{name} has {len(unknown)} tiles not in sin_grid.parquet")
    return problems


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def _local_refs(html: str) -> list[str]:
    import re

    refs = []
    for match in re.finditer(r'(?:href|src)="([^"#?]+)"', html):
        ref = match.group(1)
        if ref.startswith(
            ("http://", "https://", "data:", "mailto:", "//")
        ) or ref.startswith("./data/"):
            continue
        refs.append(ref.removeprefix("./"))
    return refs
