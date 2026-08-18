from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

import openai
import pytest
from openai import APIStatusError, BadRequestError
from openai.resources.responses.responses import AsyncResponses
from openai.types.responses import Response

from ._support import (
    PUBLIC_MODEL,
    assert_server_policy,
    completed_compaction_response,
    completed_text_response,
    completed_tool_response,
    contract_server,
    strict_compaction_sse,
    strict_custom_tool_sse,
    strict_reasoning_sse,
    strict_text_sse,
)


def _assert_cache_write_tokens(input_details: object, expected: int) -> None:
    model_fields = getattr(type(input_details), "model_fields", {})
    model_extra = getattr(input_details, "model_extra", None)
    if "cache_write_tokens" in model_fields:
        assert openai.__version__ == "3.1.0"
        assert cast(Any, input_details).cache_write_tokens == expected
        assert not model_extra or "cache_write_tokens" not in model_extra
    else:
        assert openai.__version__ == "1.109.1"
        assert model_extra == {"cache_write_tokens": expected}


def _assert_cache_write_tokens_absent(input_details: object) -> None:
    dumped = cast(Any, input_details).model_dump(exclude_none=True)
    model_extra = getattr(input_details, "model_extra", None)
    assert "cache_write_tokens" not in dumped
    assert not model_extra or "cache_write_tokens" not in model_extra


def _assert_message_phase(message: object, expected: str) -> None:
    model_fields = getattr(type(message), "model_fields", {})
    model_extra = getattr(message, "model_extra", None)
    if "phase" in model_fields:
        assert openai.__version__ == "3.1.0"
        assert cast(Any, message).phase == expected
        assert not model_extra or "phase" not in model_extra
    else:
        assert openai.__version__ == "1.109.1"
        assert model_extra == {"phase": expected}


@pytest.mark.asyncio
async def test_chat_nonstream_text_and_models_list_use_typed_sdk_interfaces(
    tmp_path: Path,
) -> None:
    async with contract_server(tmp_path, responses=[completed_text_response()]) as running:
        models = await running.client.models.list()
        completion = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "hello"}],
        )

    assert [model.id for model in models.data] == [PUBLIC_MODEL]
    assert completion.model == PUBLIC_MODEL
    assert completion.choices[0].message.content == "hello from the bridge"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 7
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_responses_message_phase_is_preserved_by_sdk_version(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[completed_text_response(phase="final_answer")],
    ) as running:
        response = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input="Preserve the confirmed phase.",
        )

    _assert_message_phase(response.output[0], "final_answer")


@pytest.mark.asyncio
async def test_chat_stream_async_iterator_includes_terminal_usage(tmp_path: Path) -> None:
    async with contract_server(tmp_path, streams=[strict_text_sse()]) as running:
        stream = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "stream this"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = [chunk async for chunk in stream]

    assert (
        "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices)
        == "streamed text"
    )
    usage = next(chunk.usage for chunk in chunks if chunk.usage is not None)
    assert usage.prompt_tokens == 2
    assert usage.completion_tokens == 3
    assert usage.total_tokens == 5
    assert_server_policy(running.upstream.calls[0], stream=True)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_chat_function_call_tool_result_then_final_response(tmp_path: Path) -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get a city's weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
    async with contract_server(
        tmp_path,
        responses=[completed_tool_response(), completed_text_response("Sunny and 30 C")],
    ) as running:
        first = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "Weather in Tokyo?"}],
            tools=cast(Any, [tool]),
            tool_choice="required",
            parallel_tool_calls=False,
        )
        assistant = first.choices[0].message
        assert assistant.tool_calls is not None
        call = cast(Any, assistant.tool_calls[0])
        final = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=cast(
                Any,
                [
                    {"role": "user", "content": "Weather in Tokyo?"},
                    {
                        "role": "assistant",
                        "content": assistant.content,
                        "tool_calls": [call.model_dump()],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": '{"temperature_c":30,"condition":"sunny"}',
                    },
                ],
            ),
            tools=cast(Any, [tool]),
        )

    assert first.model == PUBLIC_MODEL
    assert first.choices[0].finish_reason == "tool_calls"
    assert call.function.name == "weather"
    assert call.function.arguments == '{"city":"Tokyo"}'
    assert final.choices[0].message.content == "Sunny and 30 C"
    assert len(running.upstream.calls) == 2
    assert_server_policy(running.upstream.calls[0], stream=False)
    assert_server_policy(running.upstream.calls[1], stream=False)
    assert running.upstream.calls[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_weather_contract",
        "output": '{"temperature_c":30,"condition":"sunny"}',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_format", "expected_format"),
    [
        ({"type": "json_object"}, {"type": "json_object"}),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "weather_answer",
                    "description": "A weather answer",
                    "schema": {
                        "type": "object",
                        "properties": {"temperature_c": {"type": "integer"}},
                        "required": ["temperature_c"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            {
                "type": "json_schema",
                "name": "weather_answer",
                "description": "A weather answer",
                "schema": {
                    "type": "object",
                    "properties": {"temperature_c": {"type": "integer"}},
                    "required": ["temperature_c"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        ),
    ],
)
async def test_chat_response_format_json_modes(
    tmp_path: Path,
    response_format: dict[str, Any],
    expected_format: dict[str, Any],
) -> None:
    async with contract_server(
        tmp_path,
        responses=[completed_text_response('{"temperature_c":30}')],
    ) as running:
        completion = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "Return JSON weather"}],
            response_format=cast(Any, response_format),
        )

    assert completion.choices[0].message.content == '{"temperature_c":30}'
    assert running.upstream.calls[0]["text"] == {"format": expected_format}
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_responses_create_and_stream_get_final_response(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[completed_text_response("created response")],
        streams=[strict_text_sse("streamed response")],
    ) as running:
        created = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input="create a response",
        )
        async with running.client.responses.stream(
            model=PUBLIC_MODEL,
            input="stream a response",
        ) as stream:
            event_types = [event.type async for event in stream]
            final = await stream.get_final_response()

    assert created.model == PUBLIC_MODEL
    assert created.output_text == "created response"
    assert "response.output_text.delta" in event_types
    assert event_types[-1] == "response.completed"
    assert final.model == PUBLIC_MODEL
    assert final.output_text == "streamed response"
    assert_server_policy(running.upstream.calls[0], stream=False)
    assert_server_policy(running.upstream.calls[1], stream=True)


@pytest.mark.asyncio
async def test_responses_confirmed_request_controls_use_typed_sdk_surface(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[completed_text_response("controlled response")],
    ) as running:
        response = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input=[
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Apply the bounded policy."}],
                }
            ],
            reasoning={"effort": "high", "summary": "auto"},
            prompt_cache_key="contract-cache-key",
            service_tier="default",
            text={"verbosity": "high"},
        )

    assert response.output_text == "controlled response"
    assert running.upstream.calls[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert running.upstream.calls[0]["prompt_cache_key"] == "contract-cache-key"
    assert running.upstream.calls[0]["service_tier"] == "default"
    assert running.upstream.calls[0]["text"] == {"verbosity": "high"}
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_responses_buffered_reasoning_preserves_typed_summary(tmp_path: Path) -> None:
    summary_text = "bounded contract summary"
    async with contract_server(
        tmp_path,
        streams=[strict_reasoning_sse(summary_text)],
        buffered_nonstream=True,
    ) as running:
        response = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input="Explain briefly.",
            reasoning={"summary": "auto"},
        )

    reasoning = response.output[0]
    assert reasoning.type == "reasoning"
    assert reasoning.summary[0].type == "summary_text"
    assert reasoning.summary[0].text == summary_text
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_buffered_rejects_unsolicited_reasoning_summary(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_reasoning_sse("UNSOLICITED SUMMARY")],
        buffered_nonstream=True,
    ) as running:
        with pytest.raises(APIStatusError) as caught:
            await running.client.responses.create(
                model=PUBLIC_MODEL,
                input="Do not expose a reasoning summary.",
            )

    assert caught.value.status_code == 502
    assert "UNSOLICITED SUMMARY" not in str(caught.value)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_buffered_usage_preserves_cache_write_tokens(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_text_sse(cache_write_tokens=2)],
        buffered_nonstream=True,
    ) as running:
        response = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input="Use the bounded cache.",
        )

    assert response.usage is not None
    _assert_cache_write_tokens(response.usage.input_tokens_details, 2)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_buffered_usage_keeps_absent_cache_write_tokens_absent(
    tmp_path: Path,
) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_text_sse()],
        buffered_nonstream=True,
    ) as running:
        response = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input="Do not invent cache usage.",
        )

    assert response.usage is not None
    _assert_cache_write_tokens_absent(response.usage.input_tokens_details)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_stream_usage_preserves_cache_write_tokens(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_text_sse(cache_write_tokens=3)],
    ) as running:
        async with running.client.responses.stream(
            model=PUBLIC_MODEL,
            input="Stream with the bounded cache.",
        ) as stream:
            events = [event async for event in stream]
            final = await stream.get_final_response()

    completed = next(cast(Any, event) for event in events if event.type == "response.completed")
    assert completed.response.usage is not None
    _assert_cache_write_tokens(completed.response.usage.input_tokens_details, 3)
    assert final.usage is not None
    _assert_cache_write_tokens(final.usage.input_tokens_details, 3)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_stream_reasoning_preserves_typed_summary(tmp_path: Path) -> None:
    summary_text = "streamed contract summary"
    async with contract_server(tmp_path, streams=[strict_reasoning_sse(summary_text)]) as running:
        async with running.client.responses.stream(
            model=PUBLIC_MODEL,
            input="Explain while streaming.",
            reasoning={"summary": "auto"},
        ) as stream:
            events = [event async for event in stream]
            final = await stream.get_final_response()

    reasoning_items = [
        cast(Any, event).item
        for event in events
        if event.type in {"response.output_item.added", "response.output_item.done"}
        and cast(Any, event).item.type == "reasoning"
    ]
    assert [item.summary[0].text for item in reasoning_items] == [summary_text, summary_text]
    assert cast(Any, final.output[0]).summary[0].text == summary_text
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_stream_rejects_unsolicited_reasoning_summary(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_reasoning_sse("UNSOLICITED STREAM SUMMARY")],
    ) as running:
        async with running.client.responses.stream(
            model=PUBLIC_MODEL,
            input="Do not stream a reasoning summary.",
        ) as stream:
            events = [event async for event in stream]

    assert events[-1].type == "error"
    assert all(event.type != "response.completed" for event in events)
    assert "UNSOLICITED STREAM SUMMARY" not in repr(events)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_responses_buffered_custom_tool_call_output_roundtrip_uses_common_sdk_types(
    tmp_path: Path,
) -> None:
    tool = {"type": "custom", "name": "emit_probe", "description": "Emit a probe"}
    async with contract_server(
        tmp_path,
        streams=[strict_custom_tool_sse(), strict_text_sse("tool complete")],
        buffered_nonstream=True,
    ) as running:
        first = await running.client.responses.create(
            model=PUBLIC_MODEL,
            input="Invoke the required probe tool.",
            tools=cast(Any, [tool]),
            tool_choice=cast(Any, {"type": "custom", "name": "emit_probe"}),
            parallel_tool_calls=False,
        )
        call = first.output[0]
        assert call.type == "custom_tool_call"
        assert call.name == "emit_probe"
        assert call.input == "contract probe"
        final: Response = await cast(Any, running.client.responses.create)(
            model=PUBLIC_MODEL,
            input=cast(
                Any,
                [
                    call.model_dump(exclude_none=True),
                    {
                        "type": "custom_tool_call_output",
                        "call_id": call.call_id,
                        "output": "contract result",
                    },
                ],
            ),
            tools=cast(Any, [tool]),
            tool_choice="none",
            parallel_tool_calls=False,
        )

    assert final.output_text == "tool complete"
    assert len(running.upstream.byte_streams) == 2
    assert all(stream.close_calls == 1 for stream in running.upstream.byte_streams)
    assert running.upstream.calls[1]["tool_choice"] == "none"
    assert running.upstream.calls[1]["input"][-1] == {
        "type": "custom_tool_call_output",
        "call_id": "call_custom_stream_contract",
        "output": "contract result",
    }


@pytest.mark.asyncio
async def test_responses_custom_tool_stream_restores_typed_final_output(tmp_path: Path) -> None:
    tool = {"type": "custom", "name": "emit_probe", "description": "Emit a probe"}
    async with contract_server(tmp_path, streams=[strict_custom_tool_sse()]) as running:
        async with running.client.responses.stream(
            model=PUBLIC_MODEL,
            input="Invoke the required probe tool.",
            tools=cast(Any, [tool]),
            tool_choice=cast(Any, {"type": "custom", "name": "emit_probe"}),
            parallel_tool_calls=False,
        ) as stream:
            events = [event async for event in stream]
            final = await stream.get_final_response()

    assert [event.type for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert all("obfuscation" not in event.model_dump() for event in events)
    assert final.output[0].type == "custom_tool_call"
    assert final.output[0].input == "contract probe"
    assert_server_policy(running.upstream.calls[0], stream=True)


@pytest.mark.asyncio
async def test_responses_custom_tool_stream_accepts_disabled_obfuscation(tmp_path: Path) -> None:
    tool = {"type": "custom", "name": "emit_probe", "description": "Emit a probe"}
    async with contract_server(
        tmp_path,
        streams=[strict_custom_tool_sse(include_obfuscation=False)],
    ) as running:
        async with cast(Any, running.client.responses).stream(
            model=PUBLIC_MODEL,
            input="Invoke the required probe tool without obfuscation padding.",
            tools=[tool],
            tool_choice={"type": "custom", "name": "emit_probe"},
            parallel_tool_calls=False,
            stream_options={"include_obfuscation": False},
        ) as stream:
            events = [event async for event in stream]
            final = await stream.get_final_response()

    assert events[-1].type == "response.completed"
    assert final.output[0].type == "custom_tool_call"
    assert final.output[0].input == "contract probe"
    assert running.upstream.calls[0]["stream_options"] == {"include_obfuscation": False}
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_current_sdk_parses_native_compaction_output(tmp_path: Path) -> None:
    if "context_management" not in inspect.signature(AsyncResponses.create).parameters:
        pytest.skip(f"OpenAI SDK {openai.__version__} has no typed context_management field")
    async with contract_server(
        tmp_path,
        responses=[completed_compaction_response()],
    ) as running:
        response: Response = await cast(Any, running.client.responses.create)(
            model=PUBLIC_MODEL,
            input="Compact this bounded context.",
            context_management=[{"type": "compaction", "compact_threshold": 1024}],
        )

    assert [item.type for item in response.output] == ["compaction", "message", "compaction"]
    _assert_message_phase(response.output[1], "final_answer")
    assert response.output_text == "compacted response"
    assert running.upstream.calls[0]["context_management"] == [
        {"type": "compaction", "compact_threshold": 1024}
    ]


@pytest.mark.asyncio
async def test_current_sdk_streams_native_compaction_lifecycle(tmp_path: Path) -> None:
    if "context_management" not in inspect.signature(AsyncResponses.create).parameters:
        pytest.skip(f"OpenAI SDK {openai.__version__} has no typed context_management field")
    async with contract_server(tmp_path, streams=[strict_compaction_sse()]) as running:
        async with cast(Any, running.client.responses).stream(
            model=PUBLIC_MODEL,
            input="Compact this bounded stream.",
            context_management=[{"type": "compaction", "compact_threshold": 1024}],
        ) as stream:
            events = [event async for event in stream]
            final = await stream.get_final_response()

    assert events[-1].type == "response.completed"
    assert [item.type for item in final.output] == ["compaction", "message", "compaction"]
    _assert_message_phase(final.output[1], "final_answer")
    assert final.output_text == "compacted stream"
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_embeddings_return_clear_openai_unsupported_error(tmp_path: Path) -> None:
    async with contract_server(tmp_path) as running:
        with pytest.raises(BadRequestError) as caught:
            await running.client.embeddings.create(
                model="text-embedding-3-small",
                input="never sent upstream",
            )

    assert caught.value.status_code == 400
    assert caught.value.code == "unsupported_endpoint"
    assert caught.value.type == "invalid_request_error"
    assert "not supported" in caught.value.message.lower()
    assert running.upstream.calls == []
    assert running.provider.calls == []
