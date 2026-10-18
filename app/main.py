"""Application factory: health, sign-in (B-02), the dashboard tabs and record routes (B-04), Demo Mode (B-14)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.config import Settings, load_settings
from app.db import connect, migrate
from app.demo import DEMO_DIR, DemoRuntime, assert_personal_database, assert_personal_path
from app.records.dates import TIMEZONE, today_local
from app.web.analyst import build_analyst_router
from app.web.demo import build_demo_router
from app.web.deps import DataUnavailable, Db, SignInRequired
from app.web.forms import TABS, date_limit
from app.web.headers import SecurityHeadersMiddleware
from app.web.records import build_router

WEB_DIR = Path(__file__).parent / "web"
log = logging.getLogger("app")


def _dmy(value: object) -> str:
    """Display form DD.MM.YYYY of a record date, or of a UTC timestamp in Asia/Jerusalem time."""
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    text = str(value)
    if "T" in text:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return stamp.astimezone(TIMEZONE).strftime("%d.%m.%Y")
    return date.fromisoformat(text).strftime("%d.%m.%Y")


def _open_personal_database(settings: Settings) -> None:
    """Startup in personal mode: refuse demo data (ADR-005 §6.2), then create or upgrade the schema."""
    assert_personal_path(settings.database_path)
    conn = connect(settings.database_path)
    try:
        assert_personal_database(conn)  # before migrate, so a demo copy is never modified
        migrate(conn)  # creates or upgrades the schema; never removes data (ADR-004 D6)
        assert_personal_database(conn)
    finally:
        conn.close()


def create_app(settings: Settings | None = None, identity_provider: auth.IdentityProvider | None = None,
               today: Callable[[], date] = today_local, demo_dir: Path = DEMO_DIR,
               analyst_router: APIRouter | None = None) -> FastAPI:
    """Build the app. `identity_provider` replaces Google and `today` the Asia/Jerusalem clock in tests.

    In demo mode (`APP_MODE=demo`) the data comes from a fresh synthetic working copy in
    `demo_dir` (default `data/demo/`), built at startup; Google sign-in is unavailable.
    """
    settings = settings or load_settings()
    mode = settings.app_mode
    demo = DemoRuntime(demo_dir, today, forbidden=(settings.database_path,)) if settings.is_demo else None
    if demo is None:
        google_ready = bool(settings.google_client_id and settings.google_client_secret)
        provider = identity_provider or (
            auth.GoogleProvider(settings.google_client_id, settings.google_client_secret) if google_ready else None
        )
    else:
        provider = None
        ignored = settings.ignored_in_demo()
        if ignored:
            log.warning("demo mode: ignoring %s (Google sign-in is not available in demo mode)", ", ".join(ignored))
    callback_url = f"{settings.public_base_url}/auth/callback"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if demo is not None:
            demo.start()  # fresh working copy on every start (O-4); DATABASE_PATH is not used
        else:
            _open_personal_database(settings)
        yield

    app = FastAPI(title="Personal AI Dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.demo = demo
    # Claims every valid session must carry (checked by auth.session_user_id).
    app.state.session_claims = (
        (lambda: {"mode": "demo", "generation": demo.generation}) if demo is not None else (lambda: {"mode": mode})
    )
    # Order matters: the session middleware (added last) runs first, so CSRF can read the session.
    app.add_middleware(auth.CSRFMiddleware)
    app.add_middleware(
        SessionMiddleware, secret_key=auth.session_signing_key(settings.session_secret, mode),
        session_cookie=auth.session_cookie_name(mode), max_age=auth.SESSION_SECONDS, same_site="lax",
        https_only=settings.public_base_url.startswith("https://"),
    )
    if demo is not None:
        app.add_middleware(auth.DemoLocalOnlyMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost: also covers refusals and errors
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.filters["dmy"] = _dmy
    templates.env.globals["date_limit"] = date_limit
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    def page(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        context.setdefault("user", None)
        context.setdefault("tab", None)
        flash = request.session.pop("flash", None)
        return templates.TemplateResponse(
            request, template, {"csrf_token": auth.csrf_token(request), "test_auth": settings.test_auth,
                                "demo_mode": demo is not None, "ai_enabled": settings.ai_enabled,
                                "tabs": TABS, "flash": flash if isinstance(flash, str) else None, **context},
            status_code=status_code,
        )

    def message(request: Request, status_code: int, heading: str, text: str) -> HTMLResponse:
        return page(request, "message.html", status_code, title=heading, heading=heading, text=text)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(SignInRequired)
    def sign_in_required(request: Request, _exc: SignInRequired) -> Response:
        """Signed-out visitors see the sign-in page at `/`; every other page redirects there."""
        if request.url.path != "/" or request.method != "GET":
            return RedirectResponse("/", status_code=303)
        if auth.session_user_id(request) is None and "uid" in request.session:
            auth.end_session(request)  # expired or unknown user: drop the stale session
        return page(request, "signin.html", title="Sign in", google_ready=provider is not None)

    @app.exception_handler(DataUnavailable)
    def data_unavailable(request: Request, exc: DataUnavailable) -> Response:
        """The demo working copy is missing or invalid: a clear page instead of a bare 500."""
        log.error("demo data unavailable: %s", exc)
        return message(request, 503, "Demo data unavailable",
                       "The demo data could not be opened. Restart the app; see the server log.")

    app.include_router(build_router(page, today))  # `/` (Food & Health) and the record routes
    # POST /analyst/ask (B-15); tests pass a router built with a scripted model client.
    app.include_router(analyst_router or build_analyst_router(page, today))
    if demo is not None:
        app.include_router(build_demo_router(page, demo))  # personal mode has no demo routes at all

    def demo_unavailable(request: Request) -> HTMLResponse:
        return message(request, 404, "Not available in demo mode",
                       "Google sign-in is not available in demo mode. Use Explore demo data on the start page.")

    @app.get("/login")
    async def login(request: Request) -> Response:
        if demo is not None:
            return demo_unavailable(request)
        if provider is None:
            return message(request, 503, "Google sign-in isn't set up",
                           "Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to the configuration (see README).")
        try:
            return await provider.begin(request, callback_url)
        except Exception as exc:  # Google's discovery document unreachable or invalid
            log.warning("sign-in could not start: %s", type(exc).__name__)
            return message(request, 503, "Google can't be reached",
                           "Sign-in is unavailable right now. Please try again in a few minutes.")

    @app.get("/auth/callback")
    async def callback(request: Request, conn: Db) -> Response:
        if demo is not None:
            return demo_unavailable(request)
        if provider is None:
            return message(request, 503, "Google sign-in isn't set up", "See README.")
        if request.query_params.get("error"):
            return message(request, 400, "Sign-in cancelled", "Google sign-in was cancelled. You can try again.")
        try:
            identity = await provider.complete(request)
        except auth.AuthError as exc:
            log.warning("sign-in failed: %s", exc)
            return message(request, 400, "Sign-in failed", "The sign-in could not be verified. Please try again.")
        return _admit(request, conn, identity)

    def _admit(request: Request, conn: sqlite3.Connection, identity: auth.Identity) -> Response:
        try:
            user_id = auth.admit(conn, identity, settings.allowed_emails)
        except auth.NotAllowed:
            auth.end_session(request)
            log.warning("sign-in refused: account not on the allowlist")  # no email in logs (S11)
            return message(request, 403, "Access denied", "This Google account isn't allowed to use this dashboard.")
        auth.start_session(request, user_id)
        log.info("sign-in succeeded: user %d", user_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> Response:
        auth.end_session(request)
        return RedirectResponse("/", status_code=303)

    if settings.test_auth:
        @app.post("/auth/test-login")
        def test_login(request: Request, conn: Db, email: Annotated[str, Form()]) -> Response:
            """Fake sign-in for automated browser tests: localhost only, allowlisted emails only."""
            if not auth.is_direct_local_request(request):
                return message(request, 403, "Access denied", "Test sign-in only works on localhost.")
            email = email.strip()
            identity = auth.Identity(sub=auth.TEST_AUTH_SUB_PREFIX + email.casefold(), email=email,
                                     email_verified=True, name="Test user")
            return _admit(request, conn, identity)

    return app
