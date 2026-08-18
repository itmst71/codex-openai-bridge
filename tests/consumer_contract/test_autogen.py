from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Mapping, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from pydantic import BaseModel, Field

pytest.importorskip("autogen_ext", reason="requires the isolated AutoGen consumer contract")
pytest.importorskip("autogen_agentchat", reason="requires the isolated AutoGen consumer contract")

from autogen_agentchat.agents import AssistantAgent  # type: ignore[import-not-found]
from autogen_agentchat.messages import (  # type: ignore[import-not-found]
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from autogen_core import CancellationToken  # type: ignore[import-not-found]
from autogen_core.models import (  # type: ignore[import-not-found]
    CreateResult,
    LLMMessage,
    ModelFamily,
    UserMessage,
)
from autogen_core.tools import Tool, ToolSchema  # type: ignore[import-not-found]
from autogen_ext.models.openai import (  # type: ignore[import-not-found]
    OpenAIChatCompletionClient,
)

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


async def weather(city: str) -> str:
    """Return bounded weather information for a city."""
    del city
    return "bounded weather result"


def _assert_framework_versions() -> None:
    assert version("autogen-ext") == "0.7.5"
    assert version("autogen-core") == "0.7.5"
    assert version("autogen-agentchat") == "0.7.5"
    assert version("openai") == "3.2.0"


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeError:
            return False
    if host == "localhost":
        return True
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _install_loopback_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    blocked: list[str] = []
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def reject(host: object) -> None:
        blocked.append(str(host))
        raise OSError("non-loopback network is disabled by the consumer contract")

    def guarded_getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            reject(host)
        return original_getaddrinfo(cast(str | bytes | None, host), *args, **kwargs)

    def guarded_connect(sock: socket.socket, address: object) -> Any:
        if isinstance(address, tuple) and address and not _is_loopback(address[0]):
            reject(address[0])
        return original_connect(sock, address)  # type: ignore[arg-type]

    def guarded_connect_ex(sock: socket.socket, address: object) -> int:
        if isinstance(address, tuple) and address and not _is_loopback(address[0]):
            reject(address[0])
        return original_connect_ex(sock, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    return blocked


class BridgeOpenAIChatCompletionClient(OpenAIChatCompletionClient):  # type: ignore[misc]
    async def create(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Sequence[Tool | ToolSchema] = (),
        tool_choice: Tool | Literal["auto", "required", "none"] = "auto",
        json_output: bool | type[BaseModel] | None = None,
        extra_create_args: Mapping[str, Any] = {},
        cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        create_args = dict(extra_create_args)
        if tools:
            create_args["parallel_tool_calls"] = False
        return await super().create(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            json_output=json_output,
            extra_create_args=create_args,
            cancellation_token=cancellation_token,
        )


def _model_client(
    running: RunningContractServer,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> OpenAIChatCompletionClient:
    options: dict[str, Any] = {
        "model": PUBLIC_MODEL,
        "base_url": str(running.client.base_url).rstrip("/"),
        "api_key": CLIENT_TOKEN,
        "model_info": {
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "family": ModelFamily.UNKNOWN,
            "structured_output": True,
        },
        "max_retries": 0,
        "timeout": 3.0,
        "include_name_in_message": False,
    }
    if http_client is not None:
        options["http_client"] = http_client
    return BridgeOpenAIChatCompletionClient(**options)


def _assert_no_sampling_projection(payload: dict[str, Any]) -> None:
    for field in (
        "temperature",
        "top_p",
        "stop",
        "seed",
        "logit_bias",
        "presence_penalty",
        "frequency_penalty",
        "user",
    ):
        assert field not in payload


@pytest.mark.asyncio
async def test_autogen_loopback_guard_rejects_nonloopback_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = _install_loopback_guard(monkeypatch)
    with pytest.raises(OSError, match="non-loopback network is disabled"):
        socket.getaddrinfo("nonloopback.invalid", 443)
    assert blocked == ["nonloopback.invalid"]


@pytest.mark.asyncio
async def test_autogen_model_client_text_stream_and_structured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_framework_versions()
    blocked = _install_loopback_guard(monkeypatch)
    async with contract_server(
        tmp_path,
        responses=[
            completed_text_response("autogen response"),
            completed_text_response('{"temperature_c":30}'),
        ],
        streams=[strict_text_sse("autogen stream")],
    ) as running:
        client = _model_client(running)
        try:
            result = await client.create([UserMessage(content="bounded input", source="user")])
            stream_items = [
                item
                async for item in client.create_stream(
                    [UserMessage(content="bounded stream", source="user")]
                )
            ]
            with pytest.warns(UserWarning, match="PydanticSerializationUnexpectedValue"):
                structured = await client.create(
                    [UserMessage(content="bounded JSON", source="user")],
                    json_output=WeatherAnswer,
                )
        finally:
            await client.close()

    assert result.content == "autogen response"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens == 4
    assert stream_items[:-1] == ["autogen stream"]
    assert isinstance(stream_items[-1], CreateResult)
    assert stream_items[-1].content == "autogen stream"
    assert WeatherAnswer.model_validate_json(structured.content) == WeatherAnswer(temperature_c=30)
    assert running.request_paths == ["/v1/chat/completions"] * 3
    assert len(running.upstream.calls) == 3
    assert [call["stream"] for call in running.upstream.calls] == [False, True, False]
    assert [stream.close_calls for stream in running.upstream.byte_streams] == [1]
    assert blocked == []

    for call in running.upstream.calls:
        assert_server_policy(call, stream=bool(call["stream"]))
        _assert_no_sampling_projection(call)
    assert running.upstream.calls[2]["text"] == {
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


@pytest.mark.asyncio
async def test_autogen_assistant_agent_executes_local_tool_and_reflects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = _install_loopback_guard(monkeypatch)
    async with contract_server(
        tmp_path,
        responses=[
            completed_tool_response(),
            completed_text_response("autogen tool complete"),
        ],
    ) as running:
        request_shapes: list[dict[str, object]] = []

        async def record_request(request: httpx.Request) -> None:
            body = json.loads(request.content)
            request_shapes.append(
                {
                    "fields": sorted(body),
                    "message_fields": [sorted(message) for message in body.get("messages", [])],
                }
            )

        http_client = httpx.AsyncClient(event_hooks={"request": [record_request]})
        client = _model_client(
            running,
            http_client=http_client,
        )
        try:
            agent = AssistantAgent(
                name="bounded_agent",
                model_client=client,
                tools=[weather],
                system_message="Use weather, then give a brief final answer.",
                reflect_on_tool_use=True,
                max_tool_iterations=1,
            )
            task = await agent.run(task="Use weather for Tokyo.")
        finally:
            await client.close()
            await http_client.aclose()

    assert isinstance(task.messages[-1], TextMessage)
    assert task.messages[-1].content == "autogen tool complete"
    requests = [message for message in task.messages if isinstance(message, ToolCallRequestEvent)]
    executions = [
        message for message in task.messages if isinstance(message, ToolCallExecutionEvent)
    ]
    assert len(requests) == 1
    assert len(executions) == 1
    assert requests[0].content[0].id == "call_weather_contract"
    assert requests[0].content[0].name == "weather"
    assert executions[0].content[0].call_id == "call_weather_contract"
    assert executions[0].content[0].content == "bounded weather result"
    assert executions[0].content[0].is_error is False

    assert running.request_paths == ["/v1/chat/completions"] * 2
    assert len(running.upstream.calls) == 2
    assert blocked == []
    assert request_shapes == [
        {
            "fields": [
                "messages",
                "model",
                "parallel_tool_calls",
                "stream",
                "tool_choice",
                "tools",
            ],
            "message_fields": [["content", "role"], ["content", "role"]],
        },
        {
            "fields": ["messages", "model", "stream"],
            "message_fields": [
                ["content", "role"],
                ["content", "role"],
                ["content", "role", "tool_calls"],
                ["content", "role", "tool_call_id"],
            ],
        },
    ]
    for call in running.upstream.calls:
        assert_server_policy(call, stream=False)
        _assert_no_sampling_projection(call)
    assert running.upstream.calls[0]["parallel_tool_calls"] is False
    assert running.upstream.calls[0]["tools"] == [
        {
            "type": "function",
            "name": "weather",
            "description": "Return bounded weather information for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "description": "city",
                        "title": "City",
                        "type": "string",
                    }
                },
                "required": ["city"],
                "additionalProperties": False,
            },
            "strict": False,
        }
    ]
    assert "parallel_tool_calls" not in running.upstream.calls[1]
    assert "tools" not in running.upstream.calls[1]
    assert running.upstream.calls[1]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_weather_contract",
        "output": "bounded weather result",
    }
