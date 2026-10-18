"""Google sign-in, allowlist, session and CSRF (A-04 S7–S9; data rules in ADR-004 D5).

- Sign-in is Google OpenID Connect through Authlib: `state`, `nonce` and PKCE (S256); the ID
  token's signature, issuer, audience, expiry and nonce are verified server-side.
- `ALLOWED_EMAILS` admits a *new* account only if Google reports the email as verified; from
  then on the stable `google_sub` is the identity (S8). Anyone else gets 403 and no user row.
- The session is a signed, HttpOnly, SameSite=Lax cookie (Secure when served over https),
  valid for 8 hours from sign-in, replaced at sign-in and cleared at sign-out (S9).
- Every POST/PUT/PATCH/DELETE needs the session's CSRF token, in the `X-CSRF-Token` header or
  a `csrf_token` form field (CSRFMiddleware).
- Sessions are per mode (ADR-005 §6.2): personal and demo use different cookie names and
  signing keys (`session_signing_key`), and every session carries the claims the app expects
  (`mode`, plus `generation` in demo mode, supplied by `app.state.session_claims`).
  `session_user_id` rejects a session whose claims differ, so a demo session never
  authenticates in personal mode, nor the reverse, nor a demo session from an older copy.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.users import get_or_create_user

log = logging.getLogger("app.auth")

SESSION_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "dashboard_session"
DEMO_SESSION_COOKIE = "dashboard_demo_session"
DEMO_SUB_PREFIX = "demo:"  # the Demo Mode identity; never admitted in personal mode
GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
TEST_AUTH_SUB_PREFIX = "test-auth:"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_FORM_BYTES = 64 * 1024
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "true-client-ip", "via",
                  "cf-connecting-ip", "cf-ray")


class AuthError(Exception):
    """Sign-in could not be completed (cancelled, invalid state/token, provider error)."""


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool
    name: str | None


class IdentityProvider(Protocol):
    async def begin(self, request: Request, redirect_uri: str) -> Response: ...

    async def complete(self, request: Request) -> Identity: ...


class GoogleProvider:
    """Authlib-backed Google OIDC client. `metadata`/`transport` exist for offline tests."""

    def __init__(self, client_id: str, client_secret: str, *, metadata: dict[str, Any] | None = None,
                 transport: Any = None, issuers: tuple[str, ...] = GOOGLE_ISSUERS) -> None:
        from authlib.integrations.starlette_client import OAuth

        client_kwargs: dict[str, Any] = {"scope": "openid email profile", "code_challenge_method": "S256",
                                         "timeout": 10.0}
        if transport is not None:
            client_kwargs["transport"] = transport
        oauth = OAuth()
        self._client: Any = oauth.register(
            "google", client_id=client_id, client_secret=client_secret,
            server_metadata_url=None if metadata else GOOGLE_METADATA_URL, client_kwargs=client_kwargs,
        )
        if metadata:
            self._client.server_metadata.update(metadata)
        self._issuers = list(issuers)

    async def begin(self, request: Request, redirect_uri: str) -> Response:
        response: Response = await self._client.authorize_redirect(request, redirect_uri)
        return response

    async def complete(self, request: Request) -> Identity:
        from authlib.integrations.base_client import OAuthError

        try:
            token = await self._client.authorize_access_token(
                request, claims_options={"iss": {"essential": True, "values": self._issuers}}
            )
        except OAuthError as exc:
            raise AuthError(exc.error or "oauth_error") from None
        except Exception as exc:  # network, malformed token, bad signature, expired, wrong nonce/audience
            raise AuthError(type(exc).__name__) from None
        info = token.get("userinfo")
        if not info or not info.get("sub") or not info.get("email"):
            raise AuthError("missing_id_token_claims")
        return Identity(sub=str(info["sub"]), email=str(info["email"]),
                        email_verified=info.get("email_verified") is True, name=info.get("name"))


# ---------------------------------------------------------------- session helpers
def session_signing_key(secret: str, mode: str) -> str:
    """The cookie-signing key of one mode: HMAC-SHA256(SESSION_SECRET, "mode:<mode>")."""
    return hmac.new(secret.encode(), f"mode:{mode}".encode(), hashlib.sha256).hexdigest()


def session_cookie_name(mode: str) -> str:
    return DEMO_SESSION_COOKIE if mode == "demo" else SESSION_COOKIE


def _expected_claims(request: Request) -> dict[str, object] | None:
    """The claims a valid session must carry right now (set by create_app); None fails closed."""
    provider = getattr(request.app.state, "session_claims", None)
    claims = provider() if callable(provider) else None
    return claims if isinstance(claims, dict) and "mode" in claims else None


def csrf_token(request: Request) -> str:
    """The session's CSRF token, created on first use (also for signed-out visitors)."""
    token = request.session.get("csrf")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def start_session(request: Request, user_id: int) -> None:
    """Replace the whole session at sign-in (no value from before sign-in survives)."""
    claims = _expected_claims(request)
    if claims is None:
        raise RuntimeError("session claims are not configured")
    request.session.clear()
    request.session.update({"uid": user_id, "sid": secrets.token_urlsafe(16), "iat": int(time.time()),
                            "csrf": secrets.token_urlsafe(32), **claims})


def end_session(request: Request) -> None:
    request.session.clear()


def session_user_id(request: Request) -> int | None:
    """The signed-in user's id, or None if signed out, expired (8 h) or issued for another mode/copy."""
    uid, issued = request.session.get("uid"), request.session.get("iat")
    if not isinstance(uid, int) or isinstance(uid, bool) or not isinstance(issued, int):
        return None
    claims = _expected_claims(request)
    if claims is None or any(type(request.session.get(k)) is not type(v) or request.session.get(k) != v
                             for k, v in claims.items()):
        return None
    if "generation" not in claims and "generation" in request.session:
        return None
    age = time.time() - issued
    if age < -60 or age > SESSION_SECONDS:
        return None
    return uid


def current_user(request: Request, conn: sqlite3.Connection) -> sqlite3.Row | None:
    uid = session_user_id(request)
    if uid is None:
        return None
    row: sqlite3.Row | None = conn.execute("SELECT id, email, display_name FROM users WHERE id = ?", (uid,)).fetchone()
    return row


# ---------------------------------------------------------------- admission (S8)
class NotAllowed(Exception):
    """The Google account is not on the allowlist (no user row is created)."""


def admit(conn: sqlite3.Connection, identity: Identity, allowed_emails: frozenset[str]) -> int:
    """Return the user id for this identity, creating the user only for an allowlisted email.

    A known `google_sub` is the identity, whatever its email now is. A new one needs a
    verified email on the allowlist. Never deletes or re-creates users (ADR-004 D5).
    A Demo Mode identity (`demo:` subject) is always refused (ADR-005 §6.2).
    """
    if identity.sub.strip().casefold().startswith(DEMO_SUB_PREFIX):
        raise NotAllowed
    known = conn.execute("SELECT 1 FROM users WHERE google_sub = ?", (identity.sub,)).fetchone()
    if not known and not (identity.email_verified and identity.email.strip().casefold() in allowed_emails):
        raise NotAllowed
    return get_or_create_user(conn, identity.sub, identity.email.strip(), identity.name)


def is_direct_local_request(request: Request) -> bool:
    """True only for a request from a loopback address, addressed to localhost, with no proxy headers.

    The Host header is client-controlled, so the peer address is what proves the request is local.
    """
    if request.client is None:
        return False
    try:
        peer_is_loopback = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False
    host = (request.url.hostname or "").lower()
    return peer_is_loopback and host in _LOCAL_HOSTS and not any(h in request.headers for h in _PROXY_HEADERS)


class DemoLocalOnlyMiddleware:
    """Demo mode only: answer 403 to every request that is not a direct local one (ADR-005 §6.3).

    Applies to every path, static files and health check included, so nothing of the demo is
    reachable through a tunnel or reverse proxy (they add proxy headers or a non-localhost Host)
    or from another machine (non-loopback peer). Lifespan events pass through.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and not is_direct_local_request(Request(scope)):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await PlainTextResponse("Demo mode is only available on this computer (localhost).",
                                    status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------- CSRF (S9)
class CSRFMiddleware:
    """Reject state-changing requests without the session's CSRF token.

    Must run inside SessionMiddleware. The token is read from the X-CSRF-Token header, or for
    URL-encoded forms from a `csrf_token` field (the body is buffered, up to 64 KB, and
    replayed to the application unchanged).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        session: MutableMapping[str, Any] = scope.get("session") or {}
        expected = session.get("csrf")
        headers = Headers(scope=scope)
        supplied = headers.get("x-csrf-token")
        if supplied is None and headers.get("content-type", "").split(";")[0].strip().lower() == \
                "application/x-www-form-urlencoded":
            body, outcome = await _read_body(receive, _MAX_FORM_BYTES)
            if outcome == "disconnected":
                return  # the client left before sending the whole form: never act on a partial body
            if outcome == "too_large":
                await PlainTextResponse("Request too large.", status_code=413)(scope, receive, send)
                return
            values = parse_qs(body.decode("latin-1")).get("csrf_token")
            supplied = values[0] if values else None
            receive = _replay(body, receive)
        if not isinstance(expected, str) or not supplied or not hmac.compare_digest(expected, supplied):
            await PlainTextResponse("Security check failed. Reload the page and try again.",
                                    status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _read_body(receive: Receive, limit: int) -> tuple[bytes, str]:
    """Read the whole request body: (body, "complete" | "too_large" | "disconnected")."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"", "disconnected"
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return b"", "too_large"
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks), "complete"


def _replay(body: bytes, original: Receive) -> Callable[[], Awaitable[Message]]:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original()  # later calls (e.g. disconnect checks while streaming) go to the server

    return receive
