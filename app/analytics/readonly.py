"""Hardened read-only SQLite connections for the analytics tools (ADR-005 §3.3 SQL-side guarantees).

`harden_read_only(conn)` turns a connection from `app.db.connect` into one that can only read:

- `PRAGMA query_only = ON` (set first, while the app authorizer still allows that PRAGMA);
- an authorizer that allows only SELECT, READ, FUNCTION, RECURSIVE and TRANSACTION (BEGIN /
  COMMIT / ROLLBACK of a read snapshot) and denies every other action, including PRAGMA,
  ATTACH, writes and temporary objects;
- a progress handler that aborts a running statement once the current time budget is spent.

Time budget: `time_budget(conn, seconds)` sets a deadline for everything run inside it (the
analytics tools wrap each tool call in one, so a call's statements share the budget). The
deadline lives in a `ContextVar`, because a `sqlite3.Connection` cannot carry attributes or weak
references; the progress handler runs synchronously in the thread executing the statement, so
it sees that thread's budget. Outside any budget the deadline armed by `harden_read_only`
(hardening time + `SQL_STATEMENT_TIMEOUT_S`) applies, so unbudgeted work fails closed rather
than running unbounded. An aborted statement raises `sqlite3.OperationalError("interrupted")`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from app.analytics import policy
from app.db import connect

_ALLOWED_ACTIONS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                              sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION})
_PROGRESS_STEPS = 1000  # VM instructions between deadline checks

_QUERY_ONLY_ON = "PRAGMA query_only = ON"
_PROBE_PRAGMA = "PRAGMA query_only"
_BEGIN = "BEGIN"
_COMMIT = "COMMIT"
_ROLLBACK = "ROLLBACK"

_DEADLINE: ContextVar[float | None] = ContextVar("analytics_sql_deadline", default=None)


def _read_only_authorizer(action: int, _arg1: str | None, _arg2: str | None, _db: str | None,
                          _src: str | None) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _progress_handler(fallback_deadline: float) -> Callable[[], int]:
    def handler() -> int:
        deadline = _DEADLINE.get()
        limit = fallback_deadline if deadline is None else deadline
        return 1 if time.monotonic() > limit else 0  # non-zero aborts the statement
    return handler


def harden_read_only(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Make `conn` (from `app.db.connect`) read-only and time-limited; returns the same object.

    Replaces the app authorizer. Raises `ValueError` if the connection is inside a transaction
    (hardening must happen right after opening).
    """
    if conn.in_transaction:
        raise ValueError("harden_read_only needs a connection outside a transaction")
    conn.execute(_QUERY_ONLY_ON)  # before the authorizer, which denies every PRAGMA
    conn.set_authorizer(_read_only_authorizer)
    conn.set_progress_handler(_progress_handler(time.monotonic() + policy.SQL_STATEMENT_TIMEOUT_S),
                              _PROGRESS_STEPS)
    return conn


def open_read_only(path: Path | str) -> sqlite3.Connection:
    """Open the database through `app.db.connect` and harden it (see module docstring)."""
    return harden_read_only(connect(path))


def is_read_only(conn: sqlite3.Connection) -> bool:
    """True if `conn` carries the read-only authorizer (a PRAGMA is refused).

    Used as a cheap fail-closed check before tools run; it does not change any state.
    """
    try:
        conn.execute(_PROBE_PRAGMA).fetchall()
    except sqlite3.DatabaseError as exc:
        return "not authorized" in str(exc)
    return False


@contextmanager
def time_budget(conn: sqlite3.Connection, seconds: float = policy.SQL_STATEMENT_TIMEOUT_S) -> Iterator[None]:
    """Run the block with a SQL deadline of `seconds` from now (nested budgets restore the outer one).

    Raises `ValueError` if `conn` is not hardened, so a writable connection can never be used
    by the tools by mistake. Statements still running at the deadline raise
    `sqlite3.OperationalError` ("interrupted").
    """
    if not is_read_only(conn):
        raise ValueError("time_budget requires a connection hardened by harden_read_only")
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """A deferred read transaction so the queries of one tool call see one consistent snapshot.

    Joins the caller's transaction if one is already open.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute(_BEGIN)
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute(_ROLLBACK)
        raise
    conn.execute(_COMMIT)
