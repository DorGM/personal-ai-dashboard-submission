"""Bounded orchestration of one analytics question (ADR-005 §3.1, §3.5, §3.7, §3.8; worker notes §6).

`run_question` sends the fixed system prompt, a context block and the question to a `ModelClient`, executes
the model's function calls with `tools.execute_tool` under a server-built `ToolContext`, records successful
results in the per-question `Ledger`, validates the final structured answer, allows one repair turn and
renders the result. The model only ever sees tool envelopes and error dicts; it cannot write, run SQL or
choose the user.

Limits: ≤ `MAX_MODEL_TURNS` calls (including the repair turn); ≤ `MAX_TOOL_CALLS` tool calls in total and
≤ `MAX_TOOL_CALLS_PER_TURN` per turn (calls over a cap get a `limit_exceeded` error and still count); once the
total cap is reached the next call uses ``tool_choice="none"``; the cumulative token budget is checked before
each call; per-call timeout = min(30 s, remaining of the 75 s deadline), measured with an injectable clock.

Unverified live (no paid calls authorised, O-9): the exact set of fields the Responses API accepts when output
items are resent as input with ``store=False``. `sanitize_item` keeps only the documented input fields and
drops reasoning items without ``encrypted_content`` (they cannot be resolved without server-side storage).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final, Literal

from app.ai_client import (
    ModelAuthError,
    ModelClient,
    ModelError,
    ModelRateLimitError,
    ModelTimeoutError,
    function_calls,
    output_text,
)
from app.analytics import policy, readonly
from app.analytics.ledger import Ledger, LedgerFullError
from app.analytics.ports import DOMAINS, MetricsBackend
from app.analytics.prompts import ANALYST_PROMPT_VERSION, SYSTEM_PROMPT, build_context, tool_definitions
from app.analytics.render import AnswerView, render_answer
from app.analytics.schemas import final_answer_text_format
from app.analytics.tools import ToolContext, execute_tool
from app.analytics.validator import Decision, ValidationReport, decide, validate

logger = logging.getLogger("app.analytics")

# ADR-005 §3.7, verbatim.
MSG_AUTH: Final = "The OpenAI API rejected the configured key. Check OPENAI_API_KEY (see README)."
MSG_RATE_LIMIT: Final = "OpenAI's rate limit or quota was reached. Try again later or check your OpenAI billing."
MSG_TIMEOUT: Final = "The analysis didn't finish in time. Your data is unchanged; please try again."
MSG_LIMITS: Final = "The analysis could not be completed within its limits. Try a narrower question."

ResultStatus = Literal["answered", "partial", "insufficient", "error"]
ErrorKind = Literal["auth", "rate_limit", "timeout", "limits"]

_ERROR_MESSAGES: Final[Mapping[str, str]] = {
    "auth": MSG_AUTH, "rate_limit": MSG_RATE_LIMIT, "timeout": MSG_TIMEOUT, "limits": MSG_LIMITS,
}
_LIMIT_ERROR: Final = {"error": {"code": "limit_exceeded",
                                 "message": "Tool call limit reached. Answer with the evidence you have."}}
_CONTEXT_LOOKBACK_DAYS: Final = 365


@dataclass(frozen=True)
class AgentLimits:
    """Per-question limits (ADR-005 §3.5); `max_tokens` comes from AI_MAX_TOKENS_PER_QUESTION."""

    max_turns: int = policy.MAX_MODEL_TURNS
    max_tool_calls: int = policy.MAX_TOOL_CALLS
    max_tool_calls_per_turn: int = policy.MAX_TOOL_CALLS_PER_TURN
    max_tokens: int = policy.DEFAULT_MAX_TOKENS_PER_QUESTION
    max_output_tokens: int = policy.MAX_OUTPUT_TOKENS
    call_timeout_s: float = policy.MODEL_CALL_TIMEOUT_S
    deadline_s: float = policy.QUESTION_DEADLINE_S


@dataclass(frozen=True)
class AnalysisResult:
    """Outcome of one question.

    `view` is set for answered / partial / insufficient (the fallback view); `message` and `error_kind` are set
    for errors (ADR-005 §3.7 messages), which are never shown as the insufficient-evidence fallback.
    """

    status: ResultStatus
    view: AnswerView | None
    message: str | None
    error_kind: ErrorKind | None
    tokens_used: int
    tool_calls: int
    turns: int
    validator_codes: tuple[str, ...]


# --- Item handling ---

def sanitize_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reduce one model output item to the fields that are valid as Responses input (None = drop it)."""
    kind = item.get("type")
    if kind == "function_call":
        return {"type": "function_call", "call_id": str(item.get("call_id", "")), "name": str(item.get("name", "")),
                "arguments": str(item.get("arguments", ""))}
    if kind == "message":
        parts: list[dict[str, Any]] = []
        for part in item.get("content") or []:
            if isinstance(part, Mapping) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append({"type": "output_text", "text": part["text"]})
            elif isinstance(part, Mapping) and part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                parts.append({"type": "refusal", "refusal": part["refusal"]})
        return {"type": "message", "role": "assistant", "content": parts} if parts else None
    if kind == "reasoning" and isinstance(item.get("encrypted_content"), str):
        summary = [{"type": "summary_text", "text": s["text"]} for s in item.get("summary") or []
                   if isinstance(s, Mapping) and isinstance(s.get("text"), str)]
        return {"type": "reasoning", "id": str(item.get("id", "")), "encrypted_content": item["encrypted_content"],
                "summary": summary}
    return None


def _call_output(call_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id,
            "output": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}


def domain_ranges(metrics: MetricsBackend, conn: sqlite3.Connection, user_id: int,
                  today: date) -> dict[str, tuple[date | None, date | None]]:
    """Earliest and latest record date per domain (whole history) for the context block.

    A failing lookup leaves the domain as unknown rather than failing the question.
    """
    ranges: dict[str, tuple[date | None, date | None]] = {}
    period = metrics.make_period(today - timedelta(days=_CONTEXT_LOOKBACK_DAYS), today)
    with readonly.time_budget(conn):
        for domain in DOMAINS:
            try:
                with readonly.read_snapshot(conn):
                    cov = metrics.coverage(conn, user_id, domain, period)
                ranges[domain] = (cov.earliest_record, cov.latest_record)
            except Exception as exc:  # context only: log the type, never values
                logger.warning("analytics context lookup failed: %s", type(exc).__name__)
                ranges[domain] = (None, None)
    return ranges


def _safe_args(args: Mapping[str, Any] | None) -> str:
    """Normalised tool arguments for logging. The strict models admit only enums, dates, ints and nulls."""
    return "-" if args is None else json.dumps(args, separators=(",", ":"), sort_keys=True)


# --- Orchestration ---

class _Run:
    """State of one question (kept private; `run_question` is the public entry point)."""

    def __init__(self, *, question: str, user_id: int, client: ModelClient, ctx: ToolContext,
                 limits: AgentLimits, clock: Callable[[], float], request_id: str, model_name: str,
                 mode: str) -> None:
        self.question = question
        self.user_id = user_id
        self.client = client
        self.ctx = ctx
        self.limits = limits
        self.clock = clock
        self.request_id = request_id
        self.model_name = model_name
        self.mode = mode
        self.ledger = Ledger()
        self.registry = ctx.metrics.REGISTRY
        self.tokens = 0
        self.tool_calls = 0
        self.turns = 0
        self.codes: list[str] = []
        self.started = clock()
        self.tools = tool_definitions()
        self.tool_names = frozenset(str(t["name"]) for t in self.tools)

    # -- results
    def _finish(self, status: ResultStatus, *, view: AnswerView | None = None,
                error_kind: ErrorKind | None = None) -> AnalysisResult:
        result = AnalysisResult(
            status=status, view=view, message=_ERROR_MESSAGES[error_kind] if error_kind else None,
            error_kind=error_kind, tokens_used=self.tokens, tool_calls=self.tool_calls, turns=self.turns,
            validator_codes=tuple(dict.fromkeys(self.codes)))
        logger.info(
            "analytics done request=%s user=%d mode=%s model=%s prompt=%s status=%s error=%s turns=%d "
            "tool_calls=%d evidence=%d tokens=%d codes=%s",
            self.request_id, self.user_id, self.mode, self.model_name, ANALYST_PROMPT_VERSION, status,
            error_kind or "-", self.turns, self.tool_calls, len(self.ledger), self.tokens,
            ",".join(result.validator_codes) or "-")
        return result

    def _error(self, kind: ErrorKind, reason: str) -> AnalysisResult:
        logger.info("analytics stopped request=%s reason=%s", self.request_id, reason)
        return self._finish("error", error_kind=kind)

    def _render(self, report: ValidationReport, decision: Decision) -> AnalysisResult:
        if decision.action == "fallback" and decision.fallback_reason == "unparsable":
            return self._error("limits", "unparsable")
        view = render_answer(report, decision, self.ledger, self.registry, self.question, tokens_used=self.tokens)
        status: ResultStatus = "answered" if view.status == "answered" else (
            "partial" if view.status == "partial" else "insufficient")
        return self._finish(status, view=view)

    # -- budget
    def _remaining(self) -> float:
        return self.limits.deadline_s - (self.clock() - self.started)

    def _stop_reason(self) -> str | None:
        """Why no further model call may be made (checked before every call)."""
        if self.turns >= self.limits.max_turns:
            return "turn_cap"
        if self.tokens >= self.limits.max_tokens:
            return "token_budget"
        if self._remaining() <= 0:
            return "deadline"
        return None

    # -- tools
    def _run_tools(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        in_turn = 0
        for call in calls:
            call_id, name = str(call.get("call_id", "")), str(call.get("name", ""))
            over_cap = self.tool_calls >= self.limits.max_tool_calls or in_turn >= self.limits.max_tool_calls_per_turn
            self.tool_calls += 1
            in_turn += 1
            if over_cap:
                logger.info("analytics tool request=%s turn=%d name=%s outcome=limit_exceeded",
                            self.request_id, self.turns, name if name in self.tool_names else "unknown")
                outputs.append(_call_output(call_id, _LIMIT_ERROR))
                continue
            outcome = execute_tool(self.ctx, name, str(call.get("arguments", "")))
            payload: Mapping[str, Any] = outcome.result
            if outcome.ok and outcome.args is not None:
                try:
                    payload = Ledger.envelope(self.ledger.add(outcome.name, outcome.args, outcome.result))
                except (LedgerFullError, ValueError) as exc:
                    logger.warning("analytics ledger refused a result: %s", type(exc).__name__)
                    payload = {"error": {"code": "internal", "message": "The tool failed. Try a different request."}}
            code = payload.get("error", {}).get("code") if isinstance(payload.get("error"), Mapping) else None
            logger.info("analytics tool request=%s turn=%d name=%s args=%s outcome=%s",
                        self.request_id, self.turns, name if name in self.tool_names else "unknown",
                        _safe_args(outcome.args), code or "ok")
            outputs.append(_call_output(call_id, payload))
        return outputs

    # -- loop
    def run(self) -> AnalysisResult:
        text_format = final_answer_text_format()
        context = build_context(self.ctx.today, self.registry,
                                domain_ranges(self.ctx.metrics, self.ctx.conn, self.user_id, self.ctx.today))
        items: list[dict[str, Any]] = [
            {"role": "developer", "content": SYSTEM_PROMPT},
            {"role": "developer", "content": context},
            {"role": "user", "content": self.question},
        ]
        repair_used = False
        pending: tuple[ValidationReport, Decision] | None = None  # a repair that could not be sent
        while True:
            reason = self._stop_reason()
            if reason is not None:
                if pending is not None:
                    # Out of budget for the repair turn: apply the post-repair policy to the last answer.
                    report = pending[0]
                    return self._render(report, decide(report, repair_used=True))
                return self._error("timeout" if reason == "deadline" else "limits", reason)
            tool_choice = "none" if self.tool_calls >= self.limits.max_tool_calls else "auto"
            timeout = min(self.limits.call_timeout_s, self._remaining())
            began = self.clock()
            try:
                turn = self.client.respond(
                    input_items=list(items), tools=self.tools, text_format=text_format, tool_choice=tool_choice,
                    max_output_tokens=self.limits.max_output_tokens, timeout=timeout)
            except ModelAuthError:
                return self._error("auth", "model_auth")
            except ModelRateLimitError:
                return self._error("rate_limit", "model_rate_limit")
            except ModelTimeoutError:
                return self._error("timeout", "model_timeout")
            except ModelError:
                return self._error("limits", "model_error")
            self.turns += 1
            self.tokens += turn.usage.total
            logger.info("analytics turn request=%s turn=%d latency_ms=%d input_tokens=%d output_tokens=%d "
                        "tool_choice=%s", self.request_id, self.turns, int((self.clock() - began) * 1000),
                        turn.usage.input_tokens, turn.usage.output_tokens, tool_choice)
            items.extend(s for s in (sanitize_item(i) for i in turn.output_items if isinstance(i, Mapping)) if s)

            calls = function_calls(turn.output_items)
            if calls:
                items.extend(self._run_tools(calls))
                continue

            report = validate(output_text(turn.output_items) or "", self.ledger, self.registry)
            self.codes.extend(report.codes)
            decision = decide(report, repair_used=repair_used)
            logger.info("analytics validation request=%s turn=%d action=%s codes=%s", self.request_id, self.turns,
                        decision.action, ",".join(report.codes) or "-")
            if decision.action != "repair":
                return self._render(report, decision)
            repair_used = True
            pending = (report, decision)
            items.append({"role": "user", "content": decision.repair_message or ""})


def run_question(question: str, *, user_id: int, client: ModelClient, conn: sqlite3.Connection,
                 metrics: MetricsBackend, today: date, limits: AgentLimits | None = None,
                 clock: Callable[[], float] = time.monotonic, request_id: str | None = None,
                 model_name: str = "", mode: str = "personal") -> AnalysisResult:
    """Answer one question about `user_id`'s records.

    Args:
        question: the validated question (1–500 characters, control characters removed). Never logged.
        user_id: the signed-in user's id from the server-side session.
        client: the model client (a scripted fake in tests).
        conn: a hardened read-only connection (`readonly.open_read_only`); the caller closes it.
        metrics: the B-05 metrics backend.
        today: today's date in Asia/Jerusalem.
        limits: per-question limits; defaults to ADR-005 §3.5.
        clock: monotonic clock in seconds (injectable for deadline tests).
        request_id: correlation id for logs; generated when omitted.
        model_name, mode: logged only.

    Returns:
        An `AnalysisResult`. Model failures and exhausted limits are returned as `status="error"` with the
        ADR-005 §3.7 message; they are never raised.

    Raises:
        ValueError: `conn` is not a hardened read-only connection (a server bug).
    """
    if not readonly.is_read_only(conn):
        raise ValueError("run_question requires a connection hardened by readonly.open_read_only")
    run = _Run(question=question, user_id=user_id, client=client,
               ctx=ToolContext(user_id=user_id, conn=conn, today=today, metrics=metrics),
               limits=limits or AgentLimits(), clock=clock, request_id=request_id or uuid.uuid4().hex[:12],
               model_name=model_name, mode=mode)
    logger.info("analytics start request=%s user=%d mode=%s model=%s prompt=%s", run.request_id, user_id, mode,
                model_name, ANALYST_PROMPT_VERSION)
    return run.run()


__all__ = ["MSG_AUTH", "MSG_LIMITS", "MSG_RATE_LIMIT", "MSG_TIMEOUT", "AgentLimits", "AnalysisResult",
           "domain_ranges", "run_question", "sanitize_item"]
