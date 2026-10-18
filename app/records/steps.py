"""Step counts: one per date; an explicit 0 is a real value, unlike a missing day."""

from __future__ import annotations

import sqlite3

from pydantic import Field

from app.records._fields import RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate


class StepsIn(RecordIn):
    record_date: RecordDate
    steps_count: int = Field(ge=0, le=1_000_000)


SPEC = DomainSpec(name="steps", table="step_records", model=StepsIn, date_column="record_date",
                  columns=("record_date", "steps_count"), unique=("record_date",))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    return f"{row['steps_count']:,} steps"
