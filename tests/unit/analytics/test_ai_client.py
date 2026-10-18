"""Unit tests for app.ai_client. Fakes only: no API key, no network, no live OpenAI call."""

from __future__ import annotations

from typing import Any

import httpx2
import openai
import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from app.ai_client import (
    ModelAuthError,
    ModelError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelTurn,
    OpenAIModelClient,
    TokenUsage,
    function_calls,
    output_text,
)

FAKE_KEY = "sk-test-NOT-A-REAL-KEY-1234567890"
SECRET_BODY = "SECRET-RESPONSE-BODY weight 81.2 kg"
SECRET_QUESTION = "SECRET-QUESTION how did I sleep?"


class _FakeResponses:
    def __init__(self, result: Any = None, exc: BaseException | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeSDK:
    def __init__(self, result: Any = None, exc: BaseException | None = None) -> None:
        self.responses = _FakeResponses(result, exc)


class _FakeResponse:
    def __init__(self, output: list[Any], usage: Any = None) -> None:
        self.output = output
        self.usage = usage


def _usage(inp: int, out: int) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=inp,
        input_tokens_details=InputTokensDetails(cached_tokens=0, cache_write_tokens=0),
        output_tokens=out,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=inp + out,
    )


def _respond(client: OpenAIModelClient, **overrides: Any) -> ModelTurn:
    kwargs: dict[str, Any] = {
        "input_items": [{"role": "user", "content": SECRET_QUESTION}],
        "tools": [{"type": "function", "name": "get_data_coverage", "parameters": {}, "strict": True}],
        "text_format": {"type": "json_schema", "name": "answer", "schema": {}, "strict": True},
        "tool_choice": "auto",
        "max_output_tokens": 4000,
        "timeout": 30.0,
    }
    kwargs.update(overrides)
    return client.respond(**kwargs)


_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(cls: type[openai.APIStatusError], status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=_REQUEST, json={"error": {"message": SECRET_BODY}})
    return cls(f"Error code: {status} - {SECRET_BODY} {FAKE_KEY}", response=response, body={"message": SECRET_BODY})


# --- request shape -----------------------------------------------------------------------------------------------


def test_respond_sends_expected_kwargs() -> None:
    sdk = _FakeSDK(result=_FakeResponse(output=[], usage=_usage(10, 5)))
    client = OpenAIModelClient(FAKE_KEY, "gpt-test", sdk_client=sdk)
    _respond(client, tool_choice="none", timeout=12.5)

    (call,) = sdk.responses.calls
    assert call["model"] == "gpt-test"
    assert call["store"] is False
    assert call["include"] == ["reasoning.encrypted_content"]
    assert call["parallel_tool_calls"] is True
    assert call["tool_choice"] == "none"
    assert call["max_output_tokens"] == 4000
    assert call["timeout"] == 12.5
    assert call["text"] == {"format": {"type": "json_schema", "name": "answer", "schema": {}, "strict": True}}
    assert call["input"] == [{"role": "user", "content": SECRET_QUESTION}]
    assert call["tools"][0]["name"] == "get_data_coverage"


def test_lazy_factory_receives_key_and_max_retries() -> None:
    built: list[dict[str, Any]] = []
    sdk = _FakeSDK(result=_FakeResponse(output=[]))

    def factory(**kwargs: Any) -> _FakeSDK:
        built.append(kwargs)
        return sdk

    client = OpenAIModelClient(FAKE_KEY, "gpt-test")
    client._factory = factory
    assert built == []  # nothing built at construction time
    _respond(client)
    _respond(client)
    assert built == [{"api_key": FAKE_KEY, "max_retries": 1}]  # built once, reused
    assert len(sdk.responses.calls) == 2


def test_lazily_built_real_sdk_client_config_without_network() -> None:
    client = OpenAIModelClient(FAKE_KEY, "gpt-test")
    sdk = client._client()  # constructing the SDK client makes no request
    assert isinstance(sdk, openai.OpenAI)
    assert sdk.max_retries == 1
    assert client._client() is sdk


@pytest.mark.parametrize(("key", "model"), [("", "gpt-test"), (FAKE_KEY, "")])
def test_constructor_rejects_empty_values(key: str, model: str) -> None:
    with pytest.raises(ValueError):
        OpenAIModelClient(key, model)


# --- response conversion -----------------------------------------------------------------------------------------


def test_sdk_items_become_plain_dicts_and_usage_is_read() -> None:
    message = ResponseOutputMessage(
        id="msg_1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text='{"a":1}', annotations=[])],
    )
    call = ResponseFunctionToolCall(type="function_call", call_id="call_1", name="list_records", arguments="{}")
    reasoning = ResponseReasoningItem(id="rs_1", type="reasoning", summary=[], encrypted_content="ENC")
    sdk = _FakeSDK(result=_FakeResponse(output=[reasoning, call, message], usage=_usage(120, 30)))

    turn = _respond(OpenAIModelClient(FAKE_KEY, "gpt-test", sdk_client=sdk))

    assert all(type(item) is dict for item in turn.output_items)
    assert turn.output_items[0] == {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "ENC"}
    expected_call = {"type": "function_call", "call_id": "call_1", "name": "list_records", "arguments": "{}"}
    assert turn.output_items[1] == expected_call
    assert turn.output_items[2]["content"][0] == {"type": "output_text", "text": '{"a":1}', "annotations": []}
    assert turn.usage == TokenUsage(120, 30)
    assert turn.usage.total == 150


def test_dict_items_pass_through_and_missing_usage_is_zero() -> None:
    item = {"type": "function_call", "call_id": "c", "name": "n", "arguments": "{}"}
    sdk = _FakeSDK(result=_FakeResponse(output=[item], usage=None))
    turn = _respond(OpenAIModelClient(FAKE_KEY, "gpt-test", sdk_client=sdk))
    assert turn.output_items == [item]
    assert turn.output_items[0] is not item  # copied, not aliased
    assert turn.usage == TokenUsage(0, 0)


def test_dataclasses_are_frozen() -> None:
    usage = TokenUsage(1, 2)
    with pytest.raises(AttributeError):
        usage.input_tokens = 5  # type: ignore[misc]
    turn = ModelTurn(output_items=[], usage=usage)
    with pytest.raises(AttributeError):
        turn.usage = TokenUsage(0, 0)  # type: ignore[misc]


# --- error mapping -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(openai.AuthenticationError, 401), ModelAuthError),
        (_status_error(openai.PermissionDeniedError, 403), ModelAuthError),
        (_status_error(openai.RateLimitError, 429), ModelRateLimitError),
        (openai.APITimeoutError(request=_REQUEST), ModelTimeoutError),
        (openai.APIConnectionError(message=f"conn {SECRET_BODY}", request=_REQUEST), ModelTimeoutError),
        (_status_error(openai.BadRequestError, 400), ModelError),
        (_status_error(openai.InternalServerError, 500), ModelError),
        (_status_error(openai.APIStatusError, 418), ModelError),
        (openai.OpenAIError(f"generic {SECRET_BODY} {FAKE_KEY}"), ModelError),
    ],
)
def test_sdk_errors_map_to_typed_errors_without_content(exc: BaseException, expected: type[ModelError]) -> None:
    client = OpenAIModelClient(FAKE_KEY, "gpt-test", sdk_client=_FakeSDK(exc=exc))
    with pytest.raises(ModelError) as info:
        _respond(client)
    err = info.value
    assert type(err) is expected
    for text in (str(err), repr(err)):
        assert FAKE_KEY not in text
        assert "SECRET" not in text
    assert err.__cause__ is None
    assert err.__suppress_context__ is True
    assert type(exc).__name__ in str(err)
    status = getattr(exc, "status_code", None)
    if status is not None:
        assert f"status {status}" in str(err)


def test_non_sdk_exceptions_are_not_swallowed() -> None:
    client = OpenAIModelClient(FAKE_KEY, "gpt-test", sdk_client=_FakeSDK(exc=KeyError("boom")))
    with pytest.raises(KeyError):
        _respond(client)


def test_key_never_in_client_repr_or_str() -> None:
    client = OpenAIModelClient(FAKE_KEY, "gpt-test")
    assert FAKE_KEY not in repr(client)
    assert FAKE_KEY not in str(client)
    assert "gpt-test" in repr(client)
    assert client.model == "gpt-test"


# --- helpers -----------------------------------------------------------------------------------------------------


def _msg(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": list(parts)}


def test_output_text_concatenates_parts_of_last_message() -> None:
    items = [
        _msg({"type": "output_text", "text": "old"}),
        {"type": "function_call", "call_id": "c", "name": "n", "arguments": "{}"},
        _msg({"type": "output_text", "text": "ab"}, {"type": "output_text", "text": "cd"}),
    ]
    assert output_text(items) == "abcd"


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"type": "function_call", "call_id": "c", "name": "n", "arguments": "{}"}],
        [_msg()],
        [_msg({"type": "output_text", "text": "x"}, {"type": "refusal", "refusal": "no"})],
        [_msg({"type": "output_text", "text": "x"}), _msg({"type": "refusal", "refusal": "no"})],
        [{"type": "message", "content": "not-a-list"}],
    ],
)
def test_output_text_returns_none_when_no_usable_text(items: list[dict[str, Any]]) -> None:
    assert output_text(items) is None


def test_function_calls_filters_in_order() -> None:
    a = {"type": "function_call", "call_id": "1", "name": "summarize_metric", "arguments": "{}"}
    b = {"type": "function_call", "call_id": "2", "name": "list_records", "arguments": "{}"}
    items = [{"type": "reasoning", "id": "rs"}, a, _msg({"type": "output_text", "text": "t"}), b]
    assert function_calls(items) == [a, b]
    assert function_calls([]) == []
