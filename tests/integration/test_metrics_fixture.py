"""B-05 against the B-14 fixture: SQL results equal the independent pure-Python reference
(tests/support/reference.py, or direct computation from demo/synthetic_v1.json) at the golden
anchor 2026-10-17. The demo copy is built by the B-14 importer and read through a read-only
connection."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from statistics import fmean
from typing import Any

import pytest

from app.analytics import metrics
from app.analytics.metrics import Period, daily_points, summarize
from app.db import connect
from app.demo import build_working_copy
from app.records.job_applications import STATUSES as JOB_STATUSES
from app.records.meals import MEAL_TYPES
from app.records.tasks import CATEGORIES
from app.records.workouts import WORKOUT_TYPES
from tests.support.reference import ANCHOR, Reference, load
from tests.support.reference import Period as RefPeriod

DAILY = {"sleep.hours": "sleep_records", "steps.count": "step_records", "weight.kg": "weight_records"}
PERIODS = [(-29, 0), (-29, -15), (-14, 0), (-22, -16), (-6, 0)]


@pytest.fixture(scope="module")
def ref() -> Reference:
    return Reference(load(), ANCHOR)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection, int]]:
    copy = build_working_copy(tmp_path_factory.mktemp("fixture") / "data" / "demo", 1, ANCHOR)
    conn = connect(copy.path, read_only=True)
    yield conn, copy.user_id
    conn.close()


def per(p: RefPeriod) -> Period:
    return Period(p.start, p.end)


@pytest.mark.parametrize("metric_id", sorted(DAILY))
@pytest.mark.parametrize("bounds", PERIODS)
def test_daily_value_summaries_equal_the_reference(demo: tuple[sqlite3.Connection, int], ref: Reference,
                                                   metric_id: str, bounds: tuple[int, int]) -> None:
    conn, uid = demo
    period = ref.period(*bounds)
    expected = ref.summarize(metric_id, period)
    s = summarize(conn, uid, metric_id, per(period))
    assert s.n_observations == expected.documented_days == s.coverage.documented_days
    assert s.coverage.total_days == period.days
    assert s.coverage.ratio == pytest.approx(expected.coverage, abs=1e-12)
    assert s.coverage.documented_days == len(ref.documented(DAILY[metric_id], period))
    if expected.mean is None:
        assert s.mean is None and s.first is None
        return
    assert s.mean == pytest.approx(expected.mean, rel=1e-12)
    assert s.median == pytest.approx(expected.median, rel=1e-12)
    assert s.first is not None and s.last is not None and expected.first and expected.last
    assert (s.first.day, s.first.value) == expected.first and (s.last.day, s.last.value) == expected.last
    values = [v for d, v in ref.daily(metric_id).items() if d in period]
    assert (s.min, s.max) == (min(values), max(values))
    if metric_id == "weight.kg":
        assert s.sum is None
        if s.n_observations >= 2:
            assert s.change == pytest.approx(expected.last[1] - expected.first[1], abs=1e-12)
        else:
            assert s.change is None
    else:
        assert s.sum == pytest.approx(sum(values), rel=1e-12) and s.change is None


@pytest.mark.parametrize("metric_id", sorted(DAILY))
def test_daily_points_equal_the_reference(demo: tuple[sqlite3.Connection, int], ref: Reference,
                                          metric_id: str) -> None:
    conn, uid = demo
    period = ref.period(-29, 0)
    expected = ref.daily(metric_id)
    points = daily_points(conn, uid, metric_id, per(period))
    assert [p.day for p in points] == [ref.on(i) for i in range(-29, 1)]
    for p in points:
        assert p.value == expected.get(p.day) and p.documented == (p.day in expected)
        assert len(p.record_ids) == (1 if p.documented else 0)


def test_latest_overall_and_clipping_for_weight(demo: tuple[sqlite3.Connection, int], ref: Reference) -> None:
    conn, uid = demo
    rows = ref.rows("weight_records")
    rp = metrics.resolve_period(conn, uid, "weight.kg", ref.on(-29), ref.on(0), today=ANCHOR)
    assert rp.clipped and rp.requested_start == ref.on(-29) and rp.period.start == rows[0][0]
    s = summarize(conn, uid, "weight.kg", per(ref.period(-29, -10)))
    assert s.latest_overall is not None
    assert (s.latest_overall.day, s.latest_overall.value) == (rows[-1][0], rows[-1][1]["weight_kg"])
    assert s.coverage.earliest_record == rows[0][0] and s.coverage.latest_record == rows[-1][0]


def test_weekly_sleep_groups_equal_a_pure_python_iso_grouping(demo: tuple[sqlite3.Connection, int],
                                                              ref: Reference) -> None:
    conn, uid = demo
    period = ref.period(-29, 0)
    buckets: dict[str, list[float]] = defaultdict(list)
    for d, v in ref.daily("sleep.hours").items():
        if d in period:
            year, week, _ = d.isocalendar()
            buckets[f"{year}-W{week:02d}"].append(v)
    groups = summarize(conn, uid, "sleep.hours", per(period), group_by="week").groups
    assert [g.key for g in groups] == sorted({f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                                              for d in (ref.on(i) for i in range(-29, 1))})
    for g in groups:
        assert g.n == len(buckets[g.key])
        assert g.mean == (pytest.approx(fmean(buckets[g.key]), rel=1e-12) if buckets[g.key] else None)
    months = summarize(conn, uid, "steps.count", per(period), group_by="month").groups
    by_month: dict[str, list[float]] = defaultdict(list)
    for d, v in ref.daily("steps.count").items():
        if d in period:
            by_month[d.isoformat()[:7]].append(v)
    assert {g.key: g.n for g in months} == {k: len(v) for k, v in by_month.items()}


@pytest.mark.parametrize("bounds", PERIODS)
def test_workouts_by_type_and_duration(demo: tuple[sqlite3.Connection, int], ref: Reference,
                                       bounds: tuple[int, int]) -> None:
    conn, uid = demo
    period = ref.period(*bounds)
    s = summarize(conn, uid, "workouts.count", per(period), group_by="subtype")
    assert s.n_observations == ref.count("workouts", period)
    documented = len(ref.documented("workouts", period))
    assert s.coverage.documented_days == documented
    for g in s.groups:
        assert g.count == (ref.count("workouts", period, workout_type=g.key) if documented else None)
    assert [g.key for g in s.groups] == list(WORKOUT_TYPES)
    minutes = [r["duration_minutes"] for d, r in ref.rows("workouts") if d in period and r["duration_minutes"]]
    dur = summarize(conn, uid, "workouts.duration_minutes", per(period))
    assert dur.n_observations == len(minutes)
    if minutes:
        assert dur.sum == sum(minutes) and dur.mean == pytest.approx(fmean(minutes), rel=1e-12)
        assert dur.rate_per_documented_day == pytest.approx(sum(minutes) / documented, rel=1e-12)


@pytest.mark.parametrize("bounds", PERIODS)
def test_tasks_by_category_and_completion_rate(demo: tuple[sqlite3.Connection, int], ref: Reference,
                                               bounds: tuple[int, int]) -> None:
    conn, uid = demo
    period = ref.period(*bounds)
    counts = summarize(conn, uid, "tasks.count", per(period), group_by="subtype")
    assert counts.n_observations == ref.count("tasks", period)
    assert {g.key: g.count for g in counts.groups} == {c: ref.count("tasks", period, category=c) for c in CATEGORIES}
    assert counts.coverage.documented_days == len(ref.documented("tasks", period))
    rates = summarize(conn, uid, "tasks.completion_rate", per(period), group_by="subtype")
    total_done = total_all = 0
    for g in rates.groups:
        done, all_ = ref.completion_rate(g.key, period)
        total_done, total_all = total_done + done, total_all + all_
        assert g.n == all_
        assert g.pct == (pytest.approx(100 * done / all_, rel=1e-12) if all_ else None)
        one = summarize(conn, uid, "tasks.completion_rate", per(period), filters={"category": g.key})
        assert (one.ratio_numerator, one.ratio_denominator) == (done, all_)
    assert (rates.ratio_numerator, rates.ratio_denominator) == (total_done, total_all)
    completed = summarize(conn, uid, "tasks.count", per(period), filters={"status": "completed"})
    assert completed.n_observations == ref.count("tasks", period, status="completed")


def test_completion_rate_ignores_the_planned_tasks_of_the_fixture(demo: tuple[sqlite3.Connection, int],
                                                                  ref: Reference,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    conn, uid = demo
    monkeypatch.setattr(metrics, "today_local", lambda: ANCHOR)
    assert ref.count("tasks", ref.period(1, 14)) > 0  # the fixture has planned tasks
    s = summarize(conn, uid, "tasks.completion_rate", per(ref.period(-29, 14)))
    done = sum(ref.completion_rate(c, ref.period(-29, 0))[0] for c in CATEGORIES)
    all_ = sum(ref.completion_rate(c, ref.period(-29, 0))[1] for c in CATEGORIES)
    assert (s.ratio_numerator, s.ratio_denominator) == (done, all_)


@pytest.mark.parametrize("bounds", PERIODS)
def test_job_applications_by_status_and_progress(demo: tuple[sqlite3.Connection, int], ref: Reference,
                                                 bounds: tuple[int, int]) -> None:
    conn, uid = demo
    period = ref.period(*bounds)
    documented = len(ref.documented("job_applications", period))
    s = summarize(conn, uid, "job_applications.count", per(period), group_by="subtype")
    assert s.n_observations == ref.count("job_applications", period)
    assert {g.key: g.count for g in s.groups} == {
        st: (ref.count("job_applications", period, status=st) if documented else None) for st in JOB_STATUSES}
    progressing = ref.count("job_applications", period, status="interviewing") + \
        ref.count("job_applications", period, status="accepted")
    rate = summarize(conn, uid, "job_applications.progress_rate", per(period))
    assert (rate.ratio_numerator, rate.ratio_denominator) == (progressing, ref.count("job_applications", period))
    for model in ("hybrid", "remote", "onsite"):
        f = summarize(conn, uid, "job_applications.count", per(period), filters={"work_model": model})
        assert f.n_observations == ref.count("job_applications", period, work_model=model)


@pytest.mark.parametrize("bounds", PERIODS)
def test_calories_equal_the_reference(demo: tuple[sqlite3.Connection, int], ref: Reference,
                                      bounds: tuple[int, int]) -> None:
    conn, uid = demo
    period = ref.period(*bounds)
    known = {d: v for d, v in ref.daily_calories(period).items() if v is not None}
    points = daily_points(conn, uid, "calories.daily_total", per(period))
    assert {p.day: p.value for p in points if p.documented} == {d: float(v) for d, v in known.items()}
    s = summarize(conn, uid, "calories.daily_total", per(period))
    assert s.n_observations == s.coverage.documented_days == len(known)
    if known:
        assert s.mean == pytest.approx(fmean(known.values()), rel=1e-12) and s.sum == sum(known.values())
    by_day: dict[date, set[str]] = defaultdict(set)
    for d, r in ref.rows("meals"):
        if d in period and r["calories"]["kcal"] is not None:
            by_day[d].add(r["meal_type"])
    complete = sum(1 for types in by_day.values() if {"breakfast", "lunch", "dinner"} <= types)
    assert (s.calorie_complete_days, s.calorie_partial_days) == (complete, len(by_day) - complete)
    meals_s = summarize(conn, uid, "meals.count", per(period), group_by="subtype")
    assert {g.key: g.n for g in meals_s.groups} == {t: ref.count("meals", period, meal_type=t) for t in MEAL_TYPES}


@pytest.mark.parametrize(("metric_id", "table"), [("articles.count", "articles"), ("podcasts.count", "podcasts"),
                                                  ("movies.count", "movies"), ("series.count", "series"),
                                                  ("meals.count", "meals")])
def test_content_counts_and_coverage(demo: tuple[sqlite3.Connection, int], ref: Reference, metric_id: str,
                                     table: str) -> None:
    conn, uid = demo
    for bounds in PERIODS:
        period = ref.period(*bounds)
        s = summarize(conn, uid, metric_id, per(period))
        documented = ref.documented(table, period)
        assert s.n_observations == ref.count(table, period)
        assert s.coverage.documented_days == len(documented)
        expected_rate: Any = ref.count(table, period) / len(documented) if documented else None
        assert s.rate_per_documented_day == (pytest.approx(expected_rate, rel=1e-12) if documented else None)
        cov = metrics.coverage(conn, uid, metric_id.split(".")[0], per(period))  # type: ignore[arg-type]
        assert cov == s.coverage
        undocumented = [d for d in (ref.on(i) for i in range(bounds[0], bounds[1] + 1)) if d not in documented]
        assert sum((b - a).days + 1 for a, b in cov.undocumented_ranges) == len(undocumented) or cov.truncated
