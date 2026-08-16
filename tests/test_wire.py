from __future__ import annotations

from typing import Any

import pytest

from codex_openai_bridge.wire import (
    ChatCompletionRequest,
    ChatMessage,
    ChatRequestError,
    FunctionTool,
    JsonObjectResponseFormat,
    JsonSchemaResponseFormat,
    NamedFunctionToolChoice,
    ReasoningDetail,
    ToolCall,
    create_reasoning_binding_id,
    parse_chat_completion_request,
    reasoning_binding_is_valid,
)


def _parse(value: object) -> ChatCompletionRequest:
    return parse_chat_completion_request(
        value,
        public_model="codex",
        max_messages=8,
        max_tools=3,
        max_json_depth=16,
        max_json_nodes=128,
        max_string_bytes=128,
        binding_key="bridge-secret",
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
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "include": ["reasoning.encrypted_content"],
        },
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "reasoning": {"effort": "high"},
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
            max_json_depth=16,
            max_json_nodes=128,
            max_string_bytes=128,
            binding_key="bridge-secret",
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


def test_parses_exact_json_object_response_format() -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "return JSON"}],
            "response_format": {"type": "json_object"},
        }
    )

    assert request.response_format == JsonObjectResponseFormat()


@pytest.mark.parametrize(
    "response_format",
    [
        None,
        "json_object",
        {},
        {"type": "json_object", "extra": True},
        {"type": "json_object", "json_schema": {}},
        {"type": "text"},
        {"type": True},
    ],
)
def test_rejects_nonexact_json_object_response_format(response_format: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "return JSON"}],
                "response_format": response_format,
            }
        )


def test_parses_closed_json_schema_response_format_and_owns_schema() -> None:
    schema = {
        "$defs": {
            "Address": {
                "type": "object",
                "properties": {"city": {"type": "string", "title": "City"}},
                "required": ["city"],
                "additionalProperties": False,
            }
        },
        "type": "object",
        "properties": {
            "address": {"$ref": "#/$defs/Address"},
            "tags": {
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "kind": {"enum": ["home", "work", None]},
            "version": {"const": 1},
        },
        "required": ["address", "tags", "kind", "version"],
        "additionalProperties": False,
        "description": "An address result",
        "title": "Result",
    }
    document = {
        "model": "codex",
        "messages": [{"role": "user", "content": "return an address"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "address_result-1",
                "description": "Structured address",
                "schema": schema,
                "strict": True,
            },
        },
    }

    request = parse_chat_completion_request(
        document,
        public_model="codex",
        max_messages=8,
        max_tools=3,
        max_json_depth=16,
        max_json_nodes=128,
        max_string_bytes=128,
        binding_key="bridge-secret",
    )

    assert request.response_format == JsonSchemaResponseFormat(
        name="address_result-1",
        description="Structured address",
        schema=schema,
        strict=True,
    )
    schema["properties"] = {}
    parsed_format = request.response_format
    assert isinstance(parsed_format, JsonSchemaResponseFormat)
    assert "address" in parsed_format.schema["properties"]


def test_json_schema_name_accepts_exact_64_character_limit() -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "return JSON"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "a" * 64, "schema": {}},
            },
        }
    )

    parsed_format = request.response_format
    assert isinstance(parsed_format, JsonSchemaResponseFormat)
    assert parsed_format.name == "a" * 64


@pytest.mark.parametrize(
    "json_schema",
    [
        {},
        {"name": "result"},
        {"schema": {}},
        {"name": "result", "schema": {}, "extra": True},
        {"name": True, "schema": {}},
        {"name": "", "schema": {}},
        {"name": "a" * 65, "schema": {}},
        {"name": "not safe", "schema": {}},
        {"name": "café", "schema": {}},
        {"name": "result", "schema": []},
        {"name": "result", "schema": {}, "description": ""},
        {"name": "result", "schema": {}, "description": None},
        {"name": "result", "schema": {}, "strict": 1},
        {"name": "result", "schema": {}, "strict": None},
    ],
)
def test_rejects_malformed_json_schema_envelope(json_schema: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "return JSON"}],
                "response_format": {"type": "json_schema", "json_schema": json_schema},
            }
        )


@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "json_schema"},
        {"type": "json_schema", "json_schema": {"name": "result", "schema": {}}, "x": 1},
        {"type": "json_schema", "json_schema": []},
    ],
)
def test_rejects_nonexact_json_schema_response_format(response_format: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "return JSON"}],
                "response_format": response_format,
            }
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"patternProperties": {}},
        {"unevaluatedProperties": False},
        {"propertyNames": {}},
        {"allOf": []},
        {"oneOf": []},
        {"not": {}},
        {"if": {}, "then": {}},
        {"$ref": "https://example.invalid/schema"},
        {"$ref": "#/properties/value"},
        {"$ref": "#/$defs/"},
        {"type": "date"},
        {"type": []},
        {"type": ["string", "string"]},
        {"type": ["string", "date"]},
        {"properties": []},
        {"properties": {"value": []}},
        {"required": "value"},
        {"required": ["value", "value"]},
        {"required": [1]},
        {"additionalProperties": {}},
        {"items": []},
        {"enum": []},
        {"enum": [{}]},
        {"const": []},
        {"anyOf": []},
        {"anyOf": [None]},
        {"description": None},
        {"title": 1},
        {"$defs": []},
        {"$defs": {"Value": []}},
    ],
)
def test_rejects_unsupported_or_malformed_schema_subset(schema: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "return JSON"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "result", "schema": schema},
                },
            }
        )


def _parse_schema_with_bounds(
    schema: dict[Any, Any],
    *,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> ChatCompletionRequest:
    return parse_chat_completion_request(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "x"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "r", "schema": schema},
            },
        },
        public_model="codex",
        max_messages=1,
        max_tools=1,
        max_json_depth=max_json_depth,
        max_json_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
        binding_key="bridge-secret",
    )


def test_schema_depth_accepts_exact_limit_and_rejects_one_over() -> None:
    schema: dict[Any, Any] = {"anyOf": [{}]}

    assert (
        _parse_schema_with_bounds(
            schema, max_json_depth=3, max_json_nodes=3, max_string_bytes=16
        ).response_format
        is not None
    )
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse_schema_with_bounds(schema, max_json_depth=2, max_json_nodes=3, max_string_bytes=16)


def test_schema_nodes_accept_exact_limit_and_reject_one_over() -> None:
    schema: dict[Any, Any] = {"anyOf": [{}, {}]}

    assert (
        _parse_schema_with_bounds(
            schema, max_json_depth=3, max_json_nodes=4, max_string_bytes=16
        ).response_format
        is not None
    )
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse_schema_with_bounds(schema, max_json_depth=3, max_json_nodes=3, max_string_bytes=16)


def test_schema_strings_accept_exact_utf8_limit_and_reject_one_over() -> None:
    schema = {"title": "ééé"}

    assert (
        _parse_schema_with_bounds(
            schema, max_json_depth=2, max_json_nodes=2, max_string_bytes=6
        ).response_format
        is not None
    )
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse_schema_with_bounds(schema, max_json_depth=2, max_json_nodes=2, max_string_bytes=5)


@pytest.mark.parametrize(
    ("exact_schema", "over_schema", "max_string_bytes"),
    [
        (
            {"properties": {"a" * 10: {}}},
            {"properties": {"a" * 11: {}}},
            10,
        ),
        ({"$defs": {"a" * 5: {}}}, {"$defs": {"a" * 6: {}}}, 5),
        ({"required": ["a" * 8]}, {"required": ["a" * 9]}, 8),
    ],
)
def test_schema_named_positions_respect_exact_string_bound(
    exact_schema: dict[str, object],
    over_schema: dict[str, object],
    max_string_bytes: int,
) -> None:
    assert (
        _parse_schema_with_bounds(
            exact_schema,
            max_json_depth=3,
            max_json_nodes=4,
            max_string_bytes=max_string_bytes,
        ).response_format
        is not None
    )
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse_schema_with_bounds(
            over_schema,
            max_json_depth=3,
            max_json_nodes=4,
            max_string_bytes=max_string_bytes,
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"title": "\ud800"},
        {"properties": {"\ud800": {}}},
        {1: {}},
        {"const": float("inf")},
        {"const": object()},
    ],
)
def test_schema_preflight_rejects_non_json_or_nonencodable_values(
    schema: dict[object, object],
) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse_schema_with_bounds(schema, max_json_depth=8, max_json_nodes=16, max_string_bytes=16)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_json_depth": 0, "max_json_nodes": 1, "max_string_bytes": 1},
        {"max_json_depth": True, "max_json_nodes": 1, "max_string_bytes": 1},
        {"max_json_depth": 1, "max_json_nodes": -1, "max_string_bytes": 1},
        {"max_json_depth": 1, "max_json_nodes": 1, "max_string_bytes": 1.0},
    ],
)
def test_parser_requires_exact_positive_schema_limits(limits: dict[str, object]) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        parse_chat_completion_request(
            {"model": "codex", "messages": [{"role": "user", "content": "x"}]},
            public_model="codex",
            max_messages=1,
            max_tools=1,
            binding_key="bridge-secret",
            **limits,  # type: ignore[arg-type]
        )


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
            max_json_depth=16,
            max_json_nodes=128,
            max_string_bytes=128,
            binding_key="bridge-secret",
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


def _reasoning_detail(
    *,
    content: str | None,
    data: str = "c2VjcmV0",
    calls: tuple[ToolCall, ...] = (),
    index: int = 0,
) -> dict[str, object]:
    return {
        "type": "reasoning.encrypted",
        "data": data,
        "format": "openai-responses-v1",
        "id": create_reasoning_binding_id(
            binding_key="bridge-secret",
            content=content,
            tool_calls=calls,
            index=index,
            data=data,
        ),
        "index": index,
    }


def test_reasoning_binding_directly_covers_content_calls_index_and_data() -> None:
    calls = (ToolCall(call_id="call_1", name="lookup", arguments="{}"),)
    binding_id = create_reasoning_binding_id(
        binding_key="bridge-secret",
        content="visible",
        tool_calls=calls,
        index=0,
        data="c2VjcmV0",
    )

    assert reasoning_binding_is_valid(
        binding_key="bridge-secret",
        content="visible",
        tool_calls=calls,
        index=0,
        data="c2VjcmV0",
        binding_id=binding_id,
    )
    for changed in (
        {"binding_key": "rotated-secret"},
        {"content": "changed"},
        {"tool_calls": ()},
        {"index": 1},
        {"data": "Y2hhbmdlZA=="},
    ):
        values = {
            "binding_key": "bridge-secret",
            "content": "visible",
            "tool_calls": calls,
            "index": 0,
            "data": "c2VjcmV0",
            "binding_id": binding_id,
            **changed,
        }
        assert not reasoning_binding_is_valid(**values)  # type: ignore[arg-type]


def test_parses_exact_bound_reasoning_details_on_assistant_only() -> None:
    detail = _reasoning_detail(content="visible")

    request = _parse(
        {
            "model": "codex",
            "messages": [
                {
                    "role": "assistant",
                    "content": "visible",
                    "reasoning_details": [detail],
                }
            ],
        }
    )
    assert request.messages == (
        ChatMessage(
            role="assistant",
            content="visible",
            reasoning_details=(
                ReasoningDetail(data="c2VjcmV0", binding_id=detail["id"], index=0),  # type: ignore[arg-type]
            ),
        ),
    )


@pytest.mark.parametrize("data", ["YQ==", "YQ", "++8=", "--8"])
def test_assistant_reasoning_accepts_strict_base64_variants(data: str) -> None:
    detail = _reasoning_detail(content="visible", data=data)

    request = _parse(
        {
            "model": "codex",
            "messages": [
                {"role": "assistant", "content": "visible", "reasoning_details": [detail]}
            ],
        }
    )

    assert request.messages[0].reasoning_details[0].data == data


def test_reasoning_details_accept_exact_depth_nodes_and_string_then_reject_one_over() -> None:
    exact_data = "YQ" * 26
    detail = _reasoning_detail(content="visible", data=exact_data)
    document = {
        "model": "codex",
        "messages": [{"role": "assistant", "content": "visible", "reasoning_details": [detail]}],
    }

    parsed = parse_chat_completion_request(
        document,
        public_model="codex",
        max_messages=1,
        max_tools=1,
        max_json_depth=3,
        max_json_nodes=7,
        max_string_bytes=52,
        binding_key="bridge-secret",
    )
    assert parsed.messages[0].reasoning_details[0].data == exact_data

    for changed_limit in (
        {"max_json_depth": 2, "max_json_nodes": 7, "max_string_bytes": 52},
        {"max_json_depth": 3, "max_json_nodes": 6, "max_string_bytes": 52},
        {"max_json_depth": 3, "max_json_nodes": 7, "max_string_bytes": 51},
    ):
        with pytest.raises(ChatRequestError, match=r"^invalid request$"):
            parse_chat_completion_request(
                document,
                public_model="codex",
                max_messages=1,
                max_tools=1,
                binding_key="bridge-secret",
                **changed_limit,
            )


@pytest.mark.parametrize(
    "mutation",
    [
        {"reasoning_details": []},
        {"reasoning_details": None},
        {"reasoning_details": [None]},
        {"reasoning_details": [{**_reasoning_detail(content="visible"), "extra": True}]},
        {
            "reasoning_details": [
                {
                    key: value
                    for key, value in _reasoning_detail(content="visible").items()
                    if key != "id"
                }
            ]
        },
        {
            "reasoning_details": [
                {**_reasoning_detail(content="visible"), "type": "reasoning.summary"}
            ]
        },
        {"reasoning_details": [{**_reasoning_detail(content="visible"), "format": "other"}]},
        {"reasoning_details": [{**_reasoning_detail(content="visible"), "data": "bad data"}]},
        {"reasoning_details": [_reasoning_detail(content="visible", data="AB")]},
        {"reasoning_details": [{**_reasoning_detail(content="visible"), "id": ""}]},
        {"reasoning_details": [{**_reasoning_detail(content="visible"), "index": True}]},
        {"reasoning_details": [{**_reasoning_detail(content="visible"), "index": 1}]},
    ],
)
def test_rejects_malformed_reasoning_details_generically(
    mutation: dict[str, object],
) -> None:
    message = {"role": "assistant", "content": "visible", **mutation}

    with pytest.raises(ChatRequestError) as caught:
        _parse({"model": "codex", "messages": [message]})

    assert caught.value.args == ("invalid request",)
    assert "bridge-secret" not in repr(caught.value)


def test_rejects_reasoning_details_on_every_nonassistant_role() -> None:
    detail = _reasoning_detail(content="visible")
    messages_to_test: tuple[dict[str, object], ...] = (
        {"role": "user", "content": "visible", "reasoning_details": [detail]},
        {"role": "system", "content": "visible", "reasoning_details": [detail]},
        {"role": "developer", "content": "visible", "reasoning_details": [detail]},
        {
            "role": "tool",
            "content": "visible",
            "tool_call_id": "call_1",
            "reasoning_details": [detail],
        },
    )
    for message in messages_to_test:
        messages: list[dict[str, object]] = [message]
        if message["role"] == "tool":
            messages.insert(
                0,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_history_call("call_1")],
                },
            )
        with pytest.raises(ChatRequestError, match=r"^invalid request$"):
            _parse({"model": "codex", "messages": messages})


def test_binding_rejects_changed_content_or_tool_call_projection() -> None:
    detail = _reasoning_detail(content="original")
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "changed",
                        "reasoning_details": [detail],
                    }
                ],
            }
        )

    original_calls = (ToolCall(call_id="call_1", name="original", arguments="{}"),)
    call_detail = _reasoning_detail(content=None, calls=original_calls)
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [_history_call("call_1", name="changed")],
                        "reasoning_details": [call_detail],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "done"},
                ],
            }
        )


def test_rejects_duplicate_blob_with_recomputed_binding_across_messages_and_within_list() -> None:
    first = _reasoning_detail(content="first")
    second = _reasoning_detail(content="second")
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [
                    {"role": "assistant", "content": "first", "reasoning_details": [first]},
                    {"role": "user", "content": "continue"},
                    {"role": "assistant", "content": "second", "reasoning_details": [second]},
                ],
            }
        )


def test_rejects_same_decoded_blob_with_different_base64_spelling_across_messages() -> None:
    standard = _reasoning_detail(content="first", data="++8=")
    urlsafe_unpadded = _reasoning_detail(content="second", data="--8")

    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "first",
                        "reasoning_details": [standard],
                    },
                    {"role": "user", "content": "continue"},
                    {
                        "role": "assistant",
                        "content": "second",
                        "reasoning_details": [urlsafe_unpadded],
                    },
                ],
            }
        )

    duplicate = _reasoning_detail(content="same", index=1)
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "same",
                        "reasoning_details": [
                            _reasoning_detail(content="same", index=0),
                            duplicate,
                        ],
                    }
                ],
            }
        )


def test_reasoning_detail_node_budget_is_cumulative_across_assistant_messages() -> None:
    first = _reasoning_detail(content="first", data="c2VjcmV0MQ==")
    second = _reasoning_detail(content="second", data="c2VjcmV0Mg==")

    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        parse_chat_completion_request(
            {
                "model": "codex",
                "messages": [
                    {
                        "role": "assistant",
                        "content": "first",
                        "reasoning_details": [first],
                    },
                    {"role": "user", "content": "continue"},
                    {
                        "role": "assistant",
                        "content": "second",
                        "reasoning_details": [second],
                    },
                ],
            },
            public_model="codex",
            max_messages=3,
            max_tools=1,
            max_json_depth=3,
            max_json_nodes=13,
            max_string_bytes=128,
            binding_key="bridge-secret",
        )


@pytest.mark.parametrize("binding_key", ["", None, True])
def test_parser_requires_exact_nonempty_reasoning_binding_key(binding_key: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        parse_chat_completion_request(
            {"model": "codex", "messages": [{"role": "user", "content": "x"}]},
            public_model="codex",
            max_messages=1,
            max_tools=1,
            max_json_depth=3,
            max_json_nodes=7,
            max_string_bytes=52,
            binding_key=binding_key,  # type: ignore[arg-type]
        )


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
