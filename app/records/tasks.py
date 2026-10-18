"""Tasks: dated up to 14 days ahead (A-03 E7); identity = start time + normalized title per
date (E12). Several tasks per category and day are allowed; status toggles pending/completed."""

from __future__ import annotations

import re
import sqlite3
from typing import Annotated, Literal

from pydantic import AfterValidator

from app import canonical
from app.records._fields import Name, Opt, OptLongText, RecordIn
from app.records.core import NOW_SQL, DomainSpec, Repository, atomic
from app.records.dates import TASK_DAYS_AHEAD, RecordDate
from app.records.errors import NotFound

Category = Literal["family", "career", "friends", "housework", "other"]
Status = Literal["pending", "completed"]
CATEGORIES: tuple[str, ...] = ("family", "career", "friends", "housework", "other")
STATUSES: tuple[str, ...] = ("pending", "completed")
_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _clock_time(value: str) -> str:
    if not _TIME.match(value):
        raise ValueError("Enter a time as HH:MM (00:00–23:59).")
    return value


StartTime = Annotated[str, AfterValidator(_clock_time)]


class TaskIn(RecordIn):
    task_date: RecordDate
    start_time: Opt[StartTime] = None
    title: Name
    description: OptLongText = None
    category: Category
    status: Status = "pending"


SPEC = DomainSpec(
    name="tasks", table="tasks", model=TaskIn, date_column="task_date",
    columns=("task_date", "start_time", "title", "description", "category", "status"),
    unique=("task_date", "canonical_key"), key=lambda t: canonical.task_key(t.start_time, t.title),
    order="r.start_time IS NULL, r.start_time, r.id", days_ahead=TASK_DAYS_AHEAD, ascending=True,
)
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest

_SET_STATUS_SQL = (f"UPDATE tasks SET status = ?, updated_at = {NOW_SQL}"  # noqa: S608
                   " WHERE id = ? AND user_id = ? AND deleted_at IS NULL")


def set_status(conn: sqlite3.Connection, user_id: int, record_id: int, status: str) -> None:
    """Mark a task pending or completed (targeted UPDATE). Raises NotFound or ValueError."""
    if status not in STATUSES:
        raise ValueError("Unknown task status.")
    with atomic(conn):
        if conn.execute(_SET_STATUS_SQL, (status, record_id, user_id)).rowcount != 1:
            raise NotFound


def summary(row: sqlite3.Row) -> str:
    text = f"{row['start_time']} {row['title']}" if row["start_time"] else str(row["title"])
    return f"{text} · {row['category']}"
