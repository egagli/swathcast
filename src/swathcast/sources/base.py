"""The small interface every source implements.

A *source* is one distributor x one product (``asf_grd``, ``pc_rtc``, ...).
It answers one question: "which of these granules can I download from you
right now?" via :meth:`Source.lookup`. The reference catalog additionally
implements :meth:`ReferenceCatalog.list_new`, which defines the tracking
universe.

``lookup`` returns a dict holding **only the ids that were found**; an id
absent from the result was checked and is not available yet. A lookup that
could not be completed must raise (never return a partial dict silently), so
that the tracker does not mistake an outage for "not delivered yet".
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from swathcast import __version__

LOGGER = logging.getLogger(__name__)

USER_AGENT = f"swathcast/{__version__} (+https://github.com/egagli/swathcast)"


class SourceError(RuntimeError):
    """A lookup or listing could not be completed."""


@dataclass(frozen=True)
class Acquisition:
    """One product as listed by a reference catalog (Sentinel-1 fields; other
    missions fill what applies, see sources/s2.py, landsat.py, nisar.py)."""

    granule_id: str  # ESA product name without ".SAFE"
    product_type: str  # "GRD" | "SLC"
    platform: str  # "S1C", "S1D", ... (taken from the name, never hard-coded)
    mode: str  # "IW"
    polarization: str  # "DV", "DH", "SV", "SH"
    absolute_orbit: int
    relative_orbit: int | None
    orbit_direction: str | None  # "ascending" | "descending"
    datatake_id: str
    anx_start_s: float | None  # sensing start, seconds after ascending node
    sensing_start: datetime
    sensing_stop: datetime
    centroid_lon: float | None
    centroid_lat: float | None
    timeliness: str | None
    provider_timestamp: datetime | None  # reference catalog's own timestamp
    provider_timestamp_field: str | None
    #: learned-coverage key (S1: derived from track and ANX time; S2: tile;
    #: Landsat: path/row; NISAR: track-frame)
    coverage_key: str | None = None
    #: catalogs without a publication timestamp (Bhoonidhi) say whether this
    #: listing followed the previous one closely enough for a prompt sighting
    listed_promptly: bool | None = None


@dataclass
class Found:
    """A granule that a source reported as available."""

    provider_timestamp: datetime | None = None
    provider_timestamp_field: str | None = None
    provider_id: str | None = None
    # Burst-level products: how many bursts have been seen so far.
    n_items: int = 1
    notes: dict = field(default_factory=dict)


class Source(ABC):
    """One distributor x product."""

    key: str
    label: str
    distributor: str
    product: str
    #: which reference product type this source is matched against
    reference_product: str = "GRD"
    #: rows for this source are created "seen" by the reference listing
    is_reference: bool = False
    #: burst-level products keep collecting bursts after the first one
    burst_level: bool = False
    #: "full": every reference granule is expected here.
    #: "learned": only granules in coverage cells this source has ever
    #: delivered count towards its statistics (see docs/methodology.md).
    coverage: str = "full"
    timeout_hours: float = 14 * 24.0
    #: whether the provider timestamp was validated as a publication time
    provider_timestamp_validated: bool = False

    def __init__(self, session: requests.Session | None = None, **config):
        self.session = session or make_session()
        for name, value in config.items():
            if not hasattr(self, name):
                raise TypeError(f"{type(self).__name__} has no option {name!r}")
            setattr(self, name, value)

    @abstractmethod
    def lookup(self, ids: list[str]) -> dict[str, Found]:
        """Return ``{id: Found}`` for the ids that are available now."""

    # Stream-based sources (see opera.py) keep a cursor between runs.
    def get_cursor(self) -> datetime | None:
        return None

    def set_cursor(self, cursor: datetime | None) -> None:
        return None

    def describe(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "distributor": self.distributor,
            "product": self.product,
            "reference_product": self.reference_product,
            "burst_level": self.burst_level,
            "coverage": self.coverage,
            "timeout_hours": self.timeout_hours,
            "provider_timestamp_validated": self.provider_timestamp_validated,
        }


class ReferenceCatalog(Source):
    """The source that defines the tracking universe."""

    @abstractmethod
    def list_new(self, window: tuple[datetime, datetime]) -> list[Acquisition]:
        """Every product published inside ``window`` (paged to the end)."""


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers["User-Agent"] = USER_AGENT
    return session


def chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
