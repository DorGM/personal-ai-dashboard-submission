"""Migration runner safety (L1, L3): atomicity, rejection, data preservation, real concurrency."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db import MigrationError, connect, migrate, split_statements

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _close_connections(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every connection a test in this module opened (avoids ResourceWarning noise)."""
    opened: list[sqlite3.Connection] = []
    real_connect = connect

    def tracking_connect(path: Path | str) -> sqlite3.Connection:
        conn = real_connect(path)
        opened.append(conn)
        return conn

    monkeypatch.setitem(globals(), "connect", tracking_connect)
    yield
    for conn in opened:
        conn.close()


def write(mdir: Path, name: str, sql: str) -> None:
    mdir.mkdir(exist_ok=True)
    (mdir / name).write_text(sql, encoding="utf-8")


def tables(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {"schema_migrations"}


def versions(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")]


def test_real_schema_applies_cleanly_and_is_idempotent(tmp_path: Path) -> None:
    c = connect(tmp_path / "a.db")
    assert migrate(c) == ["001_init", "002_soft_delete"]
    assert migrate(c) == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failure_mid_file_leaves_no_trace(tmp_path: Path) -> None:
    m = tmp_path / "m"
    write(m, "001_ok.sql", "CREATE TABLE a (x INTEGER) STRICT;")
    write(m, "002_bad.sql",
          "CREATE TABLE b (x INTEGER) STRICT;\nINSERT INTO b VALUES (1);\nINSERT INTO nope VALUES (1);")
    c = connect(tmp_path / "m.db")
    with pytest.raises(sqlite3.OperationalError):
        migrate(c, m)
    assert tables(c) == {"a"} and versions(c) == ["001_ok"] and not c.in_transaction


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE a (x INTEGER);\nCOMMIT;\nCREATE TABLE b (x INTEGER);",
        "CREATE TABLE a (x INTEGER); COMMIT; CREATE TABLE b (x INTEGER);",
        "CREATE TABLE a (x INTEGER);\n/* sneaky */ commit;",
        "CREATE TABLE a (x INTEGER);\n  -- note\n  begin;",
        "CREATE TABLE a (x INTEGER);\nEND TRANSACTION;",
        "SAVEPOINT s;\nCREATE TABLE a (x INTEGER);",
        "PRAGMA foreign_keys = OFF;\nCREATE TABLE a (x INTEGER);",
        "ATTACH DATABASE 'other.db' AS o;",
        "CREATE TABLE a (x INTEGER);\nCOMMIT",
    ],
)
def test_transaction_and_connection_statements_rejected_before_running(tmp_path: Path, sql: str) -> None:
    m = tmp_path / "m"
    write(m, "001_x.sql", sql)
    c = connect(tmp_path / "m.db")
    with pytest.raises((MigrationError, sqlite3.ProgrammingError)):
        migrate(c, m)
    assert tables(c) == set() and versions(c) == []


def test_case_end_and_triggers_are_not_false_positives(tmp_path: Path) -> None:
    m = tmp_path / "m"
    write(m, "001_ok.sql", """
CREATE TABLE a (x INTEGER) STRICT;
CREATE VIEW v AS SELECT CASE WHEN x > 0 THEN 'pos' ELSE 'neg'
END AS sign FROM a;
CREATE TRIGGER t BEFORE INSERT ON a
BEGIN
    SELECT CASE WHEN NEW.x < 0 THEN RAISE(ABORT, 'neg; not allowed') END;
END;
INSERT INTO a VALUES (1); -- comment; with semicolon
""")
    c = connect(tmp_path / "m.db")
    assert migrate(c, m) == ["001_ok"]
    assert c.execute("SELECT sign FROM v").fetchone()[0] == "pos"


def test_split_statements_keeps_trigger_bodies_whole() -> None:
    parts = split_statements("CREATE TABLE a(x);\nCREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1; SELECT 2; END;\n")
    assert len(parts) == 2 and parts[1].endswith("END;")


def test_incomplete_statement_rejected(tmp_path: Path) -> None:
    m = tmp_path / "m"
    write(m, "001_x.sql", "CREATE TABLE a (x INTEGER)")
    with pytest.raises(MigrationError, match="incomplete"):
        migrate(connect(tmp_path / "m.db"), m)


@pytest.mark.parametrize(
    "destructive",
    ["DROP TABLE keep;", "DELETE FROM keep;", "CREATE TABLE tmp AS SELECT * FROM keep WHERE x > 1;\nDROP TABLE keep;\n"
     "ALTER TABLE tmp RENAME TO keep;"],
)
def test_migration_that_loses_rows_is_rolled_back(tmp_path: Path, destructive: str) -> None:
    m = tmp_path / "m"
    write(m, "001_base.sql", "CREATE TABLE keep (x INTEGER) STRICT;")
    c = connect(tmp_path / "m.db")
    migrate(c, m)
    c.executemany("INSERT INTO keep VALUES (?)", [(1,), (2,), (3,)])
    write(m, "002_destroy.sql", destructive)
    with pytest.raises(MigrationError, match="remove rows"):
        migrate(c, m)
    assert [r[0] for r in c.execute("SELECT x FROM keep ORDER BY x")] == [1, 2, 3]
    assert versions(c) == ["001_base"]


def test_table_rebuild_that_keeps_rows_is_allowed(tmp_path: Path) -> None:
    m = tmp_path / "m"
    write(m, "001_base.sql", "CREATE TABLE keep (x INTEGER) STRICT;")
    c = connect(tmp_path / "m.db")
    migrate(c, m)
    c.executemany("INSERT INTO keep VALUES (?)", [(1,), (2,)])
    write(m, "002_rebuild.sql", "CREATE TABLE keep_new (x INTEGER, y TEXT) STRICT;\n"
          "INSERT INTO keep_new (x) SELECT x FROM keep;\nDROP TABLE keep;\nALTER TABLE keep_new RENAME TO keep;")
    assert migrate(c, m) == ["002_rebuild"]
    assert c.execute("SELECT COUNT(*) FROM keep").fetchone()[0] == 2


def test_new_migration_preserves_existing_user_data(tmp_path: Path) -> None:
    from datetime import date

    from app.demo_seed import seed

    m = tmp_path / "m"
    m.mkdir()
    (m / "001_init.sql").write_text(
        (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"), encoding="utf-8")
    c = connect(tmp_path / "m.db")
    migrate(c, m)
    seed(c, date(2026, 10, 16))
    before = {t: c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables(c)}  # noqa: S608
    write(m, "002_add_column.sql", "ALTER TABLE tasks ADD COLUMN notes TEXT;")
    assert migrate(c, m) == ["002_add_column"]
    for t, rows in before.items():
        if t != "tasks":
            assert c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() == rows  # noqa: S608
    assert c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == len(before["tasks"])


def test_soft_delete_migration_preserves_seeded_data(tmp_path: Path) -> None:
    from datetime import date

    from app.demo_seed import seed

    m = tmp_path / "m"
    m.mkdir()
    (m / "001_init.sql").write_text(
        (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"), encoding="utf-8")
    c = connect(tmp_path / "m.db")
    migrate(c, m)
    seed(c, date(2026, 10, 16))
    before = {t: c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables(c)}  # noqa: S608
    calories = c.execute("SELECT * FROM daily_calories ORDER BY user_id, record_date").fetchall()
    (m / "002_soft_delete.sql").write_text(
        (ROOT / "migrations" / "002_soft_delete.sql").read_text(encoding="utf-8"), encoding="utf-8")
    assert migrate(c, m) == ["002_soft_delete"]
    soft = {"weight_records", "meals", "workouts", "sleep_records", "step_records", "articles", "podcasts",
            "movies", "series", "job_applications", "tasks"}
    for t, rows in before.items():
        after = c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()  # noqa: S608
        if t in soft:
            assert [tuple(r)[:-1] for r in after] == [tuple(r) for r in rows] and all(r[-1] is None for r in after)
        else:
            assert after == rows, t
    cols = {t for t in tables(c) if "deleted_at" in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}}
    assert cols == soft
    assert c.execute("SELECT * FROM daily_calories ORDER BY user_id, record_date").fetchall() == calories
    assert c.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0] == 0
    c.close()


_RUNNER = (
    "import sys, time, pathlib\n"
    "from app.db import connect, migrate\n"
    "go = pathlib.Path(sys.argv[2])\n"
    "while not go.exists(): time.sleep(0.005)\n"
    "print(','.join(migrate(connect(sys.argv[1]))) or '-')\n"
)


def test_concurrent_migrations_from_separate_processes(tmp_path: Path) -> None:
    db, go = tmp_path / "c.db", tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", _RUNNER, str(db), str(go)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) for _ in range(6)]
    go.touch()
    results = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [err for _, err in results]
    # Each file is applied exactly once overall; different processes may apply different files.
    applied = [v for out, _ in results for v in out.strip().split(",") if v != "-"]
    assert sorted(applied) == ["001_init", "002_soft_delete"]
    c = connect(db)
    assert versions(c) == ["001_init", "002_soft_delete"]
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_killed_migration_leaves_database_unchanged(tmp_path: Path) -> None:
    m, db = tmp_path / "m", tmp_path / "k.db"
    write(m, "001_base.sql", "CREATE TABLE keep (x INTEGER) STRICT;")
    c = connect(db)
    migrate(c, m)
    c.executemany("INSERT INTO keep VALUES (?)", [(1,), (2,)])
    c.close()
    write(m, "002_slow.sql", "CREATE TABLE big (x INTEGER) STRICT;\n"
          "INSERT INTO big WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT 50000000)"
          " SELECT x FROM n;")
    code = ("import sys, pathlib\nfrom app.db import connect, migrate\n"
            "migrate(connect(sys.argv[1]), pathlib.Path(sys.argv[2]))\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    p = subprocess.Popen([sys.executable, "-c", code, str(db), str(m)], env=env)
    time.sleep(1.5)
    assert p.poll() is None, "migration finished before it could be interrupted"
    p.kill()
    p.wait(timeout=30)
    c = connect(db)
    assert "big" not in tables(c)
    assert versions(c) == ["001_base"]
    assert c.execute("SELECT COUNT(*) FROM keep").fetchone()[0] == 2
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---------------------------------------------------------------- verifier round 2 (B1, B3, B4, N2)
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE a (x INTEGER);\n;COMMIT;\nCREATE TABLE b (x INTEGER);",
        "CREATE TABLE a (x INTEGER);\n ; ;\tcommit;\nCREATE TABLE b (x INTEGER);",
        "CREATE TABLE a (x INTEGER);\n;/* c */;PRAGMA foreign_keys = OFF;",
    ],
)
def test_statements_hidden_behind_semicolons_rejected(tmp_path: Path, sql: str) -> None:
    m = tmp_path / "m"
    write(m, "001_x.sql", sql)
    c = connect(tmp_path / "m.db")
    with pytest.raises(MigrationError, match="not allowed"):
        migrate(c, m)
    assert tables(c) == set() and versions(c) == []


@pytest.mark.parametrize("statement", ["COMMIT", "END", "ROLLBACK", "SAVEPOINT s", "RELEASE s",
                                       "PRAGMA foreign_keys = OFF", "ATTACH DATABASE ':memory:' AS o"])
def test_authorizer_blocks_forbidden_statements_even_if_keyword_check_is_bypassed(tmp_path: Path,
                                                                                  statement: str) -> None:
    from app.db import _apply

    c = connect(tmp_path / "m.db")
    migrate(c, tmp_path / "empty")
    with pytest.raises(MigrationError, match="not allowed"):
        _apply(c, "001_x", ["CREATE TABLE a (x INTEGER)", statement, "CREATE TABLE b (x INTEGER)"])
    assert tables(c) == set() and versions(c) == [] and not c.in_transaction


def _real_schema(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    from datetime import date

    from app.demo_seed import seed

    m = tmp_path / "m"
    m.mkdir()
    (m / "001_init.sql").write_text(
        (ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8"), encoding="utf-8")
    c = connect(tmp_path / "m.db")
    migrate(c, m)
    seed(c, date(2026, 10, 16))
    return m, c


def _rebuild_weight_records(c: sqlite3.Connection, *, keep_triggers: bool) -> str:
    table_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='weight_records'").fetchone()[0]
    extras = [r[0] for r in c.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='weight_records' AND sql IS NOT NULL AND type IN (?, ?)",
        ("index", "trigger") if keep_triggers else ("index", "index"))]
    return "\n".join([
        table_sql.replace("CREATE TABLE weight_records", "CREATE TABLE weight_records_new", 1) + ";",
        "INSERT INTO weight_records_new SELECT * FROM weight_records;",
        "DROP TABLE weight_records;",
        "ALTER TABLE weight_records_new RENAME TO weight_records;",
        *[s + ";" for s in extras],
    ])


@pytest.mark.parametrize(
    "migration",
    [
        "DROP TRIGGER weight_records_delete_requires_grant;",
        "DROP TRIGGER users_identity_immutable;",
        "DROP TRIGGER meals_identity_immutable;\nCREATE TRIGGER meals_identity_immutable BEFORE UPDATE OF id ON meals"
        "\nBEGIN SELECT RAISE(ABORT, 'x'); END;",
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id)) STRICT;",
        "CREATE TABLE extra (id INTEGER PRIMARY KEY, k TEXT UNIQUE ON CONFLICT REPLACE) STRICT;",
        "CREATE TABLE extra (id INTEGER PRIMARY KEY, m INTEGER REFERENCES meals(id) ON DELETE CASCADE) STRICT;",
        "CREATE TRIGGER sneaky AFTER INSERT ON tasks BEGIN INSERT OR REPLACE INTO deletion_grants VALUES"
        " ('users', NEW.user_id); END;",
        "UPDATE schema_migrations SET version = '999_future';",
        "DROP TABLE deletion_grants;",
        # round 2: extra auto-grant trigger next to an intact guard
        "CREATE TRIGGER g BEFORE DELETE ON weight_records BEGIN INSERT INTO deletion_grants SELECT"
        " 'weight_records', OLD.id WHERE NOT EXISTS (SELECT 1 FROM deletion_grants WHERE"
        " table_name='weight_records' AND row_id=OLD.id); END;",
        # weakened and inverted guards
        "DROP TRIGGER tasks_delete_requires_grant;\nCREATE TRIGGER tasks_delete_requires_grant BEFORE DELETE ON tasks"
        " WHEN 0 AND EXISTS (SELECT 1 FROM deletion_grants) BEGIN SELECT RAISE(ABORT, 'x'); END;",
        "DROP TRIGGER tasks_delete_requires_grant;\nCREATE TRIGGER tasks_delete_requires_grant BEFORE DELETE ON tasks"
        " WHEN EXISTS (SELECT 1 FROM deletion_grants WHERE table_name = 'tasks' AND row_id = OLD.id)"
        " BEGIN SELECT RAISE(ABORT, 'tasks: delete requires an explicit authorized deletion (ADR-004)'); END;",
        # identity trigger dropped where another BEFORE UPDATE trigger exists, or re-created without id
        "DROP TRIGGER job_reports_identity_immutable;",
        "DROP TRIGGER tasks_identity_immutable;\nCREATE TRIGGER tasks_identity_immutable BEFORE UPDATE ON tasks"
        " WHEN NEW.user_id IS NOT OLD.user_id BEGIN SELECT RAISE(ABORT, 'tasks: id and user_id are immutable'); END;",
        # temporary objects and unregistered user data
        "CREATE TEMP TRIGGER tg BEFORE DELETE ON main.tasks BEGIN SELECT 1; END;",
        "CREATE TEMP TABLE scratch (x INTEGER);",
        "CREATE TABLE meal_notes (id INTEGER PRIMARY KEY, meal_id INTEGER NOT NULL REFERENCES meals(id)) STRICT;",
        "CREATE TABLE owned (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id)) STRICT;",
        # '--' inside a literal or quoted identifier must not hide what follows
        "CREATE TABLE extra (id INTEGER PRIMARY KEY, k TEXT DEFAULT '--', UNIQUE (k) ON CONFLICT REPLACE) STRICT;",
        "CREATE TABLE extra ([a--b] TEXT, k TEXT UNIQUE ON CONFLICT REPLACE) STRICT;",
        "CREATE TABLE extra (`a--b` TEXT, k TEXT UNIQUE ON CONFLICT REPLACE) STRICT;",
    ],
)
def test_migration_that_removes_data_guards_is_rolled_back(tmp_path: Path, migration: str) -> None:
    m, c = _real_schema(tmp_path)
    before = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    write(m, "002_x.sql", migration)
    with pytest.raises(MigrationError):
        migrate(c, m)
    assert c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == before
    assert versions(c) == ["001_init"]


def test_child_table_rebuild_without_guards_is_rolled_back(tmp_path: Path) -> None:
    m, c = _real_schema(tmp_path)
    write(m, "002_rebuild.sql", _rebuild_weight_records(c, keep_triggers=False))
    with pytest.raises(MigrationError, match="weight_records_delete_requires_grant is missing"):
        migrate(c, m)
    assert versions(c) == ["001_init"]


def test_child_table_rebuild_that_recreates_guards_is_allowed(tmp_path: Path) -> None:
    m, c = _real_schema(tmp_path)
    rows = c.execute("SELECT * FROM weight_records ORDER BY id").fetchall()
    write(m, "002_rebuild.sql", _rebuild_weight_records(c, keep_triggers=True))
    assert migrate(c, m) == ["002_rebuild"]
    assert c.execute("SELECT * FROM weight_records ORDER BY id").fetchall() == rows
    with pytest.raises(sqlite3.IntegrityError, match="ADR-004"):
        c.execute("DELETE FROM weight_records")


_OPENER = (
    "import sys, time, pathlib\n"
    "from app.db import connect\n"
    "go = pathlib.Path(sys.argv[2])\n"
    "while not go.exists(): time.sleep(0.002)\n"
    "c = connect(sys.argv[1]); print(c.execute('PRAGMA journal_mode').fetchone()[0])\n"
)


def test_concurrent_first_open_of_new_database(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for attempt in range(3):
        db, go = tmp_path / f"n{attempt}.db", tmp_path / f"go{attempt}"
        procs = [subprocess.Popen([sys.executable, "-c", _OPENER, str(db), str(go)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env) for _ in range(12)]
        go.touch()
        results = [p.communicate(timeout=120) for p in procs]
        assert all(p.returncode == 0 for p in procs), [err for _, err in results]
        assert {out.strip() for out, _ in results} == {"wal"}


def test_registered_new_user_table_with_generated_guards_is_allowed(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from app import guards

    m, c = _real_schema(tmp_path)
    monkeypatch.setitem(guards.GUARDED, "notes", guards.Guard(grants=guards._own("notes"), immutable=("id", "user_id")))
    ddl = ["CREATE TABLE notes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id)"
           " ON DELETE RESTRICT, body TEXT NOT NULL) STRICT", *guards.guard_triggers("notes").values(),
           "CREATE INDEX idx_notes_user ON notes (user_id)",
           "CREATE VIEW note_counts AS SELECT user_id, COUNT(*) AS n FROM notes GROUP BY user_id",
           "ALTER TABLE tasks ADD COLUMN done_note TEXT CHECK (done_note IS NULL OR length(done_note) < 500)"]
    write(m, "002_notes.sql", ";\n".join(ddl) + ";")
    assert migrate(c, m) == ["002_notes"]
    uid = c.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
    c.execute("INSERT INTO notes (user_id, body) VALUES (?, 'hi')", (uid,))
    with pytest.raises(sqlite3.IntegrityError, match="ADR-004"):
        c.execute("DELETE FROM notes")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        c.execute("UPDATE notes SET rowid = 99")


def test_replace_function_is_not_a_false_positive(tmp_path: Path) -> None:
    m, c = _real_schema(tmp_path)
    write(m, "002_view.sql",
          "CREATE VIEW task_titles AS SELECT id FROM tasks WHERE title = 'a' OR replace(title, ' ', '') = 'b';")
    assert migrate(c, m) == ["002_view"]


def _requoted_guard(table: str, name: str, old: str, new: str) -> str:
    from app.guards import guard_triggers

    sql = guard_triggers(table)[name]
    assert old in sql
    return f"DROP TRIGGER {name};\n{sql.replace(old, new)};"


@pytest.mark.parametrize(
    "migration",
    [
        # round 3: a double-quoted WHEN clause normalized equal to the template but SQLite may
        # read it as a constant string, disabling the guard
        lambda: _requoted_guard(
            "weight_records", "weight_records_delete_requires_grant", "WHEN NOT (EXISTS", 'WHEN "NOT (EXISTS'
        ).replace("OLD.user_id))\nBEGIN", 'OLD.user_id))"\nBEGIN'),
        lambda: _requoted_guard("tasks", "tasks_identity_immutable",
                                "WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id",
                                'WHEN "NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id"'),
        # literal case matters: 'WEIGHT_RECORDS' would stop sanctioned deletion from working
        lambda: _requoted_guard("weight_records", "weight_records_delete_requires_grant",
                                "table_name = 'weight_records'", "table_name = 'WEIGHT_RECORDS'"),
    ],
)
def test_guard_with_quoted_or_altered_literals_is_rejected(tmp_path: Path, migration) -> None:  # type: ignore[no-untyped-def]
    m, c = _real_schema(tmp_path)
    write(m, "002_x.sql", migration())
    with pytest.raises(MigrationError, match="missing or altered"):
        migrate(c, m)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM weight_records")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE tasks SET rowid = rowid + 100")


def test_guard_recreated_with_different_keyword_case_and_spacing_is_accepted(tmp_path: Path) -> None:
    from app.guards import guard_triggers

    m, c = _real_schema(tmp_path)
    sql = guard_triggers("tasks")["tasks_identity_immutable"]
    variant = sql.replace("BEFORE UPDATE ON", "before   update\n on").replace("RAISE(ABORT", "raise ( abort")
    write(m, "002_x.sql", f"DROP TRIGGER tasks_identity_immutable;\n{variant};")
    assert migrate(c, m) == ["002_x"]


def test_double_quoted_strings_are_identifiers_on_app_connections(tmp_path: Path) -> None:
    import sys as _sys

    c = connect(tmp_path / "q.db")
    if _sys.version_info < (3, 12):
        pytest.skip("sqlite3.Connection.setconfig needs Python 3.12+")
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        c.execute('SELECT "not a column"')


@pytest.mark.parametrize("spelling", ['"deletion_grants"', "[deletion_grants]", "`deletion_grants`",
                                      "'deletion_grants'", '"Deletion_Grants"', "DeLeTiOn_GrAnTs"])
def test_extra_trigger_referencing_grants_in_any_spelling_is_rejected(tmp_path: Path, spelling: str) -> None:
    m, c = _real_schema(tmp_path)
    write(m, "002_x.sql", f"CREATE TRIGGER g BEFORE DELETE ON weight_records BEGIN INSERT INTO {spelling} SELECT"
                          " 'weight_records', OLD.id; END;")
    with pytest.raises(MigrationError, match="must not reference deletion_grants"):
        migrate(c, m)
    with pytest.raises(sqlite3.IntegrityError, match="ADR-004"):
        c.execute("DELETE FROM weight_records")


def test_view_over_grants_with_instead_of_trigger_is_rejected(tmp_path: Path) -> None:
    m, c = _real_schema(tmp_path)
    write(m, "002_x.sql", 'CREATE VIEW g AS SELECT * FROM "deletion_grants";\n'
                          "CREATE TRIGGER gi INSTEAD OF INSERT ON g BEGIN SELECT 1; END;\n"
                          "CREATE TRIGGER gd BEFORE DELETE ON weight_records BEGIN INSERT INTO g VALUES"
                          " ('weight_records', OLD.id); END;")
    with pytest.raises(MigrationError, match="view g must not reference deletion_grants"):
        migrate(c, m)


@pytest.mark.parametrize(
    "migration",
    [
        "INSERT INTO deletion_grants VALUES ('users', 1);",
        "INSERT INTO deletion_grants SELECT 'weight_records', id FROM weight_records;",
        "DROP TABLE deletion_grants;\nCREATE TABLE deletion_grants (table_name TEXT NOT NULL, row_id INTEGER NOT NULL,"
        " PRIMARY KEY (table_name, row_id)) STRICT;\nINSERT INTO deletion_grants VALUES ('users', 1);",
        "DROP TABLE deletion_grants;\nCREATE TABLE deletion_grants (table_name TEXT NOT NULL, row_id INTEGER NOT NULL"
        " REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (table_name, row_id)) STRICT;",
        "DROP TABLE deletion_grants;\nCREATE TABLE deletion_grants (table_name TEXT, row_id INTEGER);",
    ],
)
def test_migration_cannot_leave_grants_or_alter_grants_table(tmp_path: Path, migration: str) -> None:
    m, c = _real_schema(tmp_path)
    write(m, "002_x.sql", migration)
    with pytest.raises(MigrationError, match="deletion_grants"):
        migrate(c, m)
    assert c.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="ADR-004"):
        c.execute("DELETE FROM tasks")


def test_startup_refuses_database_with_standing_grants(tmp_path: Path) -> None:
    db = tmp_path / "g.db"
    migrate(connect(db))
    raw = sqlite3.connect(db)  # an external tool, outside the app's guards
    raw.execute("INSERT INTO deletion_grants VALUES ('users', 1)")
    raw.commit()
    raw.close()
    with pytest.raises(MigrationError, match="standing grants"):
        migrate(connect(db))
