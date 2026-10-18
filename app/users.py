"""User identity persistence for Google sign-in (docs/architecture/ADR-004 D5).

Sign-in must never delete or re-create a user row: the first sign-in INSERTs, every later
(or concurrent) sign-in finds the same row by its stable `google_sub`, so `users.id` and all
records owned by it are preserved.
"""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager, nullcontext

from app.db import transaction


def get_or_create_user(conn: sqlite3.Connection, google_sub: str, email: str, display_name: str | None) -> int:
    """Return the id of the user with this Google subject, creating the row on first sign-in.

    Runs in its own transaction, or inside the caller's if one is already open.
    Concurrent first sign-ins are safe: the UNIQUE(google_sub) conflict is resolved with
    DO NOTHING (never REPLACE), and the existing row is then read. Only the descriptive
    profile fields are refreshed, by a targeted UPDATE that touches nothing else.
    """
    if not google_sub.strip():
        raise ValueError("google_sub is required")
    if not (email or "").strip():
        raise ValueError("email is required")
    scope: AbstractContextManager[object] = nullcontext() if conn.in_transaction else transaction(conn)
    with scope:
        conn.execute(
            "INSERT INTO users (google_sub, email, display_name) VALUES (?, ?, ?)"
            " ON CONFLICT (google_sub) DO NOTHING",
            (google_sub, email, display_name),
        )
        row = conn.execute("SELECT id, email, display_name FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
        if (row["email"], row["display_name"]) != (email, display_name):
            conn.execute(
                "UPDATE users SET email = ?, display_name = ? WHERE id = ?", (email, display_name, row["id"])
            )
    return int(row["id"])
