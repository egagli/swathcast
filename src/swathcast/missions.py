"""Latency tracking beyond Sentinel-1: one tracker per mission
(Sentinel-2, Landsat 8/9, NISAR L- and S-band, MODIS, VIIRS, Sentinel-3).

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
from swathcast.sources import bhoonidhi, landsat, modis, nisar, s2, s3
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
    #: called with (session, data_dir, now) after each run, before cleanup;
    #: its failure is logged, never fails the run
    after: object = None
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
            # the map's history of S-band frames (Bhoonidhi needs a login)
            after=bhoonidhi.update_history,
            cleanup=bhoonidhi.close,
        ),
        # LANCE deletes its near-real-time products after 7 days: their
        # sources time out before that
        Mission(
            "modis",
            "MODIS (Terra, Aqua)",
            modis.ModisCatalog,
            modis.MODIS_SOURCES,
            {
                "laads_modis_l1b": 10 * 24.0,
                "laads_modis_l1b_c7": 10 * 24.0,
                "lance_modis_snow": 5 * 24.0,
                "nsidc_modis_snow": 10 * 24.0,
            },
        ),
        Mission(
            "viirs",
            "VIIRS (Suomi NPP, NOAA-20, NOAA-21)",
            modis.ViirsCatalog,
            modis.VIIRS_SOURCES,
            {
                "laads_viirs_l1b": 10 * 24.0,
                "lance_viirs_snow": 5 * 24.0,
                "nsidc_viirs_snow": 10 * 24.0,
            },
        ),
        Mission(
            "s3",
            "Sentinel-3 (OLCI, SLSTR)",
            s3.CATALOG,
            s3.SOURCES,
            {
                "cdse_s3_efr_ntc": 7 * 24.0,
                "cdse_s3_rbt_ntc": 7 * 24.0,
                "eumetsat_s3_efr": 3 * 24.0,
                "cdse_s3_lfr": 5 * 24.0,
                "pc_s3_lfr": 7 * 24.0,
                "cdse_s3_lst": 5 * 24.0,
                "pc_s3_lst": 7 * 24.0,
            },
            bootstrap_hours=3.0,
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
        result = tracker.run()
        if m.after:
            try:
                m.after(session, data_dir, now)
            except Exception:  # noqa: BLE001 - an extra, never fails the run
                LOGGER.exception("mission %s: after-run step failed", key)
        return result
    finally:
        if m.cleanup:
            m.cleanup(session)


def sources_meta(key: str) -> dict[str, dict]:
    m = MISSIONS[key]
    return {k: s.describe() for k, s in m.build_sources(make_session()).items()}
