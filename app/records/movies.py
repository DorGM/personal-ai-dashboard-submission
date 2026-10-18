"""Movies: identity = title + release year, per date."""

from __future__ import annotations

import sqlite3

from pydantic import Field

from app import canonical
from app.records._fields import Name, Opt, RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate


class MovieIn(RecordIn):
    record_date: RecordDate
    title: Name
    release_year: Opt[int] = Field(default=None, ge=1880, le=2100)


SPEC = DomainSpec(name="movies", table="movies", model=MovieIn, date_column="record_date",
                  columns=("record_date", "title", "release_year"), unique=("record_date", "canonical_key"),
                  key=lambda m: canonical.movie_key(m.title, m.release_year))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    return f"{row['title']} ({row['release_year']})" if row["release_year"] else str(row["title"])
