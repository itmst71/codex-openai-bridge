from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

pytest.importorskip("langchain_openai", reason="requires the isolated LangChain contract")

from langchain_core.messages import HumanMessage, ToolMessage  # type: ignore[import-not-found]
from langchain_openai import ChatOpenAI  # type: ignore[import-not-found]

from tests.contract._support import (
    CLIENT_TOKEN,
    PUBLIC_MODEL,
    RunningContractServer,
    assert_server_policy,
    completed_text_response,
    completed_tool_response,
    contract_server,
    strict_text_sse,
)


class WeatherAnswer(BaseModel):
    """A bounded structured weather answer."""

    temperature_c: int = Field(description="temperature in degrees Celsius")


def weather(city: str) -> str:
    """Return bounded weather information for a city."""
    del city
    return "bounded weather result"


def _llm(running: RunningContractServer, *, use_responses_api: bool) -> ChatOpenAI:
    return ChatOpenAI(
        model=PUBLIC_MODEL,
        base_url=str(running.client.base_url),
        api_key=CLIENT_TOKEN,
        temperature=None,
        max_retries=0,
        timeout=3.0,
        use_responses_api=use_responses_api,
    )


def _text(value: object) -> str:
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") in {"text", "output_text"}
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _assert_framework_versions() -> None:
    assert version("langchain-openai") == "1.5.1"
    assert version("langchain-core") == "1.5.6"
    assert version("openai") == "3.2.0"


def _assert_no_sampling_projection(payload: dict[str, Any]) -> None:
    for field in (
        "temperature",
        "top_p",
        "stop",
        "seed",
        "logit_bias",
        "presence_penalty",
        "frequency_penalty",
    ):
        assert field not in payload


@pytest.mark.asyncio
async def test_langchain_responses_nonstream_uses_strict_bridge_contract(tmp_path: Path) -> None:
    _assert_framework_versions()
    async with contract_server(
        tmp_path,
        responses=[completed_text_response("langchain response")],
    ) as running:
        message = await _llm(running, use_responses_api=True).ainvoke("bounded input")

    assert _text(message) == "langchain response"
    assert message.usage_metadata == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
        "input_token_details": {"cache_read": 0},
        "output_token_details": {"reasoning": 0},
    }
    assert len(running.upstream.calls) == 1
    _assert_no_sampling_projection(running.upstream.calls[0])
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_langchain_responses_stream_reaches_clean_terminal(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_text_sse("langchain stream")],
    ) as running:
        chunks = [
            chunk async for chunk in _llm(running, use_responses_api=True).astream("bounded stream")
        ]

    assert "".join(_text(chunk) for chunk in chunks) == "langchain stream"
    assert len(running.upstream.calls) == 1
    assert_server_policy(running.upstream.calls[0], stream=True)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_langchain_responses_json_schema_returns_pydantic_model(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[completed_text_response('{"temperature_c":30}')],
    ) as running:
        structured = _llm(running, use_responses_api=True).with_structured_output(
            WeatherAnswer,
            method="json_schema",
            strict=True,
        )
        result = await structured.ainvoke("return bounded JSON")

    assert result == WeatherAnswer(temperature_c=30)
    payload = running.upstream.calls[0]
    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "weatheranswer",
            "schema": {
                "type": "object",
                "properties": {"temperature_c": {"type": "integer"}},
                "required": ["temperature_c"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    }
    assert_server_policy(payload, stream=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_responses_api", [True, False])
async def test_langchain_function_tool_roundtrip(
    tmp_path: Path,
    use_responses_api: bool,
) -> None:
    async with contract_server(
        tmp_path,
        responses=[
            completed_tool_response(),
            completed_text_response("langchain tool complete"),
        ],
    ) as running:
        llm = _llm(running, use_responses_api=use_responses_api)
        prompt = HumanMessage(content="use the weather tool")
        first = await llm.bind_tools(
            [weather],
            tool_choice="weather",
            parallel_tool_calls=False,
        ).ainvoke([prompt])
        assert first.tool_calls == [
            {
                "name": "weather",
                "args": {"city": "Tokyo"},
                "id": "call_weather_contract",
                "type": "tool_call",
            }
        ]
        final = await llm.bind_tools(
            [weather],
            tool_choice="none",
            parallel_tool_calls=False,
        ).ainvoke(
            [
                prompt,
                first,
                ToolMessage(
                    content="bounded weather result",
                    tool_call_id="call_weather_contract",
                ),
            ]
        )

    assert _text(final) == "langchain tool complete"
    assert len(running.upstream.calls) == 2
    assert all(
        call["tools"][0]["description"] == weather.__doc__ for call in running.upstream.calls
    )
    assert running.upstream.calls[1]["tool_choice"] == "none"
    assert running.upstream.calls[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_weather_contract",
        "output": "bounded weather result",
    }
    for call in running.upstream.calls:
        _assert_no_sampling_projection(call)
        assert_server_policy(call, stream=False)
