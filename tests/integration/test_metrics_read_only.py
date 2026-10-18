"""`app.db.connect(path, read_only=True)` (ADR-005 §3.3): query_only, a deny-by-default
authorizer, a 2 s statement deadline, and refusal of a missing file (synthetic data only)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app import db
from app.analytics import metrics
from app.db import connect, migrate
from app.demo import DemoError, assert_demo_database, assert_personal_database, build_working_copy
from app.records import sleep
from app.users import get_or_create_user

T = date(2026, 10, 18)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "ro.db"
    c = connect(path)
    migrate(c)
    uid = get_or_create_user(c, "test-ro", "ro@example.com", "RO")
    sleep.create(c, uid, sleep.SleepIn(record_date=date(2026, 10, 1), hours_slept=7.0), today=T)
    c.close()
    return path


@pytest.fixture
def ro(db_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path, read_only=True)
    yield c
    c.close()


def _dump(path: Path) -> list[tuple[object, ...]]:
    c = connect(path)
    try:
        rows = [tuple(r) for r in c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")]
        rows += [tuple(r) for r in c.execute("SELECT * FROM sleep_records ORDER BY id")]
        rows += [tuple(r) for r in c.execute("SELECT * FROM users ORDER BY id")]
        rows.append((db.application_id(c),))
        return rows
    finally:
        c.close()


DENIED = [
    "INSERT INTO sleep_records (user_id, record_date, hours_slept) VALUES (1, '2026-10-02', 8)",
    "UPDATE sleep_records SET hours_slept = 1",
    "DELETE FROM sleep_records",
    "INSERT INTO deletion_grants VALUES ('users', 1)",
    "REPLACE INTO sleep_records (id, user_id, record_date, hours_slept) VALUES (1, 1, '2026-10-01', 2)",
    "PRAGMA query_only = OFF",
    "PRAGMA query_only",
    "PRAGMA foreign_keys = OFF",
    "PRAGMA application_id = 1",
    "PRAGMA user_version = 7",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA writable_schema = ON",
    "SELECT * FROM pragma_table_info('users')",
    "ATTACH DATABASE ':memory:' AS other",
    "DETACH DATABASE main",
    "CREATE TABLE x (a)",
    "CREATE TEMP TABLE x (a)",
    "CREATE TEMP VIEW v AS SELECT 1",
    "CREATE INDEX i ON sleep_records (hours_slept)",
    "DROP TABLE sleep_records",
    "DROP VIEW daily_calories",
    "ALTER TABLE users ADD COLUMN x TEXT",
    "SAVEPOINT s",
    "VACUUM",
    "ANALYZE",
    "REINDEX",
]


@pytest.mark.parametrize("sql", DENIED)
def test_writes_schema_changes_pragmas_and_attach_are_refused(db_path: Path, ro: sqlite3.Connection,
                                                              sql: str) -> None:
    before = _dump(db_path)
    with pytest.raises(sqlite3.DatabaseError):
        ro.execute(sql).fetchall()
    assert _dump(db_path) == before
    assert ro.execute("SELECT COUNT(*) FROM sleep_records").fetchone()[0] == 1  # still usable


def test_query_only_is_on(ro: sqlite3.Connection) -> None:
    ro.set_authorizer(None)  # even without the authorizer, SQLite itself refuses writes
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("DELETE FROM sleep_records")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("BEGIN IMMEDIATE")


def test_selects_views_functions_cte_and_read_snapshots_work(ro: sqlite3.Connection) -> None:
    assert ro.execute("SELECT hours_slept FROM sleep_records").fetchone()["hours_slept"] == 7.0
    assert ro.execute("SELECT COUNT(*), MAX(record_date) FROM daily_calories").fetchone()[0] == 0
    assert ro.execute("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5)"
                      " SELECT SUM(n) FROM r").fetchone()[0] == 15
    ro.execute("BEGIN")
    assert ro.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    ro.execute("COMMIT")
    ro.execute("BEGIN")
    ro.execute("ROLLBACK")
    assert db.application_id(ro) == 0  # reading the marker is the one PRAGMA allowed


def test_metrics_run_on_a_read_only_connection(ro: sqlite3.Connection) -> None:
    rp = metrics.resolve_period(ro, 1, "sleep.hours", date(2026, 9, 20), date(2026, 10, 3), today=T)
    assert rp.clipped and rp.period.start == date(2026, 10, 1)
    for metric_id in metrics.REGISTRY:
        metrics.summarize(ro, 1, metric_id, rp.period, group_by="week")
        metrics.daily_points(ro, 1, metric_id, rp.period)
    assert metrics.summarize(ro, 1, "sleep.hours", rp.period).mean == 7.0


def test_a_slow_statement_is_aborted_after_two_seconds(ro: sqlite3.Connection) -> None:
    assert db.READ_ONLY_STATEMENT_SECONDS == 2.0
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        ro.execute("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r)"
                   " SELECT COUNT(*) FROM r").fetchall()
    elapsed = time.monotonic() - started
    assert 1.9 <= elapsed < 4.0
    # the deadline is per statement: the next (and a re-used cached) statement runs normally
    for _ in range(2):
        assert ro.execute("SELECT COUNT(*) FROM sleep_records").fetchone()[0] == 1


def test_a_missing_file_is_refused_and_nothing_is_created(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "nope.db"
    with pytest.raises(FileNotFoundError):
        connect(missing, read_only=True)
    assert not missing.parent.exists()
    sibling = tmp_path / "nope.db"
    with pytest.raises(FileNotFoundError):
        connect(sibling, read_only=True)
    assert not sibling.exists()
    with pytest.raises(FileNotFoundError):
        connect(":memory:", read_only=True)
    with pytest.raises(FileNotFoundError):
        connect(tmp_path, read_only=True)  # a directory is not a database file


def test_normal_connections_are_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "new" / "dir" / "n.db"
    c = connect(path)  # still creates the parent directory and the file, writable
    try:
        migrate(c)
        assert path.is_file()
        get_or_create_user(c, "test-n", "n@example.com", "N")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("PRAGMA application_id = 5")  # the B-14 marker guard still applies
    finally:
        c.close()


def test_demo_mode_checks_work_on_read_only_connections(tmp_path: Path, db_path: Path) -> None:
    copy = build_working_copy(tmp_path / "data" / "demo", 1, date(2026, 10, 17))
    ro = connect(copy.path, read_only=True)
    try:
        assert_demo_database(ro)
        assert db.application_id(ro) == db.DEMO_APPLICATION_ID
        with pytest.raises(DemoError):
            assert_personal_database(ro)
        assert metrics.summarize(ro, copy.user_id, "sleep.hours",
                                 metrics.Period(date(2026, 9, 18), date(2026, 10, 17))).n_observations == 27
    finally:
        ro.close()
    personal = connect(db_path, read_only=True)
    try:
        assert_personal_database(personal)
        with pytest.raises(DemoError):
            assert_demo_database(personal)
    finally:
        personal.close()
