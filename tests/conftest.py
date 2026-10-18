from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TEST_SECRET = "0123456789abcdef" * 2


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(session_secret=TEST_SECRET, database_path=tmp_path / "test.db")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c
