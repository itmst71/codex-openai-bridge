from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from ._support import (
    PUBLIC_MODEL,
    assert_server_policy,
    completed_text_response,
    completed_tool_response,
    contract_server,
    strict_text_sse,
)

# Honcho request shapes frozen from commit 444897975c95393b0d48024470ece03c025d3aa4,
# especially src/llm/backends/openai.py and src/llm/history_adapters.py. Honcho is
# intentionally not imported or installed here; these calls exercise its emitted wire shapes.

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "weather",
        "description": "Get weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("token_field", ["max_tokens", "max_completion_tokens"])
async def test_honcho_token_limit_aliases_are_validated_but_not_forwarded(
    token_field: str, tmp_path: Path
) -> None:
    async with contract_server(tmp_path, responses=[completed_text_response()]) as running:
        await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "hello"}],
            **cast(Any, {token_field: 321}),
        )

    assert "max_output_tokens" not in running.upstream.calls[0]
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_honcho_tools_tool_choice_and_parallel_shape(tmp_path: Path) -> None:
    async with contract_server(tmp_path, responses=[completed_tool_response()]) as running:
        completion = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "Weather?"}],
            tools=cast(Any, [WEATHER_TOOL]),
            tool_choice={"type": "function", "function": {"name": "weather"}},
            parallel_tool_calls=False,
        )

    assert completion.choices[0].finish_reason == "tool_calls"
    payload = running.upstream.calls[0]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "weather",
            "description": "Get weather",
            "parameters": WEATHER_TOOL["function"]["parameters"],
        }
    ]
    assert payload["tool_choice"] == {"type": "function", "name": "weather"}
    assert payload["parallel_tool_calls"] is False
    assert_server_policy(payload, stream=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_format", "expected"),
    [
        ({"type": "json_object"}, {"type": "json_object"}),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "HonchoAnswer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            {
                "type": "json_schema",
                "name": "honchoanswer",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        ),
    ],
)
async def test_honcho_structured_output_shapes(
    response_format: dict[str, Any],
    expected: dict[str, Any],
    tmp_path: Path,
) -> None:
    async with contract_server(
        tmp_path,
        responses=[completed_text_response('{"answer":"ok"}')],
    ) as running:
        await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "Return JSON"}],
            response_format=cast(Any, response_format),
        )

    assert running.upstream.calls[0]["text"] == {"format": expected}
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_honcho_assistant_reasoning_details_replay(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[
            completed_tool_response(include_reasoning=True),
            completed_text_response("The weather is sunny"),
        ],
    ) as running:
        first = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "Weather in Tokyo?"}],
            tools=cast(Any, [WEATHER_TOOL]),
        )
        assistant = first.choices[0].message
        reasoning_details = (assistant.model_extra or {}).get("reasoning_details")
        assert reasoning_details
        assert assistant.tool_calls is not None
        # Pinned Honcho parses tool arguments into ``ToolCallResult.input`` and
        # OpenAIHistoryAdapter serializes that object again with ``json.dumps``.
        # The bridge binding must therefore be stable across insignificant JSON
        # whitespace/key-order changes while remaining bound to exact semantics.
        honcho_tool_calls = []
        for call in assistant.tool_calls:
            call_any = cast(Any, call)
            dumped = call_any.model_dump()
            dumped["function"]["arguments"] = json.dumps(json.loads(call_any.function.arguments))
            honcho_tool_calls.append(dumped)
        await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=cast(
                Any,
                [
                    {"role": "user", "content": "Weather in Tokyo?"},
                    {
                        "role": "assistant",
                        "content": assistant.content,
                        "tool_calls": honcho_tool_calls,
                        "reasoning_details": reasoning_details,
                    },
                    {
                        "role": "tool",
                        "tool_call_id": assistant.tool_calls[0].id,
                        "content": "sunny",
                    },
                ],
            ),
            tools=cast(Any, [WEATHER_TOOL]),
        )

    replay = running.upstream.calls[1]["input"]
    assert replay[1] == {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "Y29udHJhY3QtcmVhc29uaW5n",
    }
    assert replay[2] == {
        "type": "function_call",
        "call_id": "call_weather_contract",
        "name": "weather",
        "arguments": '{"city": "Tokyo"}',
    }
    assert replay[3] == {
        "type": "function_call_output",
        "call_id": "call_weather_contract",
        "output": "sunny",
    }
    assert_server_policy(running.upstream.calls[0], stream=False)
    assert_server_policy(running.upstream.calls[1], stream=False)


@pytest.mark.asyncio
async def test_honcho_stream_options_include_usage_shape(tmp_path: Path) -> None:
    async with contract_server(tmp_path, streams=[strict_text_sse("honcho stream")]) as running:
        stream = await running.client.chat.completions.create(
            model=PUBLIC_MODEL,
            messages=[{"role": "user", "content": "stream"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = [chunk async for chunk in stream]

    assert chunks[-1].usage is not None
    assert chunks[-1].usage.completion_tokens == 3
    assert_server_policy(running.upstream.calls[0], stream=True)
