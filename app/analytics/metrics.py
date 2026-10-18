"""Metric registry and deterministic per-period aggregates (B-05; ADR-005 §3.3, §4.2 V9).

Binding interface: docs/architecture/parallel-work-and-metrics-contract.md §4. The dashboard
(B-07) and the analytics tools (B-13) both read personal data only through this module.

Rules kept by every function:
- **Fixed SQL only.** Every query is a module-level constant string, chosen by metric id or
  domain from a closed dict; values are bound with `?`. Optional filters are fixed
  `(? IS NULL OR column = ?)` clauses, so no SQL text depends on input. Every query has
  `user_id = ?` and excludes soft-deleted rows (`deleted_at IS NULL`, or the
  `daily_calories` view, which already excludes them).
- **Documented day.** A date with at least one live row in the metric's domain. For
  `calories.daily_total` it is a date with a non-null total in `daily_calories`. A missing day
  is `None`, never 0; an explicit recorded 0 (e.g. 0 steps) is a value.
- **Event domains.** "Documented" means "something was recorded in this domain that day".
  A day without a movie row is undocumented, not "no movie"; with a filter (e.g.
  `meal_type=lunch`), a documented meal day without a lunch counts 0 lunches.
- No NaN and no division by zero: empty input gives `None` statistics and `n_observations == 0`.
- Pure computation: no logging, no network, no writes. Works on a normal or a
  `read_only=True` connection from `app.db.connect`.

Trend, correlation, condition splits, period differences and policy flags belong to B-13 and
are built from `daily_points` and `summarize`.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Literal, get_args

from app.records.dates import today_local
from app.records.job_applications import STATUSES as JOB_STATUSES
from app.records.job_applications import WORK_MODELS
from app.records.meals import MEAL_TYPES
from app.records.tasks import CATEGORIES as TASK_CATEGORIES
from app.records.tasks import STATUSES as TASK_STATUSES
from app.records.workouts import WORKOUT_TYPES

Domain = Literal["weight", "sleep", "steps", "meals", "workouts", "articles", "podcasts",
                 "movies", "series", "job_applications", "tasks"]
MetricKind = Literal["daily_value", "event_count", "event_value", "ratio"]
GroupBy = Literal["none", "subtype", "week", "month"]

DOMAINS: tuple[str, ...] = get_args(Domain)
GROUP_BY: tuple[str, ...] = get_args(GroupBy)
MAX_SPAN_DAYS = 366
MAX_GROUPS = 20
MAX_UNDOCUMENTED_RANGES = 10


class MetricError(ValueError):
    """Invalid metric request. `code` is stable (tests, tool errors); `message` is fixed English
    text that never echoes the input.

    Codes: unknown_metric, unknown_domain, unknown_filter, invalid_filter_value, invalid_range,
    range_in_future, range_too_long, invalid_group_by, group_by_unsupported.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- public data types (contract §4)
@dataclass(frozen=True)
class MetricDef:
    id: str
    kind: MetricKind
    unit: str
    domain: Domain
    label: str
    min_delta: float
    subtype_field: str | None
    allowed_filters: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusive; start <= end


@dataclass(frozen=True)
class ResolvedPeriod:
    period: Period  # after clipping
    requested_start: date
    clipped: bool  # True when start was moved to the earliest record


@dataclass(frozen=True)
class Coverage:
    earliest_record: date | None  # whole history of the domain, not only the period
    latest_record: date | None
    documented_days: int
    total_days: int
    ratio: float
    undocumented_ranges: tuple[tuple[date, date], ...]  # <= 10, oldest first
    truncated: bool


@dataclass(frozen=True)
class DailyPoint:
    day: date
    value: float | None
    documented: bool
    record_ids: tuple[int, ...]


@dataclass(frozen=True)
class RecordRef:
    day: date
    value: float
    record_id: int


@dataclass(frozen=True)
class Group:
    key: str  # subtype value, ISO week "2026-W41", or month "2026-10"
    n: int
    documented_days: int
    mean: float | None
    count: int | None
    rate_per_documented_day: float | None
    pct: float | None


@dataclass(frozen=True)
class MetricSummary:
    metric: str
    period: ResolvedPeriod
    coverage: Coverage
    n_observations: int
    mean: float | None
    median: float | None
    min: float | None
    max: float | None
    sum: float | None
    min_record: RecordRef | None
    max_record: RecordRef | None
    first: RecordRef | None
    last: RecordRef | None
    change: float | None
    rate_per_documented_day: float | None
    ratio_numerator: int | None
    ratio_denominator: int | None
    ratio_pct: float | None
    groups: tuple[Group, ...]
    groups_truncated: bool
    latest_overall: RecordRef | None
    calorie_complete_days: int | None
    calorie_partial_days: int | None


# ---------------------------------------------------------------- registry (ADR-005 §3.3, V9 defaults)
_ENUMS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "meal_type": MEAL_TYPES, "workout_type": WORKOUT_TYPES, "category": TASK_CATEGORIES,
    "task_status": TASK_STATUSES, "status": JOB_STATUSES, "work_model": WORK_MODELS,
})


def _filters(**fields: str) -> Mapping[str, frozenset[str]]:
    # filter name -> enum key in _ENUMS (tasks' "status" filter uses the task statuses)
    return MappingProxyType({name: frozenset(_ENUMS[enum]) for name, enum in fields.items()})


def _metric(metric_id: str, kind: MetricKind, unit: str, domain: Domain, label: str, min_delta: float,
            subtype_field: str | None = None,
            allowed_filters: Mapping[str, frozenset[str]] | None = None) -> tuple[str, MetricDef]:
    return metric_id, MetricDef(metric_id, kind, unit, domain, label, min_delta, subtype_field,
                                allowed_filters if allowed_filters is not None else MappingProxyType({}))


# min_delta (V9): sleep 0.25 h, steps 500, weight 0.5 kg, kcal 100, minutes 5, per-day rates 0.1
# (event counts), percent 5 pp (ratios).
REGISTRY: Mapping[str, MetricDef] = MappingProxyType(dict((
    _metric("sleep.hours", "daily_value", "h", "sleep", "Sleep", 0.25),
    _metric("steps.count", "daily_value", "steps", "steps", "Steps", 500),
    _metric("weight.kg", "daily_value", "kg", "weight", "Weight", 0.5),
    _metric("calories.daily_total", "daily_value", "kcal", "meals", "Calories (estimated)", 100),
    _metric("meals.count", "event_count", "meals", "meals", "Meals", 0.1, "meal_type",
            _filters(meal_type="meal_type")),
    _metric("workouts.count", "event_count", "workouts", "workouts", "Workouts", 0.1, "workout_type",
            _filters(workout_type="workout_type")),
    _metric("workouts.duration_minutes", "event_value", "min", "workouts", "Workout duration", 5, "workout_type",
            _filters(workout_type="workout_type")),
    _metric("articles.count", "event_count", "items", "articles", "Articles", 0.1),
    _metric("podcasts.count", "event_count", "items", "podcasts", "Podcast episodes", 0.1),
    _metric("podcasts.duration_minutes", "event_value", "min", "podcasts", "Podcast listening time", 5),
    _metric("movies.count", "event_count", "items", "movies", "Movies", 0.1),
    _metric("series.count", "event_count", "items", "series", "Series episodes", 0.1),
    _metric("tasks.count", "event_count", "tasks", "tasks", "Tasks", 0.1, "category",
            _filters(status="task_status", category="category")),
    _metric("tasks.completion_rate", "ratio", "%", "tasks", "Task completion rate", 5, "category",
            _filters(category="category")),
    _metric("job_applications.count", "event_count", "applications", "job_applications", "Job applications",
            0.1, "status", _filters(status="status", work_model="work_model")),
    _metric("job_applications.progress_rate", "ratio", "%", "job_applications", "Applications progressing",
            5, None, _filters(work_model="work_model")),
)))
"""The closed metric set. Read-only; deferred tables have no entries."""


# ---------------------------------------------------------------- fixed SQL
# Observation queries return (day, value, record_id, subtype) ordered by day, id.
# Parameters: user_id, start, end, then each name of _OBS_FILTERS[metric] bound twice.
# For calories.daily_total, `subtype` carries the view's coverage_status and record_id is the
# lowest live meal id of that day.
_OBS_SQL: Mapping[str, str] = MappingProxyType({
    "sleep.hours":
        "SELECT record_date AS day, hours_slept AS value, id AS record_id, NULL AS subtype FROM sleep_records"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "steps.count":
        "SELECT record_date AS day, steps_count AS value, id AS record_id, NULL AS subtype FROM step_records"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "weight.kg":
        "SELECT record_date AS day, weight_kg AS value, id AS record_id, NULL AS subtype FROM weight_records"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "calories.daily_total":
        "SELECT dc.record_date AS day, dc.total_calories AS value,"
        " (SELECT MIN(m.id) FROM meals AS m WHERE m.user_id = dc.user_id AND m.record_date = dc.record_date"
        " AND m.deleted_at IS NULL) AS record_id, dc.coverage_status AS subtype FROM daily_calories AS dc"
        " WHERE dc.user_id = ? AND dc.record_date BETWEEN ? AND ? AND dc.total_calories IS NOT NULL"
        " ORDER BY dc.record_date",
    "meals.count":
        "SELECT record_date AS day, 1.0 AS value, id AS record_id, meal_type AS subtype FROM meals"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?"
        " AND (? IS NULL OR meal_type = ?) ORDER BY record_date, id",
    "workouts.count":
        "SELECT record_date AS day, 1.0 AS value, id AS record_id, workout_type AS subtype FROM workouts"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?"
        " AND (? IS NULL OR workout_type = ?) ORDER BY record_date, id",
    "workouts.duration_minutes":
        "SELECT record_date AS day, duration_minutes AS value, id AS record_id, workout_type AS subtype"
        " FROM workouts WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?"
        " AND (? IS NULL OR workout_type = ?) ORDER BY record_date, id",
    "articles.count":
        "SELECT record_date AS day, 1.0 AS value, id AS record_id, NULL AS subtype FROM articles"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "podcasts.count":
        "SELECT record_date AS day, 1.0 AS value, id AS record_id, NULL AS subtype FROM podcasts"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "podcasts.duration_minutes":
        "SELECT record_date AS day, duration_minutes AS value, id AS record_id, NULL AS subtype FROM podcasts"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "movies.count":
        "SELECT record_date AS day, 1.0 AS value, id AS record_id, NULL AS subtype FROM movies"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "series.count":
        "SELECT record_date AS day, 1.0 AS value, id AS record_id, NULL AS subtype FROM series"
        " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY record_date, id",
    "tasks.count":
        "SELECT task_date AS day, 1.0 AS value, id AS record_id, category AS subtype FROM tasks"
        " WHERE user_id = ? AND deleted_at IS NULL AND task_date BETWEEN ? AND ?"
        " AND (? IS NULL OR status = ?) AND (? IS NULL OR category = ?) ORDER BY task_date, id",
    "tasks.completion_rate":
        "SELECT task_date AS day, CASE WHEN status = 'completed' THEN 1.0 ELSE 0.0 END AS value,"
        " id AS record_id, category AS subtype FROM tasks"
        " WHERE user_id = ? AND deleted_at IS NULL AND task_date BETWEEN ? AND ?"
        " AND (? IS NULL OR category = ?) ORDER BY task_date, id",
    "job_applications.count":
        "SELECT application_date AS day, 1.0 AS value, id AS record_id, status AS subtype FROM job_applications"
        " WHERE user_id = ? AND deleted_at IS NULL AND application_date BETWEEN ? AND ?"
        " AND (? IS NULL OR status = ?) AND (? IS NULL OR work_model = ?) ORDER BY application_date, id",
    "job_applications.progress_rate":
        "SELECT application_date AS day,"
        " CASE WHEN status IN ('interviewing', 'accepted') THEN 1.0 ELSE 0.0 END AS value,"
        " id AS record_id, NULL AS subtype FROM job_applications"
        " WHERE user_id = ? AND deleted_at IS NULL AND application_date BETWEEN ? AND ?"
        " AND (? IS NULL OR work_model = ?) ORDER BY application_date, id",
})
_OBS_FILTERS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "meals.count": ("meal_type",), "workouts.count": ("workout_type",),
    "workouts.duration_minutes": ("workout_type",), "tasks.count": ("status", "category"),
    "tasks.completion_rate": ("category",), "job_applications.count": ("status", "work_model"),
    "job_applications.progress_rate": ("work_model",),
})

# Most recent value in all history (dashboard tile): (day, value, record_id); daily and event values.
# Parameters: user_id, then each name of _OBS_FILTERS[metric] bound twice.
_LATEST_SQL: Mapping[str, str] = MappingProxyType({
    "sleep.hours":
        "SELECT record_date AS day, hours_slept AS value, id AS record_id FROM sleep_records"
        " WHERE user_id = ? AND deleted_at IS NULL ORDER BY record_date DESC, id DESC LIMIT 1",
    "steps.count":
        "SELECT record_date AS day, steps_count AS value, id AS record_id FROM step_records"
        " WHERE user_id = ? AND deleted_at IS NULL ORDER BY record_date DESC, id DESC LIMIT 1",
    "weight.kg":
        "SELECT record_date AS day, weight_kg AS value, id AS record_id FROM weight_records"
        " WHERE user_id = ? AND deleted_at IS NULL ORDER BY record_date DESC, id DESC LIMIT 1",
    "calories.daily_total":
        "SELECT dc.record_date AS day, dc.total_calories AS value,"
        " (SELECT MIN(m.id) FROM meals AS m WHERE m.user_id = dc.user_id AND m.record_date = dc.record_date"
        " AND m.deleted_at IS NULL) AS record_id FROM daily_calories AS dc"
        " WHERE dc.user_id = ? AND dc.total_calories IS NOT NULL ORDER BY dc.record_date DESC LIMIT 1",
    "workouts.duration_minutes":
        "SELECT record_date AS day, duration_minutes AS value, id AS record_id FROM workouts"
        " WHERE user_id = ? AND deleted_at IS NULL AND duration_minutes IS NOT NULL"
        " AND (? IS NULL OR workout_type = ?) ORDER BY record_date DESC, id DESC LIMIT 1",
    "podcasts.duration_minutes":
        "SELECT record_date AS day, duration_minutes AS value, id AS record_id FROM podcasts"
        " WHERE user_id = ? AND deleted_at IS NULL AND duration_minutes IS NOT NULL"
        " ORDER BY record_date DESC, id DESC LIMIT 1",
})

# Documented days of a domain in [start, end]: parameters user_id, start, end.
_DAYS_SQL: Mapping[str, str] = MappingProxyType({
    "weight": "SELECT DISTINCT record_date AS day FROM weight_records"
              " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "sleep": "SELECT DISTINCT record_date AS day FROM sleep_records"
             " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "steps": "SELECT DISTINCT record_date AS day FROM step_records"
             " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "meals": "SELECT DISTINCT record_date AS day FROM meals"
             " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "workouts": "SELECT DISTINCT record_date AS day FROM workouts"
                " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "articles": "SELECT DISTINCT record_date AS day FROM articles"
                " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "podcasts": "SELECT DISTINCT record_date AS day FROM podcasts"
                " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "movies": "SELECT DISTINCT record_date AS day FROM movies"
              " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "series": "SELECT DISTINCT record_date AS day FROM series"
              " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ?",
    "job_applications": "SELECT DISTINCT application_date AS day FROM job_applications"
                        " WHERE user_id = ? AND deleted_at IS NULL AND application_date BETWEEN ? AND ?",
    "tasks": "SELECT DISTINCT task_date AS day FROM tasks"
             " WHERE user_id = ? AND deleted_at IS NULL AND task_date BETWEEN ? AND ?",
})
# Earliest and latest live record of a domain (whole history): parameter user_id.
_BOUNDS_SQL: Mapping[str, str] = MappingProxyType({
    "weight": "SELECT MIN(record_date), MAX(record_date) FROM weight_records"
              " WHERE user_id = ? AND deleted_at IS NULL",
    "sleep": "SELECT MIN(record_date), MAX(record_date) FROM sleep_records WHERE user_id = ? AND deleted_at IS NULL",
    "steps": "SELECT MIN(record_date), MAX(record_date) FROM step_records WHERE user_id = ? AND deleted_at IS NULL",
    "meals": "SELECT MIN(record_date), MAX(record_date) FROM meals WHERE user_id = ? AND deleted_at IS NULL",
    "workouts": "SELECT MIN(record_date), MAX(record_date) FROM workouts WHERE user_id = ? AND deleted_at IS NULL",
    "articles": "SELECT MIN(record_date), MAX(record_date) FROM articles WHERE user_id = ? AND deleted_at IS NULL",
    "podcasts": "SELECT MIN(record_date), MAX(record_date) FROM podcasts WHERE user_id = ? AND deleted_at IS NULL",
    "movies": "SELECT MIN(record_date), MAX(record_date) FROM movies WHERE user_id = ? AND deleted_at IS NULL",
    "series": "SELECT MIN(record_date), MAX(record_date) FROM series WHERE user_id = ? AND deleted_at IS NULL",
    "job_applications": "SELECT MIN(application_date), MAX(application_date) FROM job_applications"
                        " WHERE user_id = ? AND deleted_at IS NULL",
    "tasks": "SELECT MIN(task_date), MAX(task_date) FROM tasks WHERE user_id = ? AND deleted_at IS NULL",
})
# calories.daily_total documents a day only when the view has a non-null total.
_CALORIE_DAYS_SQL = ("SELECT record_date AS day FROM daily_calories"
                     " WHERE user_id = ? AND total_calories IS NOT NULL AND record_date BETWEEN ? AND ?")
_CALORIE_BOUNDS_SQL = ("SELECT MIN(record_date), MAX(record_date) FROM daily_calories"
                       " WHERE user_id = ? AND total_calories IS NOT NULL")
_CALORIE_MEAL_IDS_SQL = ("SELECT record_date AS day, id FROM meals"
                         " WHERE user_id = ? AND deleted_at IS NULL AND record_date BETWEEN ? AND ? ORDER BY id")

_CALORIES = "calories.daily_total"
_COMPLETION = "tasks.completion_rate"
_NO_SUM = frozenset({"weight.kg"})  # a sum of body weights means nothing


# ---------------------------------------------------------------- validation
def _get_metric(metric_id: object) -> MetricDef:
    metric = REGISTRY.get(metric_id) if isinstance(metric_id, str) else None
    if metric is None:
        raise MetricError("unknown_metric", "Unknown metric.")
    return metric


def _check_filters(metric: MetricDef, filters: Mapping[str, str] | None) -> dict[str, str]:
    checked: dict[str, str] = {}
    for name, value in (filters or {}).items():
        allowed = metric.allowed_filters.get(name) if isinstance(name, str) else None
        if allowed is None:
            raise MetricError("unknown_filter", "This filter is not available for the metric.")
        if not isinstance(value, str) or value not in allowed:
            raise MetricError("invalid_filter_value", "This filter value is not allowed.")
        checked[name] = value
    return checked


def _check_date(value: object) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise MetricError("invalid_range", "Dates must be calendar dates.")
    return value


def _check_span(start: date, end: date) -> None:
    if start > end:
        raise MetricError("invalid_range", "The start date must not be after the end date.")
    if (end - start).days + 1 > MAX_SPAN_DAYS:
        raise MetricError("range_too_long", "A period may span at most 366 days.")


def _check_period(period: object) -> Period:
    if not isinstance(period, Period):
        raise MetricError("invalid_range", "Expected a period.")
    _check_span(_check_date(period.start), _check_date(period.end))
    return period


def _check_domain(domain: object) -> str:
    if not isinstance(domain, str) or domain not in _DAYS_SQL:
        raise MetricError("unknown_domain", "Unknown domain.")
    return domain


# ---------------------------------------------------------------- queries
@dataclass(frozen=True)
class _Obs:
    day: date
    value: float | None
    record_id: int
    subtype: str | None


def _query_end(metric: MetricDef, period: Period) -> date:
    # tasks.completion_rate never counts planned (future) tasks, even for a hand-built period.
    return min(period.end, today_local()) if metric.id == _COMPLETION else period.end


def _filter_params(metric: MetricDef, filters: Mapping[str, str]) -> list[object]:
    params: list[object] = []
    for name in _OBS_FILTERS.get(metric.id, ()):
        value = filters.get(name)
        params += [value, value]
    return params


def _observations(conn: sqlite3.Connection, user_id: int, metric: MetricDef, period: Period,
                  filters: Mapping[str, str]) -> list[_Obs]:
    params = [user_id, period.start.isoformat(), _query_end(metric, period).isoformat(),
              *_filter_params(metric, filters)]
    return [_Obs(date.fromisoformat(r[0]), None if r[1] is None else float(r[1]), int(r[2]), r[3])
            for r in conn.execute(_OBS_SQL[metric.id], params)]


def _domain_days(conn: sqlite3.Connection, user_id: int, domain: str, start: date, end: date) -> set[date]:
    rows = conn.execute(_DAYS_SQL[domain], (user_id, start.isoformat(), end.isoformat()))
    return {date.fromisoformat(r[0]) for r in rows}


def _documented_days(conn: sqlite3.Connection, user_id: int, metric: MetricDef, period: Period) -> set[date]:
    end = _query_end(metric, period)
    if metric.id == _CALORIES:
        rows = conn.execute(_CALORIE_DAYS_SQL, (user_id, period.start.isoformat(), end.isoformat()))
        return {date.fromisoformat(r[0]) for r in rows}
    return _domain_days(conn, user_id, metric.domain, period.start, end)


def _bounds(conn: sqlite3.Connection, user_id: int, metric: MetricDef | None,
            domain: str) -> tuple[date | None, date | None]:
    sql = _CALORIE_BOUNDS_SQL if metric is not None and metric.id == _CALORIES else _BOUNDS_SQL[domain]
    low, high = conn.execute(sql, (user_id,)).fetchone()
    return (date.fromisoformat(low) if low else None, date.fromisoformat(high) if high else None)


def _days(period: Period) -> list[date]:
    return [period.start + timedelta(days=i) for i in range((period.end - period.start).days + 1)]


def _make_coverage(days: set[date], bounds: tuple[date | None, date | None], period: Period) -> Coverage:
    calendar = _days(period)
    ranges: list[tuple[date, date]] = []
    run_start: date | None = None
    for day in calendar:
        if day not in days and run_start is None:
            run_start = day
        elif day in days and run_start is not None:
            ranges.append((run_start, day - timedelta(days=1)))
            run_start = None
    if run_start is not None:
        ranges.append((run_start, period.end))
    documented = sum(1 for day in calendar if day in days)
    total = len(calendar)
    return Coverage(earliest_record=bounds[0], latest_record=bounds[1], documented_days=documented,
                    total_days=total, ratio=documented / total if total else 0.0,
                    undocumented_ranges=tuple(ranges[:MAX_UNDOCUMENTED_RANGES]),
                    truncated=len(ranges) > MAX_UNDOCUMENTED_RANGES)


# ---------------------------------------------------------------- public functions
def resolve_period(conn: sqlite3.Connection, user_id: int, metric_id: str, start: date, end: date, *,
                   today: date) -> ResolvedPeriod:
    """Validate a requested range for a metric and clip its start to the earliest record.

    Rules: `start <= end <= today` and at most 366 calendar days (measured on the requested
    range). A start before the user's earliest live record of the metric's domain (for
    calories, the earliest day with a known total) moves to that record, and `clipped` is
    True; it is not moved when there is no record or the earliest one lies after `end`.
    Raises MetricError (unknown_metric, invalid_range, range_in_future, range_too_long).
    """
    metric = _get_metric(metric_id)
    start, end, today = _check_date(start), _check_date(end), _check_date(today)
    if start > end:
        raise MetricError("invalid_range", "The start date must not be after the end date.")
    if end > today:
        raise MetricError("range_in_future", "The end date must not be after today.")
    _check_span(start, end)
    earliest, _ = _bounds(conn, user_id, metric, metric.domain)
    new_start = earliest if earliest is not None and start < earliest <= end else start
    return ResolvedPeriod(Period(new_start, end), requested_start=start, clipped=new_start != start)


def coverage(conn: sqlite3.Connection, user_id: int, domain: Domain, period: Period) -> Coverage:
    """Documented days of a domain in a period, with the domain's whole-history bounds.

    `undocumented_ranges` are the maximal runs of undocumented days, oldest first, at most 10
    (`truncated` when there are more). Raises MetricError (unknown_domain, invalid_range,
    range_too_long).
    """
    name = _check_domain(domain)
    period = _check_period(period)
    days = _domain_days(conn, user_id, name, period.start, period.end)
    return _make_coverage(days, _bounds(conn, user_id, None, name), period)


def daily_points(conn: sqlite3.Connection, user_id: int, metric_id: str, period: Period,
                 filters: Mapping[str, str] | None = None) -> list[DailyPoint]:
    """One point per calendar day of the period, oldest first.

    - daily_value: the day's value, or None (documented False) when nothing was recorded.
    - event_count: on a documented domain day, the number of matching events (0.0 possible
      with a filter); None on an undocumented day.
    - event_value: on a documented day, the sum of the matching events' values; 0.0 when no
      event matches; None when matching events exist but none has a value.
    - ratio: the day's percentage (0–100) of matching rows; None when no row matches.
    `record_ids` lists the contributing rows (calories: the day's live meals).
    Raises MetricError for an unknown metric, filter or value, or an invalid period.
    """
    metric = _get_metric(metric_id)
    checked = _check_filters(metric, filters)
    period = _check_period(period)
    observations = _observations(conn, user_id, metric, period, checked)
    by_day: dict[date, list[_Obs]] = {}
    for obs in observations:
        by_day.setdefault(obs.day, []).append(obs)
    if metric.kind == "daily_value":
        meal_ids = _calorie_meal_ids(conn, user_id, period) if metric.id == _CALORIES else {}
        points = []
        for day in _days(period):
            rows = by_day.get(day)
            if not rows or rows[0].value is None:
                points.append(DailyPoint(day, None, False, ()))
            else:
                ids = meal_ids.get(day, ()) if metric.id == _CALORIES else tuple(r.record_id for r in rows)
                points.append(DailyPoint(day, rows[0].value, True, ids))
        return points
    documented = _documented_days(conn, user_id, metric, period)
    return [_event_point(metric, day, by_day.get(day, []), day in documented) for day in _days(period)]


def _calorie_meal_ids(conn: sqlite3.Connection, user_id: int, period: Period) -> dict[date, tuple[int, ...]]:
    ids: dict[date, list[int]] = {}
    for row in conn.execute(_CALORIE_MEAL_IDS_SQL, (user_id, period.start.isoformat(), period.end.isoformat())):
        ids.setdefault(date.fromisoformat(row[0]), []).append(int(row[1]))
    return {day: tuple(values) for day, values in ids.items()}


def _event_point(metric: MetricDef, day: date, rows: list[_Obs], documented: bool) -> DailyPoint:
    if not documented:
        return DailyPoint(day, None, False, ())
    ids = tuple(r.record_id for r in rows)
    values = [r.value for r in rows if r.value is not None]
    value: float | None
    if metric.kind == "event_count":
        value = float(len(rows))
    elif metric.kind == "event_value":
        value = math.fsum(values) if values else (None if rows else 0.0)
    else:
        value = 100.0 * math.fsum(values) / len(rows) if rows else None
    return DailyPoint(day, value, True, ids)


@dataclass(frozen=True)
class _Stats:
    n: int
    mean: float | None = None
    median: float | None = None
    min: float | None = None
    max: float | None = None
    sum: float | None = None
    min_record: RecordRef | None = None
    max_record: RecordRef | None = None
    first: RecordRef | None = None
    last: RecordRef | None = None
    rate: float | None = None
    numerator: int | None = None
    denominator: int | None = None
    pct: float | None = None


def _ref(obs: _Obs) -> RecordRef:
    assert obs.value is not None
    return RecordRef(obs.day, obs.value, obs.record_id)


def _stats(metric: MetricDef, rows: list[_Obs], documented_days: int) -> _Stats:
    """Kind-appropriate statistics of observations (ordered by day, id)."""
    if metric.kind == "event_count":
        n = len(rows)
        if documented_days == 0:
            return _Stats(n)
        return _Stats(n, sum=float(n), rate=n / documented_days)
    if metric.kind == "ratio":
        numerator = sum(1 for r in rows if r.value == 1.0)
        return _Stats(len(rows), numerator=numerator, denominator=len(rows),
                      pct=100.0 * numerator / len(rows) if rows else None)
    valued = [r for r in rows if r.value is not None]
    if not valued:
        return _Stats(0)
    values = [r.value for r in valued if r.value is not None]
    total = math.fsum(values)
    rate = total / documented_days if metric.kind == "event_value" and documented_days else None
    return _Stats(
        len(valued), mean=statistics.fmean(values), median=float(statistics.median(values)),
        min=min(values), max=max(values), sum=None if metric.id in _NO_SUM else total,
        # min()/max() keep the first of equal values, i.e. the earliest record
        min_record=_ref(min(valued, key=lambda r: r.value or 0.0)),
        max_record=_ref(max(valued, key=lambda r: r.value or 0.0)),
        first=_ref(valued[0]), last=_ref(valued[-1]), rate=rate)


def _week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return str(year) + "-W" + str(week).zfill(2)


def _month_key(day: date) -> str:
    return day.isoformat()[:7]


def _groups(metric: MetricDef, group_by: str, rows: list[_Obs], documented: set[date], period: Period,
            filters: Mapping[str, str]) -> tuple[tuple[Group, ...], bool]:
    if group_by == "none":
        return (), False
    buckets: dict[str, tuple[list[_Obs], int]] = {}
    if group_by == "subtype":
        field = metric.subtype_field
        assert field is not None  # checked by summarize
        keys = (filters[field],) if field in filters else _ENUMS[field]
        in_period = sum(1 for d in documented if period.start <= d <= period.end)
        for key in keys:
            buckets[key] = ([r for r in rows if r.subtype == key], in_period)
    else:
        key_of = _week_key if group_by == "week" else _month_key
        members: dict[str, tuple[list[_Obs], list[date]]] = {}
        for day in _days(period):
            members.setdefault(key_of(day), ([], []))[1].append(day)
        for row in rows:
            members[key_of(row.day)][0].append(row)
        for key, (group_rows, group_days) in members.items():
            buckets[key] = (group_rows, sum(1 for d in group_days if d in documented))
    groups = [_group(metric, key, group_rows, days) for key, (group_rows, days) in buckets.items()]
    return tuple(groups[:MAX_GROUPS]), len(groups) > MAX_GROUPS


def _group(metric: MetricDef, key: str, rows: list[_Obs], documented_days: int) -> Group:
    stats = _stats(metric, rows, documented_days)
    if metric.kind == "event_count":
        return Group(key, stats.n, documented_days, None, stats.n if documented_days else None, stats.rate, None)
    if metric.kind == "ratio":
        return Group(key, stats.n, documented_days, None, None, None, stats.pct)
    return Group(key, stats.n, documented_days, stats.mean, None, stats.rate, None)


def summarize(conn: sqlite3.Connection, user_id: int, metric_id: str, period: Period, *,
              group_by: GroupBy = "none", filters: Mapping[str, str] | None = None) -> MetricSummary:
    """Per-period facts of one metric for one user (see the module docstring for semantics).

    Statistics by kind:
    - daily_value: n = documented days; mean, median, min, max over their values; sum (not
      for weight); min/max/first/last records; `change` = last − first for weight.kg, n ≥ 2.
    - event_count: n = matching events; sum = n and `rate_per_documented_day` = n / documented
      domain days (both None without documented days); no mean/median/min/max.
    - event_value: n = matching events with a value; mean, median, min, max, sum, records;
      rate = sum / documented domain days.
    - ratio: numerator / denominator rows and pct (0–100, None when the denominator is 0);
      n = denominator; no other statistics.
    Groups (≤ 20, oldest/enum order first; `groups_truncated` when cut): `subtype` — one group
    per enum value of the subtype field (only the filtered value when it is filtered), each
    with the period's documented days as denominator; `week` (ISO "YYYY-Www") and `month`
    ("YYYY-MM") — every calendar bucket overlapping the period, with that bucket's documented
    days. `count` and rates are None in a group without documented days.
    `period` is reported as given (`clipped` False); use `resolve_period` first.
    Raises MetricError for an unknown metric, filter, value or group_by, or an invalid period.
    """
    metric = _get_metric(metric_id)
    checked = _check_filters(metric, filters)
    period = _check_period(period)
    if not isinstance(group_by, str) or group_by not in GROUP_BY:
        raise MetricError("invalid_group_by", "Unknown grouping.")
    if group_by == "subtype" and metric.subtype_field is None:
        raise MetricError("group_by_unsupported", "This metric has no subtype to group by.")
    rows = _observations(conn, user_id, metric, period, checked)
    documented = _documented_days(conn, user_id, metric, period)
    cover = _make_coverage(documented, _bounds(conn, user_id, metric, metric.domain), period)
    stats = _stats(metric, rows, cover.documented_days)
    groups, truncated = _groups(metric, group_by, rows, documented, period, checked)
    is_calories = metric.id == _CALORIES
    change = None
    if metric.id == "weight.kg" and stats.n >= 2 and stats.first and stats.last:
        change = stats.last.value - stats.first.value
    return MetricSummary(
        metric=metric.id, period=ResolvedPeriod(period, period.start, False), coverage=cover,
        n_observations=stats.n, mean=stats.mean, median=stats.median, min=stats.min, max=stats.max,
        sum=stats.sum, min_record=stats.min_record, max_record=stats.max_record, first=stats.first,
        last=stats.last, change=change,
        rate_per_documented_day=stats.rate if metric.kind in ("event_count", "event_value") else None,
        ratio_numerator=stats.numerator, ratio_denominator=stats.denominator, ratio_pct=stats.pct,
        groups=groups, groups_truncated=truncated, latest_overall=_latest(conn, user_id, metric, checked),
        calorie_complete_days=sum(1 for r in rows if r.subtype == "estimated_complete") if is_calories else None,
        calorie_partial_days=sum(1 for r in rows if r.subtype == "estimated_partial") if is_calories else None,
    )


def _latest(conn: sqlite3.Connection, user_id: int, metric: MetricDef, filters: Mapping[str, str]) -> RecordRef | None:
    sql = _LATEST_SQL.get(metric.id)
    if sql is None:
        return None
    row = conn.execute(sql, (user_id, *_filter_params(metric, filters))).fetchone()
    if row is None or row[1] is None or row[2] is None:
        return None
    return RecordRef(date.fromisoformat(row[0]), float(row[1]), int(row[2]))
