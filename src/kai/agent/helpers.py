"""Shared helpers for the agent package.

Centralizes the datetime/timezone utilities that scheduler and tool modules
previously each inlined or duplicated, so there is exactly one place that
knows how to produce/parse our ISO-8601 wire format and resolve timezones.
"""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UTC = ZoneInfo("UTC")


def utcnow() -> datetime:
    """Aware current UTC time. Single source so tests can monkeypatch one name."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Render ``dt`` as a whole-second UTC ``YYYY-MM-DDTHH:MM:SSZ`` string.

    The trailing ``Z`` and lack of microseconds is intentional: it is the
    format we persist, so round-tripping through :func:`parse_iso` loses no
    information the store ever recorded.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (with optional trailing ``Z``) to aware UTC.

    Naive inputs are assumed to be UTC. Raises ``ValueError`` on bad input.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def resolve_tz(name: str | None) -> ZoneInfo:
    """Resolve a timezone name to a :class:`ZoneInfo`, defaulting to UTC.

    Unknown names log nothing here and fall back to UTC rather than raising;
    callers that need to surface the error to a user should validate first.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return _UTC


def parse_relative(text: str) -> timedelta | None:
    """Parse an ``"in 2h30m"`` / ``"in 1d"`` style offset into a timedelta.

    Returns ``None`` for anything that isn't a relative offset, and ``None``
    for a zero/negative total (callers treat that as "no delta").
    """
    m = _REL_RE.match(text)
    if not m:
        return None
    matches = _REL_UNIT_RE.findall(text)
    if not matches:
        return None
    delta = timedelta()
    for amount, unit in matches:
        n = int(amount)
        u = unit.lower()
        if u.startswith("d"):
            delta += timedelta(days=n)
        elif u.startswith("h"):
            delta += timedelta(hours=n)
        elif u.startswith("m"):
            delta += timedelta(minutes=n)
    return delta if delta > timedelta() else None


def try_iso(text: str, tz: ZoneInfo) -> datetime | None:
    """Lenient ISO-8601 parse tolerating a space separator and a ``Z`` suffix.

    Accepts ``2026-06-18T15:30Z``, ``2026-06-18 15:30``, and bare
    ``2026-06-18T15:30``. Naive timestamps are localized to ``tz``. Returns
    ``None`` (never raises) so callers can try several formats in sequence.
    """
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    t = re.sub(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", r"\1T\2", t)
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


_REL_RE = re.compile(
    r"^\s*in\s+((?:(?:\d+)\s*(?:d|days?)\s*)?"
    r"(?:(?:\d+)\s*(?:h|hrs?|hours?)\s*)?"
    r"(?:(?:\d+)\s*(?:m|mins?|minutes?)\s*)?)$",
    re.IGNORECASE,
)
_REL_UNIT_RE = re.compile(r"(\d+)\s*(d|h|m|days?|hrs?|hours?|mins?|minutes?)", re.IGNORECASE)
