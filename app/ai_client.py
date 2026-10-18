"""Model client boundary for the runtime analytics agent (ADR-005 §3.2, §3.7, §3.8).

The agent loop depends only on the :class:`ModelClient` Protocol, so tests inject a scripted fake and no
OpenAI call is made. :class:`OpenAIModelClient` is the single adapter over the ``openai`` SDK Responses API.

Security notes:
- Errors raised from here carry only the SDK exception class name and HTTP status code. The API key, request
  content and response body are never placed in messages, and the SDK exception is not chained
  (``raise ... from None``) so tracebacks cannot leak the body either.
- Model output is returned as untrusted plain data; callers validate it before any use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import openai

__all__ = [
    "ModelAuthError",
    "ModelClient",
    "ModelError",
    "ModelRateLimitError",
    "ModelTimeoutError",
    "ModelTurn",
    "OpenAIModelClient",
    "TokenUsage",
    "function_calls",
    "output_text",
]

#: SDK-level retries per call; the agent's own deadline bounds total time (ADR-005 §3.2).
SDK_MAX_RETRIES = 1
#: Encrypted reasoning items are needed to resend reasoning context with store=False.
_INCLUDE = ["reasoning.encrypted_content"]


@dataclass(frozen=True)
class TokenUsage:
    """Token counts reported by one model call."""

    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        """Input plus output tokens (the quantity budgeted per question)."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelTurn:
    """One model response: output items as plain dicts (resendable as input) and token usage."""

    output_items: list[dict[str, Any]]
    usage: TokenUsage


class ModelError(Exception):
    """A model call failed. The message never contains the key, request content or response body."""


class ModelAuthError(ModelError):
    """The provider rejected the credentials (HTTP 401/403)."""


class ModelRateLimitError(ModelError):
    """The provider rate limit or quota was reached (HTTP 429)."""


class ModelTimeoutError(ModelError):
    """The call timed out or the network connection failed."""


class ModelClient(Protocol):
    """Minimal interface the analytics agent needs from a model provider."""

    def respond(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        text_format: dict[str, Any],
        tool_choice: str,
        max_output_tokens: int,
        timeout: float,
    ) -> ModelTurn:
        """Run one model turn.

        Args:
            input_items: Full conversation items (store=False, so the whole context is resent each turn).
            tools: Function tool definitions.
            text_format: The ``text.format`` object (strict JSON schema for the final answer).
            tool_choice: ``"auto"``, ``"required"`` or ``"none"``.
            max_output_tokens: Output token cap for this call.
            timeout: Per-call timeout in seconds.

        Returns:
            The model's output items and token usage.

        Raises:
            ModelError: Or one of its subclasses on any provider failure.
        """
        ...


def _describe(exc: BaseException) -> str:
    """Return a safe description: class name and, if present, the HTTP status code only."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return f"{name} (status {status})" if isinstance(status, int) else name


def _map_sdk_error(exc: openai.OpenAIError) -> ModelError:
    """Translate an SDK exception into the typed, content-free :class:`ModelError` hierarchy."""
    description = _describe(exc)
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ModelAuthError(f"OpenAI rejected the credentials: {description}")
    if isinstance(exc, openai.RateLimitError):
        return ModelRateLimitError(f"OpenAI rate limit or quota reached: {description}")
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return ModelTimeoutError(f"OpenAI call timed out or could not connect: {description}")
    return ModelError(f"OpenAI call failed: {description}")


def _to_dict(item: Any) -> dict[str, Any]:
    """Convert one SDK output item (pydantic model) or dict into a plain dict."""
    if isinstance(item, dict):
        return dict(item)
    dumped = item.model_dump(exclude_none=True)
    if not isinstance(dumped, dict):
        raise ModelError("OpenAI returned an output item that is not an object")
    return dumped


def _usage_from(response: Any) -> TokenUsage:
    """Read token usage from a response; missing usage counts as zero."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage(0, 0)
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


class OpenAIModelClient:
    """:class:`ModelClient` backed by the OpenAI Responses API.

    Args:
        api_key: OpenAI API key; held privately and never rendered by ``repr`` or errors.
        model: Model name (``OPENAI_MODEL``).
        sdk_client: Optional pre-built object exposing ``responses.create`` (tests inject a fake). When omitted,
            ``openai.OpenAI(api_key=..., max_retries=1)`` is built lazily on first use.
    """

    def __init__(self, api_key: str, model: str, *, sdk_client: Any | None = None) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        if not model:
            raise ValueError("model must be a non-empty string")
        self._api_key = api_key
        self._model = model
        self._sdk_client = sdk_client
        self._factory: Callable[..., Any] = openai.OpenAI

    def __repr__(self) -> str:
        return f"OpenAIModelClient(model={self._model!r})"

    @property
    def model(self) -> str:
        """The configured model name."""
        return self._model

    def _client(self) -> Any:
        if self._sdk_client is None:
            self._sdk_client = self._factory(api_key=self._api_key, max_retries=SDK_MAX_RETRIES)
        return self._sdk_client

    def respond(
        self,
        *,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        text_format: dict[str, Any],
        tool_choice: str,
        max_output_tokens: int,
        timeout: float,
    ) -> ModelTurn:
        """Run one Responses API call with ``store=False``. See :meth:`ModelClient.respond`."""
        try:
            response = self._client().responses.create(
                model=self._model,
                input=input_items,
                tools=tools,
                text={"format": text_format},
                tool_choice=tool_choice,
                max_output_tokens=max_output_tokens,
                store=False,
                include=list(_INCLUDE),
                parallel_tool_calls=True,
                timeout=timeout,
            )
        except openai.OpenAIError as exc:
            raise _map_sdk_error(exc) from None
        items = [_to_dict(item) for item in (getattr(response, "output", None) or [])]
        return ModelTurn(output_items=items, usage=_usage_from(response))


def output_text(items: list[dict[str, Any]]) -> str | None:
    """Return the concatenated ``output_text`` parts of the last ``message`` item.

    Returns ``None`` when there is no message item, when it has no text, or when it contains a ``refusal``
    part (a refusal is never treated as an answer).
    """
    messages = [item for item in items if item.get("type") == "message"]
    if not messages:
        return None
    content = messages[-1].get("content") or []
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "refusal":
            return None
        if part.get("type") == "output_text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts) if parts else None


def function_calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the ``function_call`` items (each carries ``name``, ``call_id`` and ``arguments``) in order."""
    return [item for item in items if item.get("type") == "function_call"]
