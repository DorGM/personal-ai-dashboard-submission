"""B-04 routes: tabs, history, forms, soft delete, Recently deleted, restore and permanent
delete; sign-in protection, CSRF and two-user isolation (TEST_AUTH sign-in, synthetic data)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect
from app.main import create_app
from tests.conftest import TEST_SECRET
from tests.integration.test_records_repo import SAMPLES

T = date(2026, 10, 18)
A, B = "a@example.com", "b@example.com"
PEER = ("127.0.0.1", 50000)
PLACEHOLDER = "לא הוכנס תיעוד עבור יום זה"
DUPLICATE = "נתון זה כבר הוכנס, אם תרצה להוסיף אליו נתונים תכניס זאת ידנית"
TAB_OF = {"weight": "health", "meals": "health", "workouts": "health", "sleep": "health", "steps": "health",
          "articles": "content", "podcasts": "content", "movies": "content", "series": "content",
          "job_applications": "jobs", "tasks": "tasks"}
ALL = sorted(TAB_OF)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate({"session_secret": TEST_SECRET, "database_path": tmp_path / "web.db",
                                    "test_auth": True, "allowed_emails": f"{A},{B}"})


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings, today=lambda: T)


def csrf_of(c: TestClient, path: str = "/") -> str:
    return c.get(path).text.split('<meta name="csrf-token" content="', 1)[1].split('"', 1)[0]


def signed_in(app: FastAPI, email: str) -> TestClient:
    c = TestClient(app, base_url="http://localhost", client=PEER)
    c.__enter__()
    r = c.post("/auth/test-login", data={"csrf_token": csrf_of(c), "email": email}, follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture
def a(app: FastAPI) -> Iterator[TestClient]:
    c = signed_in(app, A)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def b(app: FastAPI) -> Iterator[TestClient]:
    c = signed_in(app, B)
    yield c
    c.__exit__(None, None, None)


def form(values: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in values.items()}


def post(c: TestClient, path: str, data: dict[str, Any] | None = None, *, csrf: bool = True) -> Any:
    payload = form(data or {})
    if csrf:
        payload["csrf_token"] = csrf_of(c, "/analyst")
    return c.post(path, data=payload, follow_redirects=False)


def create(c: TestClient, domain: str, values: dict[str, Any]) -> int:
    r = post(c, f"/records/{domain}", values)
    assert r.status_code == 303, r.text
    return last_id(c, domain)


def db(settings: Settings) -> sqlite3.Connection:
    return connect(settings.database_path)


def last_id(c: TestClient, domain: str) -> int:
    path = c.app.state.settings.database_path  # type: ignore[attr-defined]
    table = {"weight": "weight_records", "sleep": "sleep_records", "steps": "step_records"}.get(domain, domain)
    conn = connect(path)
    try:
        return int(conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def snapshot(settings: Settings) -> dict[str, list[tuple[object, ...]]]:
    conn = db(settings)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in names}
    finally:
        conn.close()


def section(html: str, domain: str) -> str:
    start = html.index(f'<section class="domain" id="{domain}"')
    return html[start:html.index("</section>", start)]


def text_of(domain: str) -> str:
    """A distinctive text value of the domain's sample, as shown in lists."""
    values = SAMPLES[domain][0]
    for key in ("description", "title", "podcast_name", "series_name", "company_name"):
        if key in values:
            return str(values[key])
    return {"weight": "72.4 kg", "sleep": "7.5 h sleep", "steps": "9,000 steps", "workouts": "Aerobic · 30 min"}[domain]


# ---------------------------------------------------------------- shell and protection
GET_PAGES = ["/health", "/content", "/jobs", "/tasks", "/analyst", "/deleted", "/records/weight/1/edit",
             "/records/weight/1/delete", "/records/weight/1/purge"]


@pytest.mark.parametrize("path", GET_PAGES)
def test_signed_out_visitor_is_sent_to_sign_in(app: FastAPI, path: str) -> None:
    with TestClient(app, base_url="http://localhost", client=PEER) as c:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "Test sign-in" in c.get("/").text


def test_signed_out_post_with_valid_csrf_writes_nothing(app: FastAPI, settings: Settings) -> None:
    with TestClient(app, base_url="http://localhost", client=PEER) as c:
        before = snapshot(settings)
        r = c.post("/records/steps", data={"csrf_token": csrf_of(c), "record_date": "2026-10-10",
                                           "steps_count": "5"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert snapshot(settings) == before


def test_signed_in_home_is_food_and_health_with_five_tabs(a: TestClient) -> None:
    html = a.get("/").text
    assert "Signed in as" in html and '<h2 class="page-title">Food &amp; Health</h2>' in html
    nav = html[html.index('<nav class="tabs"'):html.index("</nav>")]
    labels = re.findall(r'class="tab tab-\w+"[^>]*>([^<]+)</a>', nav)
    assert labels == ["Food &amp; Health", "Content", "Jobs &amp; Career", "Tasks", "Analyst"]
    assert 'href="/deleted"' in html
    assert 'aria-current="page">Content' in a.get("/content").text


def test_analyst_placeholder(a: TestClient) -> None:
    """Without OpenAI settings the placeholder shows the personal-mode no-key message (O-5, B-14)."""
    r = a.get("/analyst")
    assert r.status_code == 200 and "AI-powered analysis is not configured. Add OPENAI_API_KEY and " \
        "OPENAI_MODEL to .env (see README) to enable it." in r.text
    assert "Coming in a later step" not in r.text


def test_analyst_placeholder_with_ai_configured(settings: Settings) -> None:
    configured = settings.model_copy(update={"openai_api_key": "sk-test-placeholder", "openai_model": "test-model"})
    c = signed_in(create_app(configured, today=lambda: T), A)
    try:
        r = c.get("/analyst")
        assert 'id="analyst-question"' in r.text and "not configured" not in r.text
    finally:
        c.__exit__(None, None, None)


def test_mobile_bottom_bar_css() -> None:
    css = (Path(__file__).resolve().parents[2] / "app/web/static/app.css").read_text(encoding="utf-8")
    mobile = css[css.index("@media (max-width: 767.98px)"):]
    assert ".tabs { position: fixed;" in mobile and "bottom: 0" in mobile
    assert re.search(r"\.cat-other \{[^}]*background: var\(--cat-other\); border-color: #[0-9a-f]{6}", css)


def test_empty_state_without_records(a: TestClient) -> None:
    html = a.get("/health").text
    assert "No weight recorded yet" in section(html, "weight") and PLACEHOLDER not in html


# ---------------------------------------------------------------- create, duplicate, edit
@pytest.mark.parametrize("domain", ALL)
def test_create_shows_record_with_its_date(a: TestClient, domain: str) -> None:
    r = post(a, f"/records/{domain}", SAMPLES[domain][0])
    assert r.status_code == 303 and r.headers["location"].startswith(f"/{TAB_OF[domain]}")
    page = a.get(r.headers["location"]).text
    html = section(page, domain)
    assert text_of(domain) in html and "(10.10.2026)" in html
    assert '<p class="flash" role="status">Saved.</p>' in page
    assert 'class="flash"' not in a.get(f"/{TAB_OF[domain]}").text  # shown once


def test_editing_a_completed_task_keeps_its_status(a: TestClient) -> None:
    rid = create(a, "tasks", {"task_date": "2026-10-18", "title": "Done already", "category": "family"})
    post(a, f"/records/tasks/{rid}/status", {"status": "completed"})
    page = a.get(f"/records/tasks/{rid}/edit").text
    assert '<option value="completed" selected>' in page
    post(a, f"/records/tasks/{rid}/edit", {"task_date": "2026-10-19", "title": "Done already",
                                           "category": "family", "status": "completed"})
    assert "Mark pending" in section(a.get("/tasks").text, "tasks")


@pytest.mark.parametrize("domain", ALL)
def test_duplicate_shows_hebrew_message(a: TestClient, settings: Settings, domain: str) -> None:
    create(a, domain, SAMPLES[domain][0])
    before = snapshot(settings)
    r = post(a, f"/records/{domain}", SAMPLES[domain][0])
    assert r.status_code == 409 and DUPLICATE in r.text and "Restore the deleted record" not in r.text
    assert snapshot(settings) == before


@pytest.mark.parametrize("domain", ALL)
def test_edit_changes_the_record(a: TestClient, settings: Settings, domain: str) -> None:
    rid = create(a, domain, SAMPLES[domain][0])
    page = a.get(f"/records/{domain}/{rid}/edit")
    assert page.status_code == 200 and 'action="/records/' + domain + f'/{rid}/edit"' in page.text
    r = post(a, f"/records/{domain}/{rid}/edit", SAMPLES[domain][2])
    assert r.status_code == 303
    conn = db(settings)
    try:
        table = {"weight": "weight_records", "sleep": "sleep_records", "steps": "step_records"}.get(domain, domain)
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()
    for key, value in SAMPLES[domain][2].items():
        assert row[key] == value, key


def test_validation_errors_are_friendly_and_keep_input(a: TestClient) -> None:
    r = post(a, "/records/weight", {"record_date": "2026-10-10", "weight_kg": "-3"})
    assert r.status_code == 400 and "Weight (kg):" in r.text and 'value="-3"' in r.text
    r = post(a, "/records/steps", {"record_date": "2026-10-19", "steps_count": "10"})
    assert r.status_code == 400 and "can&#39;t be in the future" in r.text
    r = post(a, "/records/tasks", {"task_date": "2026-11-02", "title": "Too far", "category": "other"})
    assert r.status_code == 400 and "at most 14 days" in r.text
    assert post(a, "/records/tasks", {"task_date": "2026-11-01", "title": "Ok", "category": "other"}).status_code == 303
    r = post(a, "/records/articles", {"record_date": "2026-10-10", "title": "x", "url": "javascript:alert(1)"})
    assert r.status_code == 400 and "Link:" in r.text


def test_invisible_only_title_shows_friendly_error(a: TestClient, settings: Settings) -> None:
    before = snapshot(settings)
    r = post(a, "/records/movies", {"record_date": "2026-10-10", "title": "\u200b"})
    assert r.status_code == 400 and "Title: Enter visible text" in r.text
    assert snapshot(settings) == before


def test_client_supplied_user_id_is_ignored(a: TestClient, b: TestClient, settings: Settings) -> None:
    create(b, "steps", {"record_date": "2026-10-01", "steps_count": 1})
    r = post(a, "/records/steps", {"record_date": "2026-10-10", "steps_count": "7", "user_id": "2"})
    assert r.status_code == 303
    conn = db(settings)
    try:
        owners = conn.execute("SELECT u.email FROM step_records s JOIN users u ON u.id = s.user_id"
                              " WHERE s.steps_count = 7").fetchall()
    finally:
        conn.close()
    assert [o[0] for o in owners] == [A]


def test_unknown_domain_and_deferred_tables_have_no_routes(a: TestClient) -> None:
    for domain in ("foo", "job_search_profile", "job_reports", "job_recommendations", "industry_trends",
                   "daily_calories", "users"):
        assert post(a, f"/records/{domain}", {"x": "1"}).status_code == 404
        assert a.get(f"/records/{domain}/1/edit").status_code == 404


# ---------------------------------------------------------------- history view
def test_explicit_zero_steps_is_not_a_placeholder(a: TestClient) -> None:
    create(a, "steps", {"record_date": "2026-10-15", "steps_count": 0})
    html = section(a.get("/health").text, "steps")
    assert "0 steps" in html and "(15.10.2026)" in html
    assert html.count(PLACEHOLDER) == 3  # 16.10, 17.10, 18.10 — the window starts at the first record
    assert 'lang="he" dir="rtl"' in html


def test_window_moves_within_earliest_and_today(a: TestClient) -> None:
    create(a, "sleep", {"record_date": "2026-09-01", "hours_slept": 7})
    create(a, "sleep", {"record_date": "2026-10-17", "hours_slept": 8})
    html = section(a.get("/health").text, "sleep")
    assert "04.10.2026 – 18.10.2026" in html and html.count(PLACEHOLDER) == 14
    earlier = re.search(r'href="([^"]*sleep_end=2026-10-03[^"]*)"', html)
    assert earlier and 'aria-disabled="true">Later' in html
    html2 = section(a.get(earlier.group(1).replace("&amp;", "&")).text, "sleep")
    assert "19.09.2026 – 03.10.2026" in html2 and "Later ▶</a>" in html2
    first = section(a.get("/health?sleep_end=2026-08-01").text, "sleep")
    assert "01.09.2026 – 15.09.2026" in first and "7 h sleep" in first and 'aria-disabled="true">◀ Earlier' in first
    bad = section(a.get("/health?sleep_start=2026-10-01&sleep_end=2026-10-17").text, "sleep")
    assert "at most 15 days" in bad and "04.10.2026 – 18.10.2026" in bad
    garbage = a.get("/health?sleep_end=not-a-date")
    assert garbage.status_code == 200 and "not valid" in section(garbage.text, "sleep")


def test_meal_calories_and_day_total(a: TestClient) -> None:
    create(a, "meals", {"record_date": "2026-10-10", "meal_type": "lunch", "description": "Soup",
                        "estimated_calories": 450})
    create(a, "meals", {"record_date": "2026-10-10", "meal_type": "dinner", "description": "Rice"})
    html = section(a.get("/health").text, "meals")
    assert "≈ 450 kcal est." in html and "user provided" in html and "calories unknown" in html
    assert "Day total:" in html and "estimated partial" in html


def test_tasks_show_category_colours_and_toggle(a: TestClient) -> None:
    ids = {cat: create(a, "tasks", {"task_date": "2026-10-20", "title": f"Task {cat}", "category": cat})
           for cat in ("family", "career", "friends", "housework", "other")}
    html = section(a.get("/tasks").text, "tasks")
    for cat in ids:
        assert f'class="badge cat cat-{cat}"' in html
    assert "18.10.2026 – 01.11.2026" in html
    r = post(a, f"/records/tasks/{ids['other']}/status", {"status": "completed"})
    assert r.status_code == 303
    assert "Mark pending" in section(a.get("/tasks").text, "tasks")
    assert post(a, f"/records/tasks/{ids['other']}/status", {"status": "bogus"}).status_code == 400


def test_open_applications_are_highlighted(a: TestClient) -> None:
    base = SAMPLES["job_applications"][1]
    create(a, "job_applications", {**base, "job_title": "Open role", "status": "interviewing", "interview_stage": 1})
    create(a, "job_applications", {**base, "job_title": "Closed role", "status": "rejected"})
    html = section(a.get("/jobs").text, "job_applications")
    open_items = re.findall(r'<li class="record open">.*?</li>', html, re.DOTALL)
    assert open_items and all("Open role" in item for item in open_items)
    assert "Closed role" in html and not any("Closed role" in item for item in open_items)


# ---------------------------------------------------------------- delete, restore, purge
@pytest.mark.parametrize("domain", ALL)
def test_soft_delete_restore_and_permanent_delete(a: TestClient, settings: Settings, domain: str) -> None:
    rid = create(a, domain, SAMPLES[domain][0])
    keep = create(a, domain, SAMPLES[domain][1])
    page = a.get(f"/records/{domain}/{rid}/delete")
    assert page.status_code == 200 and 'name="confirm" value="delete"' in page.text
    assert post(a, f"/records/{domain}/{rid}/delete", {}).status_code == 400  # no explicit confirmation
    assert post(a, f"/records/{domain}/{rid}/delete", {"confirm": "delete"}).status_code == 303
    assert a.get(f"/records/{domain}/{rid}/edit").status_code == 404
    deleted_page = a.get("/deleted").text
    assert f'action="/records/{domain}/{rid}/restore"' in deleted_page
    # re-adding the reserved key offers Restore / Delete permanently, not the generic message
    r = post(a, f"/records/{domain}", SAMPLES[domain][0])
    assert r.status_code == 409 and DUPLICATE not in r.text
    assert f'action="/records/{domain}/{rid}/restore"' in r.text and f'href="/records/{domain}/{rid}/purge"' in r.text
    assert post(a, f"/records/{domain}/{rid}/restore").status_code == 303
    assert a.get(f"/records/{domain}/{rid}/edit").status_code == 200
    # permanent delete: only once soft-deleted, only with confirm + acknowledgement
    assert a.get(f"/records/{domain}/{rid}/purge").status_code == 404
    assert post(a, f"/records/{domain}/{rid}/purge", {"confirm": "permanent", "acknowledge": "yes"}).status_code == 404
    post(a, f"/records/{domain}/{rid}/delete", {"confirm": "delete"})
    purge_page = a.get(f"/records/{domain}/{rid}/purge")
    assert purge_page.status_code == 200 and "can't be undone" in purge_page.text
    before = snapshot(settings)
    assert post(a, f"/records/{domain}/{rid}/purge", {"confirm": "permanent"}).status_code == 400
    assert post(a, f"/records/{domain}/{rid}/purge", {"acknowledge": "yes"}).status_code == 400
    assert post(a, f"/records/{domain}/{rid}/purge", {"confirm": "delete", "acknowledge": "yes"}).status_code == 400
    assert snapshot(settings) == before
    r = post(a, f"/records/{domain}/{rid}/purge", {"confirm": "permanent", "acknowledge": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/deleted"
    assert a.get(f"/records/{domain}/{keep}/edit").status_code == 200
    assert f"/records/{domain}/{rid}/" not in a.get("/deleted").text
    conn = db(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM deletion_grants").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert create(a, domain, SAMPLES[domain][0]) > 0  # key free again


def test_soft_deleted_records_are_hidden_from_history(a: TestClient) -> None:
    rid = create(a, "meals", SAMPLES["meals"][0])
    post(a, f"/records/meals/{rid}/delete", {"confirm": "delete"})
    html = section(a.get("/health").text, "meals")
    assert "Lentil soup" not in html and "No meals recorded yet" in html


# ---------------------------------------------------------------- isolation and CSRF
@pytest.mark.parametrize("domain", ALL)
def test_other_user_gets_404_everywhere(a: TestClient, b: TestClient, settings: Settings, domain: str) -> None:
    rid = create(a, domain, SAMPLES[domain][0])
    deleted = create(a, domain, SAMPLES[domain][1])
    post(a, f"/records/{domain}/{deleted}/delete", {"confirm": "delete"})
    before = snapshot(settings)
    assert b.get(f"/records/{domain}/{rid}/edit").status_code == 404
    assert post(b, f"/records/{domain}/{rid}/edit", SAMPLES[domain][2]).status_code == 404
    assert b.get(f"/records/{domain}/{rid}/delete").status_code == 404
    assert post(b, f"/records/{domain}/{rid}/delete", {"confirm": "delete"}).status_code == 404
    assert post(b, f"/records/{domain}/{deleted}/restore").status_code == 404
    assert b.get(f"/records/{domain}/{deleted}/purge").status_code == 404
    assert post(b, f"/records/{domain}/{deleted}/purge", {"confirm": "permanent", "acknowledge": "yes"}
                ).status_code == 404
    if domain == "tasks":
        assert post(b, f"/records/tasks/{rid}/status", {"status": "completed"}).status_code == 404
    assert snapshot(settings) == before
    assert text_of(domain) not in b.get(f"/{TAB_OF[domain]}").text
    assert f"/records/{domain}/{deleted}/" not in b.get("/deleted").text


def post_routes(rid: int) -> list[tuple[str, dict[str, str]]]:
    return [
        ("/records/steps", {"record_date": "2026-10-11", "steps_count": "1"}),
        (f"/records/steps/{rid}/edit", {"record_date": "2026-10-10", "steps_count": "2"}),
        (f"/records/steps/{rid}/delete", {"confirm": "delete"}),
        (f"/records/steps/{rid}/restore", {}),
        (f"/records/steps/{rid}/purge", {"confirm": "permanent", "acknowledge": "yes"}),
        (f"/records/tasks/{rid}/status", {"status": "completed"}),
        ("/logout", {}),
    ]


@pytest.mark.parametrize("index", range(7))
def test_every_post_requires_csrf(a: TestClient, settings: Settings, index: int) -> None:
    rid = create(a, "steps", {"record_date": "2026-10-10", "steps_count": 5})
    create(a, "tasks", {"task_date": "2026-10-10", "title": "x", "category": "other"})
    if index in (3, 4):
        post(a, f"/records/steps/{rid}/delete", {"confirm": "delete"})
    path, data = post_routes(rid)[index]
    before = snapshot(settings)
    for token in (None, "wrong"):
        payload = dict(data) if token is None else {**data, "csrf_token": token}
        assert a.post(path, data=payload, follow_redirects=False).status_code == 403
    assert snapshot(settings) == before
    assert "Signed in as" in a.get("/").text
