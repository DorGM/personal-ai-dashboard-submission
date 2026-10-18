"""The six read-only analytics tools (ADR-005 §3.3) and their dispatcher.

Arguments arrive as the JSON string the model produced and are parsed by strict Pydantic models
(`ARG_MODELS`): every field is required (nullable where optional), unknown fields are refused,
and there is no `user_id` field anywhere — the user always comes from `ToolContext`, which the
server builds from the session. Result shapes are documented in docs/analytics/worker-notes.md §2.

- T1–T5 read only through the B-05 metrics contract (`ctx.metrics`, see `ports.MetricsBackend`)
  and add the analytics-only computations: OLS trend, Pearson r, the event condition split,
  period differences, `comparable` and the policy assessment flags.
- T6 `list_records` runs analytics-owned fixed SQL: one module-level constant per domain, chosen
  by the `Domain` enum, values bound with `?`, scoped by `user_id = ?` and `deleted_at IS NULL`.
  Deferred career tables are not part of any query.
- `execute_tool` parses, runs inside a time budget and a read snapshot, converts failures to
  `{"error": {"code", "message"}}` (no input values echoed), and fits the result to
  `policy.MAX_TOOL_RESULT_BYTES`.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.analytics import policy, readonly
from app.analytics.ports import (
    DOMAINS,
    METRIC_IDS,
    CoverageLike,
    DailyPointLike,
    Domain,
    GroupBy,
    MetricDefLike,
    MetricsBackend,
    MetricSummaryLike,
    RecordRefLike,
    ResolvedPeriodLike,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    MetricId = str
else:
    MetricId = Literal[METRIC_IDS]  # the closed ADR-005 metric set, built from ports.METRIC_IDS

SortKey = Literal["date_desc", "date_asc", "value_desc", "value_asc"]
LagDays = Literal[0, 1]
MealType = Literal["breakfast", "lunch", "dinner", "night", "snacks"]
WorkoutType = Literal["aerobic", "weights", "bodyweight", "stick_spinning"]
TaskCategory = Literal["family", "career", "friends", "housework", "other"]
RecordStatus = Literal["pending", "completed", "applied", "interviewing", "accepted", "rejected"]
WorkModel = Literal["onsite", "hybrid", "remote", "unknown"]

TASKS_DAYS_AHEAD = 14  # tasks may be listed up to today + 14 (ADR-005 §3.3)
UNTRUSTED_TEXT_MAX = 200
ERROR_CODES: Final = frozenset({"invalid_arguments", "invalid_range", "unknown_metric", "invalid_filter",
                                "not_supported", "internal", "limit_exceeded"})


# ---------------------------------------------------------------- argument models
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Filters(_Args):
    """Enum-typed record filters; each tool accepts only the ones its metric/domain supports."""

    meal_type: MealType | None
    workout_type: WorkoutType | None
    category: TaskCategory | None
    status: RecordStatus | None
    work_model: WorkModel | None


class PeriodArgs(_Args):
    start: date
    end: date


class CoverageArgs(_Args):
    """T1 get_data_coverage."""

    domains: Annotated[list[Domain], Field(min_length=1, max_length=len(DOMAINS))]
    start: date
    end: date

    @field_validator("domains")
    @classmethod
    def _unique(cls, value: list[Domain]) -> list[Domain]:
        if len(set(value)) != len(value):
            raise ValueError("domains must be unique")
        return value


class SummarizeArgs(_Args):
    """T2 summarize_metric."""

    metric: MetricId
    start: date
    end: date
    group_by: GroupBy
    filters: Filters | None


class CompareArgs(_Args):
    """T3 compare_periods. period_a must end before period_b starts; difference = b − a."""

    metric: MetricId
    period_a: PeriodArgs
    period_b: PeriodArgs
    filters: Filters | None


class SeriesArgs(_Args):
    """T4 get_daily_series (daily_value metrics, ≤ 92 days)."""

    metric: MetricId
    start: date
    end: date


class RelateArgs(_Args):
    """T5 relate_metrics (y date = x date + lag_days)."""

    x_metric: MetricId
    y_metric: MetricId
    start: date
    end: date
    lag_days: LagDays


class ListArgs(_Args):
    """T6 list_records."""

    domain: Domain
    start: date
    end: date
    filters: Filters | None
    sort: SortKey
    limit: Annotated[int, Field(ge=1, le=policy.MAX_LIST_LIMIT)]


# ---------------------------------------------------------------- context, errors, outcome
@dataclass(frozen=True)
class ToolContext:
    """Server-built context of one question. `conn` must be hardened (`readonly.open_read_only`)."""

    user_id: int
    conn: sqlite3.Connection
    today: date
    metrics: MetricsBackend


class ToolError(Exception):
    """An expected, model-facing failure (bad range, unsupported combination…)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolOutcome:
    """Result of one call. `args` is the normalised arguments, None when they did not parse."""

    name: str
    args: dict[str, Any] | None
    result: dict[str, Any]
    ok: bool


_FIXED_MESSAGES: Final[Mapping[str, str]] = MappingProxyType({
    "invalid_arguments": "The arguments were rejected.",
    "invalid_range": "The date range is not allowed.",
    "unknown_metric": "The metric is not available.",
    "invalid_filter": "A filter is not allowed for this metric or domain.",
    "not_supported": "This combination is not supported.",
    "internal": "The tool failed. Try a different request.",
    "limit_exceeded": "The request exceeded a time or size limit. Narrow the request.",
})


def _error(code: str, message: str | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message or _FIXED_MESSAGES[code]}}


# ---------------------------------------------------------------- shared helpers
def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _check_range(start: date, end: date, today: date, *, max_days: int = policy.MAX_SPAN_DAYS,
                 days_ahead: int = 0) -> None:
    """start ≤ end ≤ today (+ days_ahead) and at most `max_days` calendar days (ADR-005 §3.3)."""
    if start > end:
        raise ToolError("invalid_range", "start must not be after end.")
    if end > today + timedelta(days=days_ahead):
        raise ToolError("invalid_range", "end must not be after today." if not days_ahead
                        else "end must not be more than 14 days after today.")
    if (end - start).days + 1 > max_days:
        raise ToolError("invalid_range", f"The range may cover at most {max_days} days.")


def _metric_def(ctx: ToolContext, metric_id: str) -> MetricDefLike:
    definition = ctx.metrics.REGISTRY.get(metric_id)
    if definition is None:
        raise ToolError("unknown_metric", "The metric is not available.")
    return definition


# Filter argument field -> registry filter name (identical names, kept explicit on purpose).
_FILTER_NAMES: Final[Mapping[str, str]] = MappingProxyType({
    "meal_type": "meal_type", "workout_type": "workout_type", "category": "category",
    "status": "status", "work_model": "work_model",
})


def _set_filters(filters: Filters | None) -> dict[str, str]:
    if filters is None:
        return {}
    return {k: v for k, v in filters.model_dump().items() if v is not None}


def _metric_filters(definition: MetricDefLike, filters: Filters | None) -> dict[str, str]:
    """Validate the non-null filters against the registry's `allowed_filters` of the metric."""
    out: dict[str, str] = {}
    for field, value in _set_filters(filters).items():
        name = _FILTER_NAMES[field]
        allowed = definition.allowed_filters.get(name)
        if allowed is None or value not in allowed:
            raise ToolError("invalid_filter", f"Filter '{field}' is not allowed for this metric.")
        out[name] = value
    return out


def _coverage_fraction(cov: CoverageLike) -> Fraction:
    # Exact documented/total so policy boundaries (0.4, 0.3 gap) are not blurred by float error.
    return Fraction(cov.documented_days, cov.total_days) if cov.total_days else Fraction(0)


def _policy_fraction(value: float) -> Fraction:
    return Fraction(str(value))


def _period_dict(resolved: ResolvedPeriodLike) -> dict[str, Any]:
    return {"start": _iso(resolved.period.start), "end": _iso(resolved.period.end),
            "requested_start": _iso(resolved.requested_start), "clipped": bool(resolved.clipped)}


def _coverage_dict(cov: CoverageLike) -> dict[str, Any]:
    return {
        "earliest_record": _iso(cov.earliest_record), "latest_record": _iso(cov.latest_record),
        "documented_days": cov.documented_days, "total_days": cov.total_days, "ratio": cov.ratio,
        "undocumented_ranges": [[_iso(a), _iso(b)] for a, b in cov.undocumented_ranges],
        "truncated": bool(cov.truncated),
    }


def _ref(ref: RecordRefLike | None) -> dict[str, Any] | None:
    return None if ref is None else {"date": _iso(ref.day), "value": ref.value, "record_id": ref.record_id}


def _sub(a: float | None, b: float | None) -> float | None:
    """a − b, or None if either is missing."""
    return None if a is None or b is None else a - b


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def ols_slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Ordinary least-squares slope of y on x; None when n < 2 or x has zero variance."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx


def pearson_r(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation; None when n < 2 or either variable has zero variance."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = math.fsum(xs) / n, math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    r = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sxx * syy)
    return max(-1.0, min(1.0, r))


def _summary_body(ctx: ToolContext, definition: MetricDefLike, start: date, end: date, group_by: GroupBy,
                  filters: Filters | None) -> tuple[dict[str, Any], MetricSummaryLike]:
    """The T2 result without `tool` and `assessment` (also used for T3's `a` and `b`)."""
    metric_filters = _metric_filters(definition, filters)
    _check_range(start, end, ctx.today)
    if group_by == "subtype" and definition.subtype_field is None:
        raise ToolError("not_supported", "This metric has no subtype to group by.")
    resolved = ctx.metrics.resolve_period(ctx.conn, ctx.user_id, definition.id, start, end, today=ctx.today)
    s = ctx.metrics.summarize(ctx.conn, ctx.user_id, definition.id, resolved.period, group_by=group_by,
                              filters=metric_filters or None)
    body: dict[str, Any] = {
        "metric": definition.id, "kind": definition.kind, "unit": definition.unit, "group_by": group_by,
        "filters": _set_filters(filters), "period": _period_dict(resolved), "coverage": _coverage_dict(s.coverage),
        "n_observations": s.n_observations,
        "mean": s.mean, "median": s.median, "min": s.min, "max": s.max, "sum": s.sum,
        "min_record": _ref(s.min_record), "max_record": _ref(s.max_record),
        "first": _ref(s.first), "last": _ref(s.last), "change": s.change,
        "rate_per_documented_day": s.rate_per_documented_day,
        "ratio": ({"numerator": s.ratio_numerator, "denominator": s.ratio_denominator, "pct": s.ratio_pct}
                  if definition.kind == "ratio" else None),
        "groups": [{"key": g.key, "n": g.n, "documented_days": g.documented_days, "mean": g.mean,
                    "count": g.count, "rate_per_documented_day": g.rate_per_documented_day, "pct": g.pct}
                   for g in s.groups],
        "groups_truncated": bool(s.groups_truncated),
        "calorie_complete_days": s.calorie_complete_days, "calorie_partial_days": s.calorie_partial_days,
    }
    return body, s


# ---------------------------------------------------------------- T1–T5
def get_data_coverage(ctx: ToolContext, args: CoverageArgs) -> dict[str, Any]:
    """T1: per-domain coverage; the period is clipped to the domain's earliest record when that
    record lies inside the requested period (a domain without records keeps the requested one)."""
    _check_range(args.start, args.end, ctx.today)
    domains: dict[str, Any] = {}
    for domain in args.domains:
        start = args.start
        cov = ctx.metrics.coverage(ctx.conn, ctx.user_id, domain, ctx.metrics.make_period(start, args.end))
        earliest = cov.earliest_record
        if earliest is not None and args.start < earliest <= args.end:
            start = earliest
            cov = ctx.metrics.coverage(ctx.conn, ctx.user_id, domain, ctx.metrics.make_period(start, args.end))
        domains[domain] = {
            "period": {"start": _iso(start), "end": _iso(args.end), "requested_start": _iso(args.start),
                       "clipped": start != args.start},
            "coverage": _coverage_dict(cov),
        }
    return {"tool": "get_data_coverage", "domains": domains}


def summarize_metric(ctx: ToolContext, args: SummarizeArgs) -> dict[str, Any]:
    """T2: one metric over one period, optionally grouped, with policy assessment flags."""
    definition = _metric_def(ctx, args.metric)
    body, s = _summary_body(ctx, definition, args.start, args.end, args.group_by, args.filters)
    return {"tool": "summarize_metric", **body, "assessment": {
        "enough_for_value": s.n_observations >= policy.MIN_N_VALUE,
        "low_coverage": _coverage_fraction(s.coverage) < _policy_fraction(policy.LOW_COVERAGE),
    }}


_PRIMARY_DELTA: Final[Mapping[str, str]] = MappingProxyType({
    "daily_value": "mean_abs", "event_value": "mean_abs", "event_count": "rate_abs", "ratio": "pct_points"})


def compare_periods(ctx: ToolContext, args: CompareArgs) -> dict[str, Any]:
    """T3: two periods of one metric, a strictly before b (no overlap); difference = b − a."""
    definition = _metric_def(ctx, args.metric)
    a_args, b_args = args.period_a, args.period_b
    _check_range(a_args.start, a_args.end, ctx.today)
    _check_range(b_args.start, b_args.end, ctx.today)
    if not a_args.end < b_args.start:  # a strictly earlier, so the sign of b − a reads as "later vs earlier"
        raise ToolError("invalid_range", "period_a must end before period_b starts.")
    a, sa = _summary_body(ctx, definition, a_args.start, a_args.end, "none", args.filters)
    b, sb = _summary_body(ctx, definition, b_args.start, b_args.end, "none", args.filters)

    mean_abs = _sub(sb.mean, sa.mean)
    mean_rel_pct = None if mean_abs is None or not sa.mean else mean_abs / abs(sa.mean) * 100
    cov_a, cov_b = _coverage_fraction(sa.coverage), _coverage_fraction(sb.coverage)
    gap_too_large = abs(cov_a - cov_b) > _policy_fraction(policy.COUNT_COVERAGE_GAP)
    difference = {
        "mean_abs": mean_abs, "mean_rel_pct": mean_rel_pct,
        "count_abs": None if gap_too_large else sb.n_observations - sa.n_observations,
        "rate_abs": _sub(sb.rate_per_documented_day, sa.rate_per_documented_day),
        "pct_points": _sub(sb.ratio_pct, sa.ratio_pct) if definition.kind == "ratio" else None,
    }
    primary = _PRIMARY_DELTA[definition.kind]

    reasons: list[str] = []
    for label, s, cov in (("a", sa, cov_a), ("b", sb, cov_b)):
        if s.coverage.documented_days < policy.COMPARE_MIN_DAYS:
            reasons.append(f"too_few_days_{label}")
        if cov < _policy_fraction(policy.COMPARE_MIN_COVERAGE):
            reasons.append(f"low_coverage_{label}")
    comparable = not reasons
    if gap_too_large:
        reasons.append("coverage_gap_rates_only")
    return {
        "tool": "compare_periods", "metric": definition.id, "kind": definition.kind, "unit": definition.unit,
        "filters": _set_filters(args.filters), "a": a, "b": b, "difference": difference,
        "primary_delta": primary, "direction": policy.direction(difference[primary], definition.min_delta),
        "comparable": comparable, "reasons": reasons,
    }


def get_daily_series(ctx: ToolContext, args: SeriesArgs) -> dict[str, Any]:
    """T4: one point per calendar day plus an OLS trend over documented points only."""
    definition = _metric_def(ctx, args.metric)
    if definition.kind != "daily_value":
        raise ToolError("not_supported", "Daily series are available only for daily-value metrics.")
    _check_range(args.start, args.end, ctx.today, max_days=policy.MAX_SERIES_DAYS)
    resolved = ctx.metrics.resolve_period(ctx.conn, ctx.user_id, definition.id, args.start, args.end,
                                          today=ctx.today)
    period = resolved.period
    points = ctx.metrics.daily_points(ctx.conn, ctx.user_id, definition.id, period)
    cov = ctx.metrics.summarize(ctx.conn, ctx.user_id, definition.id, period).coverage

    documented = [(p.day, p.value) for p in points if p.documented and p.value is not None]
    xs = [float((day - period.start).days) for day, _ in documented]
    ys = [value for _, value in documented]
    n = len(documented)
    span_days = (documented[-1][0] - documented[0][0]).days if n else 0
    slope = ols_slope(xs, ys)
    change = None if slope is None else slope * span_days

    is_weight = definition.id == "weight.kg"
    min_points = policy.WEIGHT_TREND_MIN_POINTS if is_weight else policy.TREND_MIN_POINTS
    reasons: list[str] = []
    if n < min_points:
        reasons.append("too_few_points")
    if span_days < policy.TREND_MIN_SPAN_DAYS:
        reasons.append("span_too_short")
    # Weight is logged a few times a week, so its trend rule has no coverage floor (worker notes D-3).
    if not is_weight and _coverage_fraction(cov) < _policy_fraction(policy.TREND_MIN_COVERAGE):
        reasons.append("low_coverage")
    if slope is None and "too_few_points" not in reasons:
        reasons.append("no_trend")
    return {
        "tool": "get_daily_series", "metric": definition.id, "unit": definition.unit,
        "period": _period_dict(resolved), "coverage": _coverage_dict(cov),
        "points": [{"date": _iso(p.day), "value": p.value, "documented": bool(p.documented)} for p in points],
        "trend": {"n_points": n, "span_days": span_days,
                  "slope_per_week": None if slope is None else slope * 7, "change_over_period": change},
        "direction": policy.direction(change, definition.min_delta),
        "assessment": {"trend_allowed": not reasons, "reasons": reasons},
    }


def _by_day(points: Sequence[DailyPointLike]) -> dict[date, DailyPointLike]:
    return {p.day: p for p in points}


def relate_metrics(ctx: ToolContext, args: RelateArgs) -> dict[str, Any]:
    """T5: association of x with y (y date = x date + lag): Pearson for a daily x, a condition
    split ("x recorded" vs "x not recorded") for an event x. Association only, never cause."""
    x_def, y_def = _metric_def(ctx, args.x_metric), _metric_def(ctx, args.y_metric)
    if x_def.kind == "ratio":
        raise ToolError("not_supported", "x_metric must not be a ratio metric.")
    if y_def.kind != "daily_value":
        raise ToolError("not_supported", "y_metric must be a daily-value metric.")
    _check_range(args.start, args.end, ctx.today)
    lag = timedelta(days=args.lag_days)
    x_last = args.end - lag
    if args.start > x_last:
        raise ToolError("invalid_range", "The range is too short for this lag.")
    rx = ctx.metrics.resolve_period(ctx.conn, ctx.user_id, x_def.id, args.start, x_last, today=ctx.today)
    ry = ctx.metrics.resolve_period(ctx.conn, ctx.user_id, y_def.id, args.start + lag, args.end, today=ctx.today)
    # Data before either history starts cannot pair; keep at least one x day.
    start = min(max(rx.period.start, ry.period.start - lag), x_last)
    x_period = ctx.metrics.make_period(start, x_last)
    y_period = ctx.metrics.make_period(start + lag, args.end)
    x_points = ctx.metrics.daily_points(ctx.conn, ctx.user_id, x_def.id, x_period)
    y_by_day = _by_day(ctx.metrics.daily_points(ctx.conn, ctx.user_id, y_def.id, y_period))
    x_cov = ctx.metrics.summarize(ctx.conn, ctx.user_id, x_def.id, x_period).coverage
    y_cov = ctx.metrics.summarize(ctx.conn, ctx.user_id, y_def.id, y_period).coverage

    result: dict[str, Any] = {
        "tool": "relate_metrics", "x_metric": x_def.id, "y_metric": y_def.id, "lag_days": args.lag_days,
        "mode": "daily" if x_def.kind == "daily_value" else "event",
        "period": {"start": _iso(start), "end": _iso(args.end), "requested_start": _iso(args.start),
                   "clipped": start > args.start},
        "x_coverage": _coverage_dict(x_cov), "y_coverage": _coverage_dict(y_cov),
        "pairs": None, "pearson_r": None, "x_mean": None, "y_mean": None,
        "with_x": None, "x_not_recorded": None, "difference": None,
    }
    reasons: list[str] = []
    if result["mode"] == "daily":
        pairs = [(xp.value, yp.value) for xp in x_points
                 if xp.documented and xp.value is not None
                 and (yp := y_by_day.get(xp.day + lag)) is not None and yp.documented and yp.value is not None]
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        r = pearson_r(xs, ys)
        result.update(pairs=len(pairs), pearson_r=r, x_mean=_mean(xs), y_mean=_mean(ys))
        if len(pairs) < policy.CORRELATION_MIN_PAIRS:
            reasons.append("too_few_pairs")
        if r is None and len(pairs) >= 2:
            reasons.append("zero_variance")
        result["direction"] = policy.r_direction(r)
    else:
        with_x: list[float] = []
        without_x: list[float] = []
        for xp in x_points:
            yp = y_by_day.get(xp.day + lag)
            if yp is None or not yp.documented or yp.value is None:
                continue
            recorded = xp.documented and xp.value is not None and xp.value > 0
            (with_x if recorded else without_x).append(yp.value)
        with_mean, without_mean = _mean(with_x), _mean(without_x)
        difference = _sub(with_mean, without_mean)
        result.update(with_x={"n_days": len(with_x), "y_mean": with_mean},
                      x_not_recorded={"n_days": len(without_x), "y_mean": without_mean},
                      difference=difference)
        if len(with_x) < policy.SPLIT_MIN_DAYS:
            reasons.append("too_few_days_with_x")
        if len(without_x) < policy.SPLIT_MIN_DAYS:
            reasons.append("too_few_days_x_not_recorded")
        result["direction"] = policy.direction(difference, y_def.min_delta)
    result["assessment"] = {"allowed": not reasons, "reasons": reasons}
    return result


# ---------------------------------------------------------------- T6 list_records (fixed SQL)
# Parameter order of every _LIST_SQL entry: user_id, start, end, then each filter of
# _LIST_FILTERS[domain] twice, then the sort key (4× with a value column, 2× without), then
# limit + 1. _COUNT_SQL takes the same values up to and including the filters.
_LIST_SQL: Final[Mapping[Domain, str]] = MappingProxyType({
    "weight": (
        "SELECT r.id AS record_id, r.record_date AS day, r.weight_kg"
        " FROM weight_records AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " CASE WHEN ? = 'value_asc' THEN r.weight_kg END ASC,"
        " CASE WHEN ? = 'value_desc' THEN r.weight_kg END DESC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "sleep": (
        "SELECT r.id AS record_id, r.record_date AS day, r.hours_slept"
        " FROM sleep_records AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " CASE WHEN ? = 'value_asc' THEN r.hours_slept END ASC,"
        " CASE WHEN ? = 'value_desc' THEN r.hours_slept END DESC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "steps": (
        "SELECT r.id AS record_id, r.record_date AS day, r.steps_count"
        " FROM step_records AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " CASE WHEN ? = 'value_asc' THEN r.steps_count END ASC,"
        " CASE WHEN ? = 'value_desc' THEN r.steps_count END DESC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    # A meal's calories are its newest estimate row (same rule as the daily_calories view).
    "meals": (
        "SELECT r.id AS record_id, r.record_date AS day, r.meal_type,"
        " e.estimated_calories AS calories, e.calorie_basis, r.description"
        " FROM meals AS r"
        " LEFT JOIN meal_calorie_estimates AS e ON e.id = (SELECT MAX(i.id) FROM meal_calorie_estimates AS i"
        " WHERE i.meal_id = r.id AND i.user_id = r.user_id)"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " AND (? IS NULL OR r.meal_type = ?)"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " CASE WHEN ? = 'value_asc' THEN e.estimated_calories END ASC,"
        " CASE WHEN ? = 'value_desc' THEN e.estimated_calories END DESC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "workouts": (
        "SELECT r.id AS record_id, r.record_date AS day, r.workout_type, r.intensity, r.duration_minutes,"
        " r.description"
        " FROM workouts AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " AND (? IS NULL OR r.workout_type = ?)"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " CASE WHEN ? = 'value_asc' THEN r.duration_minutes END ASC,"
        " CASE WHEN ? = 'value_desc' THEN r.duration_minutes END DESC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "articles": (
        "SELECT r.id AS record_id, r.record_date AS day, r.title, r.source"
        " FROM articles AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "podcasts": (
        "SELECT r.id AS record_id, r.record_date AS day, r.duration_minutes, r.podcast_name, r.episode_title"
        " FROM podcasts AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " CASE WHEN ? = 'value_asc' THEN r.duration_minutes END ASC,"
        " CASE WHEN ? = 'value_desc' THEN r.duration_minutes END DESC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "movies": (
        "SELECT r.id AS record_id, r.record_date AS day, r.release_year, r.title"
        " FROM movies AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "series": (
        "SELECT r.id AS record_id, r.record_date AS day, r.season_number, r.episode_number, r.series_name"
        " FROM series AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.record_date END ASC,"
        " r.record_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "job_applications": (
        "SELECT r.id AS record_id, r.application_date AS day, r.work_model, r.status, r.interview_stage,"
        " r.company_name, r.job_title, r.company_location, r.industry"
        " FROM job_applications AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.application_date BETWEEN ? AND ?"
        " AND (? IS NULL OR r.status = ?) AND (? IS NULL OR r.work_model = ?)"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.application_date END ASC,"
        " r.application_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
    "tasks": (
        "SELECT r.id AS record_id, r.task_date AS day, r.category, r.status, r.start_time, r.title, r.description"
        " FROM tasks AS r"
        " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.task_date BETWEEN ? AND ?"
        " AND (? IS NULL OR r.category = ?) AND (? IS NULL OR r.status = ?)"
        " ORDER BY CASE WHEN ? = 'date_asc' THEN r.task_date END ASC,"
        " r.task_date DESC, CASE WHEN ? = 'date_asc' THEN r.id END ASC, r.id DESC"
        " LIMIT ?"),
})

_COUNT_SQL: Final[Mapping[Domain, str]] = MappingProxyType({
    "weight": ("SELECT COUNT(*) FROM weight_records AS r"
               " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "sleep": ("SELECT COUNT(*) FROM sleep_records AS r"
              " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "steps": ("SELECT COUNT(*) FROM step_records AS r"
              " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "meals": ("SELECT COUNT(*) FROM meals AS r"
              " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
              " AND (? IS NULL OR r.meal_type = ?)"),
    "workouts": ("SELECT COUNT(*) FROM workouts AS r"
                 " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"
                 " AND (? IS NULL OR r.workout_type = ?)"),
    "articles": ("SELECT COUNT(*) FROM articles AS r"
                 " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "podcasts": ("SELECT COUNT(*) FROM podcasts AS r"
                 " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "movies": ("SELECT COUNT(*) FROM movies AS r"
               " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "series": ("SELECT COUNT(*) FROM series AS r"
               " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.record_date BETWEEN ? AND ?"),
    "job_applications": ("SELECT COUNT(*) FROM job_applications AS r"
                         " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.application_date BETWEEN ? AND ?"
                         " AND (? IS NULL OR r.status = ?) AND (? IS NULL OR r.work_model = ?)"),
    "tasks": ("SELECT COUNT(*) FROM tasks AS r"
              " WHERE r.user_id = ? AND r.deleted_at IS NULL AND r.task_date BETWEEN ? AND ?"
              " AND (? IS NULL OR r.category = ?) AND (? IS NULL OR r.status = ?)"),
})

# Earliest live record per domain, for clipping the requested start (ADR-005 §3.3 common rule).
_EARLIEST_SQL: Final[Mapping[Domain, str]] = MappingProxyType({
    "weight": "SELECT MIN(record_date) FROM weight_records WHERE user_id = ? AND deleted_at IS NULL",
    "sleep": "SELECT MIN(record_date) FROM sleep_records WHERE user_id = ? AND deleted_at IS NULL",
    "steps": "SELECT MIN(record_date) FROM step_records WHERE user_id = ? AND deleted_at IS NULL",
    "meals": "SELECT MIN(record_date) FROM meals WHERE user_id = ? AND deleted_at IS NULL",
    "workouts": "SELECT MIN(record_date) FROM workouts WHERE user_id = ? AND deleted_at IS NULL",
    "articles": "SELECT MIN(record_date) FROM articles WHERE user_id = ? AND deleted_at IS NULL",
    "podcasts": "SELECT MIN(record_date) FROM podcasts WHERE user_id = ? AND deleted_at IS NULL",
    "movies": "SELECT MIN(record_date) FROM movies WHERE user_id = ? AND deleted_at IS NULL",
    "series": "SELECT MIN(record_date) FROM series WHERE user_id = ? AND deleted_at IS NULL",
    "job_applications": "SELECT MIN(application_date) FROM job_applications WHERE user_id = ? AND deleted_at IS NULL",
    "tasks": "SELECT MIN(task_date) FROM tasks WHERE user_id = ? AND deleted_at IS NULL",
})

_TASK_STATUSES: Final = frozenset({"pending", "completed"})
_JOB_STATUSES: Final = frozenset({"applied", "interviewing", "accepted", "rejected"})
# Filters per domain, in SQL parameter order, with the values valid for that domain (None = the
# argument enum already restricts them).
_LIST_FILTERS: Final[Mapping[Domain, tuple[tuple[str, frozenset[str] | None], ...]]] = MappingProxyType({
    "meals": (("meal_type", None),),
    "workouts": (("workout_type", None),),
    "job_applications": (("status", _JOB_STATUSES), ("work_model", None)),
    "tasks": (("category", None), ("status", _TASK_STATUSES)),
})
_VALUE_SORT: Final = frozenset({"weight", "sleep", "steps", "meals", "workouts", "podcasts"})
# Result columns: enum/number fields, and free text shown only under untrusted_text (no URLs).
_LIST_FIELDS: Final[Mapping[Domain, tuple[str, ...]]] = MappingProxyType({
    "weight": ("weight_kg",), "sleep": ("hours_slept",), "steps": ("steps_count",),
    "meals": ("meal_type", "calories", "calorie_basis"),
    "workouts": ("workout_type", "intensity", "duration_minutes"),
    "articles": (), "podcasts": ("duration_minutes",), "movies": ("release_year",),
    "series": ("season_number", "episode_number"),
    "job_applications": ("work_model", "status", "interview_stage"),
    "tasks": ("category", "status", "start_time"),
})
_LIST_TEXT: Final[Mapping[Domain, tuple[str, ...]]] = MappingProxyType({
    "weight": (), "sleep": (), "steps": (), "meals": ("description",), "workouts": ("description",),
    "articles": ("title", "source"), "podcasts": ("podcast_name", "episode_title"), "movies": ("title",),
    "series": ("series_name",), "job_applications": ("company_name", "job_title", "company_location", "industry"),
    "tasks": ("title", "description"),
})


def _truncate(value: object) -> str | None:
    return None if value is None else str(value)[:UNTRUSTED_TEXT_MAX]


def list_records(ctx: ToolContext, args: ListArgs) -> dict[str, Any]:
    """T6: individual records of one domain (user-scoped, live rows only, fixed SQL)."""
    domain = args.domain
    _check_range(args.start, args.end, ctx.today,
                 days_ahead=TASKS_DAYS_AHEAD if domain == "tasks" else 0)
    if args.sort.startswith("value_") and domain not in _VALUE_SORT:
        raise ToolError("not_supported", "This domain has no value to sort by.")
    given = _set_filters(args.filters)
    allowed = dict(_LIST_FILTERS.get(domain, ()))
    for name, value in given.items():
        if name not in allowed:
            raise ToolError("invalid_filter", f"Filter '{name}' is not allowed for this domain.")
        values = allowed[name]
        if values is not None and value not in values:
            raise ToolError("invalid_filter", f"Filter '{name}' has a value that is not valid for this domain.")

    # Same clipping rule as T1: move the start to the earliest live record when it lies inside
    # the requested period; a domain without records (or starting after `end`) keeps the request.
    earliest_text = ctx.conn.execute(_EARLIEST_SQL[domain], (ctx.user_id,)).fetchone()[0]
    earliest = date.fromisoformat(earliest_text) if earliest_text else None
    start = earliest if earliest is not None and args.start < earliest <= args.end else args.start

    params: list[object] = [ctx.user_id, start.isoformat(), args.end.isoformat()]
    for name in allowed:
        params += [given.get(name), given.get(name)]
    total = int(ctx.conn.execute(_COUNT_SQL[domain], params).fetchone()[0])
    sort_slots = 4 if domain in _VALUE_SORT else 2
    rows = ctx.conn.execute(_LIST_SQL[domain], [*params, *([args.sort] * sort_slots), args.limit + 1]).fetchall()
    rows = rows[:args.limit]
    records = [{
        "record_id": int(row["record_id"]), "date": row["day"],
        "fields": {f: row[f] for f in _LIST_FIELDS[domain]},
        "untrusted_text": {f: _truncate(row[f]) for f in _LIST_TEXT[domain]},
    } for row in rows]
    return {
        "tool": "list_records", "domain": domain,
        "period": {"start": start.isoformat(), "end": args.end.isoformat(),
                   "requested_start": args.start.isoformat(), "clipped": start != args.start},
        "filters": given, "sort": args.sort, "total_matching": total, "returned": len(records),
        "truncated": total > len(records), "records": records,
    }


# ---------------------------------------------------------------- registry and dispatcher
_Handler = Callable[[ToolContext, Any], dict[str, Any]]
_TOOLS: Final[Mapping[str, tuple[type[_Args], _Handler]]] = MappingProxyType({
    "get_data_coverage": (CoverageArgs, get_data_coverage),
    "summarize_metric": (SummarizeArgs, summarize_metric),
    "compare_periods": (CompareArgs, compare_periods),
    "get_daily_series": (SeriesArgs, get_daily_series),
    "relate_metrics": (RelateArgs, relate_metrics),
    "list_records": (ListArgs, list_records),
})
TOOL_NAMES: tuple[str, ...] = tuple(_TOOLS)
ARG_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType({name: model for name, (model, _) in _TOOLS.items()})
"""Tool name → strict argument model (B-15 derives the OpenAI function schemas from these)."""

_ALL_MODELS: tuple[type[BaseModel], ...] = (*ARG_MODELS.values(), Filters, PeriodArgs)
_KNOWN_FIELDS: Final = frozenset(field for model in _ALL_MODELS for field in model.model_fields)


def _validation_message(exc: ValidationError) -> str:
    """Error locations and types only; unknown keys and all input values are never echoed."""
    parts: set[str] = set()
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(p) if isinstance(p, int) or p in _KNOWN_FIELDS else "?" for p in err["loc"])
        parts.add(f"{loc or 'arguments'}: {err['type']}")
    return "Invalid arguments: " + "; ".join(sorted(parts)[:6])


def _result_size(result: Mapping[str, Any]) -> int:
    # ensure_ascii=True is never shorter than UTF-8, so the limit holds for either encoding.
    return len(json.dumps(result, separators=(",", ":")).encode("utf-8"))


_TRIM_ORDER: Final = ("records", "points", "groups", "undocumented_ranges")
_TRIM_FLAG: Final[Mapping[str, str]] = MappingProxyType({
    "records": "truncated", "points": "points_truncated", "groups": "groups_truncated",
    "undocumented_ranges": "truncated"})


def _holders(node: object, key: str) -> list[dict[str, Any]]:
    """Every dict (depth-first, in order) that holds a list under `key`."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get(key), list):
            found.append(node)
        for value in node.values():
            found += _holders(value, key)
    elif isinstance(node, list):
        for value in node:
            found += _holders(value, key)
    return found


def fit_result(result: dict[str, Any], max_bytes: int = policy.MAX_TOOL_RESULT_BYTES) -> dict[str, Any]:
    """Trim list tails (records, points, groups, undocumented_ranges) until the compact JSON fits.

    Each trimmed list gets its truncated flag set (`points_truncated` is added to T4 only when
    points were trimmed); `returned` follows `records`. Mutates and returns `result`; returns a
    `limit_exceeded` error if trimming cannot make it fit.
    """
    if _result_size(result) <= max_bytes:
        return result
    for key in _TRIM_ORDER:
        for holder in reversed(_holders(result, key)):
            while holder[key] and _result_size(result) > max_bytes:
                holder[key].pop()
                holder[_TRIM_FLAG[key]] = True
                if key == "records":
                    holder["returned"] = len(holder[key])
            if _result_size(result) <= max_bytes:
                return result
    return _error("limit_exceeded")


def execute_tool(ctx: ToolContext, name: str, raw_arguments: str) -> ToolOutcome:
    """Parse and run one tool call for `ctx.user_id`; never raises for model-caused problems.

    Raises `ValueError` only for a server bug: `ctx.conn` is not a hardened read-only connection.
    """
    entry = _TOOLS.get(name)
    if entry is None:
        return ToolOutcome(name, None, _error("not_supported", "Unknown tool."), False)
    model, handler = entry
    try:
        args = model.model_validate_json(raw_arguments)
    except ValidationError as exc:
        return ToolOutcome(name, None, _error("invalid_arguments", _validation_message(exc)), False)
    normalized = args.model_dump(mode="json")
    with readonly.time_budget(ctx.conn):
        try:
            with readonly.read_snapshot(ctx.conn):
                result = handler(ctx, args)
        except ToolError as exc:
            result = _error(exc.code if exc.code in ERROR_CODES else "internal", exc.message)
        except ctx.metrics.MetricError as exc:
            code = getattr(exc, "code", None)
            result = _error(code if isinstance(code, str) and code in ERROR_CODES else "invalid_arguments")
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc):
                logger.warning("analytics tool %s failed: %s", name, type(exc).__name__)
                result = _error("internal")
            else:
                result = _error("limit_exceeded")
        except Exception as exc:  # model-facing boundary: only the exception type is logged
            logger.warning("analytics tool %s failed: %s", name, type(exc).__name__)
            result = _error("internal")
    result = fit_result(result)
    return ToolOutcome(name, normalized, result, "error" not in result)


__all__ = ["ARG_MODELS", "TOOL_NAMES", "CompareArgs", "CoverageArgs", "Filters", "ListArgs", "PeriodArgs",
           "RelateArgs", "SeriesArgs", "SummarizeArgs", "ToolContext", "ToolError", "ToolOutcome",
           "compare_periods", "execute_tool", "fit_result", "get_daily_series", "get_data_coverage",
           "list_records", "ols_slope", "pearson_r", "relate_metrics", "summarize_metric"]
