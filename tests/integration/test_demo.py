"""B-14 Demo Mode: working copies, mode exclusivity, per-mode sessions, local-only access,
fixture integrity, guarded cleanup, and the reviewer's web journey (TestClient, loopback peer)."""

from __future__ import annotations

import ast
import base64
import builtins
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import itsdangerous
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, demo
from app.config import Settings
from app.db import DEMO_APPLICATION_ID, application_id, connect, migrate, stamp_demo_application_id
from app.main import create_app
from app.users import get_or_create_user
from tests.conftest import TEST_SECRET

T = date(2026, 10, 17)  # golden anchor (ADR-005 §6.7)
PEER = ("127.0.0.1", 50000)
ROOT = Path(__file__).resolve().parents[2]
NO_KEY_DEMO = ("AI-powered analysis is not available in the current demo configuration. You can still explore the "
               "sample data, metrics, and trends. To enable the analytical agent, configure your own OpenAI API key "
               "using the setup instructions.")
BANNER = "synthetic data only. Changes stay in a local working copy and are reset on restart."


# ---------------------------------------------------------------- helpers
@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "demo"


def demo_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings.model_validate({"app_mode": "demo", "database_path": tmp_path / "personal.db", **overrides})


def demo_client(app: FastAPI, base_url: str = "http://localhost", peer: tuple[str, int] = PEER) -> TestClient:
    return TestClient(app, base_url=base_url, client=peer)


@pytest.fixture
def demo_app(tmp_path: Path, demo_dir: Path) -> FastAPI:
    return create_app(demo_settings(tmp_path), today=lambda: T, demo_dir=demo_dir)


@pytest.fixture
def client(demo_app: FastAPI) -> Iterator[TestClient]:
    with demo_client(demo_app) as c:
        yield c


def csrf_of(c: TestClient, path: str = "/") -> str:
    return c.get(path).text.split('<meta name="csrf-token" content="', 1)[1].split('"', 1)[0]


def enter(c: TestClient) -> None:
    r = c.post("/demo/login", data={"csrf_token": csrf_of(c)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def post(c: TestClient, path: str, data: dict[str, Any] | None = None) -> Any:
    payload = {k: str(v) for k, v in (data or {}).items()}
    payload["csrf_token"] = csrf_of(c, "/analyst")
    return c.post(path, data=payload, follow_redirects=False)


def runtime(app: FastAPI) -> demo.DemoRuntime:
    rt: demo.DemoRuntime = app.state.demo
    return rt


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode_session(cookie: str, secret: str, mode: str) -> dict[str, Any]:
    """Starlette's session cookie: TimestampSigner(key) over base64(JSON)."""
    signer = itsdangerous.TimestampSigner(auth.session_signing_key(secret, mode))
    data: dict[str, Any] = json.loads(base64.b64decode(signer.unsign(cookie.encode())))
    return data


def forge_session(data: dict[str, Any], secret: str, mode: str) -> str:
    signer = itsdangerous.TimestampSigner(auth.session_signing_key(secret, mode))
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


def use_cookie(c: TestClient, name: str, value: str) -> None:
    """Replace every cookie with this one (the jar may hold copies for several domains)."""
    c.cookies.clear()
    c.cookies.set(name, value)


def personal_db(path: Path, sub: str = "1001") -> int:
    conn = connect(path)
    try:
        migrate(conn)
        return get_or_create_user(conn, sub, "owner@example.com", "Owner")
    finally:
        conn.close()


def build(demo_dir: Path, generation: int = 1, anchor: date = T) -> demo.WorkingCopy:
    return demo.build_working_copy(demo_dir, generation, anchor)


# ---------------------------------------------------------------- import and working copy
def test_build_imports_every_record_with_counts_matching_the_manifest(demo_dir: Path) -> None:
    fixture = demo.load_fixture()
    copy = build(demo_dir)
    assert copy.path == demo_dir / "working-1.db"
    conn = connect(copy.path)
    try:
        assert application_id(conn) == DEMO_APPLICATION_ID
        assert [tuple(r) for r in conn.execute("SELECT google_sub, email, display_name FROM users")] == [
            ("demo:reviewer", "reviewer@demo.invalid", "Demo Reviewer")]
        for table, expected in fixture.counts.items():
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected, table  # noqa: S608
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0] == 0
        assert {r[0] for r in conn.execute("SELECT DISTINCT model FROM meal_calorie_estimates")} == {
            "synthetic-fixture"}
        assert conn.execute("SELECT COUNT(*) FROM meal_calorie_estimates WHERE calorie_basis = 'unknown'"
                            " AND estimated_calories IS NULL").fetchone()[0] >= 3
        # Dates are the anchor plus the offset: history ends today, tasks reach up to +14.
        assert conn.execute("SELECT MAX(record_date) FROM sleep_records").fetchone()[0] <= T.isoformat()
        assert conn.execute("SELECT MIN(record_date) FROM step_records").fetchone()[0] >= (
            T - timedelta(days=29)).isoformat()
        assert conn.execute("SELECT MAX(task_date) FROM tasks").fetchone()[0] <= (T + timedelta(days=14)).isoformat()
        for name in ("job_search_profile", "job_reports", "job_recommendations", "industry_trends"):
            assert conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0  # noqa: S608
    finally:
        conn.close()


def test_dates_follow_the_anchor(demo_dir: Path, tmp_path: Path) -> None:
    other = tmp_path / "b" / "data" / "demo"
    a, b = build(demo_dir, 1, T), build(other, 1, T + timedelta(days=3))
    query = "SELECT record_date FROM weight_records ORDER BY record_date"
    ca, cb = connect(a.path), connect(b.path)
    try:
        da = [date.fromisoformat(r[0]) for r in ca.execute(query)]
        db = [date.fromisoformat(r[0]) for r in cb.execute(query)]
    finally:
        ca.close()
        cb.close()
    assert [d + timedelta(days=3) for d in da] == db


def test_build_never_reuses_an_existing_file(demo_dir: Path) -> None:
    build(demo_dir, 7)
    before = sha(demo_dir / "working-7.db")
    with pytest.raises(demo.DemoError, match="already exists"):
        build(demo_dir, 7)
    assert sha(demo_dir / "working-7.db") == before


def test_build_refuses_a_directory_that_is_not_data_demo(tmp_path: Path) -> None:
    with pytest.raises(demo.DemoError, match="data/demo"):
        build(tmp_path / "elsewhere")
    assert not (tmp_path / "elsewhere").exists()


def test_build_refuses_to_become_the_personal_database(demo_dir: Path) -> None:
    with pytest.raises(demo.DemoError, match="personal database"):
        demo.build_working_copy(demo_dir, 3, T, forbidden=(demo_dir / "working-3.db",))
    assert not (demo_dir / "working-3.db").exists()


def test_runtime_reset_gives_a_fresh_copy_and_cleans_up(demo_dir: Path) -> None:
    rt = demo.DemoRuntime(demo_dir, lambda: T)
    first = rt.start()
    conn = rt.connect()
    try:
        conn.execute("UPDATE sleep_records SET hours_slept = 1.5 WHERE user_id = ?", (first.user_id,))
    finally:
        conn.close()
    second = rt.reset()
    assert second.generation > first.generation and rt.generation == second.generation
    assert not first.path.exists() and second.path.exists()
    conn = rt.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM sleep_records WHERE hours_slept = 1.5").fetchone()[0] == 0
    finally:
        conn.close()
    assert sorted(p.name for p in demo_dir.iterdir()) == [second.path.name]


def test_start_removes_stale_copies_from_an_earlier_run(demo_dir: Path) -> None:
    old = build(demo_dir, 5)
    copy = demo.DemoRuntime(demo_dir, lambda: T).start()
    assert not old.path.exists() and copy.path.exists()


# ---------------------------------------------------------------- guarded cleanup
def test_cleanup_deletes_only_marked_working_copies(demo_dir: Path, tmp_path: Path) -> None:
    stale = build(demo_dir, 1)
    Path(f"{stale.path}-wal").write_bytes(b"x")  # side files go with their db
    Path(f"{stale.path}-shm").write_bytes(b"x")
    keep = build(demo_dir, 2)
    personal = demo_dir / "working-9.db"  # right name, but a personal (unmarked) database
    personal_db(personal)
    marked_wrong_name = demo_dir / "backup.db"
    shutil.copy(keep.path, marked_wrong_name)
    nested = demo_dir / "sub" / "working-4.db"
    nested.parent.mkdir()
    shutil.copy(keep.path, nested)
    orphan_wal = demo_dir / "working-8.db-wal"
    orphan_wal.write_bytes(b"x")
    notes = demo_dir / "notes.txt"
    notes.write_text("decoy", encoding="utf-8")
    link = demo_dir / "working-6.db"
    try:
        link.symlink_to(keep.path)
    except OSError:  # Windows without the symlink privilege
        pytest.skip("creating symlinks needs extra privileges on this system")
    outside = tmp_path / "other" / "working-3.db"
    outside.parent.mkdir()
    shutil.copy(keep.path, outside)
    decoys = {p: sha(p) for p in (personal, marked_wrong_name, nested, orphan_wal, notes, outside)}

    removed = demo.remove_old_copies(demo_dir, keep=keep.path)

    assert removed == ["working-1.db"]
    assert not stale.path.exists() and not Path(f"{stale.path}-wal").exists() and not Path(f"{stale.path}-shm").exists()
    assert keep.path.exists() and link.is_symlink()
    assert all(p.exists() and sha(p) == h for p, h in decoys.items())
    assert demo.remove_copy(outside) is False and outside.exists()  # not under data/demo/


def test_is_demo_copy_reads_the_header_only(tmp_path: Path, demo_dir: Path) -> None:
    copy = build(demo_dir)
    plain = tmp_path / "plain.db"
    personal_db(plain)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 50)
    assert demo.is_demo_copy(copy.path) and not demo.is_demo_copy(plain) and not demo.is_demo_copy(junk)
    assert not demo.is_demo_copy(tmp_path / "missing.db") and not (tmp_path / "missing.db").exists()


# ---------------------------------------------------------------- mode exclusivity (both directions)
def test_demo_mode_refuses_a_personal_database(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    personal_db(path)
    conn = connect(path)
    try:
        with pytest.raises(demo.DemoError, match="not a demo working copy"):
            demo.assert_demo_database(conn)
    finally:
        conn.close()


def test_demo_mode_refuses_a_marked_copy_with_a_non_demo_user(demo_dir: Path) -> None:
    copy = build(demo_dir)
    conn = connect(copy.path)
    try:
        get_or_create_user(conn, "1001", "owner@example.com", "Owner")
        with pytest.raises(demo.DemoError, match="non-demo users"):
            demo.assert_demo_database(conn)
    finally:
        conn.close()


def test_demo_requests_refuse_a_swapped_database(demo_app: FastAPI, tmp_path: Path) -> None:
    with demo_client(demo_app) as c:
        enter(c)
        rt = runtime(demo_app)
        current = rt._current
        assert current is not None
        personal = tmp_path / "personal-swap.db"
        personal_db(personal)
        rt._current = demo.WorkingCopy(personal, current.generation, current.user_id, current.anchor)
        r = c.get("/")  # refused (B-07: a 503 page instead of a bare 500); no data from the swapped file
        assert r.status_code == 503 and "Demo data unavailable" in r.text and "Signed in as" not in r.text


def test_personal_mode_refuses_a_demo_copy_without_touching_it(tmp_path: Path, demo_dir: Path) -> None:
    copy = build(demo_dir)
    moved = tmp_path / "looks-personal.db"
    shutil.copy(copy.path, moved)
    before = sha(moved)
    app = create_app(Settings(session_secret=TEST_SECRET, database_path=moved))
    with pytest.raises(demo.DemoError, match="demo working copy"), TestClient(app):
        pass
    assert sha(moved) == before  # refused before migrate could write anything


def test_personal_mode_refuses_a_database_with_a_demo_identity(tmp_path: Path) -> None:
    path = tmp_path / "mixed.db"
    personal_db(path, sub="demo:intruder")
    app = create_app(Settings(session_secret=TEST_SECRET, database_path=path))
    with pytest.raises(demo.DemoError, match="demo identity"), TestClient(app):
        pass


@pytest.mark.parametrize("relative", ["data/demo/app.db", "DATA/Demo/x/app.db"])
def test_personal_mode_refuses_a_path_under_data_demo(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    app = create_app(Settings(session_secret=TEST_SECRET, database_path=path))
    with pytest.raises(demo.DemoError, match="data/demo"), TestClient(app):
        pass
    assert not path.exists()


def test_admit_refuses_demo_identities_even_if_allowlisted(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    personal_db(path)
    conn = connect(path)
    try:
        for sub in ("demo:reviewer", "DEMO:reviewer", " demo:x"):
            identity = auth.Identity(sub=sub, email="owner@example.com", email_verified=True, name=None)
            with pytest.raises(auth.NotAllowed):
                auth.admit(conn, identity, frozenset({"owner@example.com"}))
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        conn.close()


def test_app_connections_cannot_write_the_demo_marker(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    personal_db(path)
    conn = connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(f"PRAGMA application_id = {DEMO_APPLICATION_ID}")
        with pytest.raises(ValueError, match="already has users"):
            stamp_demo_application_id(conn)
        assert application_id(conn) == 0
    finally:
        conn.close()


def test_only_the_demo_builder_stamps_the_marker() -> None:
    users = []
    for path in [*ROOT.joinpath("app").rglob("*.py"), *ROOT.joinpath("scripts").glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {getattr(n, "id", None) or getattr(n, "attr", None) or getattr(n, "name", None)
                 for n in ast.walk(tree) if isinstance(n, ast.Name | ast.Attribute | ast.FunctionDef | ast.alias)}
        if "stamp_demo_application_id" in names:
            users.append(path.relative_to(ROOT).as_posix())
    assert sorted(users) == ["app/db.py", "app/demo.py"]


# ---------------------------------------------------------------- per-mode sessions
def test_demo_session_uses_its_own_cookie_and_claims(demo_app: FastAPI, client: TestClient) -> None:
    enter(client)
    cookie = client.cookies.get(auth.DEMO_SESSION_COOKIE)
    assert cookie and client.cookies.get(auth.SESSION_COOKIE) is None
    secret = demo_app.state.settings.session_secret
    claims = decode_session(cookie, secret, "demo")
    assert claims["mode"] == "demo" and claims["generation"] == runtime(demo_app).generation
    assert claims["uid"] == runtime(demo_app).user_id
    with pytest.raises(itsdangerous.BadSignature):
        decode_session(cookie, secret, "personal")


def test_personal_sessions_carry_the_mode_claim(tmp_path: Path) -> None:
    settings = Settings.model_validate({"session_secret": TEST_SECRET, "database_path": tmp_path / "p.db",
                                        "test_auth": True, "allowed_emails": "a@example.com"})
    with TestClient(create_app(settings), base_url="http://localhost", client=PEER) as c:
        r = c.post("/auth/test-login", data={"csrf_token": csrf_of(c), "email": "a@example.com"},
                   follow_redirects=False)
        assert r.status_code == 303
        claims = decode_session(c.cookies.get(auth.SESSION_COOKIE) or "", TEST_SECRET, "personal")
        assert claims["mode"] == "personal" and "generation" not in claims


def test_demo_cookie_is_rejected_in_personal_mode(tmp_path: Path, demo_dir: Path) -> None:
    demo_app = create_app(demo_settings(tmp_path, session_secret=TEST_SECRET), today=lambda: T, demo_dir=demo_dir)
    personal = Settings.model_validate({"session_secret": TEST_SECRET, "database_path": tmp_path / "p.db"})
    uid = personal_db(tmp_path / "p.db")
    with demo_client(demo_app) as d, TestClient(create_app(personal), base_url="http://localhost") as p:
        enter(d)
        demo_cookie = d.cookies.get(auth.DEMO_SESSION_COOKIE) or ""
        use_cookie(p, auth.SESSION_COOKIE, demo_cookie)  # other key: signature fails
        assert "Sign in with Google" in p.get("/").text or "Google sign-in" in p.get("/").text
        # Even signed with the personal key, demo claims don't authenticate in personal mode.
        use_cookie(p, auth.SESSION_COOKIE, forge_session(
            {"uid": uid, "iat": int(time.time()), "mode": "demo", "generation": 1}, TEST_SECRET,
            "personal"))
        assert "Signed in as" not in p.get("/").text
        use_cookie(p, auth.SESSION_COOKIE, forge_session(
            {"uid": uid, "iat": int(time.time()), "mode": "personal"}, TEST_SECRET, "personal"))
        assert "Signed in as" in p.get("/").text  # control: the right claims do work


def test_personal_cookie_is_rejected_in_demo_mode(tmp_path: Path, demo_dir: Path) -> None:
    demo_app = create_app(demo_settings(tmp_path, session_secret=TEST_SECRET), today=lambda: T, demo_dir=demo_dir)
    with demo_client(demo_app) as d:
        uid = runtime(demo_app).user_id
        now = int(time.time())
        use_cookie(d, auth.DEMO_SESSION_COOKIE, forge_session({"uid": uid, "iat": now, "mode": "personal"},
                                                              TEST_SECRET, "personal"))
        assert "Explore demo data" in d.get("/").text
        use_cookie(d, auth.DEMO_SESSION_COOKIE, forge_session({"uid": uid, "iat": now, "mode": "personal"},
                                                              TEST_SECRET, "demo"))
        assert "Explore demo data" in d.get("/").text
        use_cookie(d, auth.DEMO_SESSION_COOKIE, forge_session(
            {"uid": uid, "iat": now, "mode": "demo", "generation": runtime(demo_app).generation}, TEST_SECRET, "demo"))
        assert "Signed in as" in d.get("/").text  # control


def test_a_session_from_an_older_generation_is_rejected(demo_app: FastAPI, client: TestClient) -> None:
    enter(client)
    old_cookie = client.cookies.get(auth.DEMO_SESSION_COOKIE) or ""
    assert "Signed in as" in client.get("/").text
    runtime(demo_app).reset()
    assert "Explore demo data" in client.get("/").text
    use_cookie(client, auth.DEMO_SESSION_COOKIE, old_cookie)
    assert "Explore demo data" in client.get("/").text


# ---------------------------------------------------------------- local-only access
PATHS = [("GET", "/"), ("GET", "/healthz"), ("GET", "/static/app.css"), ("GET", "/login"), ("GET", "/demo/reset"),
         ("POST", "/demo/login"), ("POST", "/demo/reset"), ("GET", "/analyst"), ("GET", "/does-not-exist")]


@pytest.mark.parametrize(("method", "path"), PATHS)
@pytest.mark.parametrize("case", ["x-forwarded-for", "forwarded", "cf-connecting-ip", "via", "x-real-ip",
                                  "remote-peer", "public-host", "tunnel-host"])
def test_every_non_local_request_gets_403(demo_app: FastAPI, method: str, path: str, case: str) -> None:
    base, peer, headers = "http://localhost", PEER, {}
    if case == "remote-peer":
        peer = ("203.0.113.5", 40000)
    elif case == "public-host":
        base = "http://demo.example.com"
    elif case == "tunnel-host":
        base, headers = "https://abc-def.trycloudflare.com", {"cf-ray": "x"}
    else:
        headers = {case: "203.0.113.5"}
    with demo_client(demo_app, base, peer) as c:
        r = c.request(method, path, headers=headers, follow_redirects=False)
    assert r.status_code == 403 and "only available on this computer" in r.text


def test_local_requests_work_including_static_files(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/static/app.css").status_code == 200
    with TestClient(client.app, base_url="http://127.0.0.1:8000", client=("::1", 1)) as c6:
        assert c6.get("/healthz").status_code == 200


def test_demo_login_requires_csrf(client: TestClient) -> None:
    assert client.post("/demo/login").status_code == 403
    assert client.post("/demo/login", data={"csrf_token": "wrong"}).status_code == 403
    assert "Explore demo data" in client.get("/").text


def test_personal_mode_registers_no_demo_routes(tmp_path: Path) -> None:
    settings = Settings(session_secret=TEST_SECRET, database_path=tmp_path / "p.db")
    app = create_app(settings)
    assert app.state.demo is None
    assert not [r for r in app.routes if "/demo" in getattr(r, "path", "")]
    with TestClient(app, base_url="http://localhost", client=PEER) as c:
        token = csrf_of(c)
        assert c.get("/demo/reset").status_code == 404
        assert c.post("/demo/login", data={"csrf_token": token}).status_code == 404
        assert "Explore demo data" not in c.get("/").text and BANNER not in c.get("/").text


def test_google_routes_explain_demo_mode_and_settings_are_ignored(tmp_path: Path, demo_dir: Path,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    settings = demo_settings(tmp_path, google_client_id="id.apps.example", google_client_secret="s3cret-value",
                             allowed_emails="owner@example.com")
    with caplog.at_level(logging.WARNING):
        app = create_app(settings, today=lambda: T, demo_dir=demo_dir)
    warnings = [r.getMessage() for r in caplog.records if "ignoring" in r.getMessage()]
    assert len(warnings) == 1 and "GOOGLE_CLIENT_ID" in warnings[0] and "s3cret" not in warnings[0]
    with demo_client(app) as c:
        for path in ("/login", "/auth/callback?code=x&state=y"):
            r = c.get(path, follow_redirects=False)
            assert r.status_code == 404 and "not available in demo mode" in r.text
        assert "Sign in with Google" not in c.get("/").text


def test_demo_never_opens_database_path(tmp_path: Path, demo_dir: Path) -> None:
    personal = tmp_path / "personal.db"
    with demo_client(create_app(demo_settings(tmp_path), today=lambda: T, demo_dir=demo_dir)) as c:
        enter(c)
        c.get("/")
        post(c, "/records/weight", {"record_date": T.isoformat(), "weight_kg": "71.1"})
    assert not personal.exists()


# ---------------------------------------------------------------- fixture integrity
def test_tampered_fixture_stops_startup_with_a_clear_error(tmp_path: Path, demo_dir: Path) -> None:
    copy = tmp_path / "synthetic_v1.json"
    raw = bytearray(demo.FIXTURE_PATH.read_bytes())
    raw[raw.index(b'"weight_kg": ') + 13] ^= 1  # one digit changed
    copy.write_bytes(bytes(raw))
    rt = demo.DemoRuntime(demo_dir, lambda: T, fixture_path=copy)
    with pytest.raises(demo.DemoError, match="checksum mismatch"):
        rt.start()
    assert not demo_dir.exists() or not any(demo_dir.iterdir())


def test_fixture_with_matching_checksum_but_bad_content_is_refused(tmp_path: Path, demo_dir: Path) -> None:
    data = json.loads(demo.FIXTURE_PATH.read_bytes())
    data["records"]["moods"] = []  # an untracked domain: extra="forbid"
    raw = json.dumps(data).encode()
    fixture, checksum = tmp_path / "f.json", tmp_path / "f.sha256"
    fixture.write_bytes(raw)
    checksum.write_text(hashlib.sha256(raw).hexdigest() + "  f.json\n", encoding="ascii")
    with pytest.raises(demo.DemoError, match="invalid"):
        demo.build_working_copy(demo_dir, 1, T, fixture_path=fixture, checksum_path=checksum)


def test_importer_never_opens_demo_files_for_writing(demo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_dir = (ROOT / "demo").resolve()
    opened: list[tuple[str, str]] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | Path) and Path(file).resolve().is_relative_to(fixture_dir):
            opened.append((str(file), mode))
        return real_open(file, mode, *args, **kwargs)

    def spy_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path).resolve().is_relative_to(fixture_dir):
            opened.append((str(path), f"os-flags-{flags}"))
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    build(demo_dir)
    monkeypatch.setattr(builtins, "open", real_open)
    monkeypatch.setattr(io, "open", real_io_open)
    assert {Path(p).name for p, _ in opened} == {"synthetic_v1.json", "synthetic_v1.sha256"}
    assert {m for _, m in opened} == {"rb"}


# ---------------------------------------------------------------- the reviewer's journey
def record_id(app: FastAPI, sql: str, *params: Any) -> int:
    conn = runtime(app).connect()
    try:
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def test_reviewer_journey(demo_app: FastAPI, demo_dir: Path) -> None:
    fixture_before = (sha(demo.FIXTURE_PATH), demo.FIXTURE_PATH.stat().st_mtime_ns,
                      sha(demo.CHECKSUM_PATH), demo.CHECKSUM_PATH.stat().st_mtime_ns)
    with demo_client(demo_app) as c:
        start = c.get("/")
        assert "Explore demo data" in start.text and BANNER in start.text and "Sign in with Google" not in start.text
        enter(c)

        health = c.get("/")
        assert "Signed in as <strong>Demo Reviewer</strong>" in health.text and BANNER in health.text
        assert 'href="/demo/reset"' in health.text
        assert "h sleep" in health.text and "steps" in health.text
        assert "Interview — " in c.get("/tasks").text
        assert "Wrenfield Finance" in c.get("/jobs").text  # an open application (interviewing)
        assert "Harbor of Glass" in c.get("/content").text or "Starfall Station" in c.get("/content").text
        analyst = c.get("/analyst")
        assert NO_KEY_DEMO in analyst.text and "not configured. Add OPENAI_API_KEY" not in analyst.text

        # add
        r = post(c, "/records/articles", {"record_date": T.isoformat(), "title": "A reviewer's test note",
                                          "source": "Demo", "url": "https://example.com/note"})
        assert r.status_code == 303
        aid = record_id(demo_app, "SELECT id FROM articles WHERE title = ?", "A reviewer's test note")
        # duplicate is refused, nothing overwritten
        assert post(c, "/records/articles", {"record_date": T.isoformat(), "title": "A reviewer's test note",
                                             "source": "Demo", "url": "https://example.com/note"}).status_code == 409
        # edit
        assert post(c, f"/records/articles/{aid}/edit", {"record_date": T.isoformat(), "title": "Edited note",
                                                          "source": "Demo", "url": ""}).status_code == 303
        assert "Edited note" in c.get("/content").text
        # delete (needs confirmation) → Recently deleted → restore
        assert "Delete record" in c.get(f"/records/articles/{aid}/delete").text
        assert post(c, f"/records/articles/{aid}/delete", {"confirm": "delete"}).status_code == 303
        assert "Edited note" in c.get("/deleted").text and "Edited note" not in c.get("/content").text
        assert post(c, f"/records/articles/{aid}/restore").status_code == 303
        assert "Edited note" in c.get("/content").text
        # delete again, then permanently
        assert post(c, f"/records/articles/{aid}/delete", {"confirm": "delete"}).status_code == 303
        assert post(c, f"/records/articles/{aid}/purge", {"confirm": "permanent", "acknowledge": "yes"}
                    ).status_code == 303
        assert "Edited note" not in c.get("/deleted").text
        # an edit to fixture data, to prove the reset discards it
        sid = record_id(demo_app, "SELECT id FROM sleep_records ORDER BY record_date DESC LIMIT 1")
        conn = runtime(demo_app).connect()
        try:
            day = conn.execute("SELECT record_date FROM sleep_records WHERE id = ?", (sid,)).fetchone()[0]
        finally:
            conn.close()
        assert post(c, f"/records/sleep/{sid}/edit", {"record_date": day, "hours_slept": "3.25"}).status_code == 303
        old_path, old_generation = runtime(demo_app)._current.path, runtime(demo_app).generation  # type: ignore[union-attr]

        # reset: confirmation page, refused without confirmation, then done
        page = c.get("/demo/reset")
        assert page.status_code == 200 and "Every change you made in this demo is discarded" in page.text
        assert post(c, "/demo/reset").status_code == 400
        assert old_path.exists()
        r = post(c, "/demo/reset", {"confirm": "reset"})
        assert r.status_code == 303
        after = c.get("/")
        assert "Explore demo data" in after.text and "Demo data was reset" in after.text
        assert runtime(demo_app).generation != old_generation and not old_path.exists()
        enter(c)
        assert "Signed in as" in c.get("/").text
        conn = runtime(demo_app).connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM sleep_records WHERE hours_slept = 3.25").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM articles WHERE title IN ('Edited note',"
                                " 'A reviewer''s test note')").fetchone()[0] == 0
        finally:
            conn.close()
        assert [p.name for p in demo_dir.iterdir() if re.match(r"working-\d+\.db$", p.name)] == [
            runtime(demo_app)._current.path.name]  # type: ignore[union-attr]
    assert fixture_before == (sha(demo.FIXTURE_PATH), demo.FIXTURE_PATH.stat().st_mtime_ns,
                              sha(demo.CHECKSUM_PATH), demo.CHECKSUM_PATH.stat().st_mtime_ns)


def test_banner_mentions_charges_only_when_ai_is_enabled(tmp_path: Path, demo_dir: Path) -> None:
    settings = demo_settings(tmp_path, openai_api_key="sk-test-placeholder", openai_model="test-model")
    with demo_client(create_app(settings, today=lambda: T, demo_dir=demo_dir)) as c:
        assert "charges apply to your account" in c.get("/").text
        enter(c)
        assert NO_KEY_DEMO not in c.get("/analyst").text
    with demo_client(create_app(demo_settings(tmp_path), today=lambda: T, demo_dir=tmp_path / "x" / "data" / "demo")
                     ) as c2:
        assert "charges apply" not in c2.get("/").text
