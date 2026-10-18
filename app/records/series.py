"""Series episodes: identity = series + season + episode, per date (one row per episode)."""

from __future__ import annotations

import sqlite3

from pydantic import Field

from app import canonical
from app.records._fields import Name, Opt, RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate


class SeriesIn(RecordIn):
    record_date: RecordDate
    series_name: Name
    season_number: Opt[int] = Field(default=None, ge=1, le=10_000)
    episode_number: Opt[int] = Field(default=None, ge=1, le=10_000)


SPEC = DomainSpec(name="series", table="series", model=SeriesIn, date_column="record_date",
                  columns=("record_date", "series_name", "season_number", "episode_number"),
                  unique=("record_date", "canonical_key"),
                  key=lambda s: canonical.series_key(s.series_name, s.season_number, s.episode_number))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    text = str(row["series_name"])
    if row["season_number"]:
        text += f" S{row['season_number']}"
    if row["episode_number"]:
        text += f" E{row['episode_number']}"
    return text
