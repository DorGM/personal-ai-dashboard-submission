"""B-15 `POST /analyst/ask`: gates, sign-in and CSRF protection, rendering and escaping.

The router is mounted inside the test on an app from `create_app` (app/main.py is integration-owned and not
changed). The model client is a scripted fake behind a spy factory; no OpenAI call is made.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app import auth
from app.ai_client import ModelAuthError, ModelClient, ModelTurn
from app.analytics import agent, readonly
from app.analytics.ports import MetricsBackend, MetricsUnavailable
from app.config import Settings
from app.db import connect, migrate
from app.main import WEB_DIR, create_app
from app.records import DOMAINS
from app.users import get_or_create_user
from app.web.analyst import (
    MSG_AI_DISABLED_DEMO,
    MSG_AI_DISABLED_PERSONAL,
    MSG_EMPTY,
    MSG_HOURLY_CAP,
    MSG_IN_FLIGHT,
    MSG_TOO_LONG,
    QuestionGate,
    build_analyst_router,
    clean_question,
)
from app.web.forms import TABS
from tests.conftest import TEST_SECRET
from tests.integration.test_analytics_agent import FULL, ScriptedClient, answer, fc, finding, msg, turn
from tests.integration.test_records_web import PEER, csrf_of, signed_in
from tests.unit.analytics.fake_metrics import FakeMetrics

T = date(2026, 10, 18)
A = "a@example.com"
MALICIOUS = "<img src=x onerror=alert(1)>"


AISettings = Settings  # the AI fields live in app.config since integration


@dataclass
class Spy:
    scripts: list[list[ModelTurn | Exception]] = field(default_factory=list)
    created: int = 0
    clients: list[ScriptedClient] = field(default_factory=list)

    def factory(self, _settings: Any) -> ModelClient:
        self.created += 1
        client = ScriptedClient(self.scripts.pop(0) if self.scripts else [])
        self.clients.append(client)
        return client


@dataclass
class Env:
    app: FastAPI
    settings: AISettings
    spy: Spy
    metrics: FakeMetrics
    gate: QuestionGate
    metrics_missing: list[bool]


def make_render() -> Any:
    """Mirror of create_app's `page` renderer (the router receives `page` in production)."""
    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    def render(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        context.setdefault("user", None)
        context.setdefault("tab", None)
        return templates.TemplateResponse(request, template, {"csrf_token": auth.csrf_token(request), "tabs": TABS,
                                                              "flash": None, "test_auth": True, **context},
                                          status_code=status_code)
    return render


def build(tmp_path: Path, **ai: Any) -> Env:
    if "ai_enabled" in ai:
        ai["ai_enabled_flag"] = ai.pop("ai_enabled")
    mode = ai.pop("app_mode", "personal")
    settings = AISettings.model_validate({
        "session_secret": TEST_SECRET, "database_path": tmp_path / "web.db", "test_auth": True,
        "allowed_emails": A, "openai_api_key": "sk-test-placeholder", "openai_model": "test-model", **ai})
    spy, metrics, gate, missing = Spy(), FakeMetrics(), QuestionGate(), [False]

    def metrics_factory() -> MetricsBackend:
        if missing[0]:
            raise MetricsUnavailable("not installed")
        return metrics

    router = build_analyst_router(make_render(), lambda: T, client_factory=spy.factory,
                                  metrics_factory=metrics_factory, gate=gate)
    app = create_app(settings, today=lambda: T, analyst_router=router)
    if mode != "personal":  # wording only: demo-mode startup rules (no TEST_AUTH) are covered in test_demo.py
        settings = settings.model_copy(update={"app_mode": mode})
        app.state.settings = settings
    return Env(app, settings, spy, metrics, gate, missing)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return build(tmp_path)


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    c = signed_in(env.app, A)
    yield c
    c.__exit__(None, None, None)


def uid_of(env: Env) -> int:
    conn = connect(env.settings.database_path)
    try:
        return int(conn.execute("SELECT id FROM users WHERE email = ?", (A,)).fetchone()[0])
    finally:
        conn.close()


def add_sleep(env: Env) -> int:
    uid = uid_of(env)
    for i in range(28):
        env.metrics.add(uid, "sleep", date(2026, 9, 20) + timedelta(days=i), 6.5 if i % 2 else 7.5)
    return uid


def ask(c: TestClient, question: str, *, csrf: bool = True) -> Any:
    data = {"question": question}
    if csrf:
        data["csrf_token"] = csrf_of(c, "/analyst")
    return c.post("/analyst/ask", data=data, follow_redirects=False)


def happy() -> list[ModelTurn | Exception]:
    return [turn(fc("summarize_metric", FULL)), turn(msg(answer()))]


# --------------------------------------------------------------------------- gates
@pytest.mark.parametrize("ai", [{"ai_enabled": False}, {"openai_api_key": ""}, {"openai_model": ""}])
def test_ai_disabled_shows_message_and_never_builds_a_client(tmp_path: Path, ai: dict[str, Any]) -> None:
    env = build(tmp_path, **ai)
    c = signed_in(env.app, A)
    try:
        r = ask(c, "How did I sleep?")
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 200 and MSG_AI_DISABLED_PERSONAL in r.text
    assert env.spy.created == 0
    assert "AI-generated analysis" not in r.text
    assert 'aria-live="polite"' in r.text and 'id="analyst-question"' in r.text


def test_ai_disabled_demo_wording(tmp_path: Path) -> None:
    env = build(tmp_path, ai_enabled=False, app_mode="demo")
    c = signed_in(env.app, A)
    try:
        r = ask(c, "How did I sleep?")
    finally:
        c.__exit__(None, None, None)
    assert MSG_AI_DISABLED_DEMO in r.text and MSG_AI_DISABLED_PERSONAL not in r.text
    assert MSG_AI_DISABLED_DEMO.startswith("AI-powered analysis is not available in the current demo configuration.")
    assert env.spy.created == 0


def test_signed_out_is_redirected(env: Env) -> None:
    with TestClient(env.app, base_url="http://localhost", client=PEER) as c:
        r = c.post("/analyst/ask", data={"question": "x", "csrf_token": csrf_of(c, "/")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert env.spy.created == 0


def test_missing_csrf_is_refused(env: Env, client: TestClient) -> None:
    r = ask(client, "How did I sleep?", csrf=False)
    assert r.status_code == 403 and env.spy.created == 0


@pytest.mark.parametrize(("question", "message"), [("   \x00\x07 ", MSG_EMPTY), ("a" * 501, MSG_TOO_LONG)])
def test_question_length(env: Env, client: TestClient, question: str, message: str) -> None:
    r = ask(client, question)
    assert r.status_code == 400 and message in r.text and env.spy.created == 0


def test_clean_question() -> None:
    assert clean_question("  how\x00 did\nI\tsleep \x1b ") == "how did I sleep"


def test_one_question_in_flight(env: Env, client: TestClient) -> None:
    uid = add_sleep(env)
    assert env.gate.admit(uid, time.monotonic(), 20) == "ok"  # another request is still running
    r = ask(client, "How did I sleep?")
    assert r.status_code == 409 and MSG_IN_FLIGHT in r.text and env.spy.created == 0
    env.gate.release(uid)
    env.spy.scripts.append(happy())
    assert ask(client, "How did I sleep?").status_code == 200


def test_hourly_cap(tmp_path: Path) -> None:
    env = build(tmp_path, ai_questions_per_hour=2)
    c = signed_in(env.app, A)
    try:
        add_sleep(env)
        env.spy.scripts += [happy(), happy()]
        assert ask(c, "How did I sleep?").status_code == 200
        assert ask(c, "How did I sleep?").status_code == 200
        r = ask(c, "How did I sleep?")
    finally:
        c.__exit__(None, None, None)
    assert r.status_code == 429 and MSG_HOURLY_CAP in r.text and env.spy.created == 2


# --------------------------------------------------------------------------- rendering
def test_answer_card(env: Env, client: TestClient) -> None:
    add_sleep(env)
    env.spy.scripts.append(happy())
    r = ask(client, "How did I sleep?")
    assert r.status_code == 200, r.text
    html = r.text
    assert 'aria-live="polite"' in html and "AI-generated analysis · every number computed from your records" in html
    assert "Your average sleep was 7.0 h." in html and "Answered" in html
    assert "Based on 28 documented days of 28 · 20.09–17.10" in html
    assert "<summary>Evidence</summary>" in html and "Tokens used: 300" in html
    assert 'name="question" value="How did my steps change"' in html  # follow-up button form
    assert html.count('name="csrf_token"') >= 3
    client_calls = env.spy.clients[0].calls
    assert client_calls[0]["input_items"][2]["content"] == "How did I sleep?"


def test_chart_and_data_table(env: Env, client: TestClient) -> None:
    add_sleep(env)
    series = {"metric": "sleep.hours", "start": "2026-09-20", "end": "2026-10-17"}
    body = answer(finding(kind="trend", text="Sleep shows no clear change over {{E1.period}}.",
                          direction="no_clear_change"),
                  summary="", charts=[{"type": "line", "evidence_id": "E1"}])
    env.spy.scripts.append([turn(fc("get_daily_series", series)), turn(msg(body))])
    html = ask(client, "Sleep trend?").text
    assert '<figure role="img" aria-label="Line chart of Sleep per day' in html
    assert "<polyline" in html and "Show data" in html and "<table>" in html
    assert " style=" not in html and "<script" not in html


def test_model_error_message(env: Env, client: TestClient) -> None:
    add_sleep(env)
    env.spy.scripts.append([ModelAuthError("x")])
    r = ask(client, "How did I sleep?")
    assert r.status_code == 502 and agent.MSG_AUTH in r.text
    assert "AI-generated analysis" not in r.text and "do not provide sufficient" not in r.text


def test_metrics_unavailable_is_an_error_not_the_fallback(env: Env, client: TestClient) -> None:
    env.metrics_missing[0] = True
    r = ask(client, "How did I sleep?")
    assert r.status_code == 503 and agent.MSG_LIMITS in r.text and "do not provide sufficient" not in r.text


def test_question_is_escaped(env: Env, client: TestClient) -> None:
    add_sleep(env)
    env.spy.scripts.append(happy())
    html = ask(client, "<script>alert(1)</script> sleep?").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; sleep?" in html


def test_record_text_is_never_rendered(env: Env, client: TestClient) -> None:
    uid = uid_of(env)
    conn = connect(env.settings.database_path)
    try:
        model = DOMAINS["tasks"].SPEC.model.model_validate({"task_date": "2026-10-10", "title": MALICIOUS,
                                                            "category": "career"})
        task_id = int(DOMAINS["tasks"].create(conn, uid, model, today=T))
    finally:
        conn.close()
    list_args = {"domain": "tasks", "start": "2026-10-01", "end": "2026-10-18", "filters": None,
                 "sort": "date_desc", "limit": 10}
    body = answer(finding(kind="records", metric=None, record_refs=[f"E1#{task_id}"],
                          text=f"A task is recorded on {{{{E1.records.{task_id}.date}}}}."),
                  summary="", follow_up_questions=[])
    env.spy.scripts.append([turn(fc("list_records", list_args)), turn(msg(body))])
    html = ask(client, "What tasks did I have?").text
    assert "Task · career · 10.10.2026" in html and "A task is recorded on 10.10.2026." in html
    assert "onerror" not in html and "<img" not in html


@dataclass
class FakeDemo:
    """Stands in for B-14's `app.state.demo`: `connect()` opens the demo working copy."""

    path: Path
    connects: int = 0

    def connect(self) -> Any:
        self.connects += 1
        return connect(self.path)


def test_demo_mode_reads_the_demo_copy_never_database_path(env: Env, client: TestClient, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    uid = uid_of(env)
    demo_path = tmp_path / "demo-working.db"
    conn = connect(demo_path)
    try:
        migrate(conn)
        demo_uid = get_or_create_user(conn, "demo:reviewer", A, "Demo")
        assert demo_uid == uid  # same id, so the session user maps onto the demo copy's user
        model = DOMAINS["tasks"].SPEC.model.model_validate({"task_date": "2026-10-10", "title": "Demo task",
                                                            "category": "family"})
        task_id = int(DOMAINS["tasks"].create(conn, demo_uid, model, today=T))
    finally:
        conn.close()
    demo = FakeDemo(demo_path)
    env.app.state.demo = demo
    opened: list[Path] = []
    real_open = readonly.open_read_only

    def spy_open(path: Path | str) -> Any:
        opened.append(Path(path))
        return real_open(path)

    monkeypatch.setattr(readonly, "open_read_only", spy_open)
    list_args = {"domain": "tasks", "start": "2026-10-01", "end": "2026-10-18", "filters": None,
                 "sort": "date_desc", "limit": 10}
    body = answer(finding(kind="records", metric=None, record_refs=[f"E1#{task_id}"],
                          text=f"A task is recorded on {{{{E1.records.{task_id}.date}}}}."),
                  summary="", follow_up_questions=[])
    env.spy.scripts.append([turn(fc("list_records", list_args)), turn(msg(body))])
    html = ask(client, "What tasks did I have?").text
    assert demo.connects >= 1 and opened == []  # DATABASE_PATH was never opened by the analyst route
    assert "Task · family · 10.10.2026" in html  # the record exists only in the demo copy
