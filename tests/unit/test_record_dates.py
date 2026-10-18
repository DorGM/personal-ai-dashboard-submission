"""Date rules (A-03 E7) and the 15-day history window (PRODUCT_SPEC §1)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from app.records.dates import (
    MAX_WINDOW_DAYS,
    RecordDate,
    check_date,
    history_window,
    parse_iso,
    today_local,
)
from app.records.errors import InvalidRecord

T = date(2026, 10, 18)


def days(n: int) -> timedelta:
    return timedelta(days=n)


class _M(BaseModel):
    d: RecordDate


@pytest.mark.parametrize("value", ["2026-10-18", " 2026-02-28 ", date(2026, 1, 1)])
def test_explicit_dates_are_accepted(value: object) -> None:
    assert isinstance(_M.model_validate({"d": value}).d, date)


@pytest.mark.parametrize("value", ["", "18.10.2026", "2026-02-30", "2026-13-01", "1700000000", 1700000000,
                                   "2026-10-18T10:00", datetime(2026, 10, 18, 10), "1899-12-31", "yesterday"])
def test_non_explicit_or_invalid_dates_are_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        _M.model_validate({"d": value})


def test_future_dates_rejected_except_within_allowance() -> None:
    check_date(T, T)
    check_date(T - days(400), T)
    with pytest.raises(InvalidRecord, match="future"):
        check_date(T + days(1), T)
    check_date(T + days(14), T, days_ahead=14)
    with pytest.raises(InvalidRecord, match="14 days"):
        check_date(T + days(15), T, days_ahead=14)


def test_today_is_a_date() -> None:
    assert isinstance(today_local(), date)


def test_parse_iso() -> None:
    assert parse_iso(None) is None and parse_iso(" ") is None
    assert parse_iso("2026-10-01") == date(2026, 10, 1)
    with pytest.raises(InvalidRecord):
        parse_iso("01.10.2026")


# ---------------------------------------------------------------- window
def test_no_records_means_empty_state() -> None:
    assert history_window(None, T) is None


def test_default_is_trailing_15_days_ending_today() -> None:
    w = history_window(T - days(100), T)
    assert w is not None
    assert (w.start, w.end) == (T - days(14), T)
    assert len(w.days(ascending=False)) == MAX_WINDOW_DAYS
    assert w.days(ascending=False)[0] == T and w.has_prev and not w.has_next


def test_default_is_clipped_to_earliest_record() -> None:
    w = history_window(T - days(4), T)
    assert w is not None and (w.start, w.end) == (T - days(4), T)
    assert not w.has_prev and not w.has_next
    edge = history_window(T - days(14), T)
    assert edge is not None and edge.start == T - days(14) and not edge.has_prev
    beyond = history_window(T - days(15), T)
    assert beyond is not None and beyond.start == T - days(14) and beyond.has_prev


def test_moving_earlier_and_later_stays_within_bounds() -> None:
    w = history_window(T - days(20), T)
    assert w is not None and w.prev_end == T - days(15)
    earlier = history_window(T - days(20), T, end=w.prev_end)
    assert earlier is not None and (earlier.start, earlier.end) == (T - days(20), T - days(15))
    assert not earlier.has_prev and earlier.has_next and earlier.next_end == T
    later = history_window(T - days(20), T, end=earlier.next_end)
    assert later is not None and later.end == T


def test_requested_range_is_clamped_into_bounds() -> None:
    w = history_window(T - days(5), T, start=T - days(10), end=T + days(3))
    assert w is not None and (w.start, w.end) == (T - days(5), T)
    w = history_window(T - days(50), T, start=T - days(40))
    assert w is not None and (w.start, w.end) == (T - days(40), T - days(26))


@pytest.mark.parametrize(("start", "end"), [
    (None, T - days(400)), (T - days(420), T - days(410)), (T - days(400), None), (T - days(60), None),
])
def test_range_before_first_record_shows_first_full_window(start: date | None, end: date | None) -> None:
    w = history_window(T - days(50), T, start=start, end=end)
    assert w is not None and (w.start, w.end) == (T - days(50), T - days(36))
    short = history_window(T - days(5), T, end=T - days(400))  # fewer than 15 days of history
    assert short is not None and (short.start, short.end) == (T - days(5), T)


@pytest.mark.parametrize(("start", "end"), [(T + days(3), None), (T + days(3), T + days(5))])
def test_range_after_today_shows_last_window(start: date, end: date | None) -> None:
    w = history_window(T - days(50), T, start=start, end=end)
    assert w is not None and (w.start, w.end) == (T - days(14), T)


def test_custom_range_up_to_15_days() -> None:
    w = history_window(T - days(50), T, start=T - days(20), end=T - days(6))
    assert w is not None and len(w.days(ascending=True)) == 15
    with pytest.raises(InvalidRecord, match="15 days"):
        history_window(T - days(50), T, start=T - days(20), end=T - days(5))
    with pytest.raises(InvalidRecord, match="start date"):
        history_window(T - days(50), T, start=T - days(2), end=T - days(3))


def test_task_window_is_today_and_next_14_days() -> None:
    w = history_window(T - days(30), T, days_ahead=14)
    assert w is not None and (w.start, w.end) == (T, T + days(14)) and w.has_prev
    future_only = history_window(T + days(3), T, days_ahead=14)
    assert future_only is not None and (future_only.start, future_only.end) == (T, T + days(14))
    assert history_window(T - days(30), T, end=T + days(40), days_ahead=14).end == T + days(14)  # type: ignore[union-attr]
