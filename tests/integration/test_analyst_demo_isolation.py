"""Integration: the Analyst in demo mode reads only the active demo working copy (B-14 x B-15).

Real `create_app` in demo mode, real `DemoRuntime`, real B-05 metrics and B-13 tools; only the model is
scripted. A personal database sits at DATABASE_PATH; it must never be opened or changed, also after a reset.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.analytics import readonly
from app.db import connect, migrate
from app.main import create_app
from app.users import get_or_create_user
from app.web import analyst
from tests.integration.test_analytics_agent import ScriptedClient, answer, fc, finding, msg, turn
from tests.integration.test_demo import PEER, T, csrf_of, demo_settings, enter

COVERAGE_ARGS = {"domains": ["sleep"], "start": "2026-09-18", "end": "2026-10-17"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every database path any app connection opens."""
    paths: list[Path] = []
    real = sqlite3.connect

    def spy(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        paths.append(Path(str(database).removeprefix("file:").split("?", 1)[0]))
        conn: sqlite3.Connection = real(database, *args, **kwargs)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    return paths


@pytest.fixture
def personal_db(tmp_path: Path) -> Path:
    path = tmp_path / "personal.db"
    conn = connect(path)
    try:
        migrate(conn)
        get_or_create_user(conn, "1234567890", "owner@example.com", "Owner")
    finally:
        conn.close()
    return path


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> list[ScriptedClient]:
    clients: list[ScriptedClient] = []

    def factory(_settings: Any) -> ScriptedClient:
        body = answer(finding(kind="value", metric=None, text="Sleep coverage is {{E1.sleep.ratio}}."),
                      summary="", follow_up_questions=[])
        client = ScriptedClient([turn(fc("get_data_coverage", COVERAGE_ARGS)), turn(msg(body)), turn(msg(body))])
        clients.append(client)
        return client

    monkeypatch.setattr(analyst, "_default_client", factory)  # read when create_app builds the router
    return clients


@pytest.fixture
def client(tmp_path: Path, personal_db: Path, scripted: list[ScriptedClient]) -> Iterator[TestClient]:
    settings = demo_settings(tmp_path, openai_api_key="sk-test-placeholder", openai_model="test-model")
    assert settings.database_path == personal_db
    app = create_app(settings, today=lambda: T, demo_dir=tmp_path / "data" / "demo")
    with TestClient(app, base_url="http://localhost", client=PEER) as c:
        yield c


def ask(c: TestClient, question: str) -> Any:
    return c.post("/analyst/ask", data={"csrf_token": csrf_of(c, "/analyst"), "question": question},
                  follow_redirects=False)


def test_demo_analyst_never_opens_the_personal_db_also_after_reset(client: TestClient, personal_db: Path,
                                                                   opened: list[Path],
                                                                   scripted: list[ScriptedClient]) -> None:
    before = (sha(personal_db), personal_db.stat().st_mtime_ns)
    enter(client)
    first_copy = client.app.state.demo._current.path  # type: ignore[attr-defined]
    opened.clear()
    r = ask(client, "How well did I log my sleep?")
    assert r.status_code == 200 and len(scripted) == 1
    assert scripted[0].calls, "the scripted model was used"
    assert all(p.resolve() != personal_db.resolve() for p in opened)
    assert any(p.resolve() == first_copy.resolve() for p in opened)

    reset = client.post("/demo/reset", data={"csrf_token": csrf_of(client, "/demo/reset"), "confirm": "reset"},
                        follow_redirects=False)
    assert reset.status_code == 303
    enter(client)
    second_copy = client.app.state.demo._current.path  # type: ignore[attr-defined]
    assert second_copy != first_copy
    opened.clear()
    r = ask(client, "How well did I log my sleep?")
    assert r.status_code == 200 and len(scripted) == 2
    assert all(p.resolve() != personal_db.resolve() for p in opened)
    assert any(p.resolve() == second_copy.resolve() for p in opened)
    assert (sha(personal_db), personal_db.stat().st_mtime_ns) == before
    assert not Path(str(personal_db) + "-wal").exists()


def test_demo_analyst_connection_is_read_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[sqlite3.Connection] = []
    real = readonly.harden_read_only

    def spy(conn: sqlite3.Connection) -> sqlite3.Connection:
        hardened: sqlite3.Connection = real(conn)
        seen.append(hardened)
        with pytest.raises(sqlite3.DatabaseError):
            hardened.execute("DELETE FROM users")
        return hardened

    monkeypatch.setattr(readonly, "harden_read_only", spy)
    enter(client)
    assert ask(client, "How well did I log my sleep?").status_code == 200
    assert seen


def test_demo_mode_without_runtime_fails_closed(tmp_path: Path) -> None:
    class Req:
        class app:  # noqa: N801 - minimal stand-in for request.app
            class state:  # noqa: N801
                demo = None

    settings = demo_settings(tmp_path)
    with pytest.raises(RuntimeError):
        analyst._open_read_only(Req(), settings, None)  # type: ignore[arg-type]
