"""Podcast episodes: identity = podcast + episode, per date."""

from __future__ import annotations

import sqlite3

from pydantic import Field

from app import canonical
from app.records._fields import Name, Opt, OptName, RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate


class PodcastIn(RecordIn):
    record_date: RecordDate
    podcast_name: Name
    episode_title: OptName = None
    duration_minutes: Opt[int] = Field(default=None, ge=1, le=1440)


SPEC = DomainSpec(name="podcasts", table="podcasts", model=PodcastIn, date_column="record_date",
                  columns=("record_date", "podcast_name", "episode_title", "duration_minutes"),
                  unique=("record_date", "canonical_key"),
                  key=lambda p: canonical.podcast_key(p.podcast_name, p.episode_title))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    text = str(row["podcast_name"])
    if row["episode_title"]:
        text += f" — {row['episode_title']}"
    if row["duration_minutes"]:
        text += f" · {row['duration_minutes']} min"
    return text
