"""Record pages and actions: tabs with 15-day history, add/edit forms, soft delete, restore,
Recently deleted and permanent delete (B-04).

Every route needs a signed-in user (`CurrentUser`; signed-out visitors are redirected to the
sign-in page) and every POST passes the CSRF middleware. The user id always comes from the
session. Another user's record id behaves exactly like a missing one (404).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.records import meals, recently_deleted
from app.records import tasks as tasks_repo
from app.records.core import History
from app.records.dates import MAX_WINDOW_DAYS, parse_iso
from app.records.errors import DeletedDuplicate, DuplicateRecord, InvalidRecord, NotFound, RecordError
from app.web import aggregates
from app.web.deps import CurrentUser, Db, FormData
from app.web.forms import TAB_TITLES, VIEWS, DomainView, error_messages, form_values, parse, views_for


class Render(Protocol):
    def __call__(self, request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse: ...


@dataclass
class Section:
    """One domain's block on a tab page."""

    view: DomainView
    history: History | None
    notice: str | None = None
    prev_url: str | None = None
    next_url: str | None = None
    totals: dict[str, sqlite3.Row] = field(default_factory=dict)
    open_items: list[sqlite3.Row] = field(default_factory=list)


def _window_url(request: Request, domain: str, end: date) -> str:
    params = {k: v for k, v in request.query_params.items() if k not in {f"{domain}_start", f"{domain}_end"}}
    params[f"{domain}_end"] = end.isoformat()
    return f"{request.url.path}?{urlencode(params)}#{domain}"


def _section(request: Request, conn: sqlite3.Connection, user_id: int, view: DomainView, today: date) -> Section:
    domain, notice = view.domain, None
    try:
        start = parse_iso(request.query_params.get(f"{domain}_start"))
        end = parse_iso(request.query_params.get(f"{domain}_end"))
        history = view.module.history(conn, user_id, today=today, start=start, end=end)
    except InvalidRecord as exc:
        notice = f"{exc} Showing the default range."
        history = view.module.history(conn, user_id, today=today)
    section = Section(view, history, notice)
    if history is not None:
        w = history.window
        section.prev_url = _window_url(request, domain, w.prev_end) if w.has_prev else None
        section.next_url = _window_url(request, domain, w.next_end) if w.has_next else None
        if domain == "meals":
            section.totals = meals.day_totals(conn, user_id, w.start, w.end)
    if domain == "job_applications":
        section.open_items = view.module.list_open(conn, user_id)
    return section


def _return_url(view: DomainView, record_date: date, today: date) -> str:
    """Tab URL whose window for this domain shows `record_date` (the default one if it already does)."""
    default_start = today if view.days_ahead else today - timedelta(days=MAX_WINDOW_DAYS - 1)
    query = "" if record_date >= default_start else "?" + urlencode({f"{view.domain}_end": record_date.isoformat()})
    return f"/{view.tab}{query}#{view.domain}"


def build_router(render: Render, today: Callable[[], date]) -> APIRouter:
    """Routes for the dashboard tabs and record actions. `today` is the injectable clock."""
    router = APIRouter()

    def not_found(request: Request) -> HTMLResponse:
        return render(request, "message.html", 404, title="Not found", heading="Not found",
                      text="This record doesn't exist or isn't available.")

    def view_of(domain: str) -> DomainView | None:
        return VIEWS.get(domain)

    def redirect(request: Request, url: str, flash: str) -> Response:
        request.session["flash"] = flash
        return RedirectResponse(url, status_code=303)

    def aggregates_page(request: Request, conn: sqlite3.Connection, user: sqlite3.Row, tab: str) -> HTMLResponse:
        now = today()
        params = dict(request.query_params)
        error = None
        try:
            sel = aggregates.select_range(params, now)
        except aggregates.RangeError as exc:
            error, sel = str(exc), aggregates.default_selection(now)
        agg = aggregates.build(conn, user["id"], tab, sel)
        return render(request, "aggregates.html", 400 if error else 200, title=TAB_TITLES[tab], user=user, tab=tab,
                      sel=sel, agg=agg, error=error, today=now, presets=aggregates.PRESETS,
                      max_days=aggregates.MAX_DAYS,
                      start_value=params.get("start", sel.period.start.isoformat())[:10],
                      end_value=params.get("end", sel.period.end.isoformat())[:10])

    def tab_page(request: Request, conn: sqlite3.Connection, user: sqlite3.Row, tab: str) -> HTMLResponse:
        if request.query_params.get("view") == "aggregates" and tab in aggregates.TABS:
            return aggregates_page(request, conn, user, tab)
        now = today()
        sections = [_section(request, conn, user["id"], v, now) for v in views_for(tab)]
        return render(request, "tab.html", title=TAB_TITLES[tab], user=user, tab=tab, sections=sections,
                      today=now, values={}, max_days=MAX_WINDOW_DAYS)

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request, conn: Db, user: CurrentUser) -> HTMLResponse:
        return tab_page(request, conn, user, "health")

    for tab_name in ("health", "content", "jobs", "tasks"):
        def make(tab: str) -> Callable[..., HTMLResponse]:
            def endpoint(request: Request, conn: Db, user: CurrentUser) -> HTMLResponse:
                return tab_page(request, conn, user, tab)
            return endpoint
        router.add_api_route(f"/{tab_name}", make(tab_name), methods=["GET"], response_class=HTMLResponse)

    @router.get("/analyst", response_class=HTMLResponse)
    def analyst(request: Request, user: CurrentUser) -> HTMLResponse:
        return render(request, "analyst.html", title="Analyst", user=user, tab="analyst")

    @router.get("/deleted", response_class=HTMLResponse)
    def deleted(request: Request, conn: Db, user: CurrentUser) -> HTMLResponse:
        return render(request, "deleted.html", title="Recently deleted", user=user, tab=None,
                      items=recently_deleted(conn, user["id"]), views=VIEWS)

    # ------------------------------------------------------------ create / edit
    def form_page(request: Request, user: sqlite3.Row, view: DomainView, values: dict[str, str], *,
                  record: sqlite3.Row | None, errors: list[str], status_code: int,
                  conflict: DeletedDuplicate | None = None) -> HTMLResponse:
        return render(request, "form.html", status_code, title=f"{'Edit' if record else 'Add'} {view.singular}",
                      user=user, tab=view.tab, view=view, values=values, record=record, errors=errors,
                      conflict=conflict, today=today())

    def save(request: Request, conn: sqlite3.Connection, user: sqlite3.Row, view: DomainView, form: dict[str, str],
             record: sqlite3.Row | None) -> Response:
        values = {f.name: form.get(f.name, "") for f in view.fields}
        now = today()
        try:
            data = parse(view, form)
            if record is None:
                view.module.create(conn, user["id"], data, today=now)
            else:
                view.module.update(conn, user["id"], record["id"], data, today=now)
        except ValidationError as exc:
            return form_page(request, user, view, values, record=record, errors=error_messages(view, exc),
                             status_code=400)
        except DeletedDuplicate as exc:
            return form_page(request, user, view, values, record=record, errors=[str(exc)], status_code=409,
                             conflict=exc)
        except DuplicateRecord as exc:
            return form_page(request, user, view, values, record=record, errors=[str(exc)], status_code=409)
        except NotFound:
            return not_found(request)
        except InvalidRecord as exc:
            return form_page(request, user, view, values, record=record, errors=[str(exc)], status_code=400)
        saved_date: date = getattr(data, view.date_field)
        return redirect(request, _return_url(view, saved_date, now), "Saved." if record is None else "Changes saved.")

    @router.post("/records/{domain}")
    def create(request: Request, domain: str, conn: Db, user: CurrentUser, form: FormData) -> Response:
        view = view_of(domain)
        return not_found(request) if view is None else save(request, conn, user, view, form, None)

    @router.get("/records/{domain}/{record_id:int}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser) -> HTMLResponse:
        view = view_of(domain)
        try:
            row = view.module.get(conn, user["id"], record_id) if view else None
        except NotFound:
            row = None
        if view is None or row is None:
            return not_found(request)
        return form_page(request, user, view, form_values(view, row), record=row, errors=[], status_code=200)

    @router.post("/records/{domain}/{record_id:int}/edit")
    def edit(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser, form: FormData) -> Response:
        view = view_of(domain)
        try:
            row = view.module.get(conn, user["id"], record_id) if view else None
        except NotFound:
            row = None
        if view is None or row is None:
            return not_found(request)
        return save(request, conn, user, view, form, row)

    @router.post("/records/tasks/{record_id:int}/status")
    def task_status(request: Request, record_id: int, conn: Db, user: CurrentUser, form: FormData) -> Response:
        try:
            tasks_repo.set_status(conn, user["id"], record_id, form.get("status", ""))
            row = tasks_repo.get(conn, user["id"], record_id)
        except NotFound:
            return not_found(request)
        except ValueError:
            return render(request, "message.html", 400, title="Not saved", heading="Not saved",
                          text="Unknown task status.", user=user)
        view = VIEWS["tasks"]
        done = row["status"] == "completed"
        return redirect(request, _return_url(view, date.fromisoformat(row["task_date"]), today()),
                        "Task marked as completed." if done else "Task marked as pending.")

    # ------------------------------------------------------------ delete / restore / purge
    def confirm_page(request: Request, user: sqlite3.Row, view: DomainView, row: sqlite3.Row, action: str,
                     errors: list[str], status_code: int = 200) -> HTMLResponse:
        return render(request, "confirm.html", status_code, title="Delete permanently" if action == "purge" else
                      "Delete record", user=user, tab=view.tab, view=view, row=row, action=action,
                      summary=view.module.summary(row), errors=errors)

    def lookup(conn: sqlite3.Connection, user: sqlite3.Row, domain: str, record_id: int, *,
               deleted: bool) -> tuple[DomainView, sqlite3.Row] | None:
        view = view_of(domain)
        if view is None:
            return None
        try:
            getter = view.module.get_deleted if deleted else view.module.get
            return view, getter(conn, user["id"], record_id)
        except NotFound:
            return None

    @router.get("/records/{domain}/{record_id:int}/delete", response_class=HTMLResponse)
    def delete_form(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser) -> HTMLResponse:
        found = lookup(conn, user, domain, record_id, deleted=False)
        return confirm_page(request, user, *found, "delete", []) if found else not_found(request)

    @router.post("/records/{domain}/{record_id:int}/delete")
    def delete(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser,
               form: FormData) -> Response:
        found = lookup(conn, user, domain, record_id, deleted=False)
        if found is None:
            return not_found(request)
        view, row = found
        try:
            view.module.soft_delete(conn, user["id"], record_id, confirm=form.get("confirm", ""))
        except NotFound:
            return not_found(request)
        except InvalidRecord as exc:
            return confirm_page(request, user, view, row, "delete", [str(exc)], 400)
        return redirect(request, _return_url(view, date.fromisoformat(row[view.date_field]), today()),
                        "Moved to Recently deleted. You can restore it from there.")

    @router.post("/records/{domain}/{record_id:int}/restore")
    def restore(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser) -> Response:
        found = lookup(conn, user, domain, record_id, deleted=True)
        if found is None:
            return not_found(request)
        view, row = found
        try:
            view.module.restore(conn, user["id"], record_id)
        except NotFound:
            return not_found(request)
        return redirect(request, _return_url(view, date.fromisoformat(row[view.date_field]), today()),
                        "Record restored.")

    @router.get("/records/{domain}/{record_id:int}/purge", response_class=HTMLResponse)
    def purge_form(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser) -> HTMLResponse:
        found = lookup(conn, user, domain, record_id, deleted=True)
        return confirm_page(request, user, *found, "purge", []) if found else not_found(request)

    @router.post("/records/{domain}/{record_id:int}/purge")
    def purge(request: Request, domain: str, record_id: int, conn: Db, user: CurrentUser, form: FormData) -> Response:
        found = lookup(conn, user, domain, record_id, deleted=True)
        if found is None:
            return not_found(request)
        view, row = found
        if form.get("acknowledge") != "yes":
            return confirm_page(request, user, view, row, "purge",
                                ["Tick the box to confirm that this can't be undone."], 400)
        try:
            view.module.permanent_delete(conn, user["id"], record_id, confirm=form.get("confirm", ""))
        except NotFound:
            return not_found(request)
        except RecordError as exc:
            return confirm_page(request, user, view, row, "purge", [str(exc)], 400)
        return redirect(request, "/deleted", "Deleted permanently.")

    return router
