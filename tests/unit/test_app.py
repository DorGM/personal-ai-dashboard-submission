from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_returns_ok_without_data(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_renders_with_stylesheet(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert '<meta name="viewport"' in r.text
    assert client.get("/static/app.css").status_code == 200


def test_api_docs_disabled(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_startup_creates_schema(client: TestClient, settings) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    conn = sqlite3.connect(settings.database_path)
    try:
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [
            ("001_init",), ("002_soft_delete",)]
    finally:
        conn.close()
