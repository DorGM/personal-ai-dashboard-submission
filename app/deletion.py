"""The only sanctioned way to permanently delete rows (docs/architecture/ADR-004 D2).

The database refuses any DELETE that is not covered by a row in `deletion_grants`. This
module checks ownership, grants, deletes the row and its owned children, and revokes the
grant, all inside one savepoint in the caller's transaction, so the operation is atomic and
touches nothing else. User-facing confirmation and soft-delete flows are built on top of it later (B-04/B-07).
"""

from __future__ import annotations

import sqlite3

# table -> owned child tables and the column linking them to the parent row
_OWNED_CHILDREN: dict[str, tuple[tuple[str, str], ...]] = {
    "meals": (("meal_calorie_estimates", "meal_id"),),
}
_DELETABLE = {
    "weight_records", "meals", "workouts", "sleep_records", "step_records", "articles", "podcasts",
    "movies", "series", "job_applications", "job_search_profile", "tasks",
}


class DeletionError(LookupError):
    """The row does not exist or does not belong to the requesting user."""


def permanently_delete(conn: sqlite3.Connection, user_id: int, table: str, row_id: int) -> int:
    """Delete one user-owned row and its owned children. Returns the number of rows removed.

    Must run inside an open transaction (see app.db.transaction). Raises DeletionError if the
    row is missing or owned by someone else, ValueError for a table that can't be deleted from.
    """
    if table not in _DELETABLE:
        raise ValueError(f"permanent deletion is not supported for {table!r}")
    if not conn.in_transaction:
        raise RuntimeError("permanently_delete must run inside a transaction")
    owner = conn.execute(f"SELECT user_id FROM {table} WHERE id = ?", (row_id,)).fetchone()  # noqa: S608
    if owner is None or owner[0] != user_id:
        raise DeletionError(f"{table} {row_id} not found for this user")
    # A savepoint makes this call all-or-nothing on its own, even if a caller catches an error
    # and commits the surrounding transaction anyway.
    conn.execute("SAVEPOINT permanently_delete")
    try:
        conn.execute("INSERT INTO deletion_grants (table_name, row_id) VALUES (?, ?)", (table, row_id))
        removed = 0
        for child, column in _OWNED_CHILDREN.get(table, ()):
            removed += conn.execute(
                f"DELETE FROM {child} WHERE {column} = ? AND user_id = ?", (row_id, user_id)  # noqa: S608
            ).rowcount
        removed += conn.execute(f"DELETE FROM {table} WHERE id = ? AND user_id = ?", (row_id, user_id)).rowcount  # noqa: S608
        conn.execute("DELETE FROM deletion_grants WHERE table_name = ? AND row_id = ?", (table, row_id))
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO permanently_delete")
            conn.execute("RELEASE permanently_delete")
        raise
    conn.execute("RELEASE permanently_delete")
    return removed
