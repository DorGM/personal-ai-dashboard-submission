"""Test-only, in-memory implementation of `ports.MetricsBackend` (the B-05 contract, §4).

It is NOT a metric registry for the app: it exists so the analytics tools can be tested before
B-05 is merged. It follows the contract semantics the tools rely on:

- data is keyed by user id; a function only ever reads rows of the `user_id` it is given, and
  every call records that id in `calls` so tests can assert the server-side id was used;
- documented day = a date with ≥ 1 row in the domain; a missing day has value None, an explicit
  0 is a value;
- daily_value: the day's value (mean if several rows); event_count: number of rows matching the
  filters on a documented day (0 when documented but none match); ratio: completed / all;
- range validation and filter validation raise `FakeMetricError(code, message)`.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import MappingProxyType

from app.analytics import policy
from app.analytics.ports import Domain, GroupBy, MetricKind, MetricsBackend, PeriodLike


@dataclass(frozen=True)
class FakeDef:
    id: str
    kind: MetricKind
    unit: str
    domain: Domain
    label: str
    min_delta: float
    subtype_field: str | None = None
    allowed_filters: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class FakePeriod:
    start: date
    end: date


@dataclass(frozen=True)
class FakeResolved:
    period: FakePeriod
    requested_start: date
    clipped: bool


@dataclass(frozen=True)
class FakeCoverage:
    earliest_record: date | None
    latest_record: date | None
    documented_days: int
    total_days: int
    ratio: float
    undocumented_ranges: tuple[tuple[date, date], ...]
    truncated: bool


@dataclass(frozen=True)
class FakePoint:
    day: date
    value: float | None
    documented: bool
    record_ids: tuple[int, ...]


@dataclass(frozen=True)
class FakeRef:
    day: date
    value: float
    record_id: int


@dataclass(frozen=True)
class FakeGroup:
    key: str
    n: int
    documented_days: int
    mean: float | None
    count: int | None
    rate_per_documented_day: float | None
    pct: float | None


@dataclass(frozen=True)
class FakeSummary:
    metric: str
    period: FakeResolved
    coverage: FakeCoverage
    n_observations: int
    mean: float | None
    median: float | None
    min: float | None
    max: float | None
    sum: float | None
    min_record: FakeRef | None
    max_record: FakeRef | None
    first: FakeRef | None
    last: FakeRef | None
    change: float | None
    rate_per_documented_day: float | None
    ratio_numerator: int | None
    ratio_denominator: int | None
    ratio_pct: float | None
    groups: tuple[FakeGroup, ...]
    groups_truncated: bool
    calorie_complete_days: int | None
    calorie_partial_days: int | None


class FakeMetricError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Row:
    day: date
    value: float | None
    record_id: int
    attrs: Mapping[str, str]


_TASK_FILTERS = {"category": frozenset({"family", "career", "friends", "housework", "other"}),
                 "status": frozenset({"pending", "completed"})}
FAKE_REGISTRY: Mapping[str, FakeDef] = MappingProxyType({d.id: d for d in (
    FakeDef("sleep.hours", "daily_value", "h", "sleep", "Sleep", 0.25),
    FakeDef("steps.count", "daily_value", "steps", "steps", "Steps", 500.0),
    FakeDef("weight.kg", "daily_value", "kg", "weight", "Weight", 0.5),
    FakeDef("workouts.count", "event_count", "workouts", "workouts", "Workouts", 0.1, "workout_type",
            {"workout_type": frozenset({"aerobic", "weights", "bodyweight", "stick_spinning"})}),
    FakeDef("tasks.count", "event_count", "tasks", "tasks", "Tasks", 0.1, "category", _TASK_FILTERS),
    FakeDef("movies.count", "event_count", "items", "movies", "Movies", 0.1),
    FakeDef("tasks.completion_rate", "ratio", "%", "tasks", "Task completion", 5.0, "category",
            {"category": _TASK_FILTERS["category"]}),
)})


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


class FakeMetrics:
    """See module docstring. Add data with `add(user_id, domain, day, value, **attrs)`."""

    MetricError: type[Exception] = FakeMetricError

    def __init__(self, registry: Mapping[str, FakeDef] = FAKE_REGISTRY) -> None:
        self.REGISTRY: Mapping[str, FakeDef] = registry
        self.rows: dict[tuple[int, str], list[Row]] = {}
        self.calls: list[int] = []
        self._next_id = 1

    # ------------------------------------------------------------ test data
    def add(self, user_id: int, domain: str, day: date, value: float | None = None, **attrs: str) -> int:
        record_id = self._next_id
        self._next_id += 1
        self.rows.setdefault((user_id, domain), []).append(Row(day, value, record_id, dict(attrs)))
        return record_id

    def add_daily(self, user_id: int, domain: str, values: Mapping[date, float]) -> None:
        for day, value in values.items():
            self.add(user_id, domain, day, value)

    # ------------------------------------------------------------ internals
    def _rows(self, user_id: int, domain: str, start: date | None = None, end: date | None = None) -> list[Row]:
        rows = sorted(self.rows.get((user_id, domain), []), key=lambda r: (r.day, r.record_id))
        return [r for r in rows if (start is None or r.day >= start) and (end is None or r.day <= end)]

    def _def(self, metric_id: str) -> FakeDef:
        if metric_id not in self.REGISTRY:
            raise FakeMetricError("unknown_metric", "unknown metric")
        return self.REGISTRY[metric_id]

    def _check_filters(self, d: FakeDef, filters: Mapping[str, str] | None) -> Mapping[str, str]:
        for name, value in (filters or {}).items():
            if name not in d.allowed_filters or value not in d.allowed_filters[name]:
                raise FakeMetricError("invalid_filter", "filter not allowed")
        return filters or {}

    @staticmethod
    def _matches(row: Row, filters: Mapping[str, str]) -> bool:
        return all(row.attrs.get(k) == v for k, v in filters.items())

    def _day_values(self, user_id: int, d: FakeDef, period: PeriodLike,
                    filters: Mapping[str, str]) -> list[tuple[date, float, int]]:
        """(day, value, first record id) for each documented day of the metric."""
        by_day: dict[date, list[Row]] = {}
        for row in self._rows(user_id, d.domain, period.start, period.end):
            by_day.setdefault(row.day, []).append(row)
        out: list[tuple[date, float, int]] = []
        for day in sorted(by_day):
            rows = by_day[day]
            if d.kind == "daily_value":
                values = [r.value for r in rows if r.value is not None]
                if values:
                    out.append((day, math.fsum(values) / len(values), rows[0].record_id))
            else:
                out.append((day, float(sum(self._matches(r, filters) for r in rows)), rows[0].record_id))
        return out

    # ------------------------------------------------------------ contract
    def make_period(self, start: date, end: date) -> FakePeriod:
        if start > end:
            raise FakeMetricError("invalid_range", "start after end")
        return FakePeriod(start, end)

    def resolve_period(self, conn: sqlite3.Connection, user_id: int, metric_id: str, start: date, end: date, *,
                       today: date) -> FakeResolved:
        self.calls.append(user_id)
        d = self._def(metric_id)
        if start > end or end > today or (end - start).days + 1 > policy.MAX_SPAN_DAYS:
            raise FakeMetricError("invalid_range", "bad range")
        rows = self._rows(user_id, d.domain)
        earliest = rows[0].day if rows else None
        if earliest is not None and start < earliest <= end:
            return FakeResolved(FakePeriod(earliest, end), start, True)
        return FakeResolved(FakePeriod(start, end), start, False)

    def coverage(self, conn: sqlite3.Connection, user_id: int, domain: Domain, period: PeriodLike) -> FakeCoverage:
        self.calls.append(user_id)
        all_rows = self._rows(user_id, domain)
        documented = {r.day for r in self._rows(user_id, domain, period.start, period.end)}
        total = (period.end - period.start).days + 1
        ranges: list[tuple[date, date]] = []
        run_start: date | None = None
        for day in _days(period.start, period.end):
            if day not in documented and run_start is None:
                run_start = day
            if day in documented and run_start is not None:
                ranges.append((run_start, day - timedelta(days=1)))
                run_start = None
        if run_start is not None:
            ranges.append((run_start, period.end))
        return FakeCoverage(
            earliest_record=all_rows[0].day if all_rows else None,
            latest_record=all_rows[-1].day if all_rows else None,
            documented_days=len(documented), total_days=total,
            ratio=len(documented) / total if total else 0.0,
            undocumented_ranges=tuple(ranges[:policy.MAX_RANGES]), truncated=len(ranges) > policy.MAX_RANGES)

    def daily_points(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike,
                     filters: Mapping[str, str] | None = None) -> list[FakePoint]:
        self.calls.append(user_id)
        d = self._def(metric_id)
        values = {day: (value, rid) for day, value, rid in
                  self._day_values(user_id, d, period, self._check_filters(d, filters))}
        return [FakePoint(day, values[day][0], True, (values[day][1],)) if day in values
                else FakePoint(day, None, False, ()) for day in _days(period.start, period.end)]

    def summarize(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike, *,
                  group_by: GroupBy = "none", filters: Mapping[str, str] | None = None) -> FakeSummary:
        self.calls.append(user_id)
        d = self._def(metric_id)
        flt = self._check_filters(d, filters)
        cov = self.coverage(conn, user_id, d.domain, period)
        resolved = FakeResolved(FakePeriod(period.start, period.end), period.start, False)
        rows = [r for r in self._rows(user_id, d.domain, period.start, period.end) if self._matches(r, flt)]
        mean = median = lo = hi = total = change = rate = pct = None
        min_ref = max_ref = first = last = None
        num = den = None
        if d.kind == "daily_value":
            days = self._day_values(user_id, d, period, flt)
            n = len(days)
            if days:
                values = [v for _, v, _ in days]
                mean, median = math.fsum(values) / n, statistics.median(values)
                lo, hi, total = min(values), max(values), math.fsum(values)
                lo_day = min(days, key=lambda t: t[1])
                hi_day = max(days, key=lambda t: t[1])
                min_ref, max_ref = FakeRef(lo_day[0], lo_day[1], lo_day[2]), FakeRef(hi_day[0], hi_day[1], hi_day[2])
                first, last = FakeRef(*days[0]), FakeRef(*days[-1])
                if d.id == "weight.kg" and n >= 2:
                    change = days[-1][1] - days[0][1]
        elif d.kind == "event_count":
            n = len(rows)
            total = float(n)
            rate = n / cov.documented_days if cov.documented_days else None
        else:  # ratio
            n = den = len(rows)
            num = sum(r.attrs.get("status") == "completed" for r in rows)
            pct = num / den * 100 if den else None
        groups: list[FakeGroup] = []
        if group_by == "subtype" and d.subtype_field:
            keys = sorted({r.attrs.get(d.subtype_field, "") for r in rows})
            for key in keys:
                members = [r for r in rows if r.attrs.get(d.subtype_field) == key]
                gdays = len({r.day for r in members})
                groups.append(FakeGroup(key, len(members), gdays, None, len(members),
                                        len(members) / cov.documented_days if cov.documented_days else None, None))
        return FakeSummary(
            metric=d.id, period=resolved, coverage=cov, n_observations=n, mean=mean, median=median, min=lo,
            max=hi, sum=total, min_record=min_ref, max_record=max_ref, first=first, last=last, change=change,
            rate_per_documented_day=rate, ratio_numerator=num, ratio_denominator=den, ratio_pct=pct,
            groups=tuple(groups[:policy.MAX_GROUPS]), groups_truncated=len(groups) > policy.MAX_GROUPS,
            calorie_complete_days=None, calorie_partial_days=None)


def _conforms(fake: FakeMetrics) -> MetricsBackend:
    return fake  # mypy checks that the fake implements the B-05 contract protocol
