"""Date parsing, UTC handling, clamping, and filename stamps. PURE.

Every timestamp rssd persists is UTC and every function here is total: bad or
missing input degrades to a sensible fallback rather than raising, because a
sortable file needs a stamp even when the feed lies about it (SPEC §4.2).
"""

from __future__ import annotations

import calendar
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from rssd.models import DateOrigin


def now_utc() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Normalise to an aware UTC datetime. A naive input is assumed to already
    be UTC (feedparser and email.utils both hand us naive-but-UTC values in
    some paths)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed_date(value: object) -> datetime | None:
    """Parse a date as feedparser hands it back: a ``time.struct_time`` (from
    ``*_parsed`` fields) or a raw string (fallback for oddball formats
    feedparser didn't normalise). Never raises -- returns None on anything it
    can't make sense of.
    """
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        try:
            # struct_time from feedparser's *_parsed fields is UTC already.
            timestamp = calendar.timegm(value)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            dt = None
        if dt is not None:
            return to_utc(dt)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return to_utc(dt)
    return None


def parse_http_date(value: str | None) -> datetime | None:
    """Parse an RFC 7231 HTTP date (used for Last-Modified / Retry-After).
    Never raises."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return to_utc(dt)


def iso(dt: datetime) -> str:
    """"2026-09-15T21:14:02Z" -- second precision, always UTC, always Z."""
    dt = to_utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def iso_ms(dt: datetime) -> str:
    """"2026-09-15T21:14:02.140Z" -- millisecond precision, for the event log."""
    dt = to_utc(dt)
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def filename_stamp(dt: datetime) -> str:
    """"20260907T000000Z" -- compact ISO, no colons, for entry filenames."""
    dt = to_utc(dt)
    return dt.strftime("%Y%m%dT%H%M%SZ")


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)

_DURATION_UNITS = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def parse_duration(text: str | None) -> int | None:
    """Parse "30s" "15m" "2h" "1d" "900" into seconds. None/unparsable -> None."""
    if text is None:
        return None
    match = _DURATION_RE.match(text)
    if not match:
        return None
    count, unit = match.groups()
    multiplier = _DURATION_UNITS.get(unit.lower())
    if multiplier is None:
        return None
    return int(count) * multiplier


def resolve_published(
    published: datetime | None, first_seen: datetime, clamp_seconds: int
) -> tuple[datetime, DateOrigin]:
    """Implements SPEC §4.2.

    - A real date within the clamp window passes through, tagged "feed".
    - No date -> first_seen, tagged "first-seen".
    - A date further ahead than clamp_seconds -> first_seen + clamp_seconds,
      tagged "clamped".

    Never returns None -- sortability of the filename is non-negotiable.
    """
    first_seen = to_utc(first_seen)
    if published is None:
        return first_seen, "first-seen"
    published = to_utc(published)
    limit = first_seen + timedelta(seconds=clamp_seconds)
    if published > limit:
        return limit, "clamped"
    return published, "feed"
