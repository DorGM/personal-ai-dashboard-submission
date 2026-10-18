"""Regression tests for the data-preservation policy (docs/architecture/ADR-004, L2).

B-02 must keep these passing; they cover sign-in, conflict handling, deletion and identity.
"""

from __future__ import annotations

import ast
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from app.db import connect, migrate, transaction
from app.deletion import DeletionError, permanently_delete
from app.demo_seed import seed
from app.users import get_or_create_user

ROOT = Path(__file__).resolve().parents[2]
D = "2026-10-10"
USER_TABLES = [
    "weight_records", "meals", "meal_calorie_estimates", "workouts", "sleep_records", "step_records",
    "articles", "podcasts", "movies", "series", "job_applications", "job_search_profile", "job_reports",
    "job_recommendations", "industry_trends", "tasks",
]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "t.db")
    migrate(c)
    yield c
    c.close()


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}  # noqa: S608


def assert_healthy(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0] == 0


def seeded(conn: sqlite3.Connection) -> tuple[int, int]:
    """Seed the demo user plus a second user with one record; return both ids."""
    uid = seed(conn, date(2026, 10, 16))
    other = get_or_create_user(conn, "other-sub", "other@example.com", "Other")
    conn.execute("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, 5)", (other, D))
    return uid, other


# ---------------------------------------------------------------- sign-in (D5)
def test_first_sign_in_creates_one_user(conn: sqlite3.Connection) -> None:
    uid = get_or_create_user(conn, "sub-1", "a@example.com", "A")
    assert [tuple(r) for r in conn.execute("SELECT id, google_sub FROM users")] == [(uid, "sub-1")]


def test_repeated_sign_in_keeps_id_and_all_records(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    sub = conn.execute("SELECT google_sub FROM users WHERE id = ?", (uid,)).fetchone()[0]
    before = snapshot(conn)
    for _ in range(3):
        assert get_or_create_user(conn, sub, "demo.user@example.com", "Demo User") == uid
    assert snapshot(conn) == before
    assert_healthy(conn)


def test_sign_in_with_changed_profile_updates_only_profile_fields(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    sub = conn.execute("SELECT google_sub FROM users WHERE id = ?", (uid,)).fetchone()[0]
    before = snapshot(conn)
    assert get_or_create_user(conn, sub, "renamed@example.com", "Renamed") == uid
    after = snapshot(conn)
    assert {t for t in before if before[t] != after[t]} == {"users"}
    row = conn.execute("SELECT id, google_sub, email, display_name FROM users WHERE id = ?", (uid,)).fetchone()
    assert tuple(row) == (uid, sub, "renamed@example.com", "Renamed")


def test_blank_subject_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        get_or_create_user(conn, "  ", "a@example.com", None)


def test_concurrent_first_sign_ins_from_separate_processes(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    c = connect(db)
    migrate(c)
    go = tmp_path / "go"
    code = (
        "import sys, time, pathlib\n"
        "from app.db import connect\n"
        "from app.users import get_or_create_user\n"
        "go = pathlib.Path(sys.argv[2])\n"
        "while not go.exists(): time.sleep(0.005)\n"
        "print(get_or_create_user(connect(sys.argv[1]), 'same-sub', 'same@example.com', 'Same'))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(db), str(go)], stdout=subprocess.PIPE, text=True, env=env)
        for _ in range(8)
    ]
    go.touch()
    ids = {p.communicate(timeout=60)[0].strip() for p in procs}
    assert all(p.returncode == 0 for p in procs)
    assert len(ids) == 1
    assert c.execute("SELECT COUNT(*) FROM users WHERE google_sub = 'same-sub'").fetchone()[0] == 1
    c.close()


# ---------------------------------------------------------------- destructive SQL (D1, D3)
def test_replace_into_users_cannot_wipe_a_user(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    sub = conn.execute("SELECT google_sub FROM users WHERE id = ?", (uid,)).fetchone()[0]
    before = snapshot(conn)
    for sql in (
        "INSERT OR REPLACE INTO users (google_sub, email) VALUES (?, 'x@example.com')",
        "REPLACE INTO users (google_sub, email) VALUES (?, 'x@example.com')",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (sub,))
    assert snapshot(conn) == before


def test_replace_cannot_overwrite_existing_record(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    day = conn.execute("SELECT record_date FROM step_records WHERE user_id = ? LIMIT 1", (uid,)).fetchone()[0]
    before = snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError, match="authorized deletion"):
        conn.execute(
            "INSERT OR REPLACE INTO step_records (user_id, record_date, steps_count) VALUES (?, ?, 1)", (uid, day)
        )
    assert snapshot(conn) == before


def test_replace_cannot_rewrite_append_only_estimate(conn: sqlite3.Connection) -> None:
    seeded(conn)
    eid, uid, mid = conn.execute("SELECT id, user_id, meal_id FROM meal_calorie_estimates LIMIT 1").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO meal_calorie_estimates (id, user_id, meal_id, estimated_calories, calorie_basis)"
            " VALUES (?, ?, ?, 999, 'rough_estimate')",
            (eid, uid, mid),
        )


@pytest.mark.parametrize("table", ["users", *USER_TABLES])
def test_direct_delete_is_refused_everywhere(conn: sqlite3.Connection, table: str) -> None:
    seeded(conn)
    uid = conn.execute("SELECT MIN(id) FROM users").fetchone()[0]
    rid = conn.execute("INSERT INTO job_reports (user_id, report_date, run_number, status) VALUES (?, ?, 1, 'running')",
                       (uid, D)).lastrowid
    conn.execute(
        "INSERT INTO job_recommendations (user_id, report_id, company_name, job_title, location, work_model, industry,"
        " job_url, source_name, retrieved_at, match_reason, canonical_key, first_recommended_at)"
        " VALUES (?, ?, 'A', 'B', 'C', 'remote', 'I', 'https://example.com/j', 'web', 'now', 'fit', 'k', 'now')",
        (uid, rid),
    )
    conn.execute(
        "INSERT INTO industry_trends (user_id, report_id, snapshot_date, region, trend_type, name, description,"
        " canonical_key) VALUES (?, ?, ?, 'israel', 'company', 'Acme', 'd', 'acme')",
        (uid, rid, D),
    )
    conn.execute("INSERT INTO job_search_profile (user_id, preferences_text) VALUES (?, 'synthetic')", (uid,))
    for t in ("podcasts", "movies"):
        cols = "podcast_name" if t == "podcasts" else "title"
        conn.execute(f"INSERT INTO {t} (user_id, record_date, {cols}, canonical_key) VALUES (?, ?, 'X', 'x')",  # noqa: S608
                     (uid, D))
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0  # noqa: S608
    before = snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
    assert snapshot(conn) == before


def test_user_grant_alone_cannot_cascade_into_children(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    before = snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        conn.execute("INSERT INTO deletion_grants (table_name, row_id) VALUES ('users', ?)", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))  # RESTRICT: children still exist
    assert snapshot(conn) == before


def test_no_foreign_key_cascades_in_schema(conn: sqlite3.Connection) -> None:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})"):
            assert (fk["on_delete"], fk["on_update"]) in {("RESTRICT", "NO ACTION")}, (t, fk["table"])


# ---------------------------------------------------------------- identity (D4)
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE users SET id = id + 100",
        "UPDATE users SET google_sub = 'hijacked'",
        "UPDATE meals SET user_id = (SELECT MAX(id) FROM users)",
        "UPDATE step_records SET id = id + 100",
        "UPDATE job_reports SET user_id = (SELECT MAX(id) FROM users)",
    ],
)
def test_record_identity_is_immutable(conn: sqlite3.Connection, sql: str) -> None:
    seeded(conn)
    conn.execute("INSERT INTO job_reports (user_id, report_date, run_number, status) VALUES (1, ?, 1, 'running')", (D,))
    before = snapshot(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)
    assert snapshot(conn) == before


def test_targeted_edit_changes_only_that_record(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    sid = conn.execute("SELECT id FROM sleep_records WHERE user_id = ? LIMIT 1", (uid,)).fetchone()[0]
    before = snapshot(conn)
    conn.execute("UPDATE sleep_records SET hours_slept = 6.0 WHERE id = ? AND user_id = ?", (sid, uid))
    after = snapshot(conn)
    changed = [(t, a) for t in before for a, b in zip(after[t], before[t], strict=True) if a != b]
    assert len(changed) == 1 and changed[0][0] == "sleep_records" and changed[0][1][0] == sid


# ---------------------------------------------------------------- authorized deletion (D2)
def test_permanent_delete_removes_only_target_and_owned_children(conn: sqlite3.Connection) -> None:
    uid, other = seeded(conn)
    mid = conn.execute("SELECT id FROM meals WHERE user_id = ? LIMIT 1", (uid,)).fetchone()[0]
    before = snapshot(conn)
    with transaction(conn):
        removed = permanently_delete(conn, uid, "meals", mid)
    after = snapshot(conn)
    assert removed == 2  # the meal and its one estimate
    assert {t for t in before if before[t] != after[t]} == {"meals", "meal_calorie_estimates"}
    assert len(before["meals"]) - len(after["meals"]) == 1
    assert_healthy(conn)


def test_permanent_delete_of_other_users_row_is_refused(conn: sqlite3.Connection) -> None:
    uid, other = seeded(conn)
    sid = conn.execute("SELECT id FROM step_records WHERE user_id = ?", (other,)).fetchone()[0]
    before = snapshot(conn)
    with pytest.raises(DeletionError), transaction(conn):
        permanently_delete(conn, uid, "step_records", sid)
    assert snapshot(conn) == before


def test_failed_permanent_delete_is_atomic(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    mid = conn.execute("SELECT id FROM meals WHERE user_id = ? LIMIT 1", (uid,)).fetchone()[0]
    before = snapshot(conn)
    with pytest.raises(RuntimeError, match="boom"), transaction(conn):
        permanently_delete(conn, uid, "meals", mid)
        raise RuntimeError("boom")
    assert snapshot(conn) == before


def test_permanent_delete_guards(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    with pytest.raises(RuntimeError, match="transaction"):
        permanently_delete(conn, uid, "tasks", 1)
    with pytest.raises(ValueError), transaction(conn):
        permanently_delete(conn, uid, "job_reports", 1)
    with pytest.raises(DeletionError), transaction(conn):
        permanently_delete(conn, uid, "tasks", 999_999)
    assert_healthy(conn)


# ---------------------------------------------------------------- static guard (D3)
# REPLACE deletes the old row; IGNORE silently drops rows that fail CHECK/NOT NULL; DO UPDATE
# can overwrite identity. Plain INSERT (or ON CONFLICT DO NOTHING + SELECT) is the rule.
_FORBIDDEN = re.compile(
    r"\bOR\s+(?:REPLACE|IGNORE)\b|\bREPLACE\s+INTO\b|\bON\s+CONFLICT\b[^;]*\bDO\s+UPDATE\b"
    r"|\bON\s+CONFLICT\s+(?:REPLACE|IGNORE)\b",
    re.IGNORECASE,
)


def _sql_strings(path: Path) -> list[tuple[int, str]]:
    """SQL-bearing text only: Python string literals (not docstrings) or SQL outside comments."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".sql":
        # Whole file with comments blanked (newlines kept), so patterns split across lines match.
        without_comments = re.sub(r"--[^\n]*|/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text,
                                  flags=re.DOTALL)
        return [(1, without_comments)]
    tree = ast.parse(text)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_no_destructive_conflict_handling_in_source() -> None:
    offenders = []
    for path in [*ROOT.joinpath("app").rglob("*.py"), *ROOT.joinpath("migrations").glob("*.sql"),
                 *ROOT.joinpath("scripts").glob("*.py")]:
        for n, text in _sql_strings(path):
            if _FORBIDDEN.search(text):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []


def test_static_guard_detects_forbidden_patterns(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text('"""INSERT OR REPLACE in a docstring is fine."""\n'
                      'SQL = "INSERT INTO t VALUES (1) ON CONFLICT (a) DO UPDATE SET b = 2"\n'
                      'OTHER = "replace into t values (1)"\n', encoding="utf-8")
    hits = [n for n, text in _sql_strings(sample) if _FORBIDDEN.search(text)]
    assert hits == [2, 3]


def test_static_guard_sees_sql_split_across_lines(tmp_path: Path) -> None:
    sample = tmp_path / "bad.sql"
    sample.write_text("-- INSERT OR REPLACE in a comment is fine\nCREATE TABLE t (a INTEGER UNIQUE ON CONFLICT\n"
                      "  REPLACE);\nINSERT OR\nIGNORE INTO t VALUES (1);\n", encoding="utf-8")
    texts = [text for _, text in _sql_strings(sample)]
    assert len(_FORBIDDEN.findall(texts[0])) == 2


def test_only_deletion_module_writes_grants_or_deletes() -> None:
    for path in ROOT.joinpath("app").rglob("*.py"):
        if path.name != "deletion.py":
            for _, text in _sql_strings(path):
                if "deletion_grants" in text:  # app/db.py may check that the table exists
                    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", text, re.IGNORECASE), path
                assert not re.search(r"\bDELETE\s+FROM\b", text, re.IGNORECASE), path


def _raw_connect_calls(path: Path) -> list[int]:
    """Lines that open SQLite directly: sqlite3.connect (any alias), sqlite3.dbapi2, `from sqlite3 import connect`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {a.asname or a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
               for a in node.names if a.name.split(".")[0] == "sqlite3"}
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "sqlite3" and a.asname for a in node.names):
            lines.append(node.lineno)  # an alias hides later uses from review
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "sqlite3" and \
                any(a.name in {"connect", "dbapi2", "*"} for a in node.names):
            lines.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr in {"connect", "dbapi2"} and \
                isinstance(node.value, ast.Name) and node.value.id in aliases:
            lines.append(node.lineno)
    return lines


def test_only_app_db_opens_sqlite_connections() -> None:
    """Every connection must come from app.db.connect, which turns the guards on (ADR-004 D3)."""
    offenders = [f"{path.relative_to(ROOT)}:{n}"
                 for path in [*ROOT.joinpath("app").rglob("*.py"), *ROOT.joinpath("scripts").glob("*.py")]
                 if path != ROOT / "app" / "db.py" for n in _raw_connect_calls(path)]
    assert offenders == []


def test_raw_connect_detector_sees_aliases(tmp_path: Path) -> None:
    sample = tmp_path / "bad.py"
    sample.write_text("import sqlite3\nimport sqlite3 as s\nfrom sqlite3 import dbapi2\n"
                      "a = sqlite3.connect('x')\nb = s.connect('x')\nc = sqlite3.dbapi2.connect('x')\n",
                      encoding="utf-8")
    assert sorted(set(_raw_connect_calls(sample))) == [2, 3, 4, 5, 6]


# ---------------------------------------------------------------- verifier round 2 (B2, N1, N8, N9, N15)
@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid"])
@pytest.mark.parametrize("table", ["meals", "weight_records", "tasks"])
def test_identity_cannot_change_through_rowid_alias(conn: sqlite3.Connection, table: str, alias: str) -> None:
    seeded(conn)
    before = snapshot(conn)
    rid = conn.execute(f"SELECT id FROM {table} LIMIT 1").fetchone()[0]  # noqa: S608
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"UPDATE {table} SET {alias} = 7777 WHERE id = ?", (rid,))  # noqa: S608
    assert snapshot(conn) == before


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid"])
def test_user_without_records_cannot_be_renumbered(conn: sqlite3.Connection, alias: str) -> None:
    uid = get_or_create_user(conn, "lonely-sub", "lonely@example.com", None)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"UPDATE users SET {alias} = 999 WHERE id = ?", (uid,))  # noqa: S608
    assert conn.execute("SELECT id FROM users WHERE google_sub = 'lonely-sub'").fetchone()[0] == uid


@pytest.mark.parametrize(
    "statement",
    ["DROP TABLE step_records", "DROP TABLE users", "DROP TRIGGER meals_delete_requires_grant",
     "DROP INDEX idx_meal_calorie_estimates_meal", "DROP VIEW daily_calories", "ALTER TABLE tasks RENAME TO t2",
     "ALTER TABLE tasks DROP COLUMN title", "PRAGMA foreign_keys = OFF", "PRAGMA recursive_triggers = 0",
     "PRAGMA writable_schema = ON", "PRAGMA ignore_check_constraints = 1",
     "PRAGMA journal_mode = OFF", "PRAGMA synchronous = OFF", "PRAGMA defer_foreign_keys = ON",
     "CREATE TABLE x (a INTEGER)", "CREATE INDEX ix ON tasks (title)", "CREATE VIEW vx AS SELECT 1",
     "CREATE TRIGGER app_grant BEFORE DELETE ON tasks BEGIN INSERT INTO deletion_grants SELECT 'tasks', OLD.id; END",
     "CREATE TEMP TRIGGER tg BEFORE DELETE ON main.tasks BEGIN SELECT 1; END",
     "ATTACH DATABASE ':memory:' AS other"],
)
def test_app_connections_cannot_change_schema_or_disable_guards(conn: sqlite3.Connection, statement: str) -> None:
    seeded(conn)
    before = snapshot(conn)
    schema = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        conn.execute(statement)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema
    assert snapshot(conn) == before


def test_transaction_reports_original_error_when_statement_rolled_back(conn: sqlite3.Connection) -> None:
    get_or_create_user(conn, "dup-sub", "dup@example.com", None)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), transaction(conn):
        conn.execute("INSERT OR ROLLBACK INTO users (google_sub, email) VALUES ('dup-sub', 'x@example.com')")
    assert not conn.in_transaction


def test_permanent_delete_is_atomic_even_if_caller_swallows_the_error(conn: sqlite3.Connection) -> None:
    uid, _ = seeded(conn)
    mid = conn.execute(
        "SELECT meal_id FROM meal_calorie_estimates WHERE user_id = ? LIMIT 1", (uid,)).fetchone()[0]
    before = snapshot(conn)
    from app.db import _app_authorizer

    conn.set_authorizer(None)  # test-only: app connections refuse DDL, temp triggers included
    conn.execute("CREATE TEMP TRIGGER fail_meal_delete BEFORE DELETE ON main.meals "
                 "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    conn.set_authorizer(_app_authorizer)
    with transaction(conn):
        try:
            permanently_delete(conn, uid, "meals", mid)
        except sqlite3.IntegrityError:
            pass  # a careless caller carries on and commits
    conn.set_authorizer(None)
    conn.execute("DROP TRIGGER temp.fail_meal_delete")
    conn.set_authorizer(_app_authorizer)
    assert snapshot(conn) == before
    assert_healthy(conn)


def test_sign_in_requires_email_and_works_inside_open_transaction(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="email"):
        get_or_create_user(conn, "sub-x", "  ", None)
    with pytest.raises(ValueError, match="email"):
        get_or_create_user(conn, "sub-x", None, None)  # type: ignore[arg-type]
    with transaction(conn):
        uid = get_or_create_user(conn, "sub-x", "x@example.com", None)
    assert get_or_create_user(conn, "sub-x", "x@example.com", None) == uid
