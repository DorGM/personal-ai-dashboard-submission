"""Request dependencies: one guarded DB connection per request and the signed-in user."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request

from app import auth
from app.db import connect
from app.demo import DemoError


class SignInRequired(Exception):
    """Raised by `CurrentUser` when nobody is signed in; the app redirects to the sign-in page."""


class DataUnavailable(Exception):
    """Raised by `Db` when the demo working copy cannot be opened; the app shows a 503 page."""


def _db(request: Request) -> Iterator[sqlite3.Connection]:
    """One guarded connection per request (app.db.connect), closed afterwards.

    In demo mode it is a connection to the current demo working copy (checked to be one);
    DATABASE_PATH is never opened then.
    """
    demo = getattr(request.app.state, "demo", None)
    try:
        conn = demo.connect() if demo is not None else connect(request.app.state.settings.database_path)
    except DemoError as exc:
        raise DataUnavailable(str(exc)) from None
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(_db)]


def _user(request: Request, conn: Db) -> sqlite3.Row:
    user = auth.current_user(request, conn)
    if user is None:
        raise SignInRequired
    return user


CurrentUser = Annotated[sqlite3.Row, Depends(_user)]
"""The signed-in user's row (id from the server-side session only, never from the client)."""


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


FormData = Annotated[dict[str, str], Depends(_form)]
"""Submitted URL-encoded form fields (text values only)."""
