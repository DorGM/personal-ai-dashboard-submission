"""Calendar rules for records (A-03 E7) and the 15-day history window (PRODUCT_SPEC §1).

- "Today" is the Asia/Jerusalem calendar date.
- Record dates are explicit `YYYY-MM-DD` values; no future dates, except task dates, which may
  be up to `TASK_DAYS_AHEAD` days ahead.
- A history window spans at most `MAX_WINDOW_DAYS` calendar days and always lies inside
  [lower, upper]: lower = the user's earliest record in the domain, upper = today (tasks:
  today + 14). Undocumented days are UI placeholders, never stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BeforeValidator

from app.records.errors import InvalidRecord

TIMEZONE = ZoneInfo("Asia/Jerusalem")
MAX_WINDOW_DAYS = 15
TASK_DAYS_AHEAD = 14
MIN_DATE = date(1900, 1, 1)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today_local() -> date:
    """Today's calendar date in Asia/Jerusalem."""
    return datetime.now(TIMEZONE).date()


def _explicit_date(value: object) -> object:
    # Only a real date or an ISO `YYYY-MM-DD` string; pydantic's lax mode would otherwise also
    # accept timestamps and datetimes, which are not explicit calendar dates.
    if isinstance(value, datetime):
        raise ValueError("Enter a date as YYYY-MM-DD.")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError:
            raise ValueError("This date does not exist.") from None
        if parsed < MIN_DATE:
            raise ValueError("Enter a date after 1900.")
        return parsed
    raise ValueError("Enter a date as YYYY-MM-DD.")


RecordDate = Annotated[date, BeforeValidator(_explicit_date)]
"""Pydantic field type for an explicit calendar date."""


def check_date(value: date, today: date, *, days_ahead: int = 0) -> None:
    """Raise InvalidRecord if `value` lies more than `days_ahead` days after `today`."""
    if value > today + timedelta(days=days_ahead):
        if days_ahead:
            raise InvalidRecord(f"The date can be at most {days_ahead} days from today.")
        raise InvalidRecord("The date can't be in the future.")


def parse_iso(value: str | None) -> date | None:
    """Parse an optional query-string date; None if empty. Raises InvalidRecord if malformed."""
    if value is None or not value.strip():
        return None
    try:
        parsed = _explicit_date(value)
    except ValueError:
        raise InvalidRecord("The selected dates are not valid.") from None
    assert isinstance(parsed, date)
    return parsed


@dataclass(frozen=True)
class Window:
    """An inclusive history window inside the allowed bounds [lower, upper]."""

    start: date
    end: date
    lower: date
    upper: date

    @property
    def has_prev(self) -> bool:
        return self.start > self.lower

    @property
    def has_next(self) -> bool:
        return self.end < self.upper

    @property
    def prev_end(self) -> date:
        """End date of the adjacent earlier window."""
        return max(self.start - timedelta(days=1), self.lower)

    @property
    def next_end(self) -> date:
        """End date of the adjacent later window."""
        return min(self.end + timedelta(days=MAX_WINDOW_DAYS), self.upper)

    def days(self, *, ascending: bool) -> list[date]:
        span = (self.end - self.start).days
        out = [self.start + timedelta(days=i) for i in range(span + 1)]
        return out if ascending else out[::-1]


def history_window(earliest: date | None, today: date, *, start: date | None = None, end: date | None = None,
                   days_ahead: int = 0) -> Window | None:
    """Compute the history window to show; None when the domain has no records (empty state).

    Default: the 15 days ending at `upper` (today, or today + days_ahead for tasks), clipped to
    the earliest record. A requested range is moved into [lower, upper]: one ending before the
    earliest record becomes the first window (from the earliest record), one starting after
    `upper` becomes the last. A window longer than 15 days or with start after end raises
    InvalidRecord. For tasks (days_ahead > 0) the lower bound never exceeds today, so today
    stays reachable.
    """
    if earliest is None:
        return None
    span = timedelta(days=MAX_WINDOW_DAYS - 1)
    upper = today + timedelta(days=days_ahead)
    lower = min(earliest, today if days_ahead else upper)
    if start is not None and end is not None:
        if start > end:
            raise InvalidRecord("The start date must be on or before the end date.")
        if end - start > span:
            raise InvalidRecord(f"A history window can span at most {MAX_WINDOW_DAYS} days.")
    if start is not None and start > upper:
        start = None  # entirely after the allowed range: show the last window
    elif start is not None and start < lower and end is None:
        start = lower  # an open-ended start before the first record: the first full window
    if end is None:
        end = upper if start is None else start + span
    if end < lower:
        start, end = lower, lower + span  # entirely before the first record: show the first window
    end = min(end, upper)
    start = end - span if start is None else start
    start = min(max(start, lower), end)
    return Window(start=start, end=end, lower=lower, upper=upper)
