"""The hourly poller: read state, extend the universe, look up, close out.

One row per (granule, source). A row is *open* until it is closed by one of

* being seen (non-burst sources close at first sighting),
* a burst source going ``burst_settle_hours`` without a new burst,
* the source's timeout passing without a sighting (``status = timed_out``).

Each run is idempotent: running twice at the same ``now`` changes nothing
the second time except check counters. It is resumable: cursors are saved
together with the table, and a failed source simply keeps its rows open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from swathcast.config import TrackerConfig, coverage_cell
from swathcast.health import Health
from swathcast.sources import SOURCE_CLASSES, CDSECatalog, ReferenceCatalog, Source
from swathcast.sources.base import Acquisition, make_session
from swathcast.storage import Store, empty_frame, months_between, normalize
from swathcast.timeutil import utcnow

LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    now: datetime
    new_acquisitions: int = 0
    new_rows: int = 0
    sightings: dict[str, int] = field(default_factory=dict)
    timed_out: dict[str, int] = field(default_factory=dict)
    exit_code: int = 0


def build_sources(
    config: TrackerConfig, session=None, now: datetime | None = None
) -> dict[str, Source]:
    session = session or make_session()
    sources: dict[str, Source] = {}
    for key in config.enabled_sources:
        cls = SOURCE_CLASSES[key]
        options = dict(config.source_options.get(key, {}))
        if getattr(cls, "burst_level", False):
            options["now"] = now
        sources[key] = cls(session=session, **options)
    return sources


def rows_for(
    acq: Acquisition, sources: dict[str, Source], now: datetime, config: TrackerConfig
) -> list[dict]:
    duration = (acq.sensing_stop - acq.sensing_start).total_seconds()
    published = acq.provider_timestamp
    prompt = published is not None and (now - published) <= timedelta(
        hours=config.prompt_tolerance_hours
    )
    if acq.listed_promptly is not None:
        prompt = acq.listed_promptly
    base = {
        "granule_id": acq.granule_id,
        "product_type": acq.product_type,
        "platform": acq.platform,
        "mode": acq.mode,
        "polarization": acq.polarization,
        "absolute_orbit": acq.absolute_orbit,
        "relative_orbit": acq.relative_orbit,
        "orbit_direction": acq.orbit_direction,
        "datatake_id": acq.datatake_id,
        "anx_start_s": acq.anx_start_s,
        "coverage_cell": acq.coverage_key
        or coverage_cell(acq.relative_orbit, acq.anx_start_s, duration),
        "sensing_start": acq.sensing_start,
        "sensing_stop": acq.sensing_stop,
        "centroid_lon": acq.centroid_lon,
        "centroid_lat": acq.centroid_lat,
        "timeliness": acq.timeliness,
        "tracking_started_utc": now,
        "prompt": bool(prompt),
        "n_checks": 0,
        "n_items": 0,
    }
    rows = []
    for key, source in sources.items():
        if source.reference_product != acq.product_type:
            continue
        row = dict(
            base, source=key, timeout_hours=source.timeout_hours, status="pending"
        )
        if source.is_reference:
            row.update(
                status="seen",
                first_seen_utc=now,
                last_checked_utc=now,
                n_checks=1,
                provider_timestamp_utc=published,
                provider_timestamp_latest_utc=published,
                provider_timestamp_field=acq.provider_timestamp_field,
                provider_id=f"{acq.granule_id}.SAFE",
                n_items=1,
                closed_utc=now,
            )
        rows.append(row)
    return rows


class Tracker:
    def __init__(
        self,
        store: Store,
        config: TrackerConfig | None = None,
        *,
        now: datetime | None = None,
        catalog: ReferenceCatalog | None = None,
        sources: dict[str, Source] | None = None,
    ):
        self.store = store
        self.config = config or TrackerConfig()
        self.now = now or utcnow()
        session = make_session()
        self.catalog = catalog or CDSECatalog(session=session)
        self.sources = (
            sources
            if sources is not None
            else build_sources(self.config, session, self.now)
        )

    # ------------------------------------------------------------------
    def horizon_start(self) -> datetime:
        longest = max(
            (s.timeout_hours for s in self.sources.values()), default=14 * 24.0
        )
        return self.now - timedelta(
            hours=longest + self.config.burst_settle_hours,
            days=self.config.max_new_age_days,
        )

    def run(self) -> RunResult:
        now = self.now
        result = RunResult(now=now)
        months = months_between(self.horizon_start(), now)
        df = self.store.read_months(months)
        cursors = self.store.read_cursors()
        health = Health(self.store.read_json("health.json"), now)

        ref = self.catalog.key  # "cdse" for Sentinel-1
        before, cdse_cursor = df.copy(), cursors.get(ref)
        try:
            df, result.new_acquisitions, result.new_rows = self._extend_universe(
                df, cursors, health
            )
        except Exception as exc:  # noqa: BLE001 - keep polling the other sources
            LOGGER.exception("extending the universe failed; rolled back")
            df = before
            if cdse_cursor is None:
                cursors.pop(ref, None)
            else:
                cursors[ref] = cdse_cursor
            health.failure(ref, exc)

        for key, source in self.sources.items():
            if source.is_reference:
                continue
            # Isolate each source completely: a failure while *applying* its
            # results must not lose the other sources' work, so roll back to
            # the pre-source state and record the error.
            before, had_cursor, cursor = df.copy(), key in cursors, cursors.get(key)
            try:
                df, n_found = self._poll_source(df, key, source, cursors, health)
            except Exception as exc:  # noqa: BLE001 - isolate each source
                LOGGER.exception("applying results for %s failed; rolled back", key)
                df = before
                if had_cursor:
                    cursors[key] = cursor
                else:
                    cursors.pop(key, None)
                health.failure(key, exc)
                n_found = 0
            result.sightings[key] = n_found

        df, result.timed_out = self._close_rows(df)

        # Keep only rows inside the months we own; older rows are untouched on disk.
        df = normalize(df)
        start = df["sensing_start"]
        in_months = pd.Series(
            [
                (y, m) in set(months)
                for y, m in zip(start.dt.year, start.dt.month, strict=True)
            ],
            index=df.index,
            dtype=bool,
        )
        self.store.write(df[in_months], months)
        self.store.write_cursors(cursors)
        result.exit_code = health.exit_code(self.config.max_failing_hours)
        self.store.write_json("health.json", health.to_dict())
        LOGGER.info(
            "run %s: %d new acquisitions (%d rows), sightings %s, timed out %s, exit %d",
            now.isoformat(timespec="seconds"),
            result.new_acquisitions,
            result.new_rows,
            result.sightings,
            result.timed_out,
            result.exit_code,
        )
        return result

    # ------------------------------------------------------------------
    def _extend_universe(self, df: pd.DataFrame, cursors: dict, health: Health):
        now = self.now
        ref = self.catalog.key
        cursor = cursors.get(ref)
        start = (
            cursor - timedelta(hours=self.config.reference_overlap_hours)
            if cursor
            else now - timedelta(hours=self.config.bootstrap_hours)
        )
        try:
            listed = self.catalog.list_new((start, now))
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, not fatal
            LOGGER.exception("reference listing (%s) failed", ref)
            health.failure(ref, exc)
            return df, 0, 0

        oldest = now - timedelta(days=self.config.max_new_age_days)
        known = set(df["granule_id"].unique()) if len(df) else set()
        # One listing can name a granule twice: Sentinel-3 ids keep only the
        # frame and start, so a frame republished (new creation time or
        # baseline) inside one window lists as the same id. Keep the earliest
        # publication, or the (granule, source) key is no longer unique.
        first: dict[str, Acquisition] = {}
        for a in listed:
            prev = first.get(a.granule_id)
            if prev is None or (
                a.provider_timestamp is not None
                and (
                    prev.provider_timestamp is None
                    or a.provider_timestamp < prev.provider_timestamp
                )
            ):
                first[a.granule_id] = a
        new = [
            a
            for a in first.values()
            if a.granule_id not in known and a.sensing_start >= oldest
        ]
        too_old = sum(1 for a in listed if a.sensing_start < oldest)
        rows = [
            row for acq in new for row in rows_for(acq, self.sources, now, self.config)
        ]
        if rows:
            df = pd.concat([df, normalize(pd.DataFrame(rows))], ignore_index=True)
        cursors[ref] = now
        health.success(
            ref,
            polled=len(listed),
            found=len(new),
            window_start=start,
            window_end=now,
            skipped_too_old=too_old,
        )
        return df, len(new), len(rows)

    def _poll_source(
        self, df: pd.DataFrame, key: str, source: Source, cursors: dict, health: Health
    ):
        now = self.now
        open_mask = (df["source"] == key) & df["closed_utc"].isna()
        ids = df.loc[open_mask, "granule_id"].tolist()
        if not ids and not source.burst_level:
            health.success(key, polled=0, found=0)
            return df, 0
        source.set_cursor(cursors.get(key))
        try:
            # Stream sources read their stream even with no open rows, so the
            # cursor never falls behind.
            found = source.lookup(ids)
        except Exception as exc:  # noqa: BLE001 - isolate each source
            LOGGER.exception("source %s failed", key)
            health.failure(key, exc, polled=len(ids))
            return df, 0
        if source.get_cursor() is not None:
            cursors[key] = source.get_cursor()

        index = pd.Series(df.index[open_mask], index=ids)
        df.loc[open_mask, "last_checked_utc"] = now
        df.loc[open_mask, "n_checks"] = df.loc[open_mask, "n_checks"].fillna(0) + 1

        newly_seen = 0
        for gid, hit in found.items():
            i = index.get(gid)
            if i is None:
                continue
            first = df.at[i, "status"] == "pending"
            if first:
                newly_seen += 1
                df.at[i, "status"] = "seen"
                df.at[i, "first_seen_utc"] = now
                df.at[i, "provider_id"] = hit.provider_id
            if hit.provider_timestamp is not None:
                earliest = df.at[i, "provider_timestamp_utc"]
                if pd.isna(earliest) or hit.provider_timestamp < earliest:
                    df.at[i, "provider_timestamp_utc"] = hit.provider_timestamp
                df.at[i, "provider_timestamp_latest_utc"] = hit.provider_timestamp
                df.at[i, "provider_timestamp_field"] = hit.provider_timestamp_field
            if source.burst_level:
                raw = df.at[i, "items"]
                previous = (
                    set(filter(None, raw.split(","))) if isinstance(raw, str) else set()
                )
                merged = previous | set(hit.notes.get("items", set()))
                if len(merged) > len(previous):
                    df.at[i, "last_item_seen_utc"] = now
                df.at[i, "items"] = ",".join(sorted(merged))
                df.at[i, "n_items"] = len(merged)
            else:
                df.at[i, "n_items"] = 1
                df.at[i, "closed_utc"] = now

        not_found = [
            index[g]
            for g in ids
            if g not in found and df.at[index[g], "status"] == "pending"
        ]
        if not_found:
            df.loc[not_found, "last_not_seen_utc"] = now

        details = getattr(source, "stats", {}) or {}
        health.success(
            key, polled=len(ids), found=len(found), newly_seen=newly_seen, **details
        )
        return df, newly_seen

    def _close_rows(self, df: pd.DataFrame):
        now = self.now
        timed_out: dict[str, int] = {}
        if df.empty:
            return df, timed_out
        open_rows = df["closed_utc"].isna()
        deadline = df["sensing_start"] + pd.to_timedelta(
            df["timeout_hours"].astype(float), unit="h"
        )
        expire = open_rows & (df["status"] == "pending") & (deadline <= now)
        df.loc[expire, "status"] = "timed_out"
        df.loc[expire, "closed_utc"] = now
        for key, n in df.loc[expire, "source"].value_counts().items():
            timed_out[str(key)] = int(n)

        settle = timedelta(hours=self.config.burst_settle_hours)
        burst_keys = [k for k, s in self.sources.items() if s.burst_level]
        settled = (
            df["closed_utc"].isna()
            & (df["status"] == "seen")
            & df["source"].isin(burst_keys)
            & ((df["last_item_seen_utc"] + settle <= now) | (deadline <= now))
        )
        df.loc[settled, "closed_utc"] = now
        return df, timed_out


def run_poll(
    data_dir, now: datetime | None = None, config: TrackerConfig | None = None
) -> RunResult:
    return Tracker(Store(data_dir), config, now=now).run()


__all__ = [
    "RunResult",
    "Tracker",
    "build_sources",
    "empty_frame",
    "rows_for",
    "run_poll",
]
