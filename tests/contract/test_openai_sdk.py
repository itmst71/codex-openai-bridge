from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from openai import BadRequestError

from ._support import (
    PUBLIC_MODEL,
    assert_server_policy,
    completed_text_response,
    completed_tool_response,
    contract_server,
    strict_text_sse,
)


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
