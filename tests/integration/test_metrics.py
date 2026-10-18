"""B-05 aggregates on hand-made tables built through the B-04 repositories (synthetic data only).

Every test owns a fresh database with the user under test and a decoy user who has records on
the same dates, so a missing `user_id = ?` would change the numbers.
"""

from __future__ import annotations

import dataclasses
import math
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.analytics import metrics
from app.analytics.metrics import REGISTRY, MetricError, Period, RecordRef, daily_points, summarize
from app.db import connect, migrate
from app.records import DOMAINS, meals, tasks
from app.users import get_or_create_user

T = date(2026, 10, 18)  # "today" for record creation and range checks


def d(day: int) -> date:
    """2026-10-<day>."""
    return date(2026, 10, day)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "m.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def uid(conn: sqlite3.Connection) -> int:
    return get_or_create_user(conn, "test-metrics-a", "a@example.com", "A")


@pytest.fixture
def decoy(conn: sqlite3.Connection) -> int:
    return get_or_create_user(conn, "test-metrics-b", "b@example.com", "B")


def add(conn: sqlite3.Connection, user: int, domain: str, **values: Any) -> int:
    module = DOMAINS[domain]
    data = module.SPEC.model.model_validate(values)
    return int(module.create(conn, user, data, today=T))


def add_meal(conn: sqlite3.Connection, user: int, day: date, meal_type: meals.MealType,
             kcal: int | None) -> int:
    estimate = (meals.EstimateIn(calorie_basis="unknown") if kcal is None
                else meals.EstimateIn(estimated_calories=kcal, calorie_basis="rough_estimate"))
    return meals.create(conn, user, meals.MealIn(record_date=day, meal_type=meal_type, description="Test meal"),
                        today=T, estimate=estimate)


def soft_delete(conn: sqlite3.Connection, user: int, domain: str, record_id: int) -> None:
    DOMAINS[domain].soft_delete(conn, user, record_id, confirm="delete")


def P(a: int, b: int) -> Period:
    return Period(d(a), d(b))


def finite(value: object) -> None:
    """No NaN or infinity anywhere in a result."""
    if isinstance(value, float):
        assert math.isfinite(value)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            finite(getattr(value, f.name))
    elif isinstance(value, tuple | list):
        for item in value:
            finite(item)


# ---------------------------------------------------------------- daily values
def test_sleep_mean_over_documented_days_only(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    ids = {1: add(conn, uid, "sleep", record_date=d(1), hours_slept=7.5),
           3: add(conn, uid, "sleep", record_date=d(3), hours_slept=6.0),
           4: add(conn, uid, "sleep", record_date=d(4), hours_slept=8.0)}
    for day in (1, 2, 3, 4):
        add(conn, decoy, "sleep", record_date=d(day), hours_slept=3.0)
    s = summarize(conn, uid, "sleep.hours", P(1, 4))
    assert s.n_observations == 3
    assert s.mean == pytest.approx(21.5 / 3) and s.median == 7.5 and s.sum == 21.5
    assert (s.min, s.max) == (6.0, 8.0)
    assert s.min_record == RecordRef(d(3), 6.0, ids[3]) and s.max_record == RecordRef(d(4), 8.0, ids[4])
    assert s.first == RecordRef(d(1), 7.5, ids[1]) and s.last == RecordRef(d(4), 8.0, ids[4])
    assert s.change is None and s.rate_per_documented_day is None and s.ratio_pct is None
    assert s.calorie_complete_days is None and s.calorie_partial_days is None
    c = s.coverage
    assert (c.documented_days, c.total_days, c.ratio) == (3, 4, 0.75)
    assert c.undocumented_ranges == ((d(2), d(2)),) and not c.truncated
    assert (c.earliest_record, c.latest_record) == (d(1), d(4))
    pts = daily_points(conn, uid, "sleep.hours", P(1, 4))
    assert [(p.day, p.value, p.documented, p.record_ids) for p in pts] == [
        (d(1), 7.5, True, (ids[1],)), (d(2), None, False, ()), (d(3), 6.0, True, (ids[3],)),
        (d(4), 8.0, True, (ids[4],))]


def test_explicit_zero_steps_is_a_value_and_a_missing_day_is_none(conn: sqlite3.Connection, uid: int,
                                                                   decoy: int) -> None:
    add(conn, uid, "steps", record_date=d(1), steps_count=0)
    add(conn, uid, "steps", record_date=d(3), steps_count=5000)
    add(conn, decoy, "steps", record_date=d(2), steps_count=9999)
    s = summarize(conn, uid, "steps.count", P(1, 3))
    assert (s.n_observations, s.mean, s.min, s.sum) == (2, 2500.0, 0.0, 5000.0)
    assert s.coverage.documented_days == 2
    pts = daily_points(conn, uid, "steps.count", P(1, 3))
    assert [(p.value, p.documented) for p in pts] == [(0.0, True), (None, False), (5000.0, True)]


def test_weight_endpoints_change_and_latest_overall(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    first = add(conn, uid, "weight", record_date=d(2), weight_kg=72.4)
    add(conn, uid, "weight", record_date=d(5), weight_kg=72.0)
    last = add(conn, uid, "weight", record_date=d(9), weight_kg=71.5)
    later = add(conn, uid, "weight", record_date=d(15), weight_kg=71.0)
    add(conn, decoy, "weight", record_date=d(16), weight_kg=99.0)
    s = summarize(conn, uid, "weight.kg", P(1, 10))
    assert s.first == RecordRef(d(2), 72.4, first) and s.last == RecordRef(d(9), 71.5, last)
    assert s.change == pytest.approx(71.5 - 72.4)
    assert s.sum is None  # a sum of body weights means nothing
    assert s.latest_overall == RecordRef(d(15), 71.0, later)  # all history, not only the period
    one = summarize(conn, uid, "weight.kg", P(1, 3))
    assert one.n_observations == 1 and one.change is None and one.first == one.last
    none = summarize(conn, uid, "weight.kg", P(10, 12))
    assert none.n_observations == 0 and none.change is None and none.first is None
    assert none.latest_overall == RecordRef(d(15), 71.0, later)


# ---------------------------------------------------------------- calories
def test_calories_complete_partial_and_unknown_days(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    complete = [add_meal(conn, uid, d(1), "breakfast", 300), add_meal(conn, uid, d(1), "lunch", 500),
                add_meal(conn, uid, d(1), "dinner", 700)]
    partial = [add_meal(conn, uid, d(2), "lunch", 400), add_meal(conn, uid, d(2), "dinner", None)]
    add_meal(conn, uid, d(3), "lunch", None)  # every meal unknown: no total, not a calorie day
    for day in (1, 2, 3, 4):
        add_meal(conn, decoy, d(day), "lunch", 5000)
    s = summarize(conn, uid, "calories.daily_total", P(1, 4))
    assert (s.n_observations, s.mean, s.sum) == (2, 950.0, 1900.0)
    assert (s.calorie_complete_days, s.calorie_partial_days) == (1, 1)
    assert s.coverage.documented_days == 2 and s.coverage.undocumented_ranges == ((d(3), d(4)),)
    assert (s.coverage.earliest_record, s.coverage.latest_record) == (d(1), d(2))
    assert s.min_record == RecordRef(d(2), 400.0, min(partial))
    assert s.latest_overall == RecordRef(d(2), 400.0, min(partial))
    pts = daily_points(conn, uid, "calories.daily_total", P(1, 4))
    assert [(p.value, p.documented) for p in pts] == [(1500.0, True), (400.0, True), (None, False),
                                                      (None, False)]
    assert pts[0].record_ids == tuple(complete) and pts[1].record_ids == tuple(partial)
    # the meal domain itself documents day 3
    assert summarize(conn, uid, "meals.count", P(1, 4)).coverage.documented_days == 3


def test_calories_use_the_newest_estimate_and_skip_soft_deleted_meals(conn: sqlite3.Connection, uid: int) -> None:
    lunch = add_meal(conn, uid, d(1), "lunch", 500)
    meals.add_estimate(conn, uid, lunch, meals.EstimateIn(estimated_calories=650, calorie_basis="user_provided"))
    dinner = add_meal(conn, uid, d(1), "dinner", 900)
    assert daily_points(conn, uid, "calories.daily_total", P(1, 1))[0].value == 1550.0
    soft_delete(conn, uid, "meals", dinner)
    pt = daily_points(conn, uid, "calories.daily_total", P(1, 1))[0]
    assert (pt.value, pt.record_ids) == (650.0, (lunch,))


# ---------------------------------------------------------------- events
def test_meal_counts_by_type_with_documented_day_denominators(conn: sqlite3.Connection, uid: int,
                                                               decoy: int) -> None:
    add_meal(conn, uid, d(1), "breakfast", 300)
    add_meal(conn, uid, d(1), "lunch", 500)
    add_meal(conn, uid, d(2), "dinner", None)
    add_meal(conn, decoy, d(3), "lunch", 100)
    s = summarize(conn, uid, "meals.count", P(1, 4), group_by="subtype")
    assert (s.n_observations, s.sum, s.rate_per_documented_day) == (3, 3.0, 1.5)
    assert s.mean is None and s.median is None and s.min_record is None and s.first is None
    assert s.latest_overall is None
    groups = {g.key: g for g in s.groups}
    assert list(groups) == ["breakfast", "lunch", "dinner", "night", "snacks"]
    assert (groups["lunch"].count, groups["lunch"].documented_days, groups["lunch"].rate_per_documented_day) == \
        (1, 2, 0.5)
    assert groups["night"].count == 0 and groups["night"].n == 0  # 0 on documented meal days
    lunch = daily_points(conn, uid, "meals.count", P(1, 4), {"meal_type": "lunch"})
    assert [(p.value, p.documented) for p in lunch] == [(1.0, True), (0.0, True), (None, False), (None, False)]
    only = summarize(conn, uid, "meals.count", P(1, 4), group_by="subtype", filters={"meal_type": "lunch"})
    assert [g.key for g in only.groups] == ["lunch"] and only.n_observations == 1
    empty = summarize(conn, uid, "meals.count", P(3, 4))
    assert (empty.n_observations, empty.sum, empty.rate_per_documented_day) == (0, None, None)


def test_workout_counts_and_durations(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    run = add(conn, uid, "workouts", record_date=d(1), workout_type="aerobic", duration_minutes=30)
    add(conn, uid, "workouts", record_date=d(1), workout_type="weights", duration_minutes=45)
    add(conn, uid, "workouts", record_date=d(2), workout_type="weights")  # no duration
    add(conn, uid, "workouts", record_date=d(4), workout_type="aerobic", duration_minutes=50)
    add(conn, decoy, "workouts", record_date=d(2), workout_type="aerobic", duration_minutes=500)
    count = summarize(conn, uid, "workouts.count", P(1, 4), group_by="subtype")
    assert (count.n_observations, count.rate_per_documented_day) == (4, 4 / 3)
    assert {g.key: g.count for g in count.groups} == {"aerobic": 2, "weights": 2, "bodyweight": 0,
                                                      "stick_spinning": 0}
    dur = summarize(conn, uid, "workouts.duration_minutes", P(1, 4))
    assert (dur.n_observations, dur.sum, dur.mean, dur.median) == (3, 125.0, 125 / 3, 45.0)
    assert dur.rate_per_documented_day == pytest.approx(125 / 3)  # minutes per documented workout day
    assert dur.min_record == RecordRef(d(1), 30.0, run)
    pts = daily_points(conn, uid, "workouts.duration_minutes", P(1, 4))
    assert [p.value for p in pts] == [75.0, None, None, 50.0]  # day 2: a workout without a duration
    aerobic = daily_points(conn, uid, "workouts.duration_minutes", P(1, 4), {"workout_type": "aerobic"})
    assert [(p.value, p.documented) for p in aerobic] == [(30.0, True), (0.0, True), (None, False), (50.0, True)]
    by_type = summarize(conn, uid, "workouts.duration_minutes", P(1, 4), group_by="subtype")
    assert {g.key: g.mean for g in by_type.groups}["aerobic"] == 40.0
    latest = summarize(conn, uid, "workouts.duration_minutes", P(1, 2), filters={"workout_type": "weights"})
    assert latest.latest_overall is not None and latest.latest_overall.value == 45.0


def test_content_and_podcast_durations(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    add(conn, uid, "articles", record_date=d(1), title="One")
    add(conn, uid, "articles", record_date=d(1), title="Two")
    add(conn, uid, "movies", record_date=d(2), title="A film")
    add(conn, uid, "series", record_date=d(3), series_name="Show", season_number=1, episode_number=1)
    add(conn, uid, "podcasts", record_date=d(1), podcast_name="Cast", episode_title="1", duration_minutes=40)
    add(conn, uid, "podcasts", record_date=d(3), podcast_name="Cast", episode_title="2")
    add(conn, decoy, "articles", record_date=d(2), title="Decoy")
    a = summarize(conn, uid, "articles.count", P(1, 3))
    assert (a.n_observations, a.coverage.documented_days, a.rate_per_documented_day) == (2, 1, 2.0)
    assert summarize(conn, uid, "movies.count", P(1, 3)).n_observations == 1
    assert summarize(conn, uid, "series.count", P(1, 3)).n_observations == 1
    assert summarize(conn, uid, "podcasts.count", P(1, 3)).n_observations == 2
    p = summarize(conn, uid, "podcasts.duration_minutes", P(1, 3))
    assert (p.n_observations, p.sum, p.coverage.documented_days, p.rate_per_documented_day) == (1, 40.0, 2, 20.0)


# ---------------------------------------------------------------- tasks and applications
def test_task_counts_by_category_and_status(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    add(conn, uid, "tasks", task_date=d(1), title="Call", category="career", status="completed")
    add(conn, uid, "tasks", task_date=d(1), title="Visit", category="family")
    add(conn, uid, "tasks", task_date=d(2), title="Email", category="career")
    add(conn, decoy, "tasks", task_date=d(1), title="Decoy", category="career", status="completed")
    s = summarize(conn, uid, "tasks.count", P(1, 2), group_by="subtype")
    assert s.n_observations == 3
    assert {g.key: g.count for g in s.groups} == {"family": 1, "career": 2, "friends": 0, "housework": 0,
                                                  "other": 0}
    done = summarize(conn, uid, "tasks.count", P(1, 2), group_by="subtype", filters={"status": "completed"})
    assert {g.key: g.count for g in done.groups}["career"] == 1 and done.n_observations == 1


def test_completion_rate_excludes_future_tasks(conn: sqlite3.Connection, uid: int, decoy: int,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "today_local", lambda: T)
    add(conn, uid, "tasks", task_date=d(16), title="A", category="career", status="completed")
    add(conn, uid, "tasks", task_date=d(17), title="B", category="career")
    add(conn, uid, "tasks", task_date=d(18), title="C", category="family", status="completed")
    add(conn, uid, "tasks", task_date=d(20), title="Planned", category="career", status="completed")
    add(conn, uid, "tasks", task_date=d(25), title="Planned 2", category="family")
    add(conn, decoy, "tasks", task_date=d(17), title="Decoy", category="career", status="completed")
    hand_built = Period(d(16), d(25))  # extends past today: planned tasks must not count
    s = summarize(conn, uid, "tasks.completion_rate", hand_built, group_by="subtype")
    assert (s.ratio_numerator, s.ratio_denominator, s.n_observations) == (2, 3, 3)
    assert s.ratio_pct == pytest.approx(200 / 3)
    assert s.mean is None and s.sum is None and s.rate_per_documented_day is None
    assert {g.key: g.pct for g in s.groups} == {"family": 100.0, "career": 50.0, "friends": None,
                                                "housework": None, "other": None}
    assert s.coverage.documented_days == 3
    pts = daily_points(conn, uid, "tasks.completion_rate", hand_built)
    assert [p.value for p in pts[:3]] == [100.0, 0.0, 100.0] and all(p.value is None for p in pts[3:])
    career = summarize(conn, uid, "tasks.completion_rate", hand_built, filters={"category": "career"})
    assert (career.ratio_numerator, career.ratio_denominator, career.ratio_pct) == (1, 2, 50.0)
    # the count metric does see planned tasks in a hand-built period; resolve_period forbids it
    assert summarize(conn, uid, "tasks.count", hand_built).n_observations == 5
    with pytest.raises(MetricError) as err:
        metrics.resolve_period(conn, uid, "tasks.completion_rate", d(16), d(25), today=T)
    assert err.value.code == "range_in_future"
    empty = summarize(conn, uid, "tasks.completion_rate", P(1, 5))
    assert (empty.ratio_numerator, empty.ratio_denominator, empty.ratio_pct) == (0, 0, None)


def test_application_status_ratio_and_filters(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    for i, (status, model) in enumerate([("applied", "remote"), ("interviewing", "hybrid"), ("accepted", "remote"),
                                         ("rejected", "onsite"), ("rejected", "remote")]):
        add(conn, uid, "job_applications", application_date=d(1 + i % 2), company_name=f"Example {i}",
            job_title="Analyst", company_location="Tel Aviv", industry="Software", status=status, work_model=model)
    add(conn, decoy, "job_applications", application_date=d(1), company_name="Decoy", job_title="Analyst",
        company_location="Haifa", industry="Software", status="accepted")
    count = summarize(conn, uid, "job_applications.count", P(1, 3), group_by="subtype")
    assert {g.key: g.count for g in count.groups} == {"applied": 1, "interviewing": 1, "accepted": 1, "rejected": 2}
    assert count.rate_per_documented_day == 2.5
    rate = summarize(conn, uid, "job_applications.progress_rate", P(1, 3))
    assert (rate.ratio_numerator, rate.ratio_denominator, rate.ratio_pct) == (2, 5, 40.0)
    remote = summarize(conn, uid, "job_applications.progress_rate", P(1, 3), filters={"work_model": "remote"})
    assert (remote.ratio_numerator, remote.ratio_denominator) == (1, 3)
    open_remote = summarize(conn, uid, "job_applications.count", P(1, 3),
                            filters={"status": "applied", "work_model": "remote"})
    assert open_remote.n_observations == 1


# ---------------------------------------------------------------- soft delete
def test_soft_deleted_rows_are_excluded_everywhere(conn: sqlite3.Connection, uid: int,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "today_local", lambda: T)
    early = add(conn, uid, "sleep", record_date=d(1), hours_slept=4.0)
    add(conn, uid, "sleep", record_date=d(3), hours_slept=8.0)
    late = add(conn, uid, "sleep", record_date=d(5), hours_slept=5.0)
    task = add(conn, uid, "tasks", task_date=d(3), title="Gone", category="other")
    add(conn, uid, "tasks", task_date=d(3), title="Kept", category="other", status="completed")
    for rid in (early, late):
        soft_delete(conn, uid, "sleep", rid)
    soft_delete(conn, uid, "tasks", task)
    s = summarize(conn, uid, "sleep.hours", P(1, 5))
    assert (s.n_observations, s.mean, s.coverage.documented_days) == (1, 8.0, 1)
    assert (s.coverage.earliest_record, s.coverage.latest_record) == (d(3), d(3))
    assert s.latest_overall is not None and s.latest_overall.day == d(3)
    assert daily_points(conn, uid, "sleep.hours", P(1, 1))[0].value is None
    rp = metrics.resolve_period(conn, uid, "sleep.hours", d(1), d(5), today=T)
    assert rp.period.start == d(3)  # clipped to the earliest *live* record
    rate = summarize(conn, uid, "tasks.completion_rate", P(1, 5))
    assert (rate.ratio_numerator, rate.ratio_denominator) == (1, 1)
    assert metrics.coverage(conn, uid, "sleep", P(1, 5)).documented_days == 1


# ---------------------------------------------------------------- isolation
def _populate(conn: sqlite3.Connection, user: int, scale: float) -> None:
    for day in (1, 2, 4):
        add(conn, user, "sleep", record_date=d(day), hours_slept=round(6 * scale, 2))
        add(conn, user, "steps", record_date=d(day), steps_count=int(4000 * scale))
        add(conn, user, "weight", record_date=d(day), weight_kg=70 * scale)
        add_meal(conn, user, d(day), "lunch", int(500 * scale))
        add(conn, user, "workouts", record_date=d(day), workout_type="aerobic", duration_minutes=int(20 * scale))
        add(conn, user, "articles", record_date=d(day), title="Read")
        add(conn, user, "podcasts", record_date=d(day), podcast_name="Cast", duration_minutes=int(30 * scale))
        add(conn, user, "movies", record_date=d(day), title="Film")
        add(conn, user, "series", record_date=d(day), series_name="Show")
        add(conn, user, "tasks", task_date=d(day), title="Task", category="career",
            status="completed" if scale > 1 else "pending")
        add(conn, user, "job_applications", application_date=d(day), company_name="Example", job_title=f"Role {day}",
            company_location="Tel Aviv", industry="Software", status="accepted" if scale > 1 else "applied")


def _snapshot(conn: sqlite3.Connection, user: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric_id, m in REGISTRY.items():
        out[metric_id] = (summarize(conn, user, metric_id, P(1, 5), group_by="week"),
                          daily_points(conn, user, metric_id, P(1, 5)))
        if m.subtype_field:
            out[metric_id + "/subtype"] = summarize(conn, user, metric_id, P(1, 5), group_by="subtype")
    for domain in metrics.DOMAINS:
        out[domain] = metrics.coverage(conn, user, domain, P(1, 5))  # type: ignore[arg-type]
    return out


def test_two_users_on_the_same_dates_are_isolated(conn: sqlite3.Connection, uid: int, decoy: int,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "today_local", lambda: T)
    _populate(conn, uid, 1.0)
    before = _snapshot(conn, uid)
    _populate(conn, decoy, 1.5)  # different values, same dates, plus an extra day
    add(conn, decoy, "sleep", record_date=d(3), hours_slept=9.0)
    assert _snapshot(conn, uid) == before
    assert before["sleep.hours"][0].mean == 6.0 and before["tasks.completion_rate"][0].ratio_pct == 0.0
    decoy_view = _snapshot(conn, decoy)
    assert decoy_view["sleep.hours"][0].n_observations == 4
    assert decoy_view["tasks.completion_rate"][0].ratio_pct == 100.0


# ---------------------------------------------------------------- empty input, NaN
@pytest.mark.parametrize("metric_id", sorted(REGISTRY))
def test_empty_input_gives_none_statistics(conn: sqlite3.Connection, uid: int, decoy: int, metric_id: str) -> None:
    _populate(conn, decoy, 1.0)  # only the decoy has data
    m = REGISTRY[metric_id]
    s = summarize(conn, uid, metric_id, P(1, 5), group_by="subtype" if m.subtype_field else "month")
    assert s.n_observations == 0
    assert (s.mean, s.median, s.min, s.max, s.sum, s.rate_per_documented_day, s.change) == (None,) * 7
    assert s.min_record is None and s.max_record is None and s.first is None and s.last is None
    assert s.latest_overall is None and s.ratio_pct is None
    assert (s.coverage.documented_days, s.coverage.ratio, s.coverage.earliest_record) == (0, 0.0, None)
    assert s.coverage.undocumented_ranges == ((d(1), d(5)),)
    assert all(g.n == 0 and g.mean is None and g.count is None and g.pct is None for g in s.groups)
    assert all(p.value is None and not p.documented for p in daily_points(conn, uid, metric_id, P(1, 5)))
    finite(s)


@pytest.mark.parametrize("metric_id", sorted(REGISTRY))
def test_results_are_finite_on_data(conn: sqlite3.Connection, uid: int, metric_id: str,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "today_local", lambda: T)
    _populate(conn, uid, 1.0)
    for group_by in ("none", "week", "month"):
        finite(summarize(conn, uid, metric_id, P(1, 31), group_by=group_by))
    finite(daily_points(conn, uid, metric_id, P(1, 31)))
    assert len(daily_points(conn, uid, metric_id, P(1, 31))) == 31


# ---------------------------------------------------------------- grouping
def test_iso_week_and_month_keys_across_a_year_boundary(conn: sqlite3.Connection, uid: int) -> None:
    add(conn, uid, "sleep", record_date=date(2020, 12, 31), hours_slept=7.0)
    add(conn, uid, "sleep", record_date=date(2021, 1, 3), hours_slept=8.0)
    add(conn, uid, "sleep", record_date=date(2021, 1, 5), hours_slept=6.0)
    period = Period(date(2020, 12, 28), date(2021, 1, 12))
    weeks = summarize(conn, uid, "sleep.hours", period, group_by="week").groups
    assert [(g.key, g.n, g.documented_days, g.mean) for g in weeks] == [
        ("2020-W53", 2, 2, 7.5), ("2021-W01", 1, 1, 6.0), ("2021-W02", 0, 0, None)]
    months = summarize(conn, uid, "sleep.hours", period, group_by="month").groups
    assert [(g.key, g.n, g.mean) for g in months] == [("2020-12", 1, 7.0), ("2021-01", 2, 7.0)]


def test_week_groups_of_event_counts_use_each_weeks_documented_days(conn: sqlite3.Connection, uid: int) -> None:
    add(conn, uid, "articles", record_date=d(5), title="Mon")  # 2026-10-05 is a Monday (W41)
    add(conn, uid, "articles", record_date=d(5), title="Mon 2")
    add(conn, uid, "articles", record_date=d(7), title="Wed")
    groups = summarize(conn, uid, "articles.count", Period(d(3), d(13)), group_by="week").groups
    assert [(g.key, g.count, g.documented_days, g.rate_per_documented_day) for g in groups] == [
        ("2026-W40", None, 0, None), ("2026-W41", 3, 2, 1.5), ("2026-W42", None, 0, None)]


def test_groups_are_capped_at_twenty(conn: sqlite3.Connection, uid: int) -> None:
    year = Period(date(2025, 10, 18), date(2026, 10, 18))  # 366 days
    weeks = summarize(conn, uid, "steps.count", year, group_by="week")
    assert len(weeks.groups) == 20 and weeks.groups_truncated
    assert weeks.groups[0].key == "2025-W42"  # oldest first
    months = summarize(conn, uid, "steps.count", year, group_by="month")
    assert len(months.groups) == 13 and not months.groups_truncated
    assert summarize(conn, uid, "steps.count", year).groups == ()


# ---------------------------------------------------------------- coverage
def test_undocumented_ranges_are_capped_at_ten(conn: sqlite3.Connection, uid: int) -> None:
    for day in range(2, 26, 2):
        add(conn, uid, "steps", record_date=date(2026, 9, day), steps_count=1000)
    c = metrics.coverage(conn, uid, "steps", Period(date(2026, 9, 1), date(2026, 9, 25)))
    assert c.documented_days == 12 and c.total_days == 25 and c.ratio == 12 / 25
    assert len(c.undocumented_ranges) == 10 and c.truncated
    assert c.undocumented_ranges[0] == (date(2026, 9, 1), date(2026, 9, 1))
    assert c.undocumented_ranges[9] == (date(2026, 9, 19), date(2026, 9, 19))
    small = metrics.coverage(conn, uid, "steps", Period(date(2026, 9, 2), date(2026, 9, 8)))
    assert small.undocumented_ranges == tuple((date(2026, 9, i), date(2026, 9, i)) for i in (3, 5, 7))
    assert not small.truncated
    assert (small.earliest_record, small.latest_record) == (date(2026, 9, 2), date(2026, 9, 24))


def test_coverage_counts_a_day_once_and_rejects_unknown_domains(conn: sqlite3.Connection, uid: int) -> None:
    add(conn, uid, "tasks", task_date=d(1), title="A", category="other")
    add(conn, uid, "tasks", task_date=d(1), title="B", category="other")
    assert metrics.coverage(conn, uid, "tasks", P(1, 2)).documented_days == 1
    for bad in ("job_reports", "industry_trends", "calories", "", "tasks; DROP TABLE tasks"):
        with pytest.raises(MetricError) as err:
            metrics.coverage(conn, uid, bad, P(1, 2))  # type: ignore[arg-type]
        assert err.value.code == "unknown_domain"


# ---------------------------------------------------------------- ranges and clipping
def test_resolve_period_validates_and_clips(conn: sqlite3.Connection, uid: int, decoy: int) -> None:
    add(conn, decoy, "sleep", record_date=d(1), hours_slept=7.0)  # the decoy's earlier record is ignored
    add(conn, uid, "sleep", record_date=d(5), hours_slept=7.0)
    rp = metrics.resolve_period(conn, uid, "sleep.hours", d(1), d(10), today=T)
    assert rp.period == Period(d(5), d(10)) and rp.requested_start == d(1) and rp.clipped
    same = metrics.resolve_period(conn, uid, "sleep.hours", d(6), d(10), today=T)
    assert same.period == Period(d(6), d(10)) and not same.clipped and same.requested_start == d(6)
    before = metrics.resolve_period(conn, uid, "sleep.hours", d(1), d(3), today=T)  # earliest is after end
    assert before.period == Period(d(1), d(3)) and not before.clipped
    nothing = metrics.resolve_period(conn, uid, "movies.count", d(1), d(3), today=T)
    assert not nothing.clipped
    full = metrics.resolve_period(conn, uid, "steps.count", T - timedelta(days=365), T, today=T)
    assert (full.period.end - full.period.start).days + 1 == 366


def test_calorie_clipping_uses_the_first_day_with_a_known_total(conn: sqlite3.Connection, uid: int) -> None:
    add_meal(conn, uid, d(2), "lunch", None)
    add_meal(conn, uid, d(4), "lunch", 500)
    assert metrics.resolve_period(conn, uid, "calories.daily_total", d(1), d(9), today=T).period.start == d(4)
    assert metrics.resolve_period(conn, uid, "meals.count", d(1), d(9), today=T).period.start == d(2)


@pytest.mark.parametrize(("start", "end", "code"), [
    (d(5), d(4), "invalid_range"),
    (d(1), T + timedelta(days=1), "range_in_future"),
    (T - timedelta(days=366), T, "range_too_long"),
])
def test_resolve_period_rejects_bad_ranges(conn: sqlite3.Connection, uid: int, start: date, end: date,
                                           code: str) -> None:
    with pytest.raises(MetricError) as err:
        metrics.resolve_period(conn, uid, "sleep.hours", start, end, today=T)
    assert err.value.code == code


def test_functions_reject_invalid_periods(conn: sqlite3.Connection, uid: int) -> None:
    from datetime import datetime
    bad: list[tuple[Any, str]] = [
        (Period(d(5), d(4)), "invalid_range"),
        (Period(date(2025, 1, 1), date(2026, 1, 2)), "range_too_long"),
        (Period(datetime(2026, 10, 1, 12), d(4)), "invalid_range"),
        ((d(1), d(2)), "invalid_range"),
    ]
    for period, code in bad:
        for call in (lambda p: summarize(conn, uid, "sleep.hours", p),
                     lambda p: daily_points(conn, uid, "sleep.hours", p),
                     lambda p: metrics.coverage(conn, uid, "sleep", p)):
            with pytest.raises(MetricError) as err:
                call(period)
            assert err.value.code == code
    with pytest.raises(MetricError) as err:
        metrics.resolve_period(conn, uid, "sleep.hours", "2026-10-01", d(2), today=T)  # type: ignore[arg-type]
    assert err.value.code == "invalid_range"


# ---------------------------------------------------------------- unknown metric, filter, group_by
@pytest.mark.parametrize("metric_id", ["mood.score", "", "sleep", "SLEEP.HOURS", "job_recommendations.count",
                                       "sleep.hours' OR 1=1 --"])
def test_unknown_metric(conn: sqlite3.Connection, uid: int, metric_id: str) -> None:
    for call in (lambda: summarize(conn, uid, metric_id, P(1, 2)),
                 lambda: daily_points(conn, uid, metric_id, P(1, 2)),
                 lambda: metrics.resolve_period(conn, uid, metric_id, d(1), d(2), today=T)):
        with pytest.raises(MetricError) as err:
            call()
        assert err.value.code == "unknown_metric"
        assert metric_id not in err.value.message or not metric_id


@pytest.mark.parametrize(("metric_id", "filters", "code"), [
    ("sleep.hours", {"meal_type": "lunch"}, "unknown_filter"),
    ("tasks.count", {"user_id": "2"}, "unknown_filter"),
    ("job_applications.count", {"industry": "Software"}, "unknown_filter"),  # free text, not an enum
    ("tasks.completion_rate", {"status": "completed"}, "unknown_filter"),
    ("meals.count", {"meal_type": "brunch"}, "invalid_filter_value"),
    ("tasks.count", {"status": "applied"}, "invalid_filter_value"),  # a job status, not a task status
    ("job_applications.count", {"work_model": "Remote"}, "invalid_filter_value"),
    ("workouts.count", {"workout_type": "aerobic' OR '1'='1"}, "invalid_filter_value"),
    ("workouts.count", {"workout_type": None}, "invalid_filter_value"),
])
def test_filter_validation(conn: sqlite3.Connection, uid: int, metric_id: str, filters: dict[str, Any],
                           code: str) -> None:
    for call in (lambda: summarize(conn, uid, metric_id, P(1, 2), filters=filters),
                 lambda: daily_points(conn, uid, metric_id, P(1, 2), filters)):
        with pytest.raises(MetricError) as err:
            call()
        assert err.value.code == code


def test_group_by_validation(conn: sqlite3.Connection, uid: int) -> None:
    with pytest.raises(MetricError) as err:
        summarize(conn, uid, "sleep.hours", P(1, 2), group_by="day")  # type: ignore[arg-type]
    assert err.value.code == "invalid_group_by"
    for metric_id in ("sleep.hours", "calories.daily_total", "articles.count", "job_applications.progress_rate"):
        with pytest.raises(MetricError) as err:
            summarize(conn, uid, metric_id, P(1, 2), group_by="subtype")
        assert err.value.code == "group_by_unsupported"


def test_summary_reports_the_given_period_unclipped(conn: sqlite3.Connection, uid: int) -> None:
    s = summarize(conn, uid, "sleep.hours", P(1, 2))
    assert s.metric == "sleep.hours" and s.period.period == P(1, 2)
    assert s.period.requested_start == d(1) and not s.period.clipped


def test_tasks_module_constants_match_the_registry() -> None:
    assert REGISTRY["tasks.count"].allowed_filters["category"] == frozenset(tasks.CATEGORIES)
