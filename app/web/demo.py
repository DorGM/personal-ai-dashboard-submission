"""Demo Mode routes (registered only when APP_MODE=demo; ADR-005 §6.3, §6.6).

- `POST /demo/login`: "Explore demo data". Signs in as the synthetic demo user, only for a
  direct local request (the DemoLocalOnlyMiddleware already refuses every other request).
- `GET /demo/reset`: confirmation page. `POST /demo/reset` (field `confirm=reset`) rebuilds the
  working copy from the fixture and switches to it; the generation changes, so every demo
  session ends and the reviewer chooses "Explore demo data" again.
Both POSTs pass the CSRF middleware.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import auth
from app.demo import DemoError, DemoRuntime
from app.web.deps import FormData
from app.web.records import Render

RESET_CONFIRM = "reset"
log = logging.getLogger("app.demo")


def build_demo_router(render: Render, runtime: DemoRuntime) -> APIRouter:
    router = APIRouter()

    def refused(request: Request) -> HTMLResponse:
        return render(request, "message.html", 403, title="Access denied", heading="Access denied",
                      text="Demo mode is only available on this computer (localhost).")

    @router.post("/demo/login")
    def demo_login(request: Request) -> Response:
        if not auth.is_direct_local_request(request):
            return refused(request)
        auth.start_session(request, runtime.user_id)
        return RedirectResponse("/", status_code=303)

    def signed_in_user(request: Request) -> sqlite3.Row | None:
        """The session's demo user for the page header, or None (no session, or no working copy)."""
        if auth.session_user_id(request) is None:
            return None
        try:
            conn = runtime.connect()
        except DemoError:
            return None
        try:
            return auth.current_user(request, conn)
        finally:
            conn.close()

    @router.get("/demo/reset", response_class=HTMLResponse)
    def reset_form(request: Request) -> HTMLResponse:
        if not auth.is_direct_local_request(request):
            return refused(request)
        return render(request, "demo_reset.html", title="Reset demo data", errors=[], user=signed_in_user(request))

    @router.post("/demo/reset")
    def reset(request: Request, form: FormData) -> Response:
        if not auth.is_direct_local_request(request):
            return refused(request)
        if form.get("confirm") != RESET_CONFIRM:
            return render(request, "demo_reset.html", 400, title="Reset demo data",
                          errors=["The reset was not confirmed."], user=signed_in_user(request))
        try:
            runtime.reset()
        except DemoError as exc:
            log.error("demo reset failed: %s", exc)
            return render(request, "message.html", 500, title="Reset failed", heading="Reset failed",
                          text="The demo data could not be rebuilt. Restart the app; see the server log.")
        auth.end_session(request)
        request.session["flash"] = "Demo data was reset to the original synthetic records. " \
                                   "Choose Explore demo data to continue."
        return RedirectResponse("/", status_code=303)

    return router
