"""`POST /analyst/ask`: the Analyst question form (ADR-005 §3.1, §3.7, §6.8; worker notes §6).

A plain CSRF-protected form post (the app ships no JavaScript) that re-renders the Analyst page with the
answer card inside an ``aria-live="polite"`` region. Gates run in order and none of them calls the model:

1. AI disabled (``AI_ENABLED`` off, or no key or model) → the no-key message (demo or personal wording);
2. more than ``AI_QUESTIONS_PER_HOUR`` questions in the last hour for this user (in-memory) → 429;
3. a question already in flight for this user → 409;
4. the question, trimmed and with control characters removed, must be 1–500 characters → 400.

The user id comes from the session only. The agent reads through a hardened read-only connection that is
closed after the request. AI settings are read with ``getattr`` defaults until the integration lead adds
them to `Settings` (see the B-15 hand-off).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.ai_client import ModelClient, OpenAIModelClient
from app.analytics import agent, policy, readonly
from app.analytics.ports import MetricsBackend, MetricsUnavailable, default_backend
from app.analytics.render import AnswerView
from app.web.deps import CurrentUser, FormData

log = logging.getLogger("app.analytics")

# ADR-005 §6.8, verbatim.
MSG_AI_DISABLED_DEMO: Final = (
    "AI-powered analysis is not available in the current demo configuration. You can still explore the sample "
    "data, metrics, and trends. To enable the analytical agent, configure your own OpenAI API key using the "
    "setup instructions."
)
MSG_AI_DISABLED_PERSONAL: Final = (
    "AI-powered analysis is not configured. Add OPENAI_API_KEY and OPENAI_MODEL to .env (see README) to enable it."
)
MSG_HOURLY_CAP: Final = "You have reached the limit of questions per hour. Please try again later."
MSG_IN_FLIGHT: Final = "An analysis is already running."
MSG_EMPTY: Final = "Please enter a question."
MSG_TOO_LONG: Final = f"Questions can be at most {policy.MAX_QUESTION_CHARS} characters."
MSG_INTERNAL: Final = agent.MSG_LIMITS

_HOUR_S: Final = 3600.0
_ERROR_STATUS: Final = {"auth": 502, "rate_limit": 502, "timeout": 504, "limits": 422}


class Render(Protocol):
    def __call__(self, request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse: ...


@dataclass(frozen=True)
class Outcome:
    """What the answer region shows: an answer view, or a message (`notice` for gates, `error` for failures)."""

    view: AnswerView | None = None
    message: str | None = None
    kind: Literal["answer", "notice", "error"] = "answer"


def clean_question(raw: str) -> str:
    """Replace control characters with spaces, collapse whitespace and trim."""
    replaced = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in raw)
    return " ".join(replaced.split())


def ai_enabled(settings: Any) -> bool:
    """AI is on only with AI_ENABLED, a key and a model (ADR-005 §6.8, §9)."""
    return bool(getattr(settings, "ai_enabled", True)) and bool(getattr(settings, "openai_api_key", "")) \
        and bool(getattr(settings, "openai_model", ""))


class QuestionGate:
    """In-memory per-user hourly cap and single in-flight question (one process, development use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._asked: dict[int, deque[float]] = {}
        self._in_flight: set[int] = set()

    def admit(self, user_id: int, now: float, per_hour: int) -> Literal["ok", "rate_limited", "busy"]:
        """Atomically check both gates and, when admitted, count the question and mark it in flight."""
        with self._lock:
            asked = self._asked.setdefault(user_id, deque())
            while asked and now - asked[0] >= _HOUR_S:
                asked.popleft()
            if len(asked) >= per_hour:
                return "rate_limited"
            if user_id in self._in_flight:
                return "busy"
            asked.append(now)
            self._in_flight.add(user_id)
            return "ok"

    def check(self, user_id: int, now: float, per_hour: int) -> Literal["ok", "rate_limited", "busy"]:
        """The gate result without admitting (used before the question is validated)."""
        with self._lock:
            asked = [t for t in self._asked.get(user_id, ()) if now - t < _HOUR_S]
            if len(asked) >= per_hour:
                return "rate_limited"
            return "busy" if user_id in self._in_flight else "ok"

    def release(self, user_id: int) -> None:
        """Mark the user's question as finished."""
        with self._lock:
            self._in_flight.discard(user_id)


def _open_read_only(request: Request, settings: Any, db_path: Path | None) -> sqlite3.Connection:
    """Hardened read-only connection: injected path, else the demo working copy, else DATABASE_PATH.

    In demo mode (``app.state.demo`` set, B-14) the personal DATABASE_PATH is never opened (ADR-005 §6.2).
    """
    if db_path is not None:
        return readonly.open_read_only(db_path)
    demo = getattr(request.app.state, "demo", None)
    if demo is None and getattr(settings, "is_demo", False):
        raise RuntimeError("demo mode without a demo runtime")  # fail closed: never fall back to DATABASE_PATH
    if demo is not None:
        conn: sqlite3.Connection = demo.connect()
        try:
            return readonly.harden_read_only(conn)
        except BaseException:
            conn.close()
            raise
    return readonly.open_read_only(settings.database_path)


def _default_client(settings: Any) -> ModelClient:
    return OpenAIModelClient(str(settings.openai_api_key), str(settings.openai_model))


def build_analyst_router(render: Render, today: Callable[[], date], *,
                         client_factory: Callable[[Any], ModelClient] | None = None,
                         db_path: Path | None = None,
                         metrics_factory: Callable[[], MetricsBackend] | None = None,
                         gate: QuestionGate | None = None,
                         clock: Callable[[], float] = time.monotonic) -> APIRouter:
    """Router with ``POST /analyst/ask``. ``GET /analyst`` stays in `app.web.records`.

    Args:
        render: the app's page renderer (`create_app`'s ``page``).
        today: Asia/Jerusalem date clock.
        client_factory: builds the model client from the settings; called only after every gate passed.
        db_path: database file (tests). When omitted, the demo working copy is used if ``app.state.demo`` is set
            (DATABASE_PATH is then never read), otherwise ``app.state.settings.database_path``.
        metrics_factory: the B-05 metrics backend; defaults to `ports.default_backend`.
        gate: hourly/in-flight gate (injectable for tests).
        clock: monotonic clock for the hourly window.
    """
    router = APIRouter()
    make_client = client_factory or _default_client
    make_metrics = metrics_factory or default_backend
    gates = gate or QuestionGate()

    def page(request: Request, user: Any, status: int, question: str, outcome: Outcome) -> HTMLResponse:
        return render(request, "analyst_page.html", status, title="Analyst", user=user, tab="analyst",
                      question=question, outcome=outcome)

    @router.post("/analyst/ask", response_class=HTMLResponse)
    def ask(request: Request, user: CurrentUser, form: FormData) -> HTMLResponse:
        settings = request.app.state.settings
        user_id = int(user["id"])
        question = clean_question(form.get("question", ""))
        shown = question[:policy.MAX_QUESTION_CHARS]
        if not ai_enabled(settings):
            demo = getattr(settings, "app_mode", "personal") == "demo"
            text = MSG_AI_DISABLED_DEMO if demo else MSG_AI_DISABLED_PERSONAL
            return page(request, user, 200, shown, Outcome(message=text, kind="notice"))
        per_hour = int(getattr(settings, "ai_questions_per_hour", policy.DEFAULT_QUESTIONS_PER_HOUR))
        state = gates.check(user_id, clock(), per_hour)
        if state == "rate_limited":
            return page(request, user, 429, shown, Outcome(message=MSG_HOURLY_CAP, kind="notice"))
        if state == "busy":
            return page(request, user, 409, shown, Outcome(message=MSG_IN_FLIGHT, kind="notice"))
        if not question:
            return page(request, user, 400, "", Outcome(message=MSG_EMPTY, kind="notice"))
        if len(question) > policy.MAX_QUESTION_CHARS:
            return page(request, user, 400, shown, Outcome(message=MSG_TOO_LONG, kind="notice"))
        state = gates.admit(user_id, clock(), per_hour)  # re-checked atomically
        if state != "ok":
            status, text = (429, MSG_HOURLY_CAP) if state == "rate_limited" else (409, MSG_IN_FLIGHT)
            return page(request, user, status, shown, Outcome(message=text, kind="notice"))
        try:
            return page(request, user, *_answer(request, settings, user_id, question))
        finally:
            gates.release(user_id)

    def _answer(request: Request, settings: Any, user_id: int, question: str) -> tuple[int, str, Outcome]:
        try:
            metrics = make_metrics()
        except MetricsUnavailable:
            log.warning("analytics unavailable: metrics registry missing")
            return 503, question, Outcome(message=MSG_INTERNAL, kind="error")
        client = make_client(settings)
        conn = _open_read_only(request, settings, db_path)
        try:
            limits = agent.AgentLimits(max_tokens=int(getattr(settings, "ai_max_tokens_per_question",
                                                              policy.DEFAULT_MAX_TOKENS_PER_QUESTION)))
            result = agent.run_question(question, user_id=user_id, client=client, conn=conn, metrics=metrics,
                                        today=today(), limits=limits, model_name=str(settings.openai_model),
                                        mode=str(getattr(settings, "app_mode", "personal")))
        except Exception as exc:  # boundary: never show internals; log the type only
            log.warning("analytics failed: %s", type(exc).__name__)
            return 500, question, Outcome(message=MSG_INTERNAL, kind="error")
        finally:
            conn.close()
        if result.status == "error":
            return _ERROR_STATUS.get(result.error_kind or "limits", 422), question, \
                Outcome(message=result.message, kind="error")
        return 200, question, Outcome(view=result.view)

    return router


__all__ = ["MSG_AI_DISABLED_DEMO", "MSG_AI_DISABLED_PERSONAL", "MSG_EMPTY", "MSG_HOURLY_CAP", "MSG_IN_FLIGHT",
           "MSG_TOO_LONG", "Outcome", "QuestionGate", "ai_enabled", "build_analyst_router", "clean_question"]
