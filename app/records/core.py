"""Generic, user-scoped repository for one record table (the only write path for records).

Each domain module (app/records/<domain>.py) declares a `DomainSpec` and exposes the bound
methods of a `Repository`. All functions take `(conn, user_id, …)` and need no request, so
the Demo Mode importer (B-14) and AI intake (B-06) reuse them.

Rules enforced here (ADR-004, A-03):
- SQL is built once, at import, from the constant names in the spec; values are always bound
  with `?`. Every statement filters on `user_id = ?`; edits target
  `WHERE id = ? AND user_id = ? AND deleted_at IS NULL` and name their columns.
- Canonical keys are recomputed on create and on edit. UNIQUE violations become
  `DuplicateRecord` (an active row holds the key) or `DeletedDuplicate` (a soft-deleted row
  holds it). Nothing is ever upserted or overwritten.
- Soft delete sets `deleted_at`; restore clears it. Permanent delete is only possible for a
  soft-deleted row, with explicit confirmation, through `app.deletion.permanently_delete`.
- Each write is atomic: its own transaction, or a savepoint inside the caller's transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.db import transaction
from app.deletion import DeletionError, permanently_delete
from app.records.dates import Window, check_date, history_window, today_local
from app.records.errors import DeletedDuplicate, DuplicateRecord, InvalidRecord, NotFound

M = TypeVar("M", bound=BaseModel)

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
SOFT_DELETE_CONFIRM = "delete"
PERMANENT_CONFIRM = "permanent"


@dataclass(frozen=True)
class DomainSpec(Generic[M]):
    """Static description of one record table. Every name here is a constant, never input."""

    name: str  # domain name (ADR-005 Domain enum), e.g. "weight"
    table: str
    model: type[M]
    date_column: str  # record_date, application_date or task_date
    columns: tuple[str, ...]  # writable data columns; each is a field of `model`
    unique: tuple[str, ...]  # business key columns besides user_id (UNIQUE in the schema)
    key: Callable[[M], str] | None = None  # canonical_key from app/canonical.py, if the table has one
    order: str = "r.id"  # secondary sort inside a day (constant SQL)
    days_ahead: int = 0  # how far in the future the date may be (tasks: 14)
    select: str = "r.*"
    joins: str = ""
    ascending: bool = False  # history day order: newest first, except tasks


@dataclass(frozen=True)
class Day:
    """One calendar day of a history window; `records` empty means undocumented (UI placeholder)."""

    day: date
    records: list[sqlite3.Row]


@dataclass(frozen=True)
class History:
    window: Window
    days: list[Day]


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """A transaction of its own, or a savepoint inside the caller's open transaction."""
    if not conn.in_transaction:
        with transaction(conn):
            yield
        return
    conn.execute("SAVEPOINT records_write")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO records_write")
            conn.execute("RELEASE records_write")
        raise
    conn.execute("RELEASE records_write")


def _db_value(value: object) -> object:
    return value.isoformat() if isinstance(value, date) else value


class Repository(Generic[M]):
    """create / get / history / update / soft_delete / restore / permanent_delete for one table."""

    def __init__(self, spec: DomainSpec[M]) -> None:
        self.spec = spec
        t, d = spec.table, spec.date_column
        cols = (*spec.columns, "canonical_key") if spec.key else spec.columns
        source = f"{t} AS r {spec.joins}"
        # Built from the constant spec only (see class docstring); values are bound with ?.
        self._insert = (f"INSERT INTO {t} (user_id, {', '.join(cols)}) "  # noqa: S608
                        f"VALUES (?{', ?' * len(cols)})")
        self._update = (f"UPDATE {t} SET {', '.join(c + ' = ?' for c in cols)}, updated_at = {NOW_SQL} "  # noqa: S608
                        "WHERE id = ? AND user_id = ? AND deleted_at IS NULL")
        self._get = f"SELECT {spec.select} FROM {source} WHERE r.id = ? AND r.user_id = ? AND r.deleted_at IS NULL"  # noqa: S608
        self._get_deleted = (f"SELECT {spec.select} FROM {source} "  # noqa: S608
                             "WHERE r.id = ? AND r.user_id = ? AND r.deleted_at IS NOT NULL")
        self._range = (f"SELECT {spec.select} FROM {source} WHERE r.user_id = ? AND r.deleted_at IS NULL "  # noqa: S608
                       f"AND r.{d} BETWEEN ? AND ? ORDER BY r.{d}, {spec.order}")
        self._earliest = f"SELECT MIN({d}) FROM {t} WHERE user_id = ? AND deleted_at IS NULL"  # noqa: S608
        self._soft_delete = (f"UPDATE {t} SET deleted_at = {NOW_SQL}, updated_at = {NOW_SQL} "  # noqa: S608
                             "WHERE id = ? AND user_id = ? AND deleted_at IS NULL")
        self._restore = (f"UPDATE {t} SET deleted_at = NULL, updated_at = {NOW_SQL} "  # noqa: S608
                         "WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL")
        self._deleted = (f"SELECT {spec.select} FROM {source} WHERE r.user_id = ? AND r.deleted_at IS NOT NULL "  # noqa: S608
                         "ORDER BY r.deleted_at DESC, r.id DESC")
        self._holder = (f"SELECT id, deleted_at FROM {t} WHERE user_id = ? "  # noqa: S608
                        + "".join(f"AND {c} = ? " for c in spec.unique) + "AND id IS NOT ?")

    # ------------------------------------------------------------ reads
    def get(self, conn: sqlite3.Connection, user_id: int, record_id: int) -> sqlite3.Row:
        """The active (not soft-deleted) record. Raises NotFound, also for another user's id."""
        row: sqlite3.Row | None = conn.execute(self._get, (record_id, user_id)).fetchone()
        if row is None:
            raise NotFound
        return row

    def get_deleted(self, conn: sqlite3.Connection, user_id: int, record_id: int) -> sqlite3.Row:
        """A soft-deleted record of this user. Raises NotFound otherwise."""
        row: sqlite3.Row | None = conn.execute(self._get_deleted, (record_id, user_id)).fetchone()
        if row is None:
            raise NotFound
        return row

    def earliest(self, conn: sqlite3.Connection, user_id: int) -> date | None:
        """Date of the user's earliest active record in this domain, or None."""
        value = conn.execute(self._earliest, (user_id,)).fetchone()[0]
        return date.fromisoformat(value) if value else None

    def list_range(self, conn: sqlite3.Connection, user_id: int, start: date, end: date) -> list[sqlite3.Row]:
        """Active records with start <= date <= end, ordered by date."""
        return list(conn.execute(self._range, (user_id, start.isoformat(), end.isoformat())))

    def history(self, conn: sqlite3.Connection, user_id: int, *, today: date | None = None,
                start: date | None = None, end: date | None = None) -> History | None:
        """Day buckets for a ≤ 15-day window (see app.records.dates.history_window).

        Returns None when the user has no active records in this domain (empty state). Days
        without records have an empty list; the UI shows the placeholder, nothing is stored.
        """
        window = history_window(self.earliest(conn, user_id), today or today_local(), start=start, end=end,
                                days_ahead=self.spec.days_ahead)
        if window is None:
            return None
        by_day: dict[str, list[sqlite3.Row]] = {}
        for row in self.list_range(conn, user_id, window.start, window.end):
            by_day.setdefault(row[self.spec.date_column], []).append(row)
        days = [Day(d, by_day.get(d.isoformat(), [])) for d in window.days(ascending=self.spec.ascending)]
        return History(window, days)

    def list_deleted(self, conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
        """The user's soft-deleted records, most recently deleted first."""
        return list(conn.execute(self._deleted, (user_id,)))

    # ------------------------------------------------------------ writes
    def _values(self, data: M) -> list[object]:
        values = [_db_value(getattr(data, c)) for c in self.spec.columns]
        if self.spec.key:
            values.append(self.spec.key(data))
        return values

    def _check(self, data: M, today: date | None) -> M:
        if not isinstance(data, self.spec.model):
            raise TypeError(f"expected {self.spec.model.__name__}")
        check_date(getattr(data, self.spec.date_column), today or today_local(), days_ahead=self.spec.days_ahead)
        return data

    def _conflict(self, conn: sqlite3.Connection, user_id: int, data: M, exclude_id: int | None,
                  exc: sqlite3.IntegrityError) -> Exception:
        if "UNIQUE constraint failed" not in str(exc):
            return InvalidRecord("The record could not be saved because a value is out of range.")
        values = dict(zip((*self.spec.columns, "canonical_key"), self._values(data), strict=False))
        holder = conn.execute(self._holder, (user_id, *(values[c] for c in self.spec.unique), exclude_id)).fetchone()
        if holder is not None and holder["deleted_at"] is not None:
            return DeletedDuplicate(self.spec.name, int(holder["id"]))
        return DuplicateRecord()

    def create(self, conn: sqlite3.Connection, user_id: int, data: M, *, today: date | None = None) -> int:
        """Insert a new record and return its id. Never overwrites: a taken key raises
        DuplicateRecord or DeletedDuplicate; a future date raises InvalidRecord."""
        self._check(data, today)
        with atomic(conn):
            return self._insert_row(conn, user_id, data)

    def _insert_row(self, conn: sqlite3.Connection, user_id: int, data: M) -> int:
        try:
            cursor = conn.execute(self._insert, (user_id, *self._values(data)))
        except sqlite3.IntegrityError as exc:
            raise self._conflict(conn, user_id, data, None, exc) from None
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def update(self, conn: sqlite3.Connection, user_id: int, record_id: int, data: M, *,
               today: date | None = None) -> None:
        """Manual edit of an active record (all writable columns; canonical key recomputed).

        Raises NotFound (missing, soft-deleted or another user's), DuplicateRecord /
        DeletedDuplicate if the new values collide with another record, InvalidRecord.
        """
        self._check(data, today)
        with atomic(conn):
            self._update_row(conn, user_id, record_id, data)

    def _update_row(self, conn: sqlite3.Connection, user_id: int, record_id: int, data: M) -> None:
        try:
            cursor = conn.execute(self._update, (*self._values(data), record_id, user_id))
        except sqlite3.IntegrityError as exc:
            raise self._conflict(conn, user_id, data, record_id, exc) from None
        if cursor.rowcount != 1:
            raise NotFound

    def soft_delete(self, conn: sqlite3.Connection, user_id: int, record_id: int, *, confirm: str) -> None:
        """Hide an active record (recoverable). `confirm` must be "delete" (explicit intent)."""
        if confirm != SOFT_DELETE_CONFIRM:
            raise InvalidRecord("Deletion was not confirmed.")
        with atomic(conn):
            if conn.execute(self._soft_delete, (record_id, user_id)).rowcount != 1:
                raise NotFound

    def restore(self, conn: sqlite3.Connection, user_id: int, record_id: int) -> None:
        """Bring back a soft-deleted record. Its key was reserved, so this can't collide."""
        with atomic(conn):
            if conn.execute(self._restore, (record_id, user_id)).rowcount != 1:
                raise NotFound

    def permanent_delete(self, conn: sqlite3.Connection, user_id: int, record_id: int, *, confirm: str) -> int:
        """Permanently remove an already soft-deleted record and its owned children.

        `confirm` must be "permanent". Goes through app.deletion (grant + savepoint), so it is
        atomic and touches no other row. Returns the number of rows removed. Raises NotFound
        if the record is not soft-deleted or not this user's; InvalidRecord without confirmation.
        """
        if confirm != PERMANENT_CONFIRM:
            raise InvalidRecord("Permanent deletion was not confirmed.")
        with atomic(conn):
            self.get_deleted(conn, user_id, record_id)
            try:
                return permanently_delete(conn, user_id, self.spec.table, record_id)
            except DeletionError:
                raise NotFound from None
