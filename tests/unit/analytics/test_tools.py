"""B-13 analytics tools T1–T5 and the dispatcher, against the in-memory B-05 fake (synthetic data)."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import date, timedelta
from typing import Any

import pytest

from app.analytics import policy, readonly, tools
from app.analytics.ports import DOMAINS, PeriodLike
from app.analytics.readonly import harden_read_only
from app.analytics.tools import ToolContext, ToolOutcome, execute_tool, fit_result, ols_slope, pearson_r
from app.db import connect
from tests.unit.analytics.fake_metrics import FakeMetricError, FakeMetrics, FakeSummary

TODAY = date(2026, 10, 18)
UID = 7
DECOY = 99  # a second user whose distinctive values must never leak into results


def d(text: str) -> date:
    return date.fromisoformat(text)


def days(start: str, n: int) -> list[date]:
    return [d(start) + timedelta(days=i) for i in range(n)]


def no_filters(**set_: str) -> dict[str, str | None]:
    return {"meal_type": None, "workout_type": None, "category": None, "status": None, "work_model": None,
            **set_}


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = harden_read_only(connect(":memory:"))
    yield c
    c.close()


@pytest.fixture
def fake() -> FakeMetrics:
    f = FakeMetrics()
    # Decoy user: present on every day around the test periods with extreme values.
    for day in days("2025-10-01", 383):
        f.add(DECOY, "sleep", day, 23.5)
        f.add(DECOY, "steps", day, 999_999)
        f.add(DECOY, "weight", day, 444.0)
        f.add(DECOY, "workouts", day, workout_type="aerobic")
        f.add(DECOY, "tasks", day, category="career", status="completed")
    return f


@pytest.fixture
def ctx(conn: sqlite3.Connection, fake: FakeMetrics) -> ToolContext:
    return ToolContext(UID, conn, TODAY, fake)


def run(ctx: ToolContext, name: str, **args: Any) -> ToolOutcome:
    return execute_tool(ctx, name, json.dumps(args))


def ok(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    outcome = run(ctx, name, **args)
    assert outcome.ok, outcome.result
    assert outcome.result["tool"] == name
    assert len(json.dumps(outcome.result, separators=(",", ":")).encode()) <= policy.MAX_TOOL_RESULT_BYTES
    return outcome.result


def err(ctx: ToolContext, name: str, **args: Any) -> str:
    outcome = run(ctx, name, **args)
    assert not outcome.ok
    assert set(outcome.result) == {"error"}  # no evidence id, no data
    assert set(outcome.result["error"]) == {"code", "message"}
    code: str = outcome.result["error"]["code"]
    return code


def summarize(ctx: ToolContext, metric: str, start: str, end: str, *, group_by: str = "none",
              filters: dict[str, str | None] | None = None) -> dict[str, Any]:
    return ok(ctx, "summarize_metric", metric=metric, start=start, end=end, group_by=group_by, filters=filters)


# ---------------------------------------------------------------- T1 get_data_coverage
def test_coverage_clips_to_earliest_record_and_keeps_empty_domains(ctx: ToolContext, fake: FakeMetrics) -> None:
    for day in ("2026-10-01", "2026-10-02", "2026-10-05"):
        fake.add(UID, "sleep", d(day), 7.0)
    result = ok(ctx, "get_data_coverage", domains=["sleep", "movies"], start="2026-09-28", end="2026-10-06")
    sleep = result["domains"]["sleep"]
    assert sleep["period"] == {"start": "2026-10-01", "end": "2026-10-06", "requested_start": "2026-09-28",
                               "clipped": True}
    assert sleep["coverage"] == {
        "earliest_record": "2026-10-01", "latest_record": "2026-10-05", "documented_days": 3, "total_days": 6,
        "ratio": 0.5, "undocumented_ranges": [["2026-10-03", "2026-10-04"], ["2026-10-06", "2026-10-06"]],
        "truncated": False}
    movies = result["domains"]["movies"]
    assert movies["period"]["clipped"] is False and movies["period"]["start"] == "2026-09-28"
    assert movies["coverage"]["documented_days"] == 0 and movies["coverage"]["total_days"] == 9
    assert movies["coverage"]["earliest_record"] is None


@pytest.mark.parametrize("domains", [[], ["sleep", "sleep"], ["sleep"] * 12, ["calendar"]])
def test_coverage_rejects_bad_domain_lists(ctx: ToolContext, domains: list[str]) -> None:
    assert err(ctx, "get_data_coverage", domains=domains, start="2026-10-01", end="2026-10-02") \
        == "invalid_arguments"


def test_coverage_range_rules(ctx: ToolContext) -> None:
    assert err(ctx, "get_data_coverage", domains=["sleep"], start="2026-10-02", end="2026-10-01") == "invalid_range"
    assert err(ctx, "get_data_coverage", domains=["sleep"], start="2026-10-01", end="2026-10-19") == "invalid_range"
    assert ok(ctx, "get_data_coverage", domains=["sleep"], start="2026-10-18", end="2026-10-18")


# ---------------------------------------------------------------- T2 summarize_metric
def test_summarize_daily_value_hand_computed(ctx: ToolContext, fake: FakeMetrics) -> None:
    ids = {day: fake.add(UID, "sleep", d(day), v) for day, v in
           (("2026-10-01", 7.0), ("2026-10-02", 8.0), ("2026-10-04", 6.0))}
    r = summarize(ctx, "sleep.hours", "2026-10-01", "2026-10-04")
    assert (r["metric"], r["kind"], r["unit"], r["group_by"], r["filters"]) == ("sleep.hours", "daily_value", "h",
                                                                               "none", {})
    assert r["n_observations"] == 3
    assert (r["mean"], r["median"], r["min"], r["max"], r["sum"]) == (7.0, 7.0, 6.0, 8.0, 21.0)
    assert r["min_record"] == {"date": "2026-10-04", "value": 6.0, "record_id": ids["2026-10-04"]}
    assert r["max_record"] == {"date": "2026-10-02", "value": 8.0, "record_id": ids["2026-10-02"]}
    assert r["first"]["date"] == "2026-10-01" and r["last"]["date"] == "2026-10-04"
    assert r["change"] is None and r["ratio"] is None and r["rate_per_documented_day"] is None
    assert r["coverage"]["documented_days"] == 3 and r["coverage"]["total_days"] == 4
    assert r["groups"] == [] and r["groups_truncated"] is False
    assert r["assessment"] == {"enough_for_value": True, "low_coverage": False}


def test_summarize_explicit_zero_is_a_value_and_missing_day_is_not(ctx: ToolContext, fake: FakeMetrics) -> None:
    fake.add(UID, "steps", d("2026-10-01"), 0)
    fake.add(UID, "steps", d("2026-10-03"), 1000)
    r = summarize(ctx, "steps.count", "2026-10-01", "2026-10-03")
    assert r["n_observations"] == 2 and r["mean"] == 500.0 and r["min"] == 0
    assert r["coverage"]["documented_days"] == 2 and r["coverage"]["undocumented_ranges"] == [
        ["2026-10-02", "2026-10-02"]]


@pytest.mark.parametrize(("n", "enough"), [(2, False), (3, True)])
def test_enough_for_value_boundary(ctx: ToolContext, fake: FakeMetrics, n: int, enough: bool) -> None:
    for day in days("2026-10-01", n):
        fake.add(UID, "sleep", day, 7.0)
    r = summarize(ctx, "sleep.hours", "2026-10-01", "2026-10-03")
    assert r["assessment"]["enough_for_value"] is enough


@pytest.mark.parametrize(("documented", "low"), [(4, True), (5, False)])
def test_low_coverage_boundary(ctx: ToolContext, fake: FakeMetrics, documented: int, low: bool) -> None:
    for day in days("2026-10-01", documented):
        fake.add(UID, "sleep", day, 7.0)
    r = summarize(ctx, "sleep.hours", "2026-10-01", "2026-10-10")
    assert r["coverage"]["ratio"] == documented / 10
    assert r["assessment"]["low_coverage"] is low


def test_summarize_clips_start_to_earliest_record(ctx: ToolContext, fake: FakeMetrics) -> None:
    fake.add(UID, "weight", d("2026-10-10"), 80.0)
    fake.add(UID, "weight", d("2026-10-15"), 79.0)
    r = summarize(ctx, "weight.kg", "2026-09-01", "2026-10-18")
    assert r["period"] == {"start": "2026-10-10", "end": "2026-10-18", "requested_start": "2026-09-01",
                           "clipped": True}
    assert r["change"] == -1.0


def test_summarize_event_count_with_filter_and_groups(ctx: ToolContext, fake: FakeMetrics) -> None:
    fake.add(UID, "tasks", d("2026-10-01"), category="career", status="completed")
    fake.add(UID, "tasks", d("2026-10-01"), category="family", status="pending")
    fake.add(UID, "tasks", d("2026-10-02"), category="family", status="completed")
    fake.add(UID, "tasks", d("2026-10-03"), category="career", status="pending")
    r = summarize(ctx, "tasks.count", "2026-10-01", "2026-10-03", filters=no_filters(category="career"))
    assert r["filters"] == {"category": "career"}
    assert r["n_observations"] == 2 and r["rate_per_documented_day"] == pytest.approx(2 / 3)
    grouped = summarize(ctx, "tasks.count", "2026-10-01", "2026-10-03", group_by="subtype")
    assert [(g["key"], g["count"]) for g in grouped["groups"]] == [("career", 2), ("family", 2)]
    assert set(grouped["groups"][0]) == {"key", "n", "documented_days", "mean", "count",
                                         "rate_per_documented_day", "pct"}


def test_summarize_ratio(ctx: ToolContext, fake: FakeMetrics) -> None:
    for status in ("completed", "completed", "pending"):
        fake.add(UID, "tasks", d("2026-10-01"), category="other", status=status)
    r = summarize(ctx, "tasks.completion_rate", "2026-10-01", "2026-10-01")
    assert r["ratio"] == {"numerator": 2, "denominator": 3, "pct": pytest.approx(200 / 3)}


@pytest.mark.parametrize(("metric", "filters"), [
    ("tasks.count", no_filters(meal_type="lunch")),
    ("movies.count", no_filters(category="career")),
    ("tasks.count", no_filters(status="applied")),  # a job status is not a task status
    ("sleep.hours", no_filters(workout_type="aerobic")),
])
def test_filters_outside_registry_are_rejected(ctx: ToolContext, metric: str, filters: dict[str, Any]) -> None:
    assert err(ctx, "summarize_metric", metric=metric, start="2026-10-01", end="2026-10-02", group_by="none",
               filters=filters) == "invalid_filter"


def test_summarize_unsupported_and_unknown(ctx: ToolContext) -> None:
    base = {"start": "2026-10-01", "end": "2026-10-02", "filters": None}
    assert err(ctx, "summarize_metric", metric="sleep.hours", group_by="subtype", **base) == "not_supported"
    # A closed-set id the backend has no entry for (the fake has no calories metric).
    assert err(ctx, "summarize_metric", metric="calories.daily_total", group_by="none", **base) == "unknown_metric"
    assert err(ctx, "summarize_metric", metric="mood.score", group_by="none", **base) == "invalid_arguments"
    assert err(ctx, "summarize_metric", metric="sleep.hours", group_by="day", **base) == "invalid_arguments"


@pytest.mark.parametrize(("start", "end", "code"), [
    ("2026-10-05", "2026-10-04", "invalid_range"),       # start > end
    ("2026-10-01", "2026-10-19", "invalid_range"),       # end > today
    ("2025-10-17", "2026-10-18", "invalid_range"),       # 367 days
    ("2025-10-18", "2026-10-18", None),                  # 366 days
    ("2026-10-18", "2026-10-18", None),
    ("2026-02-30", "2026-03-01", "invalid_arguments"),   # not a calendar date
    ("01.10.2026", "2026-10-02", "invalid_arguments"),
])
def test_range_rules(ctx: ToolContext, start: str, end: str, code: str | None) -> None:
    outcome = run(ctx, "summarize_metric", metric="sleep.hours", start=start, end=end, group_by="none",
                  filters=None)
    if code is None:
        assert outcome.ok, outcome.result
    else:
        assert outcome.result["error"]["code"] == code


# ---------------------------------------------------------------- T3 compare_periods
def compare(ctx: ToolContext, metric: str, a: tuple[str, str], b: tuple[str, str]) -> dict[str, Any]:
    return ok(ctx, "compare_periods", metric=metric, period_a={"start": a[0], "end": a[1]},
              period_b={"start": b[0], "end": b[1]}, filters=None)


@pytest.mark.parametrize(("b_value", "direction"), [(7.25, "increase"), (7.125, "no_clear_change"),
                                                    (6.75, "decrease")])
def test_compare_daily_difference_and_direction_at_min_delta(ctx: ToolContext, fake: FakeMetrics, b_value: float,
                                                             direction: str) -> None:
    for day in days("2026-10-01", 3):
        fake.add(UID, "sleep", day, 7.0)
    for day in days("2026-10-04", 3):
        fake.add(UID, "sleep", day, b_value)
    r = compare(ctx, "sleep.hours", ("2026-10-01", "2026-10-03"), ("2026-10-04", "2026-10-06"))
    assert r["a"]["mean"] == 7.0 and r["b"]["mean"] == b_value
    assert "tool" not in r["a"] and "assessment" not in r["a"] and r["a"]["metric"] == "sleep.hours"
    diff = r["difference"]
    assert diff["mean_abs"] == b_value - 7.0
    assert diff["mean_rel_pct"] == pytest.approx((b_value - 7.0) / 7.0 * 100)
    assert diff["count_abs"] == 0 and diff["rate_abs"] is None and diff["pct_points"] is None
    assert r["primary_delta"] == "mean_abs" and r["direction"] == direction
    assert r["comparable"] is True and r["reasons"] == []


@pytest.mark.parametrize(("documented_a", "comparable", "reasons"), [
    (2, False, ["too_few_days_a", "coverage_gap_rates_only"]),  # coverage 2/3 vs 3/3: gap 1/3 > 0.3
    (3, True, [])])
def test_compare_minimum_days_boundary(ctx: ToolContext, fake: FakeMetrics, documented_a: int, comparable: bool,
                                       reasons: list[str]) -> None:
    for day in days("2026-10-01", documented_a):
        fake.add(UID, "sleep", day, 7.0)
    for day in days("2026-10-04", 3):
        fake.add(UID, "sleep", day, 7.0)
    r = compare(ctx, "sleep.hours", ("2026-10-01", "2026-10-03"), ("2026-10-04", "2026-10-06"))
    assert r["comparable"] is comparable and r["reasons"] == reasons


@pytest.mark.parametrize(("documented_a", "comparable"), [(39, False), (40, True)])
def test_compare_minimum_coverage_boundary(ctx: ToolContext, fake: FakeMetrics, documented_a: int,
                                           comparable: bool) -> None:
    # Two 100-day periods: coverage 0.39 vs 0.40 in period a, 0.40 in period b.
    for i in range(documented_a):
        fake.add(UID, "sleep", d("2026-01-01") + timedelta(days=2 * i), 7.0)
    for i in range(40):
        fake.add(UID, "sleep", d("2026-05-01") + timedelta(days=2 * i), 7.0)
    r = compare(ctx, "sleep.hours", ("2026-01-01", "2026-04-10"), ("2026-05-01", "2026-08-08"))
    assert r["a"]["coverage"]["total_days"] == 100 and r["a"]["coverage"]["documented_days"] == documented_a
    assert r["comparable"] is comparable
    assert r["reasons"] == ([] if comparable else ["low_coverage_a"])


@pytest.mark.parametrize(("documented_a", "counts_kept"), [(5, True), (4, False)])
def test_compare_coverage_gap_nulls_count_abs(ctx: ToolContext, fake: FakeMetrics, documented_a: int,
                                              counts_kept: bool) -> None:
    # Coverage b = 8/10; a = 5/10 (gap exactly 0.3: counts kept) or 4/10 (gap 0.4: rates only).
    for day in days("2026-09-01", documented_a):
        fake.add(UID, "workouts", day, workout_type="weights")
    for day in days("2026-10-01", 8):
        fake.add(UID, "workouts", day, workout_type="weights")
    r = compare(ctx, "workouts.count", ("2026-09-01", "2026-09-10"), ("2026-10-01", "2026-10-10"))
    assert r["difference"]["count_abs"] == (8 - documented_a if counts_kept else None)
    assert ("coverage_gap_rates_only" in r["reasons"]) is not counts_kept
    assert r["comparable"] is True  # the gap restricts counts, not the comparison itself
    assert r["primary_delta"] == "rate_abs" and r["difference"]["rate_abs"] == 0.0
    assert r["direction"] == "no_clear_change"


@pytest.mark.parametrize(("b_events", "direction"), [(50, "increase"), (41, "no_clear_change"),
                                                     (30, "decrease")])
def test_compare_event_rate_direction(ctx: ToolContext, fake: FakeMetrics, b_events: int, direction: str) -> None:
    # Period a: 2 tasks on each of 20 days (rate 2.0); period b: b_events spread over 20 days.
    for day in days("2026-08-01", 20):
        for _ in range(2):
            fake.add(UID, "tasks", day, category="other", status="pending")
    for i in range(b_events):
        fake.add(UID, "tasks", d("2026-09-01") + timedelta(days=i % 20), category="other", status="pending")
    r = compare(ctx, "tasks.count", ("2026-08-01", "2026-08-20"), ("2026-09-01", "2026-09-20"))
    assert r["difference"]["rate_abs"] == pytest.approx(b_events / 20 - 2.0)
    assert r["direction"] == direction


def test_compare_ratio_uses_percentage_points(ctx: ToolContext, fake: FakeMetrics) -> None:
    for status in ("completed", "pending"):
        fake.add(UID, "tasks", d("2026-10-01"), category="other", status=status)
    for status in ("completed", "completed", "completed", "pending"):
        fake.add(UID, "tasks", d("2026-10-05"), category="other", status=status)
    r = compare(ctx, "tasks.completion_rate", ("2026-10-01", "2026-10-02"), ("2026-10-05", "2026-10-06"))
    assert r["primary_delta"] == "pct_points" and r["difference"]["pct_points"] == 25.0
    assert r["direction"] == "increase"


@pytest.mark.parametrize(("a", "b", "code"), [
    (("2026-10-01", "2026-10-05"), ("2026-10-05", "2026-10-08"), "invalid_range"),   # share a day
    (("2026-10-03", "2026-10-09"), ("2026-10-01", "2026-10-04"), "invalid_range"),   # b inside a's span
    (("2026-10-06", "2026-10-08"), ("2026-10-01", "2026-10-05"), "invalid_range"),   # a after b (regression)
    (("2026-10-02", "2026-10-02"), ("2026-10-02", "2026-10-02"), "invalid_range"),   # same single day
    (("2026-10-01", "2026-10-05"), ("2026-10-06", "2026-10-19"), "invalid_range"),   # b ends after today
    (("2026-10-01", "2026-10-05"), ("2026-10-06", "2026-10-08"), None),              # adjacent is fine
])
def test_compare_overlap_and_range(ctx: ToolContext, a: tuple[str, str], b: tuple[str, str], code: str | None) -> None:
    outcome = run(ctx, "compare_periods", metric="sleep.hours", period_a={"start": a[0], "end": a[1]},
                  period_b={"start": b[0], "end": b[1]}, filters=None)
    assert (outcome.result.get("error", {}).get("code")) == code
    if code is not None and a[0] > b[1]:
        assert outcome.result["error"]["message"] == "period_a must end before period_b starts."


# ---------------------------------------------------------------- T4 get_daily_series
def test_series_points_and_ols_on_known_line(ctx: ToolContext, fake: FakeMetrics) -> None:
    # steps = 1000 + 100·x on every other day of a 15-day period; one explicit 0 is a real point.
    for x in range(0, 15, 2):
        fake.add(UID, "steps", d("2026-10-01") + timedelta(days=x), 1000 + 100 * x)
    r = ok(ctx, "get_daily_series", metric="steps.count", start="2026-10-01", end="2026-10-15")
    assert len(r["points"]) == 15
    assert r["points"][0] == {"date": "2026-10-01", "value": 1000, "documented": True}
    assert r["points"][1] == {"date": "2026-10-02", "value": None, "documented": False}
    trend = r["trend"]
    assert trend["n_points"] == 8 and trend["span_days"] == 14
    assert trend["slope_per_week"] == pytest.approx(700.0)
    assert trend["change_over_period"] == pytest.approx(1400.0)
    assert math.isclose(trend["change_over_period"], trend["slope_per_week"] / 7 * trend["span_days"],
                        rel_tol=policy.DERIVED_TOLERANCE)
    assert r["direction"] == "increase"
    assert r["assessment"] == {"trend_allowed": True, "reasons": []}


def test_series_explicit_zero_is_documented(ctx: ToolContext, fake: FakeMetrics) -> None:
    fake.add(UID, "steps", d("2026-10-01"), 0)
    r = ok(ctx, "get_daily_series", metric="steps.count", start="2026-10-01", end="2026-10-02")
    assert r["points"] == [{"date": "2026-10-01", "value": 0, "documented": True},
                           {"date": "2026-10-02", "value": None, "documented": False}]
    assert r["trend"] == {"n_points": 1, "span_days": 0, "slope_per_week": None, "change_over_period": None}
    assert r["direction"] == "not_applicable"


@pytest.mark.parametrize(("offsets", "reasons"), [
    ((0, 2, 4, 6, 8, 10, 12, 14), []),                                  # 8 pts, span 14, cov 8/15
    ((0, 2, 4, 6, 8, 10, 14), ["low_coverage"]),                         # 7 pts, cov 7/15 < 0.5
    ((0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12), ["span_too_short"]),    # span 12
    ((0, 1, 2, 3, 4, 14, 13, 12), []),
    ((0, 1, 2, 3, 4, 14), ["too_few_points", "low_coverage"]),
])
def test_series_trend_assessment(ctx: ToolContext, fake: FakeMetrics, offsets: tuple[int, ...],
                                 reasons: list[str]) -> None:
    for x in offsets:
        fake.add(UID, "sleep", d("2026-10-01") + timedelta(days=x), 7.0 + x / 8)
    r = ok(ctx, "get_daily_series", metric="sleep.hours", start="2026-10-01", end="2026-10-15")
    assert r["assessment"] == {"trend_allowed": not reasons, "reasons": reasons}


def test_series_weight_trend_has_no_coverage_floor(ctx: ToolContext, fake: FakeMetrics) -> None:
    for x, kg in ((0, 80.0), (5, 79.5), (10, 79.0), (14, 78.6)):
        fake.add(UID, "weight", d("2026-10-01") + timedelta(days=x), kg)
    r = ok(ctx, "get_daily_series", metric="weight.kg", start="2026-10-01", end="2026-10-15")
    assert r["coverage"]["ratio"] < 0.5
    assert r["assessment"] == {"trend_allowed": True, "reasons": []}
    assert r["direction"] == "decrease"


def test_series_limits(ctx: ToolContext) -> None:
    assert ok(ctx, "get_daily_series", metric="sleep.hours", start="2026-07-19", end="2026-10-18")  # 92 days
    assert err(ctx, "get_daily_series", metric="sleep.hours", start="2026-07-18", end="2026-10-18") \
        == "invalid_range"  # 93 days
    assert err(ctx, "get_daily_series", metric="workouts.count", start="2026-10-01", end="2026-10-02") \
        == "not_supported"
    assert err(ctx, "get_daily_series", metric="tasks.completion_rate", start="2026-10-01", end="2026-10-02") \
        == "not_supported"


def test_ols_and_pearson_helpers() -> None:
    assert ols_slope([0, 1, 2, 3], [1, 3, 5, 7]) == pytest.approx(2.0)
    assert ols_slope([1], [1]) is None
    assert ols_slope([2, 2, 2], [1, 2, 3]) is None
    assert pearson_r([1, 2, 3, 4, 5], [2, 4, 5, 4, 5]) == pytest.approx(6 / math.sqrt(60))
    assert pearson_r([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert pearson_r([1, 2, 3], [4, 4, 4]) is None
    assert pearson_r([1], [1]) is None


# ---------------------------------------------------------------- T5 relate_metrics
def relate(ctx: ToolContext, x: str, y: str, start: str, end: str, lag: int = 0) -> dict[str, Any]:
    return ok(ctx, "relate_metrics", x_metric=x, y_metric=y, start=start, end=end, lag_days=lag)


def test_relate_daily_pearson_on_known_data(ctx: ToolContext, fake: FakeMetrics) -> None:
    for i, (x, y) in enumerate(((1, 2), (2, 4), (3, 5), (4, 4), (5, 5))):
        day = d("2026-10-01") + timedelta(days=i)
        fake.add(UID, "sleep", day, float(x))
        fake.add(UID, "steps", day, 1000 * y)
    fake.add(UID, "sleep", d("2026-10-06"), 9.0)  # x without y: not a pair
    r = relate(ctx, "sleep.hours", "steps.count", "2026-10-01", "2026-10-06")
    assert r["mode"] == "daily" and r["pairs"] == 5
    assert r["pearson_r"] == pytest.approx(6 / math.sqrt(60))
    assert r["x_mean"] == 3.0 and r["y_mean"] == 4000.0
    assert r["with_x"] is None and r["x_not_recorded"] is None and r["difference"] is None
    assert r["direction"] == "increase"
    assert r["assessment"] == {"allowed": False, "reasons": ["too_few_pairs"]}
    assert r["x_coverage"]["documented_days"] == 6 and r["y_coverage"]["documented_days"] == 5


def test_relate_zero_variance_gives_null_r(ctx: ToolContext, fake: FakeMetrics) -> None:
    for i, day in enumerate(days("2026-10-01", 10)):
        fake.add(UID, "sleep", day, 7.0)
        fake.add(UID, "steps", day, 1000 * i)
    r = relate(ctx, "sleep.hours", "steps.count", "2026-10-01", "2026-10-10")
    assert r["pairs"] == 10 and r["pearson_r"] is None and r["direction"] == "not_applicable"
    assert r["assessment"] == {"allowed": False, "reasons": ["zero_variance"]}


def test_relate_lag_pairs_x_with_next_day_y(ctx: ToolContext, fake: FakeMetrics) -> None:
    for i, day in enumerate(days("2026-10-01", 11)):
        fake.add(UID, "steps", day, 1000 * i)
        fake.add(UID, "sleep", day, 5.0 + (i - 1) * 0.5)  # y(d+1) is linear in x(d)
    r = relate(ctx, "steps.count", "sleep.hours", "2026-10-01", "2026-10-11", lag=1)
    assert r["pairs"] == 10 and r["pearson_r"] == pytest.approx(1.0)
    assert r["x_coverage"]["total_days"] == 10 and r["y_coverage"]["total_days"] == 10
    assert r["assessment"]["allowed"] is True


def test_relate_event_split_labels_unrecorded_days(ctx: ToolContext, fake: FakeMetrics) -> None:
    # Workout on Oct 1–5 → sleep the next night 8 h; no workout recorded Oct 6–10 → 7 h.
    for day in days("2026-10-01", 5):
        fake.add(UID, "workouts", day, workout_type="weights")
    for day in days("2026-10-02", 5):
        fake.add(UID, "sleep", day, 8.0)
    for day in days("2026-10-07", 5):
        fake.add(UID, "sleep", day, 7.0)
    r = relate(ctx, "workouts.count", "sleep.hours", "2026-10-01", "2026-10-11", lag=1)
    assert r["mode"] == "event"
    assert r["with_x"] == {"n_days": 5, "y_mean": 8.0}
    assert r["x_not_recorded"] == {"n_days": 5, "y_mean": 7.0}
    assert r["difference"] == 1.0 and r["direction"] == "increase"
    assert r["pairs"] is None and r["pearson_r"] is None
    assert r["assessment"] == {"allowed": True, "reasons": []}


def test_relate_event_split_minimum_days(ctx: ToolContext, fake: FakeMetrics) -> None:
    for day in days("2026-10-01", 4):
        fake.add(UID, "workouts", day, workout_type="weights")
    for day in days("2026-10-01", 10):
        fake.add(UID, "sleep", day, 7.0)
    r = relate(ctx, "workouts.count", "sleep.hours", "2026-10-01", "2026-10-10")
    assert r["with_x"]["n_days"] == 4 and r["x_not_recorded"]["n_days"] == 6
    assert r["assessment"] == {"allowed": False, "reasons": ["too_few_days_with_x"]}
    assert r["direction"] == "no_clear_change"


def test_relate_rejections(ctx: ToolContext) -> None:
    base = {"start": "2026-10-01", "end": "2026-10-05", "lag_days": 0}
    assert err(ctx, "relate_metrics", x_metric="tasks.completion_rate", y_metric="sleep.hours", **base) \
        == "not_supported"
    assert err(ctx, "relate_metrics", x_metric="sleep.hours", y_metric="workouts.count", **base) == "not_supported"
    assert err(ctx, "relate_metrics", x_metric="sleep.hours", y_metric="steps.count", start="2026-10-05",
               end="2026-10-05", lag_days=1) == "invalid_range"
    assert err(ctx, "relate_metrics", x_metric="sleep.hours", y_metric="steps.count", start="2026-10-01",
               end="2026-10-05", lag_days=2) == "invalid_arguments"


# ---------------------------------------------------------------- dispatcher, scoping, safety
VALID_ARGS: dict[str, dict[str, Any]] = {
    "get_data_coverage": {"domains": ["sleep"], "start": "2026-10-01", "end": "2026-10-02"},
    "summarize_metric": {"metric": "sleep.hours", "start": "2026-10-01", "end": "2026-10-02", "group_by": "none",
                         "filters": None},
    "compare_periods": {"metric": "sleep.hours", "period_a": {"start": "2026-10-01", "end": "2026-10-02"},
                        "period_b": {"start": "2026-10-03", "end": "2026-10-04"}, "filters": None},
    "get_daily_series": {"metric": "sleep.hours", "start": "2026-10-01", "end": "2026-10-02"},
    "relate_metrics": {"x_metric": "sleep.hours", "y_metric": "steps.count", "start": "2026-10-01",
                       "end": "2026-10-02", "lag_days": 0},
}


def test_arg_models_cover_every_tool_and_have_no_user_id() -> None:
    assert set(tools.TOOL_NAMES) == set(tools.ARG_MODELS) and len(tools.TOOL_NAMES) == 6
    for model in tools.ARG_MODELS.values():
        schema = json.dumps(model.model_json_schema())
        assert "user_id" not in schema
        assert all(f.is_required() for f in model.model_fields.values())


@pytest.mark.parametrize("name", sorted(VALID_ARGS))
def test_valid_args_succeed_and_are_normalised(ctx: ToolContext, name: str) -> None:
    outcome = run(ctx, name, **VALID_ARGS[name])
    assert outcome.ok and outcome.args == VALID_ARGS[name]


@pytest.mark.parametrize("name", sorted(VALID_ARGS))
def test_user_id_argument_is_rejected_without_echo(ctx: ToolContext, fake: FakeMetrics, name: str) -> None:
    outcome = run(ctx, name, **VALID_ARGS[name], user_id=DECOY)
    assert outcome.result["error"]["code"] == "invalid_arguments" and outcome.args is None
    message = outcome.result["error"]["message"]
    assert "user_id" not in message and str(DECOY) not in message
    assert DECOY not in fake.calls


@pytest.mark.parametrize("name", sorted(VALID_ARGS))
def test_missing_field_is_rejected(ctx: ToolContext, name: str) -> None:
    args = dict(VALID_ARGS[name])
    args.pop(next(iter(args)))
    assert err(ctx, name, **args) == "invalid_arguments"


def test_filters_object_must_list_every_field(ctx: ToolContext) -> None:
    args = {**VALID_ARGS["summarize_metric"], "filters": {"status": None}}
    assert err(ctx, "summarize_metric", **args) == "invalid_arguments"


def test_invalid_json_and_unknown_tool(ctx: ToolContext) -> None:
    outcome = execute_tool(ctx, "summarize_metric", "{not json")
    assert outcome.result["error"]["code"] == "invalid_arguments"
    outcome = execute_tool(ctx, "run_sql", "{}")
    assert outcome.result == {"error": {"code": "not_supported", "message": "Unknown tool."}} and not outcome.ok


def test_strict_mode_rejects_coercion(ctx: ToolContext) -> None:
    args = {**VALID_ARGS["relate_metrics"], "lag_days": "1"}
    assert err(ctx, "relate_metrics", **args) == "invalid_arguments"


def test_backend_is_called_only_with_the_context_user(ctx: ToolContext, fake: FakeMetrics) -> None:
    for day in days("2026-10-01", 4):
        fake.add(UID, "sleep", day, 7.0)
        fake.add(UID, "steps", day, 5000)
    fake.calls.clear()
    results = [ok(ctx, name, **args) for name, args in VALID_ARGS.items()]
    assert fake.calls and set(fake.calls) == {UID}
    blob = json.dumps(results)
    assert "23.5" not in blob and "999999" not in blob and "444.0" not in blob  # decoy values never appear


class _RaisingFake(FakeMetrics):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def summarize(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike, *,
                  group_by: Any = "none", filters: Any = None) -> FakeSummary:
        raise self.exc


def test_backend_metric_error_keeps_its_code(conn: sqlite3.Connection) -> None:
    c = ToolContext(UID, conn, TODAY, _RaisingFake(FakeMetricError("invalid_filter", "secret detail")))
    outcome = run(c, "summarize_metric", **VALID_ARGS["summarize_metric"])
    assert outcome.result["error"]["code"] == "invalid_filter"
    assert "secret" not in outcome.result["error"]["message"]


def test_unexpected_exception_is_internal_and_logs_type_only(conn: sqlite3.Connection,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    c = ToolContext(UID, conn, TODAY, _RaisingFake(KeyError("private record text")))
    with caplog.at_level(logging.WARNING, logger="app.analytics.tools"):
        outcome = run(c, "summarize_metric", **VALID_ARGS["summarize_metric"])
    assert outcome.result["error"]["code"] == "internal" and not outcome.ok
    assert "KeyError" in caplog.text and "private record text" not in caplog.text
    assert not conn.in_transaction  # the read snapshot was closed


_SLOW_SQL = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


class _SlowFake(FakeMetrics):
    def summarize(self, conn: sqlite3.Connection, user_id: int, metric_id: str, period: PeriodLike, *,
                  group_by: Any = "none", filters: Any = None) -> FakeSummary:
        conn.execute(_SLOW_SQL).fetchone()
        raise AssertionError("not reached")


def test_slow_query_hits_the_time_budget(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    original = readonly.time_budget

    def short_budget(c: sqlite3.Connection, seconds: float = 0.2) -> AbstractContextManager[None]:
        return original(c, 0.2)

    monkeypatch.setattr(readonly, "time_budget", short_budget)
    outcome = run(ToolContext(UID, conn, TODAY, _SlowFake()), "summarize_metric", **VALID_ARGS["summarize_metric"])
    assert outcome.result["error"]["code"] == "limit_exceeded"


def test_writable_connection_is_refused() -> None:
    writable = connect(":memory:")
    try:
        with pytest.raises(ValueError, match="hardened"):
            execute_tool(ToolContext(UID, writable, TODAY, FakeMetrics()), "summarize_metric",
                         json.dumps(VALID_ARGS["summarize_metric"]))
    finally:
        writable.close()


# ---------------------------------------------------------------- result size
def _size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, separators=(",", ":")).encode())


def test_fit_result_trims_records_from_the_end() -> None:
    records = [{"record_id": i, "date": "2026-10-01", "fields": {},
                "untrusted_text": {"title": "x" * 200}} for i in range(50)]
    result = {"tool": "list_records", "total_matching": 50, "returned": 50, "truncated": False, "records": records}
    fitted = fit_result(result)
    assert _size(fitted) <= policy.MAX_TOOL_RESULT_BYTES
    assert fitted["truncated"] is True and fitted["returned"] == len(fitted["records"]) < 50
    assert [r["record_id"] for r in fitted["records"]] == list(range(fitted["returned"]))


def test_fit_result_trims_points_then_ranges_and_flags_them() -> None:
    ranges = [["2026-01-01", "2026-01-02"]] * 10
    result = {"tool": "get_daily_series", "coverage": {"undocumented_ranges": ranges, "truncated": False},
              "points": [{"date": "2026-10-01", "value": 1.0, "documented": True, "pad": "p" * 80}] * 92}
    fitted = fit_result(result)
    assert _size(fitted) <= policy.MAX_TOOL_RESULT_BYTES
    assert fitted["points_truncated"] is True and fitted["coverage"]["truncated"] is False
    tiny = fit_result({"tool": "x", "coverage": {"undocumented_ranges": [["a", "b"]] * 5, "truncated": False}},
                      max_bytes=80)
    assert tiny["coverage"]["truncated"] is True and _size(tiny) <= 80


def test_fit_result_gives_up_with_limit_exceeded() -> None:
    assert fit_result({"tool": "x", "blob": "y" * 9000}) == {
        "error": {"code": "limit_exceeded", "message": tools._FIXED_MESSAGES["limit_exceeded"]}}


def test_coverage_result_with_many_domains_fits(ctx: ToolContext, fake: FakeMetrics) -> None:
    for day in days("2025-10-18", 366):
        if day.toordinal() % 3 == 0:
            for domain in ("sleep", "steps", "weight", "meals", "workouts", "articles", "podcasts", "movies",
                           "series", "job_applications", "tasks"):
                fake.add(UID, domain, day, 1.0)
    r = ok(ctx, "get_data_coverage", domains=list(DOMAINS), start="2025-10-18", end="2026-10-18")
    assert all(len(v["coverage"]["undocumented_ranges"]) <= policy.MAX_RANGES for v in r["domains"].values())

