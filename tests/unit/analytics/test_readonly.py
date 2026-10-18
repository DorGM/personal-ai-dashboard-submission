"""Hardened read-only connection: only reads pass, writes/PRAGMA/ATTACH/temp objects are refused,
and the progress handler enforces the time budget (synthetic data only)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.analytics import policy, readonly
from app.analytics.readonly import harden_read_only, is_read_only, open_read_only, read_snapshot, time_budget
from app.db import connect, migrate
from app.records import meals, steps
from app.users import get_or_create_user

T = date(2026, 10, 18)
SLOW = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "ro.db"
    conn = connect(path)
    migrate(conn)
    uid = get_or_create_user(conn, "ro-test", "ro@example.com", "RO")
    meals.create(conn, uid, meals.MealIn.model_validate(
        {"record_date": "2026-10-10", "meal_type": "lunch", "description": "Soup", "estimated_calories": 400}),
        today=T)
    steps.create(conn, uid, steps.StepsIn.model_validate({"record_date": "2026-10-10", "steps_count": 0}), today=T)
    conn.close()
    return path


@pytest.fixture
def ro(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_read_only(db_path)
    yield conn
    conn.close()


def _snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    conn = connect(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n} ORDER BY rowid")] for n in names}
    finally:
        conn.close()


@pytest.mark.parametrize("statement", [
    "INSERT INTO users (google_sub, email) VALUES ('x', 'x@example.com')",
    "UPDATE users SET email = 'y@example.com'",
    "DELETE FROM step_records",
    "REPLACE INTO step_records (id, user_id, record_date, steps_count) VALUES (1, 1, '2026-10-10', 5)",
    "PRAGMA query_only = OFF",
    "PRAGMA query_only",
    "PRAGMA user_version",
    "PRAGMA table_info(users)",
    "SELECT * FROM pragma_table_info('users')",
    "ATTACH DATABASE ':memory:' AS other",
    "DETACH DATABASE main",
    "CREATE TEMP TABLE scratch (x INTEGER)",
    "CREATE TEMP VIEW v AS SELECT 1",
    "CREATE TABLE scratch (x INTEGER)",
    "DROP VIEW daily_calories",
    "ALTER TABLE users ADD COLUMN extra TEXT",
    "SAVEPOINT s1",
    "VACUUM",
    "ANALYZE",
    "REINDEX",
])
def test_everything_but_reads_is_refused(ro: sqlite3.Connection, db_path: Path, statement: str) -> None:
    before = _snapshot(db_path)
    with pytest.raises(sqlite3.DatabaseError):
        ro.execute(statement).fetchall()
    assert _snapshot(db_path) == before


def test_selects_work_including_the_daily_calories_view(ro: sqlite3.Connection) -> None:
    with time_budget(ro):
        row = ro.execute("SELECT total_calories, coverage_status FROM daily_calories").fetchone()
        assert tuple(row) == (400, "estimated_partial")
        assert ro.execute("SELECT steps_count FROM step_records").fetchone()[0] == 0
        assert ro.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 5)"
                          " SELECT sum(x) FROM c").fetchone()[0] == 15
        assert ro.execute("SELECT upper('a'), date('2026-10-18', '+1 day')").fetchone()[1] == "2026-10-19"


def test_query_only_holds_even_without_the_authorizer(ro: sqlite3.Connection) -> None:
    ro.set_authorizer(None)  # defence in depth: the PRAGMA still blocks writes
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("DELETE FROM step_records")


def test_read_snapshot_commits_and_rolls_back(ro: sqlite3.Connection) -> None:
    with time_budget(ro), read_snapshot(ro):
        assert ro.in_transaction
        ro.execute("SELECT 1").fetchone()
    assert not ro.in_transaction
    with pytest.raises(RuntimeError), time_budget(ro), read_snapshot(ro):
        raise RuntimeError("boom")
    assert not ro.in_transaction


def test_slow_statement_is_interrupted_by_the_budget(ro: sqlite3.Connection) -> None:
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="interrupted"), time_budget(ro, 0.2):
        ro.execute(SLOW).fetchone()
    assert time.monotonic() - started < 1.5
    with time_budget(ro):  # a fresh budget works again
        assert ro.execute("SELECT 1").fetchone()[0] == 1


def test_nested_budget_restores_the_outer_deadline(ro: sqlite3.Connection) -> None:
    with time_budget(ro, 30):
        with time_budget(ro, 0.1):
            pass
        time.sleep(0.15)
        assert ro.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 20000)"
                          " SELECT count(*) FROM c").fetchone()[0] == 20000


def test_unbudgeted_work_fails_closed_after_the_default(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "SQL_STATEMENT_TIMEOUT_S", 0.1)
    conn = open_read_only(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute(SLOW).fetchone()
    finally:
        conn.close()


def test_writable_connections_are_detected(db_path: Path) -> None:
    writable = connect(db_path)
    try:
        assert not is_read_only(writable)
        with pytest.raises(ValueError, match="hardened"), time_budget(writable):
            pass
        writable.execute("BEGIN")
        with pytest.raises(ValueError, match="transaction"):
            harden_read_only(writable)
        writable.execute("ROLLBACK")
    finally:
        writable.close()


def test_open_read_only_goes_through_app_db_connect(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path | str] = []

    def spy(path: Path | str) -> sqlite3.Connection:
        opened.append(path)
        return connect(path)

    monkeypatch.setattr(readonly, "connect", spy)
    conn = open_read_only(db_path)
    try:
        assert opened == [db_path] and is_read_only(conn)
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("PRAGMA foreign_keys")
    finally:
        conn.close()
