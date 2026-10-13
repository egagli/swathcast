"""Per-source health bookkeeping (``health.json``)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from swathcast.timeutil import parse_utc, to_iso

LOGGER = logging.getLogger(__name__)


class Health:
    """Mutable view over ``health.json``.

    One entry per source key (plus ``cdse`` for the reference listing). Each
    run records an attempt; a failure keeps ``last_success_utc`` and sets
    ``failing_since_utc`` on the first of a consecutive run of failures.
    """

    def __init__(self, payload: dict | None, now: datetime):
        payload = payload or {}
        self.now = now
        self.sources: dict[str, dict] = dict(payload.get("sources", {}))
        self.run: dict = {}

    def _entry(self, key: str) -> dict:
        return self.sources.setdefault(
            key,
            {
                "last_attempt_utc": None,
                "last_success_utc": None,
                "last_error": None,
                "last_error_utc": None,
                "consecutive_failures": 0,
                "failing_since_utc": None,
                "granules_polled": 0,
                "granules_found": 0,
                "details": {},
            },
        )

    def success(self, key: str, polled: int, found: int, **details) -> None:
        entry = self._entry(key)
        entry.update(
            last_attempt_utc=to_iso(self.now),
            last_success_utc=to_iso(self.now),
            consecutive_failures=0,
            failing_since_utc=None,
            granules_polled=int(polled),
            granules_found=int(found),
            details=details,
        )
        self.run[key] = "ok"

    def failure(self, key: str, error: BaseException | str, polled: int = 0) -> None:
        entry = self._entry(key)
        message = (
            f"{type(error).__name__}: {error}"
            if isinstance(error, BaseException)
            else str(error)
        )
        entry.update(
            last_attempt_utc=to_iso(self.now),
            last_error=message[:1000],
            last_error_utc=to_iso(self.now),
            consecutive_failures=int(entry.get("consecutive_failures") or 0) + 1,
            failing_since_utc=entry.get("failing_since_utc") or to_iso(self.now),
            granules_polled=int(polled),
            granules_found=0,
        )
        self.run[key] = "error"

    def failing_too_long(self, max_hours: float) -> list[str]:
        late = []
        for key, entry in self.sources.items():
            since = entry.get("failing_since_utc")
            if since and self.now - parse_utc(since) > timedelta(hours=max_hours):
                late.append(key)
        return sorted(late)

    def all_failed_this_run(self) -> bool:
        return bool(self.run) and all(v == "error" for v in self.run.values())

    def exit_code(self, max_failing_hours: float) -> int:
        if self.all_failed_this_run():
            LOGGER.error("every source failed this run")
            return 2
        late = self.failing_too_long(max_failing_hours)
        if late:
            LOGGER.error(
                "sources failing for more than %s h: %s",
                max_failing_hours,
                ", ".join(late),
            )
            return 3
        return 0

    def to_dict(self) -> dict:
        return {
            "generated_at": to_iso(self.now),
            "this_run": dict(sorted(self.run.items())),
            "sources": dict(sorted(self.sources.items())),
        }
