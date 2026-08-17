from __future__ import annotations

import json
import os
from typing import Any, cast

import pytest
from openai import OpenAI
from openai.types.chat.completion_create_params import ResponseFormat

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_CODEX") != "1",
    reason="requires RUN_LIVE_CODEX=1",
)


def _live_client() -> OpenAI:
    base_url = os.environ.get("CODEX_BRIDGE_LIVE_BASE_URL")
    api_key = os.environ.get("CODEX_BRIDGE_LIVE_API_KEY")
    if not base_url or not api_key:
        pytest.fail(
            "CODEX_BRIDGE_LIVE_BASE_URL and CODEX_BRIDGE_LIVE_API_KEY "
            "are required when RUN_LIVE_CODEX=1"
        )
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        max_retries=0,
        timeout=90.0,
    )


@pytest.mark.parametrize(
    ("response_format", "expected_field"),
    [
        ({"type": "json_object"}, None),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "live_result",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
            "answer",
        ),
    ],
)
def test_live_codex_structured_output(
    response_format: ResponseFormat,
    expected_field: str | None,
) -> None:
    with _live_client() as client:
        completion = client.chat.completions.create(
            model="codex",
            messages=[
                {
                    "role": "user",
                    "content": "Return a JSON object. For a schema request, set answer to yes.",
                }
            ],
            response_format=response_format,
        )

    content = completion.choices[0].message.content
    assert content is not None
    parsed = json.loads(content)
    assert type(parsed) is dict
    if expected_field is not None:
        assert parsed.get(expected_field) == "yes"


def test_live_codex_nonstream_chat_and_responses() -> None:
    with _live_client() as client:
        chat = client.chat.completions.create(
            model="codex",
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
        )
        assert (chat.choices[0].message.content or "").strip() == "OK"

        response = client.responses.create(
            model="codex",
            input="Reply with exactly OK.",
        )
        assert response.output_text.strip() == "OK"


def test_live_codex_chat_and_responses_streams_reach_clean_terminals() -> None:
    with _live_client() as client:
        chat_stream = client.chat.completions.create(
            model="codex",
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chat_text: list[str] = []
        chat_usage = False
        for chunk in chat_stream:
            if chunk.choices and chunk.choices[0].delta.content:
                chat_text.append(chunk.choices[0].delta.content)
            chat_usage = chat_usage or chunk.usage is not None
        assert "".join(chat_text).strip() == "OK"
        assert chat_usage is True

        response_stream = client.responses.create(
            model="codex",
            input="Reply with exactly OK.",
            stream=True,
        )
        event_types: list[str] = []
        response_text: list[str] = []
        for event in response_stream:
            event_types.append(event.type)
            if event.type == "response.output_text.delta":
                response_text.append(event.delta)
        assert "".join(response_text).strip() == "OK"
        assert "response.completed" in event_types
        assert "error" not in event_types


def test_live_codex_honcho_style_tool_roundtrip() -> None:
    weather_tool = {
        "type": "function",
        "function": {
            "name": "lookup_weather",
            "description": "Return weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
    with _live_client() as client:
        first = client.chat.completions.create(
            model="codex",
            messages=[{"role": "user", "content": "Use lookup_weather for Tokyo."}],
            tools=cast(Any, [weather_tool]),
            tool_choice={"type": "function", "function": {"name": "lookup_weather"}},
            parallel_tool_calls=False,
        )
        message = first.choices[0].message
        assert message.tool_calls is not None
        assert len(message.tool_calls) == 1
        call = cast(Any, message.tool_calls[0])
        assert call.function.name == "lookup_weather"
        assert json.loads(call.function.arguments) == {"city": "Tokyo"}

        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [call.model_dump(exclude_none=True)],
        }
        reasoning_details = (message.model_extra or {}).get("reasoning_details")
        if reasoning_details is not None:
            assistant["reasoning_details"] = reasoning_details
        second = client.chat.completions.create(
            model="codex",
            messages=cast(
                Any,
                [
                    {"role": "user", "content": "Use lookup_weather for Tokyo."},
                    assistant,
                    {"role": "tool", "tool_call_id": call.id, "content": "sunny"},
                ],
            ),
            tools=cast(Any, [weather_tool]),
            tool_choice="none",
            parallel_tool_calls=False,
        )
        assert (second.choices[0].message.content or "").strip()
