from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from rssd import timeutil


def test_now_utc_is_aware():
    now = timeutil.now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_utc_naive_assumed_utc():
    naive = datetime(2026, 9, 15, 21, 14, 2)
    result = timeutil.to_utc(naive)
    assert result.tzinfo is timezone.utc
    assert result.hour == 21


def test_to_utc_converts_other_zones():
    from datetime import timezone as tz

    plus_five = datetime(2026, 9, 15, 21, 0, 0, tzinfo=tz(timedelta(hours=5)))
    result = timeutil.to_utc(plus_five)
    assert result.hour == 16
    assert result.tzinfo == timezone.utc


def test_parse_feed_date_struct_time():
    st = time.struct_time((2026, 9, 7, 0, 0, 0, 0, 250, 0))
    result = timeutil.parse_feed_date(st)
    assert result == datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_feed_date_string_rfc822():
    result = timeutil.parse_feed_date("Tue, 15 Sep 2026 19:30:03 +0000")
    assert result == datetime(2026, 9, 15, 19, 30, 3, tzinfo=timezone.utc)


def test_parse_feed_date_string_iso():
    result = timeutil.parse_feed_date("2026-09-07T00:00:00+00:00")
    assert result == datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_feed_date_none_input():
    assert timeutil.parse_feed_date(None) is None


def test_parse_feed_date_garbage_never_raises():
    assert timeutil.parse_feed_date("not a date at all") is None
    assert timeutil.parse_feed_date("") is None
    assert timeutil.parse_feed_date(12345) is None
    assert timeutil.parse_feed_date([1, 2, 3]) is None


def test_parse_feed_date_bad_struct_time_never_raises():
    # Year way out of range for a real calendar date.
    st = time.struct_time((999999999, 1, 1, 0, 0, 0, 0, 1, 0))
    assert timeutil.parse_feed_date(st) is None


def test_parse_http_date():
    result = timeutil.parse_http_date("Tue, 15 Sep 2026 21:14:02 GMT")
    assert result == datetime(2026, 9, 15, 21, 14, 2, tzinfo=timezone.utc)


def test_parse_http_date_none_and_garbage():
    assert timeutil.parse_http_date(None) is None
    assert timeutil.parse_http_date("") is None
    assert timeutil.parse_http_date("not a date") is None


def test_iso():
    dt = datetime(2026, 9, 15, 21, 14, 2, tzinfo=timezone.utc)
    assert timeutil.iso(dt) == "2026-09-15T21:14:02Z"


def test_iso_naive_treated_as_utc():
    dt = datetime(2026, 9, 15, 21, 14, 2)
    assert timeutil.iso(dt) == "2026-09-15T21:14:02Z"


def test_iso_ms():
    dt = datetime(2026, 9, 15, 21, 14, 2, 140000, tzinfo=timezone.utc)
    assert timeutil.iso_ms(dt) == "2026-09-15T21:14:02.140Z"


def test_filename_stamp():
    dt = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
    assert timeutil.filename_stamp(dt) == "20260907T000000Z"


def test_filename_stamp_has_no_colons():
    dt = timeutil.now_utc()
    stamp = timeutil.filename_stamp(dt)
    assert ":" not in stamp


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", 30),
        ("15m", 900),
        ("2h", 7200),
        ("1d", 86400),
        ("900", 900),
        ("0", 0),
        ("  15m  ", 900),
        ("15M", 900),
    ],
)
def test_parse_duration(text, expected):
    assert timeutil.parse_duration(text) == expected


@pytest.mark.parametrize("text", [None, "", "abc", "15x", "-5m", "5.5m"])
def test_parse_duration_invalid(text):
    assert timeutil.parse_duration(text) is None


def test_resolve_published_feed_origin():
    first_seen = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
    published = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)
    dt, origin = timeutil.resolve_published(published, first_seen, 86400)
    assert dt == published
    assert origin == "feed"


def test_resolve_published_none_becomes_first_seen():
    first_seen = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
    dt, origin = timeutil.resolve_published(None, first_seen, 86400)
    assert dt == first_seen
    assert origin == "first-seen"


def test_resolve_published_clamped():
    first_seen = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
    far_future = first_seen + timedelta(days=30)
    dt, origin = timeutil.resolve_published(far_future, first_seen, 86400)
    assert dt == first_seen + timedelta(seconds=86400)
    assert origin == "clamped"


def test_resolve_published_at_exact_boundary_not_clamped():
    first_seen = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
    exactly_at_limit = first_seen + timedelta(seconds=86400)
    dt, origin = timeutil.resolve_published(exactly_at_limit, first_seen, 86400)
    assert dt == exactly_at_limit
    assert origin == "feed"


def test_resolve_published_never_returns_none():
    first_seen = timeutil.now_utc()
    for published in (None, first_seen, first_seen + timedelta(days=999)):
        dt, origin = timeutil.resolve_published(published, first_seen, 86400)
        assert dt is not None
        assert isinstance(dt, datetime)
