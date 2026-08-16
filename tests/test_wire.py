from __future__ import annotations

import pytest

from codex_openai_bridge.wire import (
    ChatCompletionRequest,
    ChatMessage,
    ChatRequestError,
    FunctionTool,
    NamedFunctionToolChoice,
    ToolCall,
    parse_chat_completion_request,
)


def _parse(value: object) -> ChatCompletionRequest:
    return parse_chat_completion_request(
        value,
        public_model="codex",
        max_messages=8,
        max_tools=3,
    )


def test_parses_exact_public_model_and_minimal_text_message() -> None:
    assert _parse(
        {"model": "codex", "messages": [{"role": "user", "content": "hello"}]}
    ) == ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="hello"),),
        max_output_tokens=None,
    )


@pytest.mark.parametrize("model", ["other", "Codex", "codex ", True, None])
def test_rejects_every_model_other_than_the_exact_public_alias(model: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": model, "messages": [{"role": "user", "content": "text"}]})


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"model": "codex"},
        {"messages": [{"role": "user", "content": "text"}]},
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "temperature": 0,
        },
    ],
)
def test_rejects_missing_or_extra_core_fields_without_echoing_values(value: object) -> None:
    marker = "SENSITIVE_REQUEST_VALUE"
    with pytest.raises(ChatRequestError) as caught:
        _parse(value)

    assert caught.value.args == ("invalid request",)
    assert marker not in repr(caught.value)


@pytest.mark.parametrize("role", ["tool", "function", "User", "", True, None])
def test_rejects_unknown_or_non_string_roles(role: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": "codex", "messages": [{"role": role, "content": "text"}]})


@pytest.mark.parametrize("content", [None, True, 7, ["text"], {"type": "text"}])
def test_rejects_non_string_and_multimodal_content(content: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": "codex", "messages": [{"role": "user", "content": content}]})


@pytest.mark.parametrize(
    "messages",
    [
        [],
        "not-a-list",
        [None],
        [{"role": "user"}],
        [{"role": "user", "content": "text", "name": "extra"}],
        [
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
            {"role": "user", "content": "3"},
        ],
    ],
)
def test_rejects_malformed_messages_and_configured_count_overflow(messages: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        parse_chat_completion_request(
            {"model": "codex", "messages": messages},
            public_model="codex",
            max_messages=2,
            max_tools=3,
        )


@pytest.mark.parametrize(
    "field",
    ["max_tokens", "max_completion_tokens"],
)
def test_maps_one_supported_token_limit(field: str) -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            field: 17,
        }
    )

    assert request.max_output_tokens == 17


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, 0, -1])
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_rejects_non_positive_or_non_exact_integer_token_limits(field: str, value: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                field: value,
            }
        )


def test_rejects_both_token_limit_aliases_together() -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                "max_tokens": 1,
                "max_completion_tokens": 2,
            }
        )


def test_stream_may_be_absent_or_exact_false() -> None:
    without_stream = _parse({"model": "codex", "messages": [{"role": "user", "content": "text"}]})
    with_false = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "stream": False,
        }
    )

    assert with_false == without_stream


@pytest.mark.parametrize("stream", [True, 0, 1, None, "false"])
def test_rejects_streaming_and_non_boolean_stream_values(stream: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                "stream": stream,
            }
        )


def test_parses_ordered_function_definitions_and_owns_schema_data() -> None:
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "weather"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "description": "Look up weather",
                        "parameters": schema,
                        "strict": True,
                    },
                },
                {
                    "type": "function",
                    "function": {"name": "lookup_time", "parameters": {}},
                },
            ],
        }
    )

    assert request.tools == (
        FunctionTool(
            name="lookup_weather",
            description="Look up weather",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            strict=True,
        ),
        FunctionTool(name="lookup_time", description=None, parameters={}, strict=None),
    )
    schema["properties"] = {}
    assert request.tools[0].parameters["properties"] == {"city": {"type": "string"}}


@pytest.mark.parametrize(
    "tools",
    [
        None,
        {},
        [],
        [{"type": "web_search", "function": {"name": "f", "parameters": {}}}],
        [{"type": "function", "function": {"name": "f", "parameters": {}}, "extra": 1}],
        [{"type": "function"}],
        [{"type": "function", "function": {"name": "f"}}],
        [{"type": "function", "function": {"name": "f", "parameters": {}, "extra": 1}}],
        [{"type": "function", "function": {"name": "", "parameters": {}}}],
        [{"type": "function", "function": {"name": True, "parameters": {}}}],
        [{"type": "function", "function": {"name": "f", "description": 1, "parameters": {}}}],
        [{"type": "function", "function": {"name": "f", "description": None, "parameters": {}}}],
        [{"type": "function", "function": {"name": "f", "parameters": []}}],
        [{"type": "function", "function": {"name": "f", "parameters": {}, "strict": 1}}],
        [{"type": "function", "function": {"name": "f", "parameters": {}, "strict": None}}],
        [
            {"type": "function", "function": {"name": "same", "parameters": {}}},
            {"type": "function", "function": {"name": "same", "parameters": {}}},
        ],
        [
            {"type": "function", "function": {"name": "one", "parameters": {}}},
            {"type": "function", "function": {"name": "two", "parameters": {}}},
        ],
    ],
)
def test_rejects_malformed_duplicate_or_over_limit_function_definitions(tools: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        parse_chat_completion_request(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                "tools": tools,
            },
            public_model="codex",
            max_messages=8,
            max_tools=1,
        )


@pytest.mark.parametrize("choice", ["auto", "required", "none"])
def test_parses_supported_string_tool_choices(choice: str) -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
            "tool_choice": choice,
        }
    )

    assert request.tool_choice == choice


def test_parses_declared_specific_function_choice_and_parallel_setting() -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "tools": [
                {"type": "function", "function": {"name": "first", "parameters": {}}},
                {"type": "function", "function": {"name": "second", "parameters": {}}},
            ],
            "tool_choice": {"type": "function", "function": {"name": "second"}},
            "parallel_tool_calls": False,
        }
    )

    assert request.tool_choice == NamedFunctionToolChoice(name="second")
    assert request.parallel_tool_calls is False


def test_none_tool_choice_is_the_only_choice_permitted_without_tools() -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "tool_choice": "none",
        }
    )

    assert request.tool_choice == "none"


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_choice": "sometimes"},
        {"tool_choice": True},
        {"tool_choice": "auto"},
        {"tool_choice": "required"},
        {"tool_choice": {"type": "function", "function": {"name": "missing"}}},
        {"tool_choice": {"type": "function"}},
        {"tool_choice": {"type": "function", "function": {}}},
        {"tool_choice": {"type": "function", "function": {"name": "f", "extra": 1}}},
        {"tool_choice": {"type": "function", "function": {"name": "f"}, "extra": 1}},
        {"tool_choice": {"type": "builtin", "function": {"name": "f"}}},
        {"parallel_tool_calls": True},
    ],
)
def test_rejects_invalid_or_toolless_choice_and_parallel_settings(
    extra: dict[str, object],
) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                **extra,
            }
        )


@pytest.mark.parametrize("parallel", [None, 0, 1, "true"])
def test_parallel_tool_calls_requires_exact_bool(parallel: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
                "parallel_tool_calls": parallel,
            }
        )


def _history_call(call_id: str, name: str = "lookup", arguments: str = "{}") -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_parses_parallel_tool_history_with_results_in_either_order() -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [
                {"role": "user", "content": "compare"},
                {
                    "role": "assistant",
                    "content": "Checking both.",
                    "tool_calls": [
                        _history_call("call_first", "first", '{"value":1}'),
                        _history_call("call_second", "second", '{"value":2}'),
                    ],
                },
                {"role": "tool", "tool_call_id": "call_second", "content": "second result"},
                {"role": "tool", "tool_call_id": "call_first", "content": "first result"},
                {"role": "user", "content": "summarize"},
            ],
        }
    )

    assert request.messages[1] == ChatMessage(
        role="assistant",
        content="Checking both.",
        tool_calls=(
            ToolCall(call_id="call_first", name="first", arguments='{"value":1}'),
            ToolCall(call_id="call_second", name="second", arguments='{"value":2}'),
        ),
    )
    assert request.messages[2] == ChatMessage(
        role="tool",
        content="second result",
        tool_call_id="call_second",
    )


def test_parses_null_assistant_content_when_tool_calls_are_present() -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [
                {"role": "user", "content": "lookup"},
                {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
                {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            ],
        }
    )

    assert request.messages[1].content is None


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "unknown", "content": "result"}],
        [
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
        ],
        [
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
            {"role": "user", "content": "too soon"},
        ],
        [
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_2")]},
        ],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_history_call("same"), _history_call("same")],
            },
            {"role": "tool", "tool_call_id": "same", "content": "result"},
        ],
        [
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
            {"role": "tool", "tool_call_id": "wrong", "content": "result"},
        ],
        [
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
            {"role": "tool", "tool_call_id": "call_1", "content": "first"},
            {"role": "tool", "tool_call_id": "call_1", "content": "duplicate"},
        ],
        [
            {"role": "assistant", "content": None, "tool_calls": [_history_call("call_1")]},
            {"role": "tool", "content": "missing id"},
        ],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{**_history_call("call_1"), "type": "web_search"}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_history_call("call_1", arguments='{"a":1,"a":2}')],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_history_call("call_1", arguments='{"a":1e999}')],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_history_call("call_1")],
                "extra": "closed",
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        [{"role": "assistant", "content": None}],
    ],
)
def test_rejects_invalid_or_incomplete_tool_history(messages: list[dict[str, object]]) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": "codex", "messages": messages})
