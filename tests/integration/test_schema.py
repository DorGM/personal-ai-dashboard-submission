from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app import canonical
from app.db import connect, migrate, transaction
from app.deletion import permanently_delete
from app.demo_seed import seed

D = "2026-10-10"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "t.db")
    migrate(c)
    yield c
    c.close()


def user(conn: sqlite3.Connection, sub: str = "sub-1") -> int:
    uid = conn.execute("INSERT INTO users (google_sub, email) VALUES (?, ?)", (sub, f"{sub}@x.com")).lastrowid
    assert uid is not None
    return uid


def meal(conn: sqlite3.Connection, uid: int, mtype: str = "lunch", day: str = D) -> int:
    mid = conn.execute(
        "INSERT INTO meals (user_id, record_date, meal_type, description) VALUES (?, ?, ?, 'food')",
        (uid, day, mtype),
    ).lastrowid
    assert mid is not None
    return mid


def estimate(conn: sqlite3.Connection, uid: int, mid: int, kcal: int | None, basis: str = "rough_estimate") -> None:
    conn.execute(
        "INSERT INTO meal_calorie_estimates (user_id, meal_id, estimated_calories, calorie_basis)"
        " VALUES (?, ?, ?, ?)",
        (uid, mid, kcal, basis),
    )


def test_migrations_are_idempotent_and_recorded(conn: sqlite3.Connection) -> None:
    assert migrate(conn) == []
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [
        "001_init", "002_soft_delete"]


def test_seventeen_domain_tables_plus_view(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables -= {"schema_migrations", "deletion_grants"}
    assert len(tables) == 17
    assert "daily_calories" not in tables
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert views == {"daily_calories"}


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sleep_records (user_id, record_date, hours_slept) VALUES (999, ?, 7)", (D,))




def test_meal_unique_per_type_per_day(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    meal(conn, uid, "lunch")
    meal(conn, uid, "dinner")
    with pytest.raises(sqlite3.IntegrityError):
        meal(conn, uid, "lunch")


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("INSERT INTO weight_records (user_id, record_date, weight_kg) VALUES (?, ?, ?)", (D, 0)),
        ("INSERT INTO weight_records (user_id, record_date, weight_kg) VALUES (?, ?, ?)", ("2026-02-30", 70)),
        ("INSERT INTO weight_records (user_id, record_date, weight_kg) VALUES (?, ?, ?)", ("10.10.2026", 70)),
        ("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, ?)", (D, -1)),
        ("INSERT INTO sleep_records (user_id, record_date, hours_slept) VALUES (?, ?, ?)", (D, 25)),
        ("INSERT INTO meals (user_id, record_date, meal_type, description) VALUES (?, ?, ?, 'x')", (D, "brunch")),
        ("INSERT INTO workouts (user_id, record_date, workout_type, intensity) VALUES (?, ?, ?, 'fast')",
         (D, "aerobic")),
        ("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, ?)", (D, "twelve")),
    ],
)
def test_check_constraints(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> None:
    uid = user(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (uid, *params))


def test_explicit_zero_steps_allowed(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    conn.execute("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, 0)", (uid, D))


def test_stick_spinning_intensity_allowed(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    conn.execute(
        "INSERT INTO workouts (user_id, record_date, workout_type, intensity) VALUES (?, ?, 'stick_spinning', 'fast')",
        (uid, D),
    )


def test_calorie_estimates_append_only(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    mid = meal(conn, uid)
    estimate(conn, uid, mid, 500)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE meal_calorie_estimates SET estimated_calories = 1 WHERE meal_id = ?", (mid,))


def test_unknown_basis_cannot_carry_calories(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    mid = meal(conn, uid)
    with pytest.raises(sqlite3.IntegrityError):
        estimate(conn, uid, mid, 300, "unknown")


def test_estimate_cannot_point_at_other_users_meal(conn: sqlite3.Connection) -> None:
    a, b = user(conn), user(conn, "sub-2")
    mid = meal(conn, a)
    with pytest.raises(sqlite3.IntegrityError):
        estimate(conn, b, mid, 100)


def test_daily_calories_view_uses_latest_estimate_and_coverage(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    b, lunch = meal(conn, uid, "breakfast"), meal(conn, uid, "lunch")
    estimate(conn, uid, b, 300)
    estimate(conn, uid, lunch, None, "unknown")

    def row() -> sqlite3.Row:
        sql = "SELECT * FROM daily_calories WHERE user_id = ? AND record_date = ?"
        r: sqlite3.Row | None = conn.execute(sql, (uid, D)).fetchone()
        assert r is not None
        return r

    assert (row()["total_calories"], row()["coverage_status"]) == (300, "estimated_partial")
    estimate(conn, uid, b, 350)  # a meal edit adds a new row
    estimate(conn, uid, lunch, 600)
    assert (row()["total_calories"], row()["coverage_status"]) == (950, "estimated_partial")
    d = meal(conn, uid, "dinner")
    estimate(conn, uid, d, 700)
    assert (row()["total_calories"], row()["coverage_status"]) == (1650, "estimated_complete")
    with transaction(conn):
        permanently_delete(conn, uid, "meals", d)
    assert row()["total_calories"] == 950
    assert conn.execute("SELECT COUNT(*) FROM meal_calorie_estimates WHERE meal_id = ?", (d,)).fetchone()[0] == 0


def test_daily_calories_unknown_when_no_calories(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    estimate(conn, uid, meal(conn, uid), None, "unknown")
    r = conn.execute("SELECT total_calories, coverage_status FROM daily_calories").fetchone()
    assert (r[0], r[1]) == (None, "unknown")


def test_content_identity_with_missing_url_still_unique(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    sql = "INSERT INTO articles (user_id, record_date, title, source, canonical_key) VALUES (?, ?, ?, NULL, ?)"
    conn.execute(sql, (uid, D, "Same", canonical.article_key("Same", None, None)))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (uid, D, " same ", canonical.article_key(" same ", None, None)))
    conn.execute(sql, (uid, D, "Other", canonical.article_key("Other", None, None)))  # distinct items OK


def test_series_episodes_are_distinct_rows(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    sql = ("INSERT INTO series (user_id, record_date, series_name, season_number, episode_number, canonical_key)"
           " VALUES (?, ?, 'Show', 1, ?, ?)")
    conn.execute(sql, (uid, D, 1, canonical.series_key("Show", 1, 1)))
    conn.execute(sql, (uid, D, 2, canonical.series_key("Show", 1, 2)))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (uid, D, 2, canonical.series_key("Show", 1, 2)))


def report(conn: sqlite3.Connection, uid: int, n: int, day: str = D) -> int:
    rid = conn.execute(
        "INSERT INTO job_reports (user_id, report_date, run_number, status) VALUES (?, ?, ?, 'running')",
        (uid, day, n),
    ).lastrowid
    assert rid is not None
    return rid


def test_run_now_capped_at_three_per_day(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    for n in (1, 2, 3):
        report(conn, uid, n)
    with pytest.raises(sqlite3.IntegrityError):
        report(conn, uid, 4)
    with pytest.raises(sqlite3.IntegrityError):
        report(conn, uid, 3)
    report(conn, uid, 1, "2026-10-11")  # next day resets


def test_manual_and_intake_tables_are_not_capped(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    for i in range(10):
        conn.execute(
            "INSERT INTO tasks (user_id, task_date, title, category, canonical_key) VALUES (?, ?, ?, 'other', ?)",
            (uid, D, f"t{i}", canonical.task_key(None, f"t{i}")),
        )


def rec(conn: sqlite3.Connection, uid: int, rid: int, key: str) -> None:
    conn.execute(
        "INSERT INTO job_recommendations (user_id, report_id, company_name, job_title, location, work_model,"
        " industry, job_url, source_name, retrieved_at, match_reason, canonical_key, first_recommended_at)"
        " VALUES (?, ?, 'A', 'B', 'C', 'remote', 'I', 'https://x', 'web', 'now', 'fit', ?, 'now')",
        (uid, rid, key),
    )


def test_recommendation_unique_across_runs_and_scoped_to_owner(conn: sqlite3.Connection) -> None:
    a, b = user(conn), user(conn, "sub-2")
    r1, r2 = report(conn, a, 1), report(conn, a, 2)
    rec(conn, a, r1, "url:https://x/1")
    with pytest.raises(sqlite3.IntegrityError):
        rec(conn, a, r2, "url:https://x/1")
    with pytest.raises(sqlite3.IntegrityError):
        rec(conn, b, r1, "url:https://x/2")  # b cannot attach to a's report


def test_report_slot_cannot_be_freed_or_moved(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    rid = report(conn, uid, 1)
    with pytest.raises(sqlite3.IntegrityError, match="authorized deletion"):
        conn.execute("DELETE FROM job_reports WHERE id = ?", (rid,))
    for col, val in (("report_date", "2026-10-11"), ("run_number", 2)):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE job_reports SET {col} = ? WHERE id = ?", (val, rid))  # noqa: S608
    conn.execute("UPDATE job_reports SET status = 'failed', error = 'x' WHERE id = ?", (rid,))




def test_task_exact_duplicate_rejected_but_other_time_allowed(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    sql = ("INSERT INTO tasks (user_id, task_date, start_time, title, category, canonical_key)"
           " VALUES (?, ?, ?, ?, 'family', ?)")
    conn.execute(sql, (uid, D, None, "Call", canonical.task_key(None, "Call")))
    conn.execute(sql, (uid, D, "10:00", "Call", canonical.task_key("10:00", "Call")))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (uid, D, None, "call ", canonical.task_key(None, "call ")))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (uid, D, "24:30", "Late", canonical.task_key("24:30", "Late")))




def test_transaction_rolls_back_on_error(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        conn.execute("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, 1)", (uid, D))
        conn.execute("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, 1)", (uid, D))
    assert conn.execute("SELECT COUNT(*) FROM step_records").fetchone()[0] == 0


def test_seed_produces_synthetic_data_with_gaps(conn: sqlite3.Connection) -> None:
    uid = seed(conn, date(2026, 10, 16))
    days = {r[0] for r in conn.execute("SELECT record_date FROM sleep_records WHERE user_id = ?", (uid,))}
    assert len(days) == 4  # deliberately gappy
    assert conn.execute("SELECT email FROM users").fetchone()[0].endswith("@example.com")




def test_known_basis_requires_calories(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    with pytest.raises(sqlite3.IntegrityError):
        estimate(conn, uid, meal(conn, uid), None, "user_provided")


def test_empty_canonical_key_rejected(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (user_id, task_date, title, category, canonical_key) VALUES (?, ?, 'x', 'other', '')",
            (uid, D),
        )


def test_complete_coverage_ignores_snack_without_calories(conn: sqlite3.Connection) -> None:
    uid = user(conn)
    for mtype, kcal in (("breakfast", 400), ("lunch", 500), ("dinner", 600)):
        estimate(conn, uid, meal(conn, uid, mtype), kcal)
    estimate(conn, uid, meal(conn, uid, "snacks"), None, "unknown")
    r = conn.execute("SELECT total_calories, coverage_status FROM daily_calories").fetchone()
    assert (r[0], r[1]) == (1500, "estimated_complete")



