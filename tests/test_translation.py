from __future__ import annotations

import pytest

from codex_openai_bridge.translation import (
    UpstreamResponseError,
    chat_request_to_responses,
    responses_to_chat_completion,
)
from codex_openai_bridge.wire import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionTool,
    JsonObjectResponseFormat,
    JsonSchemaResponseFormat,
    NamedFunctionToolChoice,
    ToolCall,
)


def test_translation_uses_upstream_model_and_forces_non_persistence_and_non_streaming() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="hello"),),
        max_output_tokens=None,
    )

    assert chat_request_to_responses(request, upstream_model="upstream-model") == {
        "model": "upstream-model",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "store": False,
        "stream": False,
    }


def test_system_and_developer_text_become_ordered_deterministic_instructions() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(role="system", content="system text"),
            ChatMessage(role="user", content="question"),
            ChatMessage(role="developer", content="developer text"),
        ),
        max_output_tokens=None,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["instructions"] == "system text\n\ndeveloper text"
    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "question"}]}
    ]


def test_ordered_user_and_assistant_history_uses_role_appropriate_text_parts() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="second"),
            ChatMessage(role="user", content="third"),
        ),
        max_output_tokens=23,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "second"}],
        },
        {"role": "user", "content": [{"type": "input_text", "text": "third"}]},
    ]
    assert payload["max_output_tokens"] == 23


def test_function_definitions_map_in_order_without_schema_aliasing() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="weather"),),
        max_output_tokens=None,
        tools=(
            FunctionTool(
                name="lookup_weather",
                description="Look up weather",
                parameters={"type": "object", "properties": {"city": {"type": "string"}}},
                strict=True,
            ),
            FunctionTool(name="lookup_time", description=None, parameters={}, strict=None),
        ),
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup_weather",
            "description": "Look up weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
            },
            "strict": True,
        },
        {"type": "function", "name": "lookup_time", "parameters": {}},
    ]
    first_parameters = payload["tools"][0]["parameters"]
    assert isinstance(first_parameters, dict)
    first_parameters["type"] = "changed"
    assert request.tools[0].parameters["type"] == "object"


@pytest.mark.parametrize("choice", ["auto", "required", "none"])
def test_string_tool_choice_maps_exactly(choice: str) -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="text"),),
        max_output_tokens=None,
        tools=(FunctionTool(name="f", description=None, parameters={}, strict=None),),
        tool_choice=choice,  # type: ignore[arg-type]
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["tool_choice"] == choice


def test_specific_tool_choice_and_parallel_setting_map_to_responses_shape() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="text"),),
        max_output_tokens=None,
        tools=(FunctionTool(name="f", description=None, parameters={}, strict=None),),
        tool_choice=NamedFunctionToolChoice(name="f"),
        parallel_tool_calls=True,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["tool_choice"] == {"type": "function", "name": "f"}
    assert payload["parallel_tool_calls"] is True


def test_json_object_response_format_maps_to_exact_native_text_format() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="return JSON"),),
        max_output_tokens=None,
        response_format=JsonObjectResponseFormat(),
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload == {
        "model": "upstream-model",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "return JSON"}]}],
        "store": False,
        "stream": False,
        "text": {"format": {"type": "json_object"}},
    }


def test_json_schema_response_format_flattens_exactly_without_aliasing() -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="answer"),),
        max_output_tokens=20,
        response_format=JsonSchemaResponseFormat(
            name="answer_result",
            description="One answer",
            schema=schema,
            strict=True,
        ),
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "answer_result",
            "description": "One answer",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    }
    translated_schema = payload["text"]["format"]["schema"]
    translated_schema["properties"] = {}
    parsed_format = request.response_format
    assert isinstance(parsed_format, JsonSchemaResponseFormat)
    assert parsed_format.schema["properties"] == {"answer": {"type": "string"}}


def test_optional_json_schema_fields_are_omitted_and_tools_coexist_exactly() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="use a tool then answer"),),
        max_output_tokens=None,
        tools=(
            FunctionTool(
                name="lookup",
                description=None,
                parameters={"type": "object"},
                strict=None,
            ),
        ),
        tool_choice=NamedFunctionToolChoice(name="lookup"),
        parallel_tool_calls=False,
        response_format=JsonSchemaResponseFormat(
            name="result",
            description=None,
            schema={"type": "object"},
            strict=None,
        ),
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload == {
        "model": "upstream-model",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "use a tool then answer"}],
            }
        ],
        "store": False,
        "stream": False,
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "lookup"},
        "parallel_tool_calls": False,
        "text": {"format": {"type": "json_schema", "name": "result", "schema": {"type": "object"}}},
    }


def test_parallel_tool_history_maps_calls_then_results_in_message_order() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(role="user", content="compare"),
            ChatMessage(
                role="assistant",
                content="Checking both.",
                tool_calls=(
                    ToolCall(call_id="call_first", name="first", arguments='{"value":1}'),
                    ToolCall(call_id="call_second", name="second", arguments='{"value":2}'),
                ),
            ),
            ChatMessage(role="tool", content="second result", tool_call_id="call_second"),
            ChatMessage(role="tool", content="first result", tool_call_id="call_first"),
        ),
        max_output_tokens=None,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "compare"}]},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking both."}],
        },
        {
            "type": "function_call",
            "call_id": "call_first",
            "name": "first",
            "arguments": '{"value":1}',
        },
        {
            "type": "function_call",
            "call_id": "call_second",
            "name": "second",
            "arguments": '{"value":2}',
        },
        {"type": "function_call_output", "call_id": "call_second", "output": "second result"},
        {"type": "function_call_output", "call_id": "call_first", "output": "first result"},
    ]


def test_null_assistant_tool_call_content_does_not_create_empty_message() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(
                role="assistant",
                content=None,
                tool_calls=(ToolCall(call_id="call_1", name="f", arguments="{}"),),
            ),
            ChatMessage(role="tool", content="done", tool_call_id="call_1"),
        ),
        max_output_tokens=None,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["input"] == [
        {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "done"},
    ]


def _completed_response() -> dict[str, object]:
    return {
        "id": "resp_test",
        "status": "completed",
        "created_at": 1_723_456_789,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "id": "msg_test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
        ],
        "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    }


def test_completed_assistant_response_maps_to_openai_chat_completion() -> None:
    assert responses_to_chat_completion(_completed_response(), public_model="codex") == {
        "id": "resp_test",
        "object": "chat.completion",
        "created": 1_723_456_789,
        "model": "codex",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


def test_completed_function_call_maps_to_tool_calls_with_null_content() -> None:
    response = _completed_response()
    response["output"] = [
        {
            "id": "fc_upstream",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_weather",
            "name": "lookup_weather",
            "arguments": '{"city":"Tokyo"}',
        }
    ]

    completion = responses_to_chat_completion(response, public_model="codex")

    choice = completion["choices"][0]
    assert choice == {
        "index": 0,
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {
                        "name": "lookup_weather",
                        "arguments": '{"city":"Tokyo"}',
                    },
                }
            ],
        },
        "finish_reason": "tool_calls",
    }
    assert "fc_upstream" not in repr(completion)


def test_text_and_parallel_function_calls_preserve_text_and_call_order() -> None:
    response = _completed_response()
    response["output"] = [
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "call_second",
            "name": "second",
            "arguments": '{"value":2}',
        },
        {
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking both."}],
        },
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "call_first",
            "name": "first",
            "arguments": '{"value":1}',
        },
    ]

    completion = responses_to_chat_completion(response, public_model="codex")

    message = completion["choices"][0]["message"]
    assert message["content"] == "Checking both."
    assert [call["id"] for call in message["tool_calls"]] == ["call_second", "call_first"]
    assert completion["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize(
    "call",
    [
        {"type": "function_call", "status": "completed", "call_id": "c", "name": "f"},
        {
            "type": "function_call",
            "status": "in_progress",
            "call_id": "c",
            "name": "f",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "",
            "name": "f",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "c",
            "name": True,
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "c",
            "name": "f",
            "arguments": {},
        },
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "c",
            "name": "f",
            "arguments": "{}",
            "secret": "SENSITIVE_UPSTREAM_FIELD",
        },
        {
            "id": "",
            "type": "function_call",
            "status": "completed",
            "call_id": "c",
            "name": "f",
            "arguments": "{}",
        },
        {
            "id": None,
            "type": "function_call",
            "status": "completed",
            "call_id": "c",
            "name": "f",
            "arguments": "{}",
        },
    ],
)
def test_rejects_malformed_or_non_allowlisted_function_call_items(
    call: dict[str, object],
) -> None:
    response = _completed_response()
    response["output"] = [call]

    with pytest.raises(UpstreamResponseError) as caught:
        responses_to_chat_completion(response, public_model="codex")

    assert caught.value.args == ("invalid upstream response",)
    assert "SENSITIVE_UPSTREAM_FIELD" not in repr(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        "",
        "null",
        "[]",
        "true",
        "1",
        '{"a":1} trailing',
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1e999}',
    ],
)
def test_function_call_arguments_require_one_strict_json_object(arguments: str) -> None:
    response = _completed_response()
    response["output"] = [
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "c",
            "name": "f",
            "arguments": arguments,
        }
    ]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


def test_rejects_duplicate_function_call_ids() -> None:
    response = _completed_response()
    response["output"] = [
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "same",
            "name": "first",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "status": "completed",
            "call_id": "same",
            "name": "second",
            "arguments": "{}",
        },
    ]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


@pytest.mark.parametrize(
    "mutation",
    [
        ("id", None),
        ("id", ""),
        ("status", "in_progress"),
        ("created_at", True),
        ("created_at", -1),
        ("output", None),
        ("output", []),
        ("usage", None),
    ],
)
def test_malformed_response_core_is_rejected_generically(mutation: tuple[str, object]) -> None:
    response = _completed_response()
    response[mutation[0]] = mutation[1]

    with pytest.raises(UpstreamResponseError) as caught:
        responses_to_chat_completion(response, public_model="codex")

    assert caught.value.args == ("invalid upstream response",)


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "1", None])
def test_usage_requires_exact_nonnegative_integers(field: str, value: object) -> None:
    response = _completed_response()
    usage = response["usage"]
    assert isinstance(usage, dict)
    usage[field] = value

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


def test_assistant_message_item_must_be_completed() -> None:
    response = _completed_response()
    output = response["output"]
    assert isinstance(output, list)
    message = output[1]
    assert isinstance(message, dict)
    message["status"] = "in_progress"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


@pytest.mark.parametrize(
    "content",
    [
        [],
        None,
        [{"type": "input_text", "text": "wrong kind"}],
        [{"type": "output_text", "text": None}],
    ],
)
def test_assistant_output_requires_output_text_parts(content: object) -> None:
    response = _completed_response()
    output = response["output"]
    assert isinstance(output, list)
    message = output[1]
    assert isinstance(message, dict)
    message["content"] = content

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


@pytest.mark.parametrize(
    "unsupported_item",
    [
        None,
        "invalid",
        {"type": "function_call", "name": "unsupported"},
        {"type": "message", "role": "user", "content": []},
    ],
)
def test_rejects_malformed_or_unsupported_output_items(unsupported_item: object) -> None:
    response = _completed_response()
    output = response["output"]
    assert isinstance(output, list)
    output.insert(0, unsupported_item)

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")
