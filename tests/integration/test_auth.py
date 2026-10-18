"""B-02 sign-in, allowlist, session and CSRF (A-04 S7–S9; ADR-004 D5).

The real Authlib client runs against an in-process fake Google (httpx MockTransport): it signs
ID tokens with a test RSA key, serves the JWKS, and checks PKCE. No network calls are made.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from app import auth
from app.config import Settings
from app.db import connect
from app.demo_seed import seed
from app.main import create_app
from tests.conftest import TEST_SECRET

CLIENT_ID = "test-client.apps.example"
ALLOWED = "owner@example.com"
ISSUER = "https://accounts.google.com"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Authlib's HTTP client library (httpx2 when installed, else httpx); the mock must come from it.
oauth_http: Any = importlib.import_module("authlib.integrations.httpx_client._compat").httpx2


class FakeGoogle:
    """Just enough of Google's OIDC endpoints for the authorization-code + PKCE flow."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "test-key"}, private=True)
        self.codes: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.token_requests: list[dict[str, list[str]]] = []

    metadata = {
        "issuer": ISSUER, "authorization_endpoint": AUTH_URL, "token_endpoint": TOKEN_URL, "jwks_uri": JWKS_URL,
        "id_token_signing_alg_values_supported": ["RS256"], "_loaded_at": 1.0,
    }

    def approve(self, authorize_url: str, **claims: Any) -> dict[str, str]:
        """Simulate the user approving: returns the callback query (code + state)."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = secrets.token_urlsafe(8)
        with self.lock:
            self.codes[code] = {"query": q, "claims": claims}
        return {"code": code, "state": q["state"]}

    def handler(self, request: Any) -> Any:
        url = str(request.url)
        if url == JWKS_URL:
            return oauth_http.Response(200, json=KeySet([self.key]).as_dict(private=False))
        if url == TOKEN_URL:
            form = parse_qs(request.content.decode())
            with self.lock:
                self.token_requests.append(form)
                entry = self.codes.pop(form["code"][0], None)
            if entry is None:
                return oauth_http.Response(400, json={"error": "invalid_grant"})
            q = entry["query"]
            verifier = form.get("code_verifier", [""])[0]
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if q.get("code_challenge_method") != "S256" or challenge != q.get("code_challenge"):
                return oauth_http.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 600, "nonce": q.get("nonce"),
                      "sub": "1001", "email": ALLOWED, "email_verified": True, "name": "Owner"}
            claims.update(entry["claims"])
            claims = {k: v for k, v in claims.items() if v is not None}
            key = claims.pop("_sign_with", self.key)
            id_token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key)
            return oauth_http.Response(200, json={"access_token": "at", "token_type": "Bearer", "expires_in": 3600,
                                             "id_token": id_token, "scope": "openid email profile"})
        return oauth_http.Response(404)


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "session_secret": TEST_SECRET, "database_path": tmp_path / "auth.db", "google_client_id": CLIENT_ID,
        "google_client_secret": "test-client-secret", "allowed_emails": ALLOWED,
    }
    values.update(overrides)
    return Settings(**values)


LOOPBACK_PEER = ("127.0.0.1", 50000)


def make_client(settings: Settings, google: FakeGoogle, base_url: str = "http://localhost") -> TestClient:
    provider = auth.GoogleProvider(settings.google_client_id, settings.google_client_secret,
                                   metadata=dict(FakeGoogle.metadata),
                                   transport=oauth_http.MockTransport(google.handler))
    return TestClient(create_app(settings, identity_provider=provider), base_url=base_url)


@pytest.fixture
def client(tmp_path: Path, google: FakeGoogle) -> Iterator[TestClient]:
    with make_client(make_settings(tmp_path), google) as c:
        yield c


def sign_in(client: TestClient, google: FakeGoogle, **claims: Any) -> Any:
    start = client.get("/login", follow_redirects=False)
    assert start.status_code == 302
    return client.get("/auth/callback", params=google.approve(start.headers["location"], **claims),
                      follow_redirects=False)


def users(path: Path) -> list[tuple[Any, ...]]:
    conn = connect(path)
    try:
        return [tuple(r) for r in conn.execute("SELECT id, google_sub, email, display_name FROM users ORDER BY id")]
    finally:
        conn.close()


def csrf_of(client: TestClient) -> str:
    r = client.get("/")
    marker = '<meta name="csrf-token" content="'
    return r.text.split(marker, 1)[1].split('"', 1)[0]


# ---------------------------------------------------------------- protection and redirect
def test_signed_out_visitor_sees_sign_in_page_only(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "Sign in with Google" in r.text and "Signed in as" not in r.text


def test_login_redirects_to_google_with_state_nonce_and_pkce(client: TestClient) -> None:
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    url = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == AUTH_URL
    assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
    assert q["scope"] == "openid email profile"
    assert q["redirect_uri"] == "http://localhost:8000/auth/callback"  # from PUBLIC_BASE_URL, not the Host header
    assert len(q["state"]) >= 20 and len(q["nonce"]) >= 20
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) >= 43


def test_login_without_google_configuration_is_explained(tmp_path: Path) -> None:
    settings = Settings(session_secret=TEST_SECRET, database_path=tmp_path / "x.db")
    with TestClient(create_app(settings)) as c:
        r = c.get("/login")
        assert r.status_code == 503 and "set up" in r.text
        assert "Sign in with Google" not in c.get("/").text


def test_login_when_google_is_unreachable_shows_a_page(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def unreachable(request: Any) -> Any:
        raise oauth_http.ConnectError("no route to host", request=request)

    settings = make_settings(tmp_path)
    provider = auth.GoogleProvider(settings.google_client_id, settings.google_client_secret,
                                   transport=oauth_http.MockTransport(unreachable))  # metadata must be fetched
    with caplog.at_level(logging.DEBUG), TestClient(create_app(settings, identity_provider=provider)) as c:
        r = c.get("/login", follow_redirects=False)
    assert r.status_code == 503 and "can" in r.text and "be reached" in r.text
    assert settings.google_client_secret not in caplog.text


# ---------------------------------------------------------------- sign-in outcomes
def test_allowlisted_first_sign_in_creates_user_and_session(client: TestClient, google: FakeGoogle,
                                                            tmp_path: Path) -> None:
    r = sign_in(client, google)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert users(tmp_path / "auth.db") == [(1, "1001", ALLOWED, "Owner")]
    assert "Signed in as <strong>Owner</strong>" in client.get("/").text
    assert google.token_requests[0]["code_verifier"][0]  # PKCE verifier was sent


def test_allowlist_match_ignores_case_and_spaces(client: TestClient, google: FakeGoogle, tmp_path: Path) -> None:
    assert sign_in(client, google, email="  Owner@Example.COM ").status_code == 303
    assert users(tmp_path / "auth.db")[0][2] == "Owner@Example.COM"


@pytest.mark.parametrize("claims", [{"email": "stranger@example.com"}, {"email_verified": False},
                                    {"email_verified": None}, {"email_verified": "true"}])
def test_not_allowlisted_or_unverified_gets_403_and_no_user(client: TestClient, google: FakeGoogle, tmp_path: Path,
                                                             claims: dict[str, Any]) -> None:
    r = sign_in(client, google, **claims)
    assert r.status_code == 403 and "allowed to use this dashboard" in r.text
    assert users(tmp_path / "auth.db") == []
    assert "Sign in with Google" in client.get("/").text


def test_known_account_keeps_access_by_google_sub(tmp_path: Path, google: FakeGoogle) -> None:
    """S8: after the first sign-in the stable google_sub is the identity, even if the email changes."""
    settings = make_settings(tmp_path)
    with make_client(settings, google) as c:
        sign_in(c, google)
    with make_client(make_settings(tmp_path, allowed_emails=""), google) as c:
        assert sign_in(c, google, email="renamed@example.com").status_code == 303
    assert users(tmp_path / "auth.db") == [(1, "1001", "renamed@example.com", "Owner")]


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "not-the-nonce"},
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 3600},
        {"sub": None},
        {"email": None},
        {"_sign_with": RSAKey.generate_key(2048, parameters={"kid": "test-key"}, private=True)},
    ],
)
def test_invalid_id_token_is_rejected(client: TestClient, google: FakeGoogle, tmp_path: Path,
                                      claims: dict[str, Any]) -> None:
    r = sign_in(client, google, **claims)
    assert r.status_code == 400 and "could not be verified" in r.text
    assert users(tmp_path / "auth.db") == []


def test_wrong_or_missing_state_is_rejected(client: TestClient, google: FakeGoogle, tmp_path: Path) -> None:
    start = client.get("/login", follow_redirects=False)
    params = google.approve(start.headers["location"])
    assert client.get("/auth/callback", params={**params, "state": "forged"}).status_code == 400
    assert client.get("/auth/callback", params={"code": params["code"]}).status_code == 400
    assert users(tmp_path / "auth.db") == []


def test_callback_from_another_browser_is_rejected(tmp_path: Path, google: FakeGoogle) -> None:
    settings = make_settings(tmp_path)
    with make_client(settings, google) as victim, make_client(settings, google) as attacker:
        start = attacker.get("/login", follow_redirects=False)
        params = google.approve(start.headers["location"])
        assert victim.get("/auth/callback", params=params).status_code == 400  # state lives in the starter's session
    assert users(tmp_path / "auth.db") == []


def test_authorization_code_cannot_be_replayed(client: TestClient, google: FakeGoogle) -> None:
    start = client.get("/login", follow_redirects=False)
    params = google.approve(start.headers["location"])
    assert client.get("/auth/callback", params=params, follow_redirects=False).status_code == 303
    assert client.get("/auth/callback", params=params).status_code == 400


def test_cancelled_sign_in_is_explained(client: TestClient) -> None:
    r = client.get("/auth/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400 and "cancelled" in r.text


def test_failed_sign_in_logs_no_email(client: TestClient, google: FakeGoogle, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    sign_in(client, google, email="stranger@example.com")
    sign_in(client, google)
    assert caplog.records and not any("example.com" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- data preservation through the real flow (L2)
def test_repeated_sign_in_keeps_user_and_all_records(tmp_path: Path, google: FakeGoogle) -> None:
    settings = make_settings(tmp_path)
    with make_client(settings, google) as c:
        sign_in(c, google)
    conn = connect(settings.database_path)
    seed_conn_user = conn.execute("SELECT id FROM users").fetchone()[0]
    _add_records(conn, seed_conn_user)
    before = _snapshot(conn)
    conn.close()
    for name in ("Owner", "Owner Renamed", "Owner Renamed"):
        with make_client(settings, google) as c:
            assert sign_in(c, google, name=name).status_code == 303
    conn = connect(settings.database_path)
    after = _snapshot(conn)
    assert {t: rows for t, rows in after.items() if t != "users"} == {t: r for t, r in before.items() if t != "users"}
    # same users (ids, subs, emails); only the signed-in user's display name changed
    assert [r[:3] for r in after["users"]] == [r[:3] for r in before["users"]]
    uid = seed_conn_user
    assert [r for r in after["users"] if r[0] != uid] == [r for r in before["users"] if r[0] != uid]
    assert conn.execute("SELECT display_name FROM users WHERE id = ?", (seed_conn_user,)).fetchone()[0] == \
        "Owner Renamed"
    conn.close()


def test_concurrent_first_sign_ins_create_one_user(tmp_path: Path, google: FakeGoogle) -> None:
    settings = make_settings(tmp_path)
    with make_client(settings, google):
        pass  # run migrations once
    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def run() -> None:
        try:
            c = make_client(settings, google)
            start = c.get("/login", follow_redirects=False)
            params = google.approve(start.headers["location"])
            barrier.wait(timeout=30)
            results.append(c.get("/auth/callback", params=params, follow_redirects=False).status_code)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == [] and results == [303] * 8
    assert users(settings.database_path) == [(1, "1001", ALLOWED, "Owner")]


def _add_records(conn: sqlite3.Connection, uid: int) -> None:
    conn.execute("INSERT INTO step_records (user_id, record_date, steps_count) VALUES (?, '2026-10-10', 9000)",
                 (uid,))
    conn.execute("INSERT INTO weight_records (user_id, record_date, weight_kg) VALUES (?, '2026-10-10', 70.5)",
                 (uid,))
    seed(conn, date(2026, 10, 16))  # a second (demo) user with a full data set


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}  # noqa: S608


# ---------------------------------------------------------------- session (S9)
def test_session_cookie_flags(client: TestClient, google: FakeGoogle) -> None:
    cookie = sign_in(client, google).headers["set-cookie"].lower()
    assert cookie.startswith(auth.SESSION_COOKIE) and "httponly" in cookie and "samesite=lax" in cookie
    assert f"max-age={auth.SESSION_SECONDS}" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain http://localhost; Secure is set when PUBLIC_BASE_URL is https


def test_session_cookie_is_secure_behind_https(tmp_path: Path, google: FakeGoogle) -> None:
    settings = make_settings(tmp_path, public_base_url="https://demo.example.com")
    with make_client(settings, google, base_url="https://demo.example.com") as c:
        start = c.get("/login", follow_redirects=False)
        assert "redirect_uri=https%3A%2F%2Fdemo.example.com%2Fauth%2Fcallback" in start.headers["location"]
        r = c.get("/auth/callback", params=google.approve(start.headers["location"]), follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower()


def test_session_is_replaced_at_sign_in(client: TestClient, google: FakeGoogle) -> None:
    before_cookie = client.cookies.get(auth.SESSION_COOKIE)
    before_csrf = csrf_of(client)
    sign_in(client, google)
    assert client.cookies.get(auth.SESSION_COOKIE) != before_cookie
    assert csrf_of(client) != before_csrf


def test_session_expires_eight_hours_after_sign_in_even_when_active(client: TestClient, google: FakeGoogle,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """The cookie is re-signed on every response (rolling), but the session itself is absolute."""
    sign_in(client, google)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 5 * 3600)
    assert "Signed in as" in client.get("/").text  # still valid after 5 h; cookie refreshed here
    monkeypatch.setattr(time, "time", lambda: real() + auth.SESSION_SECONDS + 60)
    assert "Sign in with Google" in client.get("/").text


def test_tampered_session_cookie_is_ignored(client: TestClient, google: FakeGoogle) -> None:
    sign_in(client, google)
    value = client.cookies.get(auth.SESSION_COOKIE) or ""
    client.cookies.set(auth.SESSION_COOKIE, value[:-4] + ("AAAA" if not value.endswith("AAAA") else "BBBB"))
    assert "Sign in with Google" in client.get("/").text


def test_session_for_missing_user_is_signed_out(client: TestClient, google: FakeGoogle, tmp_path: Path) -> None:
    sign_in(client, google)
    other = make_settings(tmp_path, database_path=tmp_path / "other.db")
    with TestClient(create_app(other), base_url="http://localhost") as c2:
        c2.cookies.set(auth.SESSION_COOKIE, client.cookies.get(auth.SESSION_COOKIE) or "")
        assert "Sign in with Google" in c2.get("/").text


# ---------------------------------------------------------------- CSRF and sign-out
def test_logout_requires_csrf_token(client: TestClient, google: FakeGoogle) -> None:
    sign_in(client, google)
    assert client.post("/logout").status_code == 403
    assert client.post("/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert "Signed in as" in client.get("/").text


@pytest.mark.parametrize("how", ["header", "form"])
def test_logout_with_token_clears_session(client: TestClient, google: FakeGoogle, how: str) -> None:
    sign_in(client, google)
    token = csrf_of(client)
    if how == "header":
        r = client.post("/logout", headers={"X-CSRF-Token": token}, follow_redirects=False)
    else:
        r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "Sign in with Google" in client.get("/").text
    assert client.post("/logout", headers={"X-CSRF-Token": token}).status_code == 403  # old token is gone


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_state_changing_methods_need_csrf_even_on_unknown_routes(client: TestClient, method: str) -> None:
    assert client.request(method, "/api/anything").status_code == 403


def test_oversized_form_is_refused(client: TestClient) -> None:
    r = client.post("/logout", content=b"csrf_token=" + b"a" * (70 * 1024),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_csrf_form_body_is_passed_through_unchanged(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, test_auth=True)
    with TestClient(create_app(settings), base_url="http://localhost", client=LOOPBACK_PEER) as c:
        r = c.post("/auth/test-login", data={"csrf_token": csrf_of(c), "email": ALLOWED}, follow_redirects=False)
        assert r.status_code == 303  # the route still read the email after the middleware read the token


def test_csrf_form_check_keeps_streamed_responses_intact(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))

    @app.post("/stream-probe")
    def stream_probe() -> StreamingResponse:
        return StreamingResponse(iter([f"chunk{i};" for i in range(5)]), media_type="text/plain")

    with TestClient(app, base_url="http://localhost") as c:
        token = csrf_of(c)
        via_form = c.post("/stream-probe", data={"csrf_token": token})
        via_header = c.post("/stream-probe", headers={"X-CSRF-Token": token})
    assert via_form.status_code == via_header.status_code == 200
    assert via_form.text == via_header.text == "chunk0;chunk1;chunk2;chunk3;chunk4;"


def test_form_cut_short_by_a_disconnect_never_reaches_the_route(tmp_path: Path) -> None:
    """A client that drops mid-form must not have the partial form treated as complete."""
    import asyncio

    reached: list[bytes] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(b"called")

    token = "t" * 43
    chunks = [{"type": "http.request", "body": f"csrf_token={token}&email=a".encode(), "more_body": True},
              {"type": "http.disconnect"}]
    sent: list[Any] = []

    async def receive() -> Any:
        return chunks.pop(0)

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/x", "session": {"csrf": token},
             "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}
    asyncio.run(auth.CSRFMiddleware(app)(scope, receive, send))
    assert reached == [] and sent == []


# ---------------------------------------------------------------- TEST_AUTH (A-05)
def test_test_login_route_absent_by_default(client: TestClient) -> None:
    assert client.post("/auth/test-login", headers={"X-CSRF-Token": csrf_of(client)},
                       data={"email": ALLOWED}).status_code in {404, 405}
    assert "Test sign-in" not in client.get("/").text


def test_test_login_signs_in_allowlisted_email_on_localhost(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, test_auth=True, google_client_id="", google_client_secret="")
    with TestClient(create_app(settings), base_url="http://localhost", client=LOOPBACK_PEER) as c:
        assert "Test sign-in" in c.get("/").text
        r = c.post("/auth/test-login", data={"csrf_token": csrf_of(c), "email": ALLOWED}, follow_redirects=False)
        assert r.status_code == 303 and "Signed in as" in c.get("/").text
    assert users(settings.database_path)[0][1] == auth.TEST_AUTH_SUB_PREFIX + ALLOWED


@pytest.mark.parametrize(
    ("base_url", "peer", "headers", "email"),
    [
        ("http://localhost", LOOPBACK_PEER, {}, "stranger@example.com"),
        ("http://demo.example.com", LOOPBACK_PEER, {}, ALLOWED),
        # a remote client that sends "Host: localhost" to a server bound to all interfaces
        ("http://localhost", ("203.0.113.9", 40000), {}, ALLOWED),
        ("http://localhost", ("testclient", 50000), {}, ALLOWED),
        ("http://localhost", LOOPBACK_PEER, {"X-Forwarded-For": "203.0.113.9"}, ALLOWED),
        ("http://localhost", LOOPBACK_PEER, {"CF-Connecting-IP": "203.0.113.9"}, ALLOWED),
        ("http://localhost", LOOPBACK_PEER, {"X-Real-IP": "203.0.113.9"}, ALLOWED),
        ("http://localhost", LOOPBACK_PEER, {"True-Client-IP": "203.0.113.9"}, ALLOWED),
        ("http://localhost", LOOPBACK_PEER, {"Via": "1.1 proxy"}, ALLOWED),
    ],
)
def test_test_login_refused_off_localhost_or_for_others(tmp_path: Path, base_url: str, peer: tuple[str, int],
                                                        headers: dict[str, str], email: str) -> None:
    settings = make_settings(tmp_path, test_auth=True)
    with TestClient(create_app(settings), base_url=base_url, client=peer) as c:
        token = csrf_of(c)
        r = c.post("/auth/test-login", data={"csrf_token": token, "email": email}, headers=headers)
        assert r.status_code == 403
    assert users(settings.database_path) == []


def test_test_login_needs_csrf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, test_auth=True)
    with TestClient(create_app(settings), base_url="http://localhost", client=LOOPBACK_PEER) as c:
        assert c.post("/auth/test-login", data={"email": ALLOWED}).status_code == 403
    assert users(settings.database_path) == []
