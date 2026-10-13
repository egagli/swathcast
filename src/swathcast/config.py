"""Tunable parameters. Defaults are documented in docs/methodology.md."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TrackerConfig:
    #: how often the poller runs (sets the +/- resolution of first_seen)
    poll_interval_hours: float = 1.0
    #: re-read this much of the CDSE publication stream before the cursor
    reference_overlap_hours: float = 3.0
    #: first run ever: list this far back
    bootstrap_hours: float = 24.0
    #: products whose sensing is older than this when first listed are ignored
    max_new_age_days: float = 14.0
    #: a row is "prompt" if tracking began within this long of CDSE publication.
    #: Only prompt rows enter the statistics (see methodology: left censoring).
    prompt_tolerance_hours: float = 2.5
    #: burst products: stop collecting new bursts this long after the last one
    burst_settle_hours: float = 24.0
    #: a sighting whose previous negative check is older than this (poller
    #: outage) is excluded from the statistics: first_seen is too coarse
    max_resolution_hours: float = 3.0
    #: health: a source failing for longer than this fails the job
    max_failing_hours: float = 24.0
    #: per-source overrides of Source attributes, e.g. timeout_hours
    source_options: dict[str, dict] = field(
        default_factory=lambda: {
            "cdse_grd": {},
            "cdse_slc": {},
            "asf_grd": {"timeout_hours": 14 * 24.0},
            "asf_slc": {"timeout_hours": 14 * 24.0},
            "pc_grd": {"timeout_hours": 14 * 24.0},
            "pc_rtc": {"timeout_hours": 14 * 24.0},
            "opera_rtc": {"timeout_hours": 14 * 24.0},
            "opera_cslc": {"timeout_hours": 14 * 24.0},
        }
    )

    @property
    def enabled_sources(self) -> list[str]:
        return list(self.source_options)


#: coverage cell width along the orbit, seconds after the ascending node
COVERAGE_CELL_SECONDS = 60


def coverage_cell(
    relative_orbit: int | None, anx_start_s: float | None, duration_s: float
) -> str | None:
    """A (track, along-orbit bin) key that repeats every cycle.

    Used to learn where a partial-coverage source delivers. Slices of the same
    track and orbit position share a cell regardless of platform or cycle.
    """
    if relative_orbit is None or anx_start_s is None:
        return None
    centre = anx_start_s + duration_s / 2.0
    return f"{int(relative_orbit):03d}-{int(centre // COVERAGE_CELL_SECONDS):03d}"
