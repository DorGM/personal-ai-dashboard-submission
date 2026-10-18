"""Structural types for the B-05 metrics contract that the analytics tools consume.

The metric registry and per-period facts belong to `app.analytics.metrics` (B-05, main
workstream; contract in docs/architecture/parallel-work-and-metrics-contract.md §4). The
analytics tools (B-13) depend only on the read-only shapes below, so they can be built and
tested against an injected fake before B-05 is merged, and against the real module after.
Nothing here defines a metric: ids, kinds, units and thresholds come from the registry.
"""

from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol, cast

Domain = Literal["weight", "sleep", "steps", "meals", "workouts", "articles", "podcasts",
                 "movies", "series", "job_applications", "tasks"]
MetricKind = Literal["daily_value", "event_count", "event_value", "ratio"]
GroupBy = Literal["none", "subtype", "week", "month"]

DOMAINS: tuple[Domain, ...] = ("weight", "sleep", "steps", "meals", "workouts", "articles", "podcasts",
                               "movies", "series", "job_applications", "tasks")

# The closed metric-id set of ADR-005 §3.3 (ids only; definitions live in the B-05 registry).
# A test asserts this equals the registry's keys once B-05 is merged.
METRIC_IDS: tuple[str, ...] = (
    "sleep.hours", "steps.count", "weight.kg", "calories.daily_total", "meals.count", "workouts.count",
    "workouts.duration_minutes", "articles.count", "podcasts.count", "movies.count", "series.count",
    "podcasts.duration_minutes", "tasks.count", "tasks.completion_rate", "job_applications.count",
    "job_applications.progress_rate",
)


class MetricDefLike(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def kind(self) -> MetricKind: ...
    @property
    def unit(self) -> str: ...
    @property
    def domain(self) -> Domain: ...
    @property
    def label(self) -> str: ...
    @property
    def min_delta(self) -> float: ...
    @property
    def subtype_field(self) -> str | None: ...
    @property
    def allowed_filters(self) -> Mapping[str, frozenset[str]]: ...


class PeriodLike(Protocol):
    @property
    def start(self) -> date: ...
    @property
    def end(self) -> date: ...


class ResolvedPeriodLike(Protocol):
    @property
    def period(self) -> PeriodLike: ...
    @property
    def requested_start(self) -> date: ...
    @property
    def clipped(self) -> bool: ...


class CoverageLike(Protocol):
    @property
    def earliest_record(self) -> date | None: ...
    @property
    def latest_record(self) -> date | None: ...
    @property
    def documented_days(self) -> int: ...
    @property
    def total_days(self) -> int: ...
    @property
    def ratio(self) -> float: ...
    @property
    def undocumented_ranges(self) -> Sequence[tuple[date, date]]: ...
    @property
    def truncated(self) -> bool: ...


class DailyPointLike(Protocol):
    @property
    def day(self) -> date: ...
    @property
    def value(self) -> float | None: ...
    @property
    def documented(self) -> bool: ...
    @property
    def record_ids(self) -> Sequence[int]: ...


class RecordRefLike(Protocol):
    @property
    def day(self) -> date: ...
    @property
    def value(self) -> float: ...
    @property
    def record_id(self) -> int: ...


class GroupLike(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def n(self) -> int: ...
    @property
    def documented_days(self) -> int: ...
    @property
    def mean(self) -> float | None: ...
    @property
    def count(self) -> int | None: ...
    @property
    def rate_per_documented_day(self) -> float | None: ...
    @property
    def pct(self) -> float | None: ...


class MetricSummaryLike(Protocol):
    @property
    def metric(self) -> str: ...
    @property
    def period(self) -> ResolvedPeriodLike: ...
    @property
    def coverage(self) -> CoverageLike: ...
    @property
    def n_observations(self) -> int: ...
    @property
    def mean(self) -> float | None: ...
    @property
    def median(self) -> float | None: ...
    @property
    def min(self) -> float | None: ...
    @property
    def max(self) -> float | None: ...
    @property
    def sum(self) -> float | None: ...
    @property
    def min_record(self) -> RecordRefLike | None: ...
    @property
    def max_record(self) -> RecordRefLike | None: ...
    @property
    def first(self) -> RecordRefLike | None: ...
    @property
    def last(self) -> RecordRefLike | None: ...
    @property
    def change(self) -> float | None: ...
    @property
    def rate_per_documented_day(self) -> float | None: ...
    @property
    def ratio_numerator(self) -> int | None: ...
    @property
    def ratio_denominator(self) -> int | None: ...
    @property
    def ratio_pct(self) -> float | None: ...
    @property
    def groups(self) -> Sequence[GroupLike]: ...
    @property
    def groups_truncated(self) -> bool: ...
    @property
    def calorie_complete_days(self) -> int | None: ...
    @property
    def calorie_partial_days(self) -> int | None: ...


class MetricsBackend(Protocol):
    """The functions and registry of `app.analytics.metrics` (B-05 contract §4).

    Every function takes the server-side `user_id`; none of them may be reachable with a
    model-supplied id. `MetricError` instances carry `.code` and `.message`.
    """

    @property
    def REGISTRY(self) -> Mapping[str, MetricDefLike]: ...  # noqa: N802 (contract name)
    @property
    def MetricError(self) -> type[Exception]: ...  # noqa: N802
    def make_period(self, start: date, end: date) -> PeriodLike: ...
    def resolve_period(self, conn: sqlite3.Connection, user_id: int, metric_id: str, start: date, end: date, *,
                       today: date) -> ResolvedPeriodLike: ...
    def coverage(self, conn: sqlite3.Connection, user_id: int, domain: Domain, period: PeriodLike) -> CoverageLike: ...
    def daily_points(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike,
                     filters: Mapping[str, str] | None = None) -> Sequence[DailyPointLike]: ...
    def summarize(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike, *,
                  group_by: GroupBy = "none", filters: Mapping[str, str] | None = None) -> MetricSummaryLike: ...


@dataclass(frozen=True)
class _ModuleBackend:
    """Adapts the B-05 module (functions plus the `Period` dataclass) to `MetricsBackend`."""

    module: Any

    @property
    def REGISTRY(self) -> Mapping[str, MetricDefLike]:  # noqa: N802
        return cast(Mapping[str, MetricDefLike], self.module.REGISTRY)

    @property
    def MetricError(self) -> type[Exception]:  # noqa: N802
        return cast(type[Exception], self.module.MetricError)

    def make_period(self, start: date, end: date) -> PeriodLike:
        return cast(PeriodLike, self.module.Period(start=start, end=end))

    def resolve_period(self, conn: sqlite3.Connection, user_id: int, metric_id: str, start: date, end: date, *,
                       today: date) -> ResolvedPeriodLike:
        return cast(ResolvedPeriodLike, self.module.resolve_period(conn, user_id, metric_id, start, end, today=today))

    def coverage(self, conn: sqlite3.Connection, user_id: int, domain: Domain, period: PeriodLike) -> CoverageLike:
        return cast(CoverageLike, self.module.coverage(conn, user_id, domain, period))

    def daily_points(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike,
                     filters: Mapping[str, str] | None = None) -> Sequence[DailyPointLike]:
        return cast(Sequence[DailyPointLike], self.module.daily_points(conn, user_id, metric_id, period, filters))

    def summarize(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike, *,
                  group_by: GroupBy = "none", filters: Mapping[str, str] | None = None) -> MetricSummaryLike:
        return cast(MetricSummaryLike, self.module.summarize(conn, user_id, metric_id, period,
                                                             group_by=group_by, filters=filters))


class MetricsUnavailable(RuntimeError):
    """The B-05 metric registry is not installed in this build."""


def default_backend() -> MetricsBackend:
    """The real B-05 registry, imported lazily so this package works before B-05 is merged."""
    try:
        module = importlib.import_module("app.analytics.metrics")
    except ModuleNotFoundError as exc:
        if exc.name != "app.analytics.metrics":
            raise  # a real import error inside B-05 must not be hidden
        raise MetricsUnavailable("app.analytics.metrics (B-05) is not available") from exc
    return _ModuleBackend(module)
