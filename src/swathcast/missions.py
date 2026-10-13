"""Latency tracking beyond Sentinel-1: one tracker per mission.

Each mission has its own reference catalog, sources and tracking table under
``<data>/missions/<key>/`` (same layout as Sentinel-1's at the data root).
Sentinel-1 is unchanged; see docs/methodology.md "Other missions".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from swathcast.config import TrackerConfig
from swathcast.sources import bhoonidhi, landsat, nisar, s2
from swathcast.sources.base import ReferenceCatalog, Source, make_session
from swathcast.storage import Store

LOGGER = logging.getLogger(__name__)


@dataclass
class Mission:
    key: str
    label: str
    catalog: type[ReferenceCatalog]
    sources: list[type[Source]]
    timeouts_h: dict[str, float] = field(default_factory=dict)
    #: first run: list this far back (Sentinel-2 lists ~1,300 products/hour)
    bootstrap_hours: float = 6.0
    #: environment variables the mission needs (credentials); skipped without
    requires_env: tuple[str, ...] = ()
    #: called with the session after each run (e.g. to log out)
    cleanup: object = None

    def available(self) -> bool:
        import os

        return all(os.environ.get(v) for v in self.requires_env)

    def config(self) -> TrackerConfig:
        return TrackerConfig(
            bootstrap_hours=self.bootstrap_hours,
            source_options={
                cls.key: (
                    {"timeout_hours": self.timeouts_h[cls.key]}
                    if cls.key in self.timeouts_h
                    else {}
                )
                for cls in self.sources
            },
        )

    def build_sources(self, session) -> dict[str, Source]:
        cfg = self.config()
        return {
            cls.key: cls(session=session, **cfg.source_options[cls.key])
            for cls in self.sources
        }

    def store(self, data_dir: Path) -> Store:
        return Store(Path(data_dir) / "missions" / self.key)


MISSIONS: dict[str, Mission] = {
    m.key: m
    for m in (
        Mission(
            "s2",
            "Sentinel-2 (1-in-8 tile sample)",
            s2.CATALOG,
            s2.SOURCES,
            {
                "es_s2_l1c": 7 * 24.0,
                "es_s2_l2a": 7 * 24.0,
                "pc_s2_l2a": 10 * 24.0,
                "hls_s30": 10 * 24.0,
            },
            bootstrap_hours=3.0,
        ),
        Mission(
            "landsat",
            "Landsat 8/9",
            landsat.CATALOG,
            landsat.SOURCES,
            {
                "es_landsat_l2": 7 * 24.0,
                "pc_landsat_l2": 10 * 24.0,
                "hls_l30": 10 * 24.0,
            },
        ),
        Mission(
            "nisar",
            "NISAR",
            nisar.CATALOG,
            nisar.SOURCES,
            {"asf_nisar_gcov": 10 * 24.0},
        ),
        Mission(
            "nisar_s",
            "NISAR S-band (ISRO Bhoonidhi)",
            bhoonidhi.CATALOG,
            bhoonidhi.SOURCES,
            {"bhoonidhi_nisar_s_gcov": 10 * 24.0},
            # no prompt sightings on the first run (see sources/bhoonidhi.py)
            bootstrap_hours=24.0,
            requires_env=(bhoonidhi.ENV_USER, bhoonidhi.ENV_PASSWORD),
            cleanup=bhoonidhi.close,
        ),
    )
}


def run_mission(key: str, data_dir: Path, now: datetime | None = None):
    from swathcast.tracker import Tracker

    m = MISSIONS[key]
    if not m.available():
        LOGGER.info("mission %s skipped: %s not set", key, ", ".join(m.requires_env))
        return None
    session = make_session()
    tracker = Tracker(
        m.store(data_dir),
        m.config(),
        now=now,
        catalog=m.catalog(session=session),
        sources=m.build_sources(session),
    )
    try:
        return tracker.run()
    finally:
        if m.cleanup:
            m.cleanup(session)


def sources_meta(key: str) -> dict[str, dict]:
    m = MISSIONS[key]
    return {k: s.describe() for k, s in m.build_sources(make_session()).items()}
