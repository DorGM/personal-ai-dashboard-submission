"""B-07 Aggregates views: numbers equal `metrics.summarize`, undocumented days are gaps,
range validation, period comparison, two-user isolation, security headers, and the B-14
polish items (DemoError page, signed-in header on /demo/reset, favicon). Demo copy at the
golden anchor 2026-10-17; synthetic data only."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.analytics import metrics
from app.analytics.metrics import Period
from app.config import Settings
from app.main import create_app
from app.web import aggregates as agg
from app.web.headers import CSP
from tests.conftest import TEST_SECRET

T = date(2026, 10, 17)
PEER = ("127.0.0.1", 50000)
TABS = ("health", "content", "jobs", "tasks")


def csrf_of(c: TestClient, path: str = "/") -> str:
    return c.get(path).text.split('<meta name="csrf-token" content="', 1)[1].split('"', 1)[0]


@pytest.fixture
def demo_app(tmp_path: Path) -> FastAPI:
    settings = Settings.model_validate({"app_mode": "demo", "database_path": tmp_path / "personal.db"})
    return create_app(settings, today=lambda: T, demo_dir=tmp_path / "data" / "demo")


@pytest.fixture
def demo(demo_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(demo_app, base_url="http://localhost", client=PEER) as c:
        r = c.post("/demo/login", data={"csrf_token": csrf_of(c)}, follow_redirects=False)
        assert r.status_code == 303
        yield c


@pytest.fixture
def conn(demo_app: FastAPI, demo: TestClient) -> Iterator[sqlite3.Connection]:
    db = demo_app.state.demo.connect()
    yield db
    db.close()


def tile_html(html: str, metric_id: str) -> str:
    m = re.search(rf'<li class="tile" data-metric="{re.escape(metric_id)}">(.*?)</li>', html, re.S)
    assert m, metric_id
    return m.group(1)


def compare_html(html: str, metric_id: str) -> str:
    m = re.search(rf'<tr data-metric="{re.escape(metric_id)}">(.*?)</tr>', html, re.S)
    assert m, metric_id
    return m.group(1)


def chart_html(html: str, title: str) -> str:
    m = re.search(rf"<figure class=\"chart\">\s*<figcaption>{re.escape(title)}(.*?)</figure>", html, re.S)
    assert m, title
    return m.group(1)


# ---------------------------------------------------------------- pages and values
@pytest.mark.parametrize("tab", TABS)
@pytest.mark.parametrize("query", ["", "&days=7", "&days=14", "&days=30", "&start=2026-08-01&end=2026-10-17"])
def test_aggregates_pages_render(demo: TestClient, tab: str, query: str) -> None:
    r = demo.get(f"/{tab}?view=aggregates{query}")
    assert r.status_code == 200
    assert '<nav class="view-toggle"' in r.text and 'aria-current="page">Aggregates' in r.text
    assert 'role="img"' in r.text and "Show data" in r.text


def test_history_view_has_the_toggle(demo: TestClient) -> None:
    r = demo.get("/health")
    assert r.status_code == 200 and 'aria-current="page">History' in r.text and '<ol class="days">' in r.text


@pytest.mark.parametrize("days", [7, 14, 30])
@pytest.mark.parametrize("tab", TABS)
def test_tiles_equal_metrics_summarize(demo: TestClient, conn: sqlite3.Connection, demo_app: FastAPI,
                                       tab: str, days: int) -> None:
    uid = demo_app.state.demo.user_id
    period = Period(T - timedelta(days=days - 1), T)
    html = demo.get(f"/{tab}?view=aggregates&days={days}").text
    for metric_id in agg.TAB_TILES[tab]:
        m = metrics.REGISTRY[metric_id]
        s = metrics.summarize(conn, uid, metric_id, period)
        block = tile_html(html, metric_id)
        assert f"{s.coverage.documented_days} of {days} days recorded" in block
        if m.kind == "daily_value":
            expected = agg.fmt(s.mean, m.unit)
            assert s.latest_overall is not None
            assert f"on {s.latest_overall.day.strftime('%d.%m.%Y')}" in block
        elif m.kind == "ratio":
            expected = agg.fmt(s.ratio_pct, "%")
        else:
            expected = agg.fmt(s.sum or 0.0, m.unit, decimals=0)
        assert f"<strong>{expected}</strong>" in block, (metric_id, block)


def test_sleep_mean_value_is_the_metric_value(demo: TestClient, conn: sqlite3.Connection, demo_app: FastAPI) -> None:
    s = metrics.summarize(conn, demo_app.state.demo.user_id, "sleep.hours", Period(T - timedelta(days=29), T))
    assert s.mean is not None
    assert f"<strong>{s.mean:.1f} h</strong>" in tile_html(demo.get("/health?view=aggregates&days=30").text,
                                                         "sleep.hours")


def test_comparison_uses_the_previous_equal_period(demo: TestClient, conn: sqlite3.Connection,
                                                   demo_app: FastAPI) -> None:
    uid = demo_app.state.demo.user_id
    cur, prev = Period(T - timedelta(days=13), T), Period(T - timedelta(days=27), T - timedelta(days=14))
    html = demo.get("/health?view=aggregates&days=14").text
    assert "Compared with the previous 14 days" in html
    for metric_id in agg.TAB_COMPARE["health"]:
        expected = agg.compare(metric_id, metrics.summarize(conn, uid, metric_id, cur),
                               metrics.summarize(conn, uid, metric_id, prev))
        row = compare_html(html, metric_id)
        assert expected.current in row and expected.previous in row
        assert (expected.difference or expected.verdict) in row


def test_comparison_without_previous_data_says_not_enough_data(demo: TestClient) -> None:
    # The fixture spans 30 days, so the 30 days before the last 30 have no records.
    row = compare_html(demo.get("/health?view=aggregates&days=30").text, "sleep.hours")
    assert "not enough data" in row


# ---------------------------------------------------------------- gaps
def test_undocumented_days_are_gaps_not_zero(demo: TestClient, conn: sqlite3.Connection, demo_app: FastAPI) -> None:
    uid = demo_app.state.demo.user_id
    points = metrics.daily_points(conn, uid, "sleep.hours", Period(T - timedelta(days=29), T))
    missing = [p for p in points if not p.documented]
    assert missing, "fixture should have undocumented sleep days"
    runs, run = [], 0
    for p in points:
        if p.documented:
            run += 1
        else:
            runs.append(run)
            run = 0
    runs.append(run)
    block = chart_html(demo.get("/health?view=aggregates&days=30").text, "Sleep per day")
    assert block.count("<polyline") == sum(1 for n in runs if n >= 2)
    assert block.count('<circle class="dot"') == len(points) - len(missing)
    assert block.count("<td>not recorded</td>") == len(missing)
    assert "<td>0.0 h</td>" not in block
    for p in missing:
        assert f"<th scope=\"row\">{p.day.strftime('%d.%m.%Y')}</th><td>not recorded</td>" in block


def test_line_chart_breaks_at_missing_days() -> None:
    days = [date(2026, 10, d) for d in range(1, 6)]
    values = [1.0, None, 3.0, 4.0, None]
    pts = [metrics.DailyPoint(d, v, v is not None, ()) for d, v in zip(days, values, strict=True)]
    c = agg.line_chart("x", pts, "h")
    assert len(c.segments) == 1 and len(c.segments[0].split()) == 2  # 3.0–4.0; the lone 1.0 is a dot only
    assert len(c.dots) == 3 and c.documented == 3 and c.total == 5
    assert [r.value for r in c.rows] == ["1.0 h", "not recorded", "3.0 h", "4.0 h", "not recorded"]


def test_task_bars_use_category_colours(demo: TestClient) -> None:
    html = demo.get("/tasks?view=aggregates&days=30").text
    for cat in ("family", "career", "friends", "housework", "other"):
        assert f'class="bar cat-{cat}"' in html


# ---------------------------------------------------------------- range validation
@pytest.mark.parametrize(("query", "message"), [
    ("start=2026-10-10&end=2026-10-01", "The start date must not be after the end date."),
    ("start=2026-10-10&end=2026-10-18", "The end date must not be after today."),
    ("start=2025-10-15&end=2026-10-17", "A range may span at most 366 days."),
    ("start=2026-10-10", "Choose both a start date and an end date."),
    ("start=yesterday&end=2026-10-17", "Enter dates as YYYY-MM-DD."),
    ("days=99", "Choose 7, 14 or 30 days, or enter your own dates."),
])
def test_invalid_ranges_show_a_friendly_error(demo: TestClient, query: str, message: str) -> None:
    r = demo.get(f"/health?view=aggregates&{query}")
    assert r.status_code == 400 and message in r.text and 'role="alert"' in r.text
    assert "Showing the last 7 days instead." in r.text and 'data-metric="sleep.hours"' in r.text


def test_range_of_366_days_is_accepted(demo: TestClient) -> None:
    r = demo.get("/health?view=aggregates&start=2025-10-17&end=2026-10-17")
    assert r.status_code == 200 and "366 of" not in r.text and "of 366 days recorded" in r.text


def test_select_range_presets_and_previous() -> None:
    sel = agg.select_range({"days": "14"}, T)
    assert sel.period == Period(date(2026, 10, 4), T) and sel.preset == 14
    assert sel.previous == Period(date(2026, 9, 20), date(2026, 10, 3))
    assert agg.select_range({}, T).days == 7


def test_compare_threshold_and_minimum_days() -> None:
    def summary(mean: float | None, documented: int) -> metrics.MetricSummary:
        cov = metrics.Coverage(None, None, documented, 7, documented / 7, (), False)
        p = metrics.ResolvedPeriod(Period(T, T), T, False)
        return metrics.MetricSummary("sleep.hours", p, cov, documented, mean, None, None, None, None, None, None,
                                     None, None, None, None, None, None, None, (), False, None, None, None)
    assert agg.compare("sleep.hours", summary(7.2, 5), summary(7.0, 5)).verdict == "no clear change"
    big = agg.compare("sleep.hours", summary(7.5, 5), summary(7.0, 5))
    assert big.verdict == "higher" and big.difference == "+0.5 h"
    assert agg.compare("sleep.hours", summary(7.5, 2), summary(7.0, 5)).verdict == "not enough data"
    assert agg.compare("sleep.hours", summary(7.5, 5), summary(None, 0)).verdict == "not enough data"


# ---------------------------------------------------------------- isolation (personal mode, two users)
A, B = "a@example.com", "b@example.com"


@pytest.fixture
def personal_app(tmp_path: Path) -> FastAPI:
    settings = Settings.model_validate({"session_secret": TEST_SECRET, "database_path": tmp_path / "web.db",
                                        "test_auth": True, "allowed_emails": f"{A},{B}"})
    return create_app(settings, today=lambda: T)


def sign_in(app: FastAPI, email: str) -> TestClient:
    c = TestClient(app, base_url="http://localhost", client=PEER)
    c.__enter__()
    assert c.post("/auth/test-login", data={"csrf_token": csrf_of(c), "email": email},
                  follow_redirects=False).status_code == 303
    return c


def test_second_users_data_is_never_shown(personal_app: FastAPI) -> None:
    a, b = sign_in(personal_app, A), sign_in(personal_app, B)
    try:
        for day in ("2026-10-15", "2026-10-16", "2026-10-17"):
            r = b.post("/records/sleep", data={"csrf_token": csrf_of(b, "/analyst"), "record_date": day,
                                               "hours_slept": "3.3"}, follow_redirects=False)
            assert r.status_code == 303
        mine = b.get("/health?view=aggregates").text
        assert "<strong>3.3 h</strong>" in tile_html(mine, "sleep.hours")
        theirs = a.get("/health?view=aggregates").text
        sleep = tile_html(theirs, "sleep.hours")
        assert "3.3 h" not in theirs and "0 of 7 days recorded" in sleep and "No records yet" in sleep
    finally:
        a.__exit__(None, None, None)
        b.__exit__(None, None, None)


# ---------------------------------------------------------------- headers and polish
@pytest.mark.parametrize("path", ["/", "/health?view=aggregates", "/static/app.css", "/healthz", "/demo/reset"])
def test_security_headers_on_every_response(demo: TestClient, path: str) -> None:
    r = demo.get(path)
    assert r.headers["content-security-policy"] == CSP
    assert "frame-ancestors 'none'" in CSP and "img-src 'self' data:" in CSP
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_security_headers_on_refused_demo_request(demo_app: FastAPI) -> None:
    with TestClient(demo_app, base_url="http://example.com", client=("203.0.113.5", 4000)) as c:
        r = c.get("/")
        assert r.status_code == 403 and r.headers["x-frame-options"] == "DENY"


def test_templates_have_no_inline_script_or_style() -> None:
    root = Path(__file__).resolve().parents[2] / "app" / "web" / "templates"
    for page in root.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert "<script" not in text and "<style" not in text and "style=" not in text, page.name


def test_favicon_is_an_inline_data_uri(demo: TestClient) -> None:
    assert '<link rel="icon" href="data:image/svg+xml,' in demo.get("/").text


def test_demo_error_shows_a_friendly_page(demo_app: FastAPI, demo: TestClient) -> None:
    from app.demo import DemoError

    def broken() -> sqlite3.Connection:
        raise DemoError("The demo working copy is not available; restart the app.")

    demo_app.state.demo.connect = broken
    r = demo.get("/health")
    assert r.status_code == 503 and "Demo data unavailable" in r.text and "Traceback" not in r.text


def test_demo_reset_page_shows_the_signed_in_header(demo: TestClient) -> None:
    assert "Signed in as <strong>Demo Reviewer</strong>" in demo.get("/demo/reset").text


def test_demo_reset_page_signed_out_has_no_header(demo_app: FastAPI) -> None:
    with TestClient(demo_app, base_url="http://localhost", client=PEER) as c:
        r = c.get("/demo/reset")
        assert r.status_code == 200 and "Signed in as" not in r.text
