"""Partitioned Parquet store for the granule-tracking table.

Layout under the data directory (the ``data`` branch in production)::

    observations/year=YYYY/month=MM/part-0.parquet   one row per granule x source
    state/cursors.json                               listing/stream cursors
    health.json                                      per-source health

Partitions are keyed on the *sensing* month. Only the partitions that can
still hold open rows are rewritten on each run, so old months are immutable
in practice. Writes go to a temporary file and are renamed into place.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from swathcast.timeutil import parse_utc, to_iso

LOGGER = logging.getLogger(__name__)

TIME_COLUMNS = [
    "sensing_start",
    "sensing_stop",
    "tracking_started_utc",
    "first_seen_utc",
    "last_not_seen_utc",
    "last_checked_utc",
    "provider_timestamp_utc",
    "provider_timestamp_latest_utc",
    "last_item_seen_utc",
    "closed_utc",
]

#: The tracking-table schema. This is a data contract: stats.py, the site
#: and tests/test_schema.py depend on it.
SCHEMA = pa.schema(
    [
        ("granule_id", pa.string()),
        ("source", pa.string()),
        ("product_type", pa.string()),
        ("platform", pa.string()),
        ("mode", pa.string()),
        ("polarization", pa.string()),
        ("absolute_orbit", pa.int32()),
        ("relative_orbit", pa.int16()),
        ("orbit_direction", pa.string()),
        ("datatake_id", pa.string()),
        ("anx_start_s", pa.float32()),
        ("coverage_cell", pa.string()),
        ("sensing_start", pa.timestamp("us", tz="UTC")),
        ("sensing_stop", pa.timestamp("us", tz="UTC")),
        ("centroid_lon", pa.float32()),
        ("centroid_lat", pa.float32()),
        ("timeliness", pa.string()),
        ("tracking_started_utc", pa.timestamp("us", tz="UTC")),
        ("prompt", pa.bool_()),
        ("status", pa.string()),  # pending | seen | timed_out
        ("first_seen_utc", pa.timestamp("us", tz="UTC")),
        ("last_not_seen_utc", pa.timestamp("us", tz="UTC")),
        ("last_checked_utc", pa.timestamp("us", tz="UTC")),
        ("n_checks", pa.int32()),
        ("provider_timestamp_utc", pa.timestamp("us", tz="UTC")),
        ("provider_timestamp_latest_utc", pa.timestamp("us", tz="UTC")),
        ("provider_timestamp_field", pa.string()),
        ("provider_id", pa.string()),
        ("n_items", pa.int32()),
        ("items", pa.string()),
        ("last_item_seen_utc", pa.timestamp("us", tz="UTC")),
        ("closed_utc", pa.timestamp("us", tz="UTC")),
        ("timeout_hours", pa.float32()),
    ]
)
COLUMNS = SCHEMA.names
KEY = ["granule_id", "source"]


def empty_frame() -> pd.DataFrame:
    return SCHEMA.empty_table().to_pandas()


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a frame to the schema's column set, order and dtypes."""
    df = df.copy()
    for name in COLUMNS:
        if name not in df.columns:
            df[name] = None
    df = df[COLUMNS]
    for name in TIME_COLUMNS:
        df[name] = pd.to_datetime(df[name], utc=True, format="ISO8601")
    table = pa.Table.from_pandas(df, schema=SCHEMA, preserve_index=False)
    return table.to_pandas()


def _partition_dir(root: Path, year: int, month: int) -> Path:
    return root / "observations" / f"year={year:04d}" / f"month={month:02d}"


def months_between(start: datetime, end: datetime) -> list[tuple[int, int]]:
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    # -- observations ------------------------------------------------------
    def partitions(self) -> list[tuple[int, int]]:
        found = []
        for path in sorted(
            (self.root / "observations").glob("year=*/month=*/part-0.parquet")
        ):
            year = int(path.parent.parent.name.split("=")[1])
            month = int(path.parent.name.split("=")[1])
            found.append((year, month))
        return found

    def read_months(self, months: list[tuple[int, int]]) -> pd.DataFrame:
        frames = []
        for year, month in months:
            path = _partition_dir(self.root, year, month) / "part-0.parquet"
            if path.exists():
                frames.append(pq.read_table(path, schema=SCHEMA).to_pandas())
        if not frames:
            return empty_frame()
        return normalize(pd.concat(frames, ignore_index=True))

    def read_all(self) -> pd.DataFrame:
        return self.read_months(self.partitions())

    def read_window(self, start: datetime, end: datetime) -> pd.DataFrame:
        return self.read_months(months_between(start, end))

    def write(self, df: pd.DataFrame, months: list[tuple[int, int]]) -> list[Path]:
        """Rewrite the given month partitions from ``df``.

        Rows of ``df`` must all belong to ``months``. Every month in
        ``months`` is rewritten (an empty month deletes nothing: it is simply
        not written).
        """
        df = normalize(df)
        start = df["sensing_start"]
        written = []
        wanted = set(months)
        present = set(zip(start.dt.year, start.dt.month, strict=True))
        stray = present - wanted
        if stray:
            raise ValueError(
                f"rows outside the partitions being written: {sorted(stray)}"
            )
        for year, month in sorted(wanted):
            part = df[(start.dt.year == year) & (start.dt.month == month)]
            if part.empty:
                continue
            part = part.sort_values(["sensing_start", "granule_id", "source"])
            target = _partition_dir(self.root, year, month) / "part-0.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".parquet.tmp")
            table = pa.Table.from_pandas(part, schema=SCHEMA, preserve_index=False)
            pq.write_table(table, tmp, compression="zstd")
            os.replace(tmp, target)
            written.append(target)
        return written

    # -- small JSON state ----------------------------------------------------
    def read_json(self, relpath: str, default=None):
        path = self.root / relpath
        if not path.exists():
            return default
        return json.loads(path.read_text())

    def write_json(self, relpath: str, payload) -> Path:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n"
        )
        os.replace(tmp, path)
        return path

    def read_cursors(self) -> dict[str, datetime]:
        raw = self.read_json("state/cursors.json", default={}) or {}
        return {key: parse_utc(value) for key, value in raw.items() if value}

    def write_cursors(self, cursors: dict[str, datetime | None]) -> None:
        self.write_json(
            "state/cursors.json",
            {key: to_iso(value) for key, value in sorted(cursors.items()) if value},
        )


def _json_default(value):
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, pd.Timestamp):
        return to_iso(value.to_pydatetime())
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")
