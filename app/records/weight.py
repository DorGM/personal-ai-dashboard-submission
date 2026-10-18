"""Weight records: one per user and date (PRODUCT_SPEC §2.1)."""

from __future__ import annotations

import sqlite3

from pydantic import Field

from app.records._fields import RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate


class WeightIn(RecordIn):
    record_date: RecordDate
    weight_kg: float = Field(gt=0, lt=500)


SPEC = DomainSpec(name="weight", table="weight_records", model=WeightIn, date_column="record_date",
                  columns=("record_date", "weight_kg"), unique=("record_date",))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    return f"{row['weight_kg']:g} kg"
