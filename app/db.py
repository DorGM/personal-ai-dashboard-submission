"""SQLite connection and migration runner (data-preservation rules: docs/architecture/ADR-004)."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.guards import GRANTS_TABLE_SQL, GUARDED, guard_triggers, normalize_sql

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Statements a migration may not contain: they would end or nest our transaction, change
# connection-level state, or reach outside the main database.
_FORBIDDEN_KEYWORDS = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA", "VACUUM",
                       "ATTACH", "DETACH"}
# Whitespace, comments and stray ";" before a statement's first keyword.
_LEADING_COMMENTS = re.compile(r"\A(?:\s+|;|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", re.DOTALL)

# Settings that would switch the data-preservation guards off, or risk corruption, if changed
# at runtime (connect() sets journal_mode itself before the authorizer is installed).
_GUARD_PRAGMAS = {"foreign_keys", "recursive_triggers", "writable_schema", "ignore_check_constraints",
                  "defer_foreign_keys", "journal_mode", "synchronous", "locking_mode",
                  # The Demo Mode marker (ADR-005 §6.2): only stamp_demo_application_id may write it.
                  "application_id"}
# App connections may not change the schema at all (temporary tables, indexes and views stay
# allowed for queries); only the migration runner can.
_APP_DENIED = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_VIEW,
               sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_VTABLE,
               sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_INDEX,
               sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_ALTER_TABLE,
               sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
_READ_ONLY_PRAGMAS = {"quick_check", "integrity_check", "table_info", "table_xinfo", "index_list", "index_info",
                      "index_xinfo", "foreign_key_list", "foreign_key_check"}
# Temporary objects would outlive the migration on this connection and escape the invariants.
_MIGRATION_DENIED = {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_PRAGMA,
                     sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_CREATE_TEMP_TABLE,
                     sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_VIEW,
                     sqlite3.SQLITE_CREATE_TEMP_TRIGGER}


DEMO_APPLICATION_ID = 0x44454D4F  # "DEMO": marks a synthetic Demo Mode working copy (ADR-005 §6.2)


class MigrationError(RuntimeError):
    """A migration was rejected or failed; the database is unchanged."""


def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the data-preservation guards enforced (ADR-004).

    - foreign_keys and recursive_triggers are on; recursive_triggers makes the implicit delete
      of INSERT OR REPLACE fire the delete guards (D3).
    - An authorizer refuses schema changes (CREATE, DROP, ALTER, ATTACH) and changing those
      settings; only the migration runner may change the schema.
    Transactions are explicit (isolation_level=None).

    `read_only=True` opens the analytics connection of ADR-005 §3.3 instead; see
    `_connect_read_only`. It raises FileNotFoundError when the database file does not exist.
    """
    if read_only:
        return _connect_read_only(path)
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    _disable_double_quoted_strings(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        _enable_wal(conn)
    conn.set_authorizer(_app_authorizer)
    return conn


READ_ONLY_STATEMENT_SECONDS = 2.0
"""A statement on a read-only connection is aborted ("interrupted") after this long (ADR-005 §3.5)."""
_READ_ONLY_PROGRESS_STEPS = 1000  # VM instructions between deadline checks
_READ_ONLY_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                      sqlite3.SQLITE_RECURSIVE}
# BEGIN (deferred), COMMIT and ROLLBACK give a consistent read snapshot; BEGIN IMMEDIATE and
# EXCLUSIVE share the same action code but fail anyway because query_only forbids the write lock.
_READ_ONLY_TRANSACTION = {"BEGIN", "COMMIT", "ROLLBACK"}


def _connect_read_only(path: Path | str) -> sqlite3.Connection:
    """The read-only analytics connection (ADR-005 §3.3).

    - The file must already exist: it is opened with `mode=rw` (never `rwc`), so nothing,
      not even a parent directory, is created. `:memory:` is refused.
    - `PRAGMA query_only = ON`, plus an authorizer that allows only SELECT, READ, FUNCTION,
      RECURSIVE and BEGIN/COMMIT/ROLLBACK. Everything else is denied, including every PRAGMA
      and ATTACH, with one exception: reading (never setting) `application_id`, which the
      Demo Mode checks (`app.demo.assert_demo_database`) need.
    - Each statement gets a deadline of READ_ONLY_STATEMENT_SECONDS from the moment it starts
      running (a trace callback marks the start, also for re-used cached statements); a
      progress handler aborts it after that with `sqlite3.OperationalError: interrupted`.
      The busy timeout is bounded the same way.
    `mode=ro` is not used: on a WAL database it can fail when the -shm file is absent.
    """
    if str(path) == ":memory:" or not Path(path).is_file():
        raise FileNotFoundError("read-only connection refused: the database file does not exist")
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False,
                           timeout=READ_ONLY_STATEMENT_SECONDS)
    try:
        conn.row_factory = sqlite3.Row
        _disable_double_quoted_strings(conn)
        conn.execute("PRAGMA query_only = ON")
        deadline = [0.0]  # fail closed: without a start mark every long statement is aborted

        def mark_start(_statement: str) -> None:  # the SQL text is ignored, never logged
            deadline[0] = time.monotonic() + READ_ONLY_STATEMENT_SECONDS

        def over_deadline() -> int:
            return 1 if time.monotonic() > deadline[0] else 0

        conn.set_trace_callback(mark_start)
        conn.set_progress_handler(over_deadline, _READ_ONLY_PROGRESS_STEPS)
        conn.set_authorizer(_read_only_authorizer)
    except BaseException:
        conn.close()
        raise
    return conn


def _read_only_authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None,
                          _src: str | None) -> int:
    if action in _READ_ONLY_ALLOWED:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_TRANSACTION and (arg1 or "").upper() in _READ_ONLY_TRANSACTION:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() == "application_id" and arg2 is None:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _disable_double_quoted_strings(conn: sqlite3.Connection) -> None:
    # Make "x" always an identifier, never a string (Python 3.12+; on 3.11 the guard
    # comparison in app.guards.normalize_sql keeps quotes, so it fails safe either way).
    setconfig = getattr(conn, "setconfig", None)
    if setconfig is not None:
        for name in ("SQLITE_DBCONFIG_DQS_DML", "SQLITE_DBCONFIG_DQS_DDL"):
            if hasattr(sqlite3, name):
                setconfig(getattr(sqlite3, name), False)


def _enable_wal(conn: sqlite3.Connection, wait_seconds: float = 30.0) -> None:
    # Switching a new database to WAL ignores busy_timeout, so concurrent first opens retry.
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def _app_authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _src: str | None) -> int:
    if action in _APP_DENIED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _GUARD_PRAGMAS and arg2 is not None:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _stamp_authorizer(action: int, arg1: str | None, *_rest: str | None) -> int:
    allowed = action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() == "application_id"
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY


def application_id(conn: sqlite3.Connection) -> int:
    """The database header's application_id (0 for an ordinary database)."""
    return int(conn.execute("PRAGMA application_id").fetchone()[0])


def stamp_demo_application_id(conn: sqlite3.Connection) -> None:
    """Mark a freshly built database as a Demo Mode working copy. Only the demo builder calls this.

    App connections cannot write `application_id` (their authorizer denies it), so no request
    can turn a personal database into a "demo" one or remove the marker. This function lifts
    that for exactly one statement: a temporary authorizer that allows only
    `PRAGMA application_id` and denies everything else. It refuses (ValueError) a database
    that already has users or another application_id, so it can only mark an empty new copy.
    """
    has_users = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
    if has_users and conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        raise ValueError("refusing to mark a database that already has users as a demo copy")
    if application_id(conn) not in {0, DEMO_APPLICATION_ID}:
        raise ValueError("refusing to mark a database that belongs to another application")
    conn.set_authorizer(_stamp_authorizer)
    try:
        conn.execute(f"PRAGMA application_id = {int(DEMO_APPLICATION_ID)}")  # PRAGMA can't bind; constant int
    finally:
        conn.set_authorizer(_app_authorizer)


def _migration_authorizer(action: int, arg1: str | None, *_rest: str | None) -> int:
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _READ_ONLY_PRAGMAS:
        return sqlite3.SQLITE_OK  # SQLite itself runs quick_check during ALTER TABLE ADD COLUMN
    return sqlite3.SQLITE_DENY if action in _MIGRATION_DENIED else sqlite3.SQLITE_OK


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE … COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:  # a failed statement may already have rolled back
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def split_statements(sql: str) -> list[str]:
    """Split a script into complete statements (trigger bodies stay whole)."""
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if _LEADING_COMMENTS.sub("", buffer).strip().strip(";").strip():
                statements.append(buffer.strip())
            buffer = ""
    if _LEADING_COMMENTS.sub("", buffer).strip():
        raise MigrationError("migration ends with an incomplete statement")
    return statements


def _first_keyword(statement: str) -> str:
    body = _LEADING_COMMENTS.sub("", statement)
    match = re.match(r"[A-Za-z]+", body)
    return match.group(0).upper() if match else ""


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    query = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    names = [r[0] for r in conn.execute(query)]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}  # noqa: S608


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending NNN_name.sql files in order. Returns the versions applied by this call.

    Also refuses (MigrationError) a database holding standing deletion grants.

    Each file runs statement by statement inside one BEGIN IMMEDIATE transaction, so it is
    all-or-nothing, and concurrent runners serialize on the write lock; the applied-versions
    check happens after the lock is held. Transaction control, PRAGMA and ATTACH/DETACH are
    refused twice: by keyword before anything runs, and by an SQLite authorizer while the
    statements run. Before COMMIT the runner verifies that no pre-existing table lost rows,
    that the data-preservation guards are still in place (_check_invariants), and that
    integrity and foreign-key checks pass; otherwise it rolls back and raises MigrationError.
    """
    conn.set_authorizer(_migration_authorizer)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))) STRICT"
        )
    finally:
        conn.set_authorizer(_app_authorizer)
    applied: list[str] = []
    for file in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
        statements = split_statements(file.read_text(encoding="utf-8"))
        bad = sorted({kw for kw in map(_first_keyword, statements) if kw in _FORBIDDEN_KEYWORDS})
        if bad:
            raise MigrationError(f"{file.name}: statements not allowed in migrations: {', '.join(bad)}")
        if _apply(conn, file.stem, statements):
            applied.append(file.stem)
    _check_no_standing_grants(conn)
    return applied


def _check_no_standing_grants(conn: sqlite3.Connection) -> None:
    # Runs at every startup: a grant left behind (e.g. by an external tool) would make rows
    # deletable by any stray DELETE, so refuse to start until it is removed.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'deletion_grants'").fetchone() \
            and conn.execute("SELECT 1 FROM deletion_grants LIMIT 1").fetchone():
        raise MigrationError("deletion_grants contains standing grants; refusing to start (ADR-004 D2)")


def _apply(conn: sqlite3.Connection, version: str, statements: list[str]) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
            conn.execute("ROLLBACK")
            return False
        before = _row_counts(conn)
        versions_before = _versions(conn)
        conn.set_authorizer(_migration_authorizer)
        try:
            for statement in statements:
                try:
                    conn.execute(statement)  # execute() runs exactly one statement
                except sqlite3.DatabaseError as exc:
                    if "not authorized" in str(exc):
                        raise MigrationError(
                            f"{version}: transaction control, PRAGMA and ATTACH/DETACH are not allowed in migrations"
                        ) from None
                    raise
                if not conn.in_transaction:
                    raise MigrationError(f"{version}: a statement ended the transaction")
        finally:
            conn.set_authorizer(_app_authorizer)
        after = _row_counts(conn)
        lost = [t for t, n in before.items() if after.get(t, 0) < n]
        if lost:
            raise MigrationError(f"{version}: would remove rows from {', '.join(sorted(lost))}")
        if _versions(conn) != versions_before:
            raise MigrationError(f"{version}: must not change schema_migrations")
        _check_invariants(conn, version)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError(f"{version}: integrity_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError(f"{version}: foreign key violations")
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        conn.execute("COMMIT")
        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _versions(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")]


_SILENT_CONFLICT = re.compile(
    r"\bON\s+CONFLICT\s+(?:REPLACE|IGNORE)\b|\bOR\s+(?:REPLACE|IGNORE)\b(?!\s*\()|\bREPLACE\s+INTO\b"
)
_EXEMPT_TABLES = {"schema_migrations", "deletion_grants"}


def _check_invariants(conn: sqlite3.Connection, version: str) -> None:
    """The ADR-004 guards must survive every migration (a rebuilt table must re-create them).

    - every registered user-owned table (app/guards.py) has its guard triggers exactly as
      generated by guard_triggers(); no other trigger or view may reference deletion_grants;
    - any table with a user_id column or a foreign key to a registered table is registered,
      and its user_id references users; no foreign key cascades or sets null;
    - deletion_grants has its exact definition and holds no rows (grants exist only inside
      app/deletion.py's savepoint);
    - nothing uses REPLACE/IGNORE conflict resolution; no temporary triggers exist.
    """
    schema = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    problems: list[str] = []
    tables = {r["name"].lower() for r in schema if r["type"] == "table"}
    if tables & GUARDED.keys():
        grants = next((r for r in schema if r["type"] == "table" and r["name"].lower() == "deletion_grants"), None)
        if grants is None or normalize_sql(grants["sql"], keep_literals=True) != normalize_sql(
                GRANTS_TABLE_SQL, keep_literals=True):
            problems.append("deletion_grants table is missing or altered")
        elif conn.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0]:
            problems.append("deletion_grants must be empty (no standing grants)")
    for row in schema:
        if _SILENT_CONFLICT.search(normalize_sql(row["sql"], keep_literals=False)):
            problems.append(f"{row['name']} uses REPLACE/IGNORE conflict resolution")
    triggers = {r["name"].lower(): r for r in schema if r["type"] == "trigger"}
    required: dict[str, tuple[str, str]] = {}
    for table in sorted(tables & GUARDED.keys()):
        for name, sql in guard_triggers(table).items():
            required[name.lower()] = (table, normalize_sql(sql, keep_literals=True))
    for name, (table, expected) in required.items():
        row = triggers.get(name)
        if row is None or row["tbl_name"].lower() != table or normalize_sql(row["sql"], keep_literals=True) != expected:
            problems.append(f"{table}: guard trigger {name} is missing or altered")
    # Raw, case-insensitive search: catches every quoting form ("x", [x], `x`, 'x'); a comment
    # that mentions the name is refused too, which fails safe.
    for row in schema:
        name = row["name"].lower()
        if row["type"] in {"trigger", "view"} and name not in required and "deletion_grants" in row["sql"].lower():
            problems.append(f"{row['type']} {name} must not reference deletion_grants")
    if conn.execute("SELECT 1 FROM sqlite_temp_master WHERE type = 'trigger'").fetchone():
        problems.append("temporary triggers are not allowed")
    for table in sorted(tables - _EXEMPT_TABLES):
        columns = {c["name"].lower() for c in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}
        fks = conn.execute('SELECT "from", "table", "to", on_delete, on_update FROM pragma_foreign_key_list(?)',
                           (table,)).fetchall()
        for fk in fks:
            if fk["on_delete"] not in {"RESTRICT", "NO ACTION"} or fk["on_update"] not in {"RESTRICT", "NO ACTION"}:
                problems.append(f"{table}: foreign key to {fk['table']} must be ON DELETE/UPDATE RESTRICT")
        owned = "user_id" in columns or any(fk["table"].lower() in GUARDED for fk in fks)
        if owned and table not in GUARDED:
            problems.append(f"{table}: user-owned table must be registered in app/guards.py with its guards")
        # user_id must point at users(id) directly, or via a composite (parent_id, user_id) key
        # on a registered parent, which itself references users.
        if table in GUARDED and table != "users" and not any(
            fk["from"].lower() == "user_id"
            and ((fk["table"].lower() == "users" and fk["to"] in {None, "id"})
                 or (fk["table"].lower() in GUARDED and (fk["to"] or "").lower() == "user_id"))
            for fk in fks
        ):
            problems.append(f"{table}: user_id must reference users(id)")
    if problems:
        raise MigrationError(f"{version}: breaks data-preservation guards: {'; '.join(problems)}")
