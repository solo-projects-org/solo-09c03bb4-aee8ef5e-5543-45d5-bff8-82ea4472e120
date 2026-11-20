"""UTC time helpers.

All archive timestamps are stored as integer epoch milliseconds (UTC) so that
ordering and range queries are exact.  Display strings always end in ``Z`` to
make the broadcast time zone unambiguous.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_from_ms(ms: int) -> str:
    """Format epoch milliseconds as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    seconds, millis = divmod(int(ms), 1000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def now_iso() -> str:
    return iso_from_ms(now_ms())


def parse_iso_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp (with or without ``Z``) as UTC epoch ms."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_clock_ms(value: str) -> int:
    """Parse a WebVTT/MPEG-TS style clock ``HH:MM:SS.mmm``.

    WebVTT also permits ``MM:SS.mmm`` (the hour component is optional).
    """
    text = value.strip().replace(",", ".")
    parts = text.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        raise ValueError(f"invalid clock time: {value!r}")
    seconds_float = float(seconds)
    whole_seconds = int(seconds_float)
    millis = int(round((seconds_float - whole_seconds) * 1000))
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + whole_seconds * 1000
        + millis
    )


def clock_from_ms(ms: int) -> str:
    """Format epoch-relative milliseconds as ``HH:MM:SS.mmm``."""
    ms = int(ms)
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
