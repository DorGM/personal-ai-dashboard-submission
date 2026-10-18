"""Sleep records: hours per night, dated by the wake-up date (A-03 E5); one per date."""

from __future__ import annotations

import sqlite3

from pydantic import Field

from app.records._fields import RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate


class SleepIn(RecordIn):
    record_date: RecordDate
    hours_slept: float = Field(gt=0, le=24)


SPEC = DomainSpec(name="sleep", table="sleep_records", model=SleepIn, date_column="record_date",
                  columns=("record_date", "hours_slept"), unique=("record_date",))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    return f"{row['hours_slept']:g} h sleep"
