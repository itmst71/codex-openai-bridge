from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest

pytest.importorskip("agents", reason="requires the isolated OpenAI Agents SDK contract")

from agents import (  # type: ignore[import-not-found]
    Agent,
    OpenAIChatCompletionsModel,
    Runner,
    function_tool,
    set_tracing_disabled,
)

from tests.contract._support import (
    PUBLIC_MODEL,
    assert_server_policy,
    completed_text_response,
    completed_tool_response,
    contract_server,
    strict_text_sse,
)

set_tracing_disabled(True)


@function_tool  # type: ignore[untyped-decorator]
def weather(city: str) -> str:
    """Return bounded weather information for a city.

    Args:
        city: City name to look up.
    """
    del city
    return "bounded weather result"


def _model(running_client: object) -> OpenAIChatCompletionsModel:
    return OpenAIChatCompletionsModel(
        model=PUBLIC_MODEL,
        openai_client=running_client,
    )


def _assert_framework_versions() -> None:
    assert version("openai-agents") == "0.21.1"
    assert version("openai") == "3.2.0"


@pytest.mark.asyncio
async def test_openai_agents_chat_model_completes_without_external_tracing(tmp_path: Path) -> None:
    _assert_framework_versions()
    async with contract_server(
        tmp_path,
        responses=[completed_text_response("agents response")],
    ) as running:
        agent = Agent(
            name="bounded-agent",
            instructions="Answer briefly.",
            model=_model(running.client),
        )
        result = await Runner.run(agent, "bounded input", max_turns=1)

    assert result.final_output == "agents response"
    assert len(running.upstream.calls) == 1
    assert_server_policy(running.upstream.calls[0], stream=False)


@pytest.mark.asyncio
async def test_openai_agents_chat_model_stream_reaches_clean_terminal(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        streams=[strict_text_sse("agents stream")],
    ) as running:
        agent = Agent(
            name="bounded-stream-agent",
            instructions="Answer briefly.",
            model=_model(running.client),
        )
        result = Runner.run_streamed(agent, "bounded stream", max_turns=1)
        event_types = [event.type async for event in result.stream_events()]

    assert result.final_output == "agents stream"
    assert "raw_response_event" in event_types
    assert "run_item_stream_event" in event_types
    assert len(running.upstream.calls) == 1
    assert_server_policy(running.upstream.calls[0], stream=True)
    assert running.upstream.byte_streams[0].close_calls == 1


@pytest.mark.asyncio
async def test_openai_agents_chat_model_executes_local_function_tool_loop(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[
            completed_tool_response(),
            completed_text_response("agents tool complete"),
        ],
    ) as running:
        agent = Agent(
            name="bounded-tool-agent",
            instructions="Use weather, then answer briefly.",
            model=_model(running.client),
            tools=[weather],
        )
        result = await Runner.run(agent, "use the weather tool", max_turns=3)

    assert result.final_output == "agents tool complete"
    assert len(running.upstream.calls) == 2
    assert running.upstream.calls[0]["tools"][0]["name"] == "weather"
    assert running.upstream.calls[0]["tools"][0]["description"]
    assert running.upstream.calls[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_weather_contract",
        "output": "bounded weather result",
    }
    for call in running.upstream.calls:
        assert_server_policy(call, stream=False)
