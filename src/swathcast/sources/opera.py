"""OPERA burst products (RTC-S1, CSLC-S1) at ASF DAAC, via CMR.

OPERA products are per *burst*, and each names its parent SLC in the UMM
``InputGranules`` field. CMR cannot search by input granule, so instead of
per-id lookups this source reads the complete CMR insert/update stream
(``updated_since=<cursor>``, paged to the end) and maps every burst back to
its parent SLC by that identifier. The cursor is persisted by the tracker
between runs, so the scan is complete and resumable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from swathcast.sources.base import Found, Source
from swathcast.sources.cmr import (
    additional_attribute,
    provider_insert_date,
    search_umm,
)
from swathcast.timeutil import parse_utc, to_iso, utcnow

LOGGER = logging.getLogger(__name__)


def parent_slc(name: str) -> str:
    for suffix in (".SAFE", "-SLC"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


class OperaSource(Source):
    distributor = "OPERA at ASF DAAC (NASA/JPL)"
    reference_product = "SLC"
    burst_level = True
    coverage = "learned"
    provider = "ASF"
    short_name = ""
    #: re-read this much of the stream before the cursor (CMR indexing lag)
    overlap_hours = 3.0
    #: on the very first run, start the stream this far back
    bootstrap_hours = 3.0
    #: ignore bursts whose sensing time is older than this (reprocessing)
    max_sensing_age_days = 21.0

    def __init__(self, session=None, now: datetime | None = None, **config):
        super().__init__(session=session, **config)
        self._cursor: datetime | None = None
        self._now = now
        self.stats: dict = {}

    def get_cursor(self) -> datetime | None:
        return self._cursor

    def set_cursor(self, cursor: datetime | None) -> None:
        self._cursor = cursor

    def _stream(self, since: datetime, now: datetime) -> list[dict]:
        oldest = now - timedelta(days=self.max_sensing_age_days)
        params = [
            ("short_name", self.short_name),
            ("provider", self.provider),
            ("updated_since", to_iso(since)),
            ("temporal", f"{to_iso(oldest)},"),
        ]
        return search_umm(self.session, params)

    def lookup(self, ids: list[str]) -> dict[str, Found]:
        now = self._now or utcnow()
        since = (
            self._cursor - timedelta(hours=self.overlap_hours)
            if self._cursor
            else now - timedelta(hours=self.bootstrap_hours)
        )
        items = self._stream(since, now)
        wanted = set(ids)
        found: dict[str, Found] = {}
        unmatched = 0
        for item in items:
            umm = item.get("umm", {})
            inputs = [parent_slc(g) for g in umm.get("InputGranules", []) or []]
            burst = (
                additional_attribute(umm, "OPERA_BURST_ID")
                or [umm.get("GranuleUR", "")]
            )[0]
            insert = provider_insert_date(umm)
            insert_dt = parse_utc(insert) if insert else None
            matched = False
            for slc in inputs:
                if slc not in wanted:
                    continue
                matched = True
                entry = found.setdefault(
                    slc,
                    Found(
                        provider_timestamp_field="min(ProviderDates[Insert]) over bursts",
                        n_items=0,
                        notes={"items": set()},
                    ),
                )
                entry.notes["items"].add(burst)
                entry.n_items = len(entry.notes["items"])
                if insert_dt and (
                    entry.provider_timestamp is None
                    or insert_dt < entry.provider_timestamp
                ):
                    entry.provider_timestamp = insert_dt
            unmatched += not matched
        self.stats = {"stream_items": len(items), "unmatched_items": unmatched}
        # Advance only after the whole stream was read successfully.
        self._cursor = now
        return found


class OperaRTC(OperaSource):
    key = "opera_rtc"
    label = "OPERA RTC-S1"
    product = "RTC"
    short_name = "OPERA_L2_RTC-S1_V1"


class OperaCSLC(OperaSource):
    key = "opera_cslc"
    label = "OPERA CSLC-S1"
    product = "CSLC"
    short_name = "OPERA_L2_CSLC-S1_V1"
