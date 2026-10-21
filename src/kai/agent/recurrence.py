"""Recurrence arithmetic for scheduled tasks."""

from calendar import monthrange
from datetime import datetime, timedelta


def advance_weekly(next_dt: datetime, interval: int, weekdays: tuple[int, ...] | None) -> datetime:
    """Advance to the next matching weekday, or one interval ahead."""
    if not weekdays:
        return next_dt + timedelta(weeks=interval)
    for _ in range(7 * interval + 1):
        next_dt = next_dt + timedelta(days=1)
        if next_dt.weekday() in weekdays:
            break
    return next_dt


def advance_monthly(next_dt: datetime, interval: int) -> datetime:
    """Advance by whole months, clamping the day to the month length."""
    year = next_dt.year
    month = next_dt.month + interval
    while month > 12:
        month -= 12
        year += 1
    day = min(next_dt.day, monthrange(year, month)[1])
    return next_dt.replace(year=year, month=month, day=day)
