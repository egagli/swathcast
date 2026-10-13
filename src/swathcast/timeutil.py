"""The one place timestamps are parsed.

Every timestamp in this package is a timezone-aware ``datetime`` in UTC.
Providers send ISO-8601 strings in several shapes (``Z`` suffix, explicit
offsets, 7-digit fractional seconds, no timezone at all). :func:`parse_utc`
accepts all of them and *converts* any offset to UTC rather than discarding
it. A string without timezone information is rejected unless the caller says
it is known to be UTC, because silently guessing is how latencies end up off
by a time zone.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pandas as pd

_FRACTION = re.compile(r"(\.\d+)")


def parse_utc(
    value: str | datetime | pd.Timestamp, *, assume_utc: bool = False
) -> datetime:
    """Parse ``value`` into a timezone-aware UTC datetime.

    Offsets are converted (``2026-01-01T02:00:00+02:00`` -> ``00:00Z``).
    Fractional seconds beyond microseconds are truncated. Naive inputs raise
    ``ValueError`` unless ``assume_utc=True``.
    """
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty timestamp")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        # Python <3.11-style fromisoformat is strict about >6 fraction digits.
        match = _FRACTION.search(text)
        if match and len(match.group(1)) > 7:
            text = text.replace(match.group(1), match.group(1)[:7], 1)
        dt = datetime.fromisoformat(text)
    else:
        raise TypeError(f"cannot parse timestamp from {type(value).__name__}")

    if dt.tzinfo is None or dt.utcoffset() is None:
        if not assume_utc:
            raise ValueError(f"timestamp has no timezone: {value!r}")
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_iso(dt: datetime | None) -> str | None:
    """Format as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), or ``None``."""
    if dt is None or pd.isna(dt):
        return None
    return parse_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_odata(dt: datetime) -> str:
    """Format for an OData filter literal (millisecond precision, ``Z``)."""
    return (
        parse_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{parse_utc(dt).microsecond // 1000:03d}Z"
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


def hours_between(start: datetime, end: datetime) -> float:
    return (parse_utc(end) - parse_utc(start)).total_seconds() / 3600.0
