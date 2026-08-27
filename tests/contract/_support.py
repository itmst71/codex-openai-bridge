from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.upstream import BufferedResponsesUpstream

CLIENT_TOKEN = "c" * 43
PUBLIC_MODEL = "codex"
UPSTREAM_MODEL = "server-owned-model"


class StaticCredentialProvider:
    def __init__(self) -> None:
        self.credential = Credential(
            access_token="synthetic-upstream-token",
            base_url="https://chatgpt.com/backend-api/codex",
            account_id="contract-account",
            expires_at=4_102_444_800,
        )
        self.calls: list[bool] = []

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        self.calls.append(force_refresh)
        return self.credential


class FakeResponsesByteStream:
    def __init__(self, wire: bytes) -> None:
        self._chunks: Iterator[bytes] = iter((wire,))
        self.close_calls = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.close_calls += 1


class RecordingUpstream:
    def __init__(
        self,
        *,
        responses: list[object] | None = None,
        streams: list[bytes] | None = None,
    ) -> None:
        self.responses = deque(responses or [])
        self.streams = deque(streams or [])
        self.calls: list[dict[str, Any]] = []
        self.byte_streams: list[FakeResponsesByteStream] = []

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        assert credential.access_token == "synthetic-upstream-token"
        self.calls.append(deepcopy(payload))
        if not self.responses:
            raise AssertionError("unexpected non-streaming upstream call")
        return self.responses.popleft()

    async def create_stream(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> FakeResponsesByteStream:
        assert credential.access_token == "synthetic-upstream-token"
        self.calls.append(deepcopy(payload))
        if not self.streams:
            raise AssertionError("unexpected streaming upstream call")
        stream = FakeResponsesByteStream(self.streams.popleft())
        self.byte_streams.append(stream)
        return stream

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class RunningContractServer:
    client: AsyncOpenAI
    upstream: RecordingUpstream
    settings: Settings
    provider: StaticCredentialProvider
    request_paths: list[str]


@asynccontextmanager
async def contract_server(
    tmp_path: Path,
    *,
    responses: list[object] | None = None,
    streams: list[bytes] | None = None,
    buffered_nonstream: bool = False,
) -> AsyncIterator[RunningContractServer]:
    token_file = tmp_path / "contract-client-token"
    token_file.write_text(CLIENT_TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    continuation_key_file = tmp_path / "contract-continuation-key"
    continuation_key_file.write_text("d" * 43 + "\n", encoding="ascii")
    continuation_key_file.chmod(0o600)
    settings = replace(
        Settings.from_env(),
        client_token_file=token_file,
        continuation_key_file=continuation_key_file,
        public_model=PUBLIC_MODEL,
        upstream_model=UPSTREAM_MODEL,
        model_map=MappingProxyType({PUBLIC_MODEL: UPSTREAM_MODEL}),
        total_request_deadline_seconds=5.0,
    )
    upstream = RecordingUpstream(responses=responses, streams=streams)
    provider = StaticCredentialProvider()
    app_upstream = BufferedResponsesUpstream(upstream, settings) if buffered_nonstream else upstream
    request_paths: list[str] = []

    @web.middleware
    async def record_request_path(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        request_paths.append(request.path)
        return await handler(request)

    app = create_app(settings, provider, upstream=app_upstream)
    app.middlewares.insert(0, record_request_path)
    server = TestServer(app)
    await server.start_server()
    client = AsyncOpenAI(
        api_key=CLIENT_TOKEN,
        base_url=str(server.make_url("/v1/")),
        max_retries=0,
        timeout=3.0,
    )
    try:
        yield RunningContractServer(
            client=client,
            upstream=upstream,
            settings=settings,
            provider=provider,
            request_paths=request_paths,
        )
    finally:
        await client.close()
        await server.close()


def completed_text_response(
    text: str = "hello from the bridge",
    *,
    response_id: str = "resp_contract",
    message_id: str = "msg_contract",
    phase: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": message_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    if phase is not None:
        message["phase"] = phase
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_723_456_789,
        "status": "completed",
        "output": [message],
        "usage": {
            "input_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 4,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 7,
        },
    }


def completed_tool_response(*, include_reasoning: bool = False) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    if include_reasoning:
        output.append(
            {
                "id": "reasoning_contract",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "Y29udHJhY3QtcmVhc29uaW5n",
            }
        )
    output.append(
        {
            "id": "function_contract",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_weather_contract",
            "name": "weather",
            "arguments": '{"city":"Tokyo"}',
        }
    )
    return {
        "id": "resp_tool_contract",
        "object": "response",
        "created_at": 1_723_456_790,
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 7,
        },
    }


def completed_custom_tool_response() -> dict[str, Any]:
    return {
        "id": "resp_custom_contract",
        "object": "response",
        "created_at": 1_723_456_792,
        "status": "completed",
        "output": [
            {
                "id": "ctc_contract",
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_custom_contract",
                "name": "emit_probe",
                "input": "contract probe",
            }
        ],
        "usage": {
            "input_tokens": 3,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1},
            "output_tokens": 4,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 7,
        },
    }


def completed_compaction_response() -> dict[str, Any]:
    return {
        "id": "resp_compaction_contract",
        "object": "response",
        "created_at": 1_723_456_793,
        "status": "completed",
        "output": [
            {
                "id": "cmp_contract_open",
                "type": "compaction",
                "encrypted_content": "Y29tcGFjdC1vcGVu",
            },
            {
                "id": "msg_compaction_contract",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": "compacted response", "annotations": []}
                ],
            },
            {
                "id": "cmp_contract_close",
                "type": "compaction",
                "encrypted_content": "Y29tcGFjdC1jbG9zZQ==",
            },
        ],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 4,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 104,
        },
    }


def strict_custom_tool_sse(
    tool_input: str = "contract probe", *, include_obfuscation: bool = True
) -> bytes:
    added = {
        "id": "ctc_stream_contract",
        "type": "custom_tool_call",
        "status": "in_progress",
        "call_id": "call_custom_stream_contract",
        "name": "emit_probe",
        "input": "",
    }
    done = {**added, "status": "completed", "input": tool_input}
    input_delta: dict[str, Any] = {
        "type": "response.custom_tool_call_input.delta",
        "item_id": "ctc_stream_contract",
        "output_index": 0,
        "delta": tool_input,
    }
    if include_obfuscation:
        input_delta["obfuscation"] = "never-public"
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_custom_stream_contract",
                "created_at": 1_723_456_794,
                "status": "in_progress",
            },
        },
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_custom_stream_contract",
                "created_at": 1_723_456_794,
                "status": "in_progress",
            },
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        input_delta,
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": "ctc_stream_contract",
            "output_index": 0,
            "input": tool_input,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_custom_stream_contract",
                "object": "response",
                "created_at": 1_723_456_794,
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 1},
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 5,
                },
            },
        },
    ]
    frames: list[bytes] = []
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        event_type = str(event["type"]).encode("ascii")
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        frames.append(b"event: " + event_type + b"\ndata: " + data + b"\n\n")
    return b"".join(frames)


def strict_compaction_sse() -> bytes:
    first = {
        "id": "cmp_stream_contract_open",
        "type": "compaction",
        "encrypted_content": "Y29udHJhY3QtY29tcGFjdC1vcGVu",
    }
    message_added = {
        "id": "msg_stream_compaction_contract",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
        "phase": "final_answer",
    }
    message_done = {
        **message_added,
        "status": "completed",
        "content": [{"type": "output_text", "text": "compacted stream", "annotations": []}],
    }
    second = {
        "id": "cmp_stream_contract_close",
        "type": "compaction",
        "encrypted_content": "Y29udHJhY3QtY29tcGFjdC1jbG9zZQ==",
    }
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_stream_compaction_contract",
                "created_at": 1_723_456_795,
                "status": "in_progress",
            },
        },
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_stream_compaction_contract",
                "created_at": 1_723_456_795,
                "status": "in_progress",
            },
        },
        {"type": "response.output_item.added", "output_index": 0, "item": first},
        {"type": "response.output_item.done", "output_index": 0, "item": dict(first)},
        {"type": "response.output_item.added", "output_index": 1, "item": message_added},
        {
            "type": "response.content_part.added",
            "item_id": "msg_stream_compaction_contract",
            "output_index": 1,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_stream_compaction_contract",
            "output_index": 1,
            "content_index": 0,
            "delta": "compacted stream",
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_stream_compaction_contract",
            "output_index": 1,
            "content_index": 0,
            "text": "compacted stream",
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_stream_compaction_contract",
            "output_index": 1,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": "compacted stream",
                "annotations": [],
            },
        },
        {"type": "response.output_item.done", "output_index": 1, "item": message_done},
        {"type": "response.output_item.added", "output_index": 2, "item": second},
        {"type": "response.output_item.done", "output_index": 2, "item": dict(second)},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_stream_compaction_contract",
                "object": "response",
                "created_at": 1_723_456_795,
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 12,
                },
            },
        },
    ]
    frames: list[bytes] = []
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        event_type = str(event["type"]).encode("ascii")
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        frames.append(b"event: " + event_type + b"\ndata: " + data + b"\n\n")
    return b"".join(frames)


def strict_text_sse(text: str = "streamed text", *, cache_write_tokens: int | None = None) -> bytes:
    input_token_details = {"cached_tokens": 0}
    if cache_write_tokens is not None:
        input_token_details["cache_write_tokens"] = cache_write_tokens
    added = {
        "id": "msg_stream_contract",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    done = {
        **added,
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_stream_contract",
                "created_at": 1_723_456_791,
                "status": "in_progress",
            },
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.content_part.added",
            "item_id": "msg_stream_contract",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_stream_contract",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_stream_contract",
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_stream_contract",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_stream_contract",
                "object": "response",
                "created_at": 1_723_456_791,
                "status": "completed",
                "output": [done],
                "usage": {
                    "input_tokens": 2,
                    "input_tokens_details": input_token_details,
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 5,
                },
            },
        },
    ]
    frames: list[bytes] = []
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        event_type = str(event["type"]).encode("ascii")
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        frames.append(b"event: " + event_type + b"\ndata: " + data + b"\n\n")
    frames.append(b"data: [DONE]\n\n")
    return b"".join(frames)


def strict_reasoning_sse(summary_text: str = "bounded reasoning summary") -> bytes:
    events: list[dict[str, Any]] = []
    for raw_frame in strict_text_sse("reasoned response").split(b"\n\n"):
        if not raw_frame or raw_frame == b"data: [DONE]":
            continue
        data_line = next(line for line in raw_frame.splitlines() if line.startswith(b"data: "))
        event = json.loads(data_line.removeprefix(b"data: "))
        if "output_index" in event:
            event["output_index"] += 1
        events.append(event)

    added = {
        "id": "rs_stream_contract",
        "type": "reasoning",
        "status": "in_progress",
        "summary": [{"type": "summary_text", "text": summary_text}],
    }
    done = {
        **added,
        "status": "completed",
        "encrypted_content": "Ym91bmRlZC1yZWFzb25pbmc=",
    }
    completed = events[-1]
    completed_response = completed["response"]
    assert isinstance(completed_response, dict)
    completed_response["output"] = [done, *completed_response["output"]]
    events[1:1] = [
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
    ]

    frames: list[bytes] = []
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        event_type = str(event["type"]).encode("ascii")
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        frames.append(b"event: " + event_type + b"\ndata: " + data + b"\n\n")
    frames.append(b"data: [DONE]\n\n")
    return b"".join(frames)


def assert_server_policy(payload: dict[str, Any], *, stream: bool) -> None:
    assert payload["model"] == UPSTREAM_MODEL
    assert payload["store"] is False
    assert payload["stream"] is stream
    assert payload["include"] == ["reasoning.encrypted_content"]
