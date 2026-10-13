from datetime import UTC, datetime, timedelta, timezone

import pandas as pd
import pytest

from swathcast.timeutil import hours_between, parse_utc, to_iso, to_odata


def test_z_suffix():
    assert parse_utc("2026-09-28T23:50:00.570907Z") == datetime(
        2026, 9, 28, 23, 50, 0, 570907, tzinfo=UTC
    )


def test_offset_is_converted_not_dropped():
    dt = parse_utc("2026-01-01T02:30:00+02:00")
    assert dt == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    assert dt.tzinfo is UTC
    assert parse_utc("2025-12-31T19:00:00-05:00") == datetime(
        2026, 1, 1, 0, 0, tzinfo=UTC
    )


def test_seven_digit_fraction_and_lowercase_z():
    assert parse_utc("2026-09-28T12:00:00.1234567z").microsecond == 123456


def test_naive_rejected_unless_assumed():
    with pytest.raises(ValueError):
        parse_utc("2026-09-28T12:00:00")
    assert parse_utc("2026-09-28T12:00:00", assume_utc=True) == datetime(
        2026, 9, 28, 12, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        parse_utc(datetime(2026, 1, 1))


def test_datetime_and_timestamp_inputs_are_converted():
    tz = timezone(timedelta(hours=-8))
    assert parse_utc(datetime(2026, 1, 1, 16, tzinfo=tz)) == datetime(
        2026, 1, 2, 0, tzinfo=UTC
    )
    assert parse_utc(pd.Timestamp("2026-01-01T00:00:00+01:00")) == datetime(
        2025, 12, 31, 23, tzinfo=UTC
    )


def test_bad_inputs():
    with pytest.raises(ValueError):
        parse_utc("")
    with pytest.raises(TypeError):
        parse_utc(12345)


def test_formatting():
    dt = parse_utc("2026-09-28T23:50:00.570907+01:00")
    assert to_iso(dt) == "2026-09-28T22:50:00Z"
    assert to_odata(dt) == "2026-09-28T22:50:00.570Z"
    assert to_iso(None) is None
    assert hours_between("2026-01-01T00:00:00Z", "2026-01-01T01:30:00Z") == 1.5
