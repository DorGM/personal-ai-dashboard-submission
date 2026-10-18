"""Articles: identity = canonical URL, else normalized title + source, per date (A-03 §3).

URLs are validated by app.urls (syntax only); B-04 never fetches them.
"""

from __future__ import annotations

import sqlite3

from app import canonical
from app.records._fields import Name, Opt, OptName, RecordIn
from app.records.core import DomainSpec, Repository
from app.records.dates import RecordDate
from app.urls import HttpUrl


class ArticleIn(RecordIn):
    record_date: RecordDate
    title: Name
    source: OptName = None
    url: Opt[HttpUrl] = None


SPEC = DomainSpec(name="articles", table="articles", model=ArticleIn, date_column="record_date",
                  columns=("record_date", "title", "source", "url"), unique=("record_date", "canonical_key"),
                  key=lambda a: canonical.article_key(a.title, a.source, a.url))
repo = Repository(SPEC)
create, get, get_deleted, history, update = repo.create, repo.get, repo.get_deleted, repo.history, repo.update
soft_delete, restore, permanent_delete, list_deleted = repo.soft_delete, repo.restore, repo.permanent_delete, \
    repo.list_deleted
earliest = repo.earliest


def summary(row: sqlite3.Row) -> str:
    return f"{row['title']} ({row['source']})" if row["source"] else str(row["title"])
