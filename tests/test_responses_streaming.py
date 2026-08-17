from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from openai import AsyncOpenAI
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseErrorEvent,
    ResponseOutputItemDoneEvent,
    ResponseTextDeltaEvent,
)

import codex_openai_bridge.app as app_module
from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.responses import (
    ResponsesRequestError,
    parse_responses_request,
    responses_request_to_upstream,
)
from codex_openai_bridge.responses_stream import translate_responses_sse_to_public
from codex_openai_bridge.translation import UpstreamResponseError

TOKEN = "a" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _parse(document: object) -> Any:
    return parse_responses_request(
        document,
        public_model="codex",
        max_items=16,
        max_tools=8,
        max_json_depth=16,
        max_json_nodes=256,
        max_string_bytes=4096,
    )


@pytest.mark.parametrize(("raw", "expected"), [(None, False), (False, False), (True, True)])
def test_stream_is_an_exact_boolean_and_is_preserved_upstream(
    raw: bool | None,
    expected: bool,
) -> None:
    document: dict[str, object] = {"model": "codex", "input": "hello"}
    if raw is not None:
        document["stream"] = raw

    request = _parse(document)
    payload = responses_request_to_upstream(request, upstream_model="server-model")

    assert request.stream is expected
    assert payload == {
        "model": "server-model",
        "input": "hello",
        "store": False,
        "stream": expected,
        "include": ["reasoning.encrypted_content"],
    }


@pytest.mark.parametrize("raw", [0, 1, "true", None, [], {}])
def test_stream_rejects_every_non_boolean_exact_type(raw: object) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse({"model": "codex", "input": "hello", "stream": raw})


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _ByteStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
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


class _StreamingOnlyUpstream:
    def __init__(self, wire: bytes) -> None:
        self.stream = _ByteStream([wire])
        self.stream_calls: list[tuple[Credential, dict[str, Any]]] = []
        self.nonstream_calls = 0

    async def create_stream(self, credential: Credential, payload: dict[str, Any]) -> _ByteStream:
        self.stream_calls.append((credential, payload))
        return self.stream

    async def create_response(self, credential: Credential, payload: dict[str, Any]) -> object:
        del credential, payload
        self.nonstream_calls += 1
        raise AssertionError("stream:true must never use create_response")


class _CredentialManager:
    def __init__(self) -> None:
        self.credential = Credential(
            access_token="upstream-token",
            base_url="https://chatgpt.com/backend-api/codex",
            account_id="account-1",
            expires_at=4_102_444_800,
        )

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        assert force_refresh is False
        return self.credential


def _settings(tmp_path: Path) -> Settings:
    token_file = tmp_path / "client-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    return replace(Settings.from_env(), client_token_file=token_file)


def _event(value: object) -> bytes:
    assert isinstance(value, dict)
    encoded = json.dumps(value, separators=(",", ":")).encode()
    return b"event: " + str(value["type"]).encode() + b"\ndata: " + encoded + b"\n\n"


def _text_events() -> list[dict[str, Any]]:
    added = {
        "id": "msg_stream",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    done = {
        **added,
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }
    completed = {
        "id": "resp_stream",
        "object": "response",
        "created_at": 7,
        "status": "completed",
        "model": "SENSITIVE_UPSTREAM_MODEL",
        "output": [done],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 3,
        },
        "internal_account": "SENSITIVE_INTERNAL_ACCOUNT",
    }
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_stream",
                "created_at": 7,
                "status": "in_progress",
                "model": "SENSITIVE_UPSTREAM_MODEL",
                "internal_account": "SENSITIVE_INTERNAL_ACCOUNT",
            },
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.content_part.added",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "delta": "hello",
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "text": "hello",
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_stream",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "hello", "annotations": []},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {"type": "response.completed", "response": completed},
    ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
    return events


async def _translate(
    events: list[dict[str, Any]],
    *,
    document: dict[str, object] | None = None,
    max_sse_event_bytes: int = 4096,
    max_stream_bytes: int = 65536,
) -> list[bytes]:
    request = _parse(document or {"model": "codex", "input": "hello", "stream": True})
    wire = b"".join(_event(event) for event in events) + b"data: [DONE]\n\n"
    return [
        frame
        async for frame in translate_responses_sse_to_public(
            _chunks(wire),
            request=request,
            public_model="codex",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=max_sse_event_bytes,
            max_stream_bytes=max_stream_bytes,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
        )
    ]


def _decode(frame: bytes) -> tuple[str, dict[str, Any]]:
    assert frame.endswith(b"\n\n")
    event_line, data_line = frame[:-2].split(b"\n")
    return event_line.removeprefix(b"event: ").decode(), json.loads(
        data_line.removeprefix(b"data: ")
    )


@pytest.mark.asyncio
async def test_direct_stream_projects_named_responses_events_and_completed_authority() -> None:
    frames = await _translate(_text_events())

    assert frames[-1] == b"data: [DONE]\n\n"
    decoded = [_decode(frame) for frame in frames[:-1]]
    assert [name for name, _ in decoded] == [event["type"] for event in _text_events()]
    created = decoded[0][1]["response"]
    assert created == {
        "id": "resp_stream",
        "object": "response",
        "created_at": 7,
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "model": "codex",
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": None,
    }
    delta = next(data for name, data in decoded if name == "response.output_text.delta")
    assert delta["logprobs"] == []
    completed = decoded[-1][1]["response"]
    assert completed["model"] == "codex"
    assert completed["output"][0]["content"][0]["text"] == "hello"
    assert "SENSITIVE" not in repr(decoded)
    assert ResponseCreatedEvent.model_validate(decoded[0][1]).response.model == "codex"
    assert ResponseTextDeltaEvent.model_validate(delta).delta == "hello"
    assert ResponseCompletedEvent.model_validate(decoded[-1][1]).response.model == "codex"


@pytest.mark.asyncio
async def test_unknown_nonterminal_is_dropped_without_disturbing_original_order() -> None:
    events = _text_events()
    events.insert(
        1,
        {"type": "response.future.telemetry", "sequence_number": 1, "secret": "SENSITIVE"},
    )
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    frames = await _translate(events)

    assert b"future.telemetry" not in b"".join(frames)
    assert b"SENSITIVE" not in b"".join(frames)


@pytest.mark.asyncio
async def test_every_response_snapshot_is_rebuilt_with_the_public_model() -> None:
    events = _text_events()
    events.insert(
        1,
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_stream",
                "created_at": 7,
                "status": "in_progress",
                "model": "SENSITIVE_SECOND_UPSTREAM_MODEL",
                "metadata": {"secret": "SENSITIVE_METADATA"},
            },
        },
    )
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    decoded = [_decode(frame) for frame in (await _translate(events))[:-1]]
    snapshots = [data["response"] for name, data in decoded if "response" in data]

    assert [snapshot["model"] for snapshot in snapshots] == ["codex", "codex", "codex"]
    assert "SENSITIVE" not in repr(decoded)


@pytest.mark.asyncio
async def test_completed_snapshot_must_exactly_match_streamed_item_authority() -> None:
    events = _text_events()
    response = events[-1]["response"]
    assert isinstance(response, dict)
    output = response["output"]
    assert isinstance(output, list)
    message = output[0]
    assert isinstance(message, dict)
    content = message["content"]
    assert isinstance(content, list)
    part = content[0]
    assert isinstance(part, dict)
    part["text"] = "authority mismatch"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "backward"])
async def test_direct_stream_requires_strict_sequence_authority(mutation: str) -> None:
    events = _text_events()
    if mutation == "missing":
        events[3].pop("sequence_number")
    elif mutation == "duplicate":
        events[3]["sequence_number"] = events[2]["sequence_number"]
    else:
        events[3]["sequence_number"] = 0

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(events)


@pytest.mark.asyncio
async def test_direct_stream_rejects_logprobs_instead_of_exposing_them() -> None:
    events = _text_events()
    events[3]["logprobs"] = [{"token": "SENSITIVE"}]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(events)


@pytest.mark.asyncio
async def test_direct_stream_event_and_cumulative_caps_are_exact() -> None:
    events = _text_events()
    encoded_events = [_event(event) for event in events]
    wire_bytes = sum(len(event) for event in encoded_events) + len(b"data: [DONE]\n\n")
    event_bytes = max(len(event) - 1 for event in encoded_events)

    assert await _translate(
        events,
        max_sse_event_bytes=event_bytes,
        max_stream_bytes=wire_bytes,
    )
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(
            events,
            max_sse_event_bytes=event_bytes - 1,
            max_stream_bytes=wire_bytes,
        )
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(
            events,
            max_sse_event_bytes=event_bytes,
            max_stream_bytes=wire_bytes - 1,
        )


@pytest.mark.asyncio
async def test_unknown_terminal_event_is_rejected_without_exposure() -> None:
    events = _text_events()
    events.insert(
        1,
        {
            "type": "response.future.done",
            "sequence_number": 1,
            "secret": "SENSITIVE_UNKNOWN_TERMINAL",
        },
    )
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    with pytest.raises(UpstreamResponseError) as caught:
        await _translate(events)
    assert "SENSITIVE" not in repr(caught.value)


def _function_events() -> list[dict[str, Any]]:
    added = {
        "id": "fc_stream",
        "type": "function_call",
        "status": "in_progress",
        "call_id": "call_stream",
        "name": "weather",
        "arguments": "",
    }
    done = {**added, "status": "completed", "arguments": '{"city":"Tokyo"}'}
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {"id": "resp_function", "created_at": 9, "status": "in_progress"},
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream",
            "output_index": 0,
            "delta": '{"city":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_stream",
            "output_index": 0,
            "delta": '"Tokyo"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_stream",
            "output_index": 0,
            "arguments": '{"city":"Tokyo"}',
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_function",
                "object": "response",
                "created_at": 9,
                "status": "completed",
                "output": [done],
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 3,
                },
            },
        },
    ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
    return events


def _tool_document() -> dict[str, object]:
    return {
        "model": "codex",
        "input": "hello",
        "stream": True,
        "tools": [
            {
                "type": "function",
                "name": "weather",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }


@pytest.mark.asyncio
async def test_function_events_are_projected_and_request_tool_policy_is_enforced() -> None:
    frames = await _translate(_function_events(), document=_tool_document())
    decoded = [_decode(frame) for frame in frames[:-1]]

    added = next(data for name, data in decoded if name == "response.output_item.added")
    assert added["item"]["name"] == "weather"
    completed = decoded[-1][1]["response"]
    assert completed["output"][0]["call_id"] == "call_stream"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_function_events())


def _reasoning_events(*, encrypted_content: str = "YQ==") -> list[dict[str, Any]]:
    events = _text_events()
    created = events.pop(0)
    completed = events.pop()
    for event in events:
        if "output_index" in event:
            event["output_index"] = 1
    added = {
        "id": "rs_stream",
        "type": "reasoning",
        "status": "in_progress",
        "summary": [],
    }
    done = {
        **added,
        "status": "completed",
        "encrypted_content": encrypted_content,
    }
    response = completed["response"]
    assert isinstance(response, dict)
    response["output"] = [done, *response["output"]]
    result = [
        created,
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        *events,
        completed,
    ]
    for sequence, event in enumerate(result):
        event["sequence_number"] = sequence
    return result


def _reasoning_function_events() -> list[dict[str, Any]]:
    events = _function_events()
    created = events.pop(0)
    completed = events.pop()
    for event in events:
        if "output_index" in event:
            event["output_index"] = 1
    added = {
        "id": "rs_function",
        "type": "reasoning",
        "status": "in_progress",
        "summary": [],
    }
    done = {**added, "status": "completed", "encrypted_content": "YQ=="}
    response = completed["response"]
    assert isinstance(response, dict)
    response["output"] = [done, *response["output"]]
    result = [
        created,
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        *events,
        completed,
    ]
    for sequence, event in enumerate(result):
        event["sequence_number"] = sequence
    return result


@pytest.mark.asyncio
async def test_reasoning_events_preserve_only_canonical_encrypted_content() -> None:
    frames = await _translate(_reasoning_events())
    decoded = [_decode(frame) for frame in frames[:-1]]
    reasoning_done = next(
        data["item"]
        for name, data in decoded
        if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
    )
    assert reasoning_done == {
        "id": "rs_stream",
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "encrypted_content": "YQ==",
    }
    reasoning_event = next(
        data
        for name, data in decoded
        if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
    )
    assert ResponseOutputItemDoneEvent.model_validate(reasoning_event).item.type == "reasoning"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_reasoning_events(encrypted_content="AB"))


@pytest.mark.asyncio
async def test_reasoning_summary_is_strictly_validated_then_stripped() -> None:
    events = _reasoning_events()
    summary = [{"type": "summary_text", "text": "SENSITIVE SUMMARY"}]
    for event in events:
        if event["type"] in {"response.output_item.added", "response.output_item.done"}:
            item = event["item"]
            assert isinstance(item, dict)
            if item["type"] == "reasoning":
                item["summary"] = summary

    frames = await _translate(events)
    decoded = [_decode(frame) for frame in frames[:-1]]
    reasoning_items = [
        data["item"]
        for name, data in decoded
        if name in {"response.output_item.added", "response.output_item.done"}
        and data["item"]["type"] == "reasoning"
    ]
    assert reasoning_items
    assert all(item["summary"] == [] for item in reasoning_items)
    assert b"SENSITIVE SUMMARY" not in b"".join(frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    [
        None,
        "SENSITIVE",
        [{"type": "summary_text", "text": 1}],
        [{"type": "other", "text": "SENSITIVE"}],
        [{"type": "summary_text", "text": "SENSITIVE", "extra": True}],
    ],
)
async def test_malformed_reasoning_summary_fails_closed(summary: object) -> None:
    events = _reasoning_events()
    for event in events:
        if event["type"] in {"response.output_item.added", "response.output_item.done"}:
            item = event["item"]
            assert isinstance(item, dict)
            if item["type"] == "reasoning":
                item["summary"] = summary

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(events)


@pytest.mark.asyncio
async def test_stream_response_cannot_reuse_a_historical_reasoning_item_id() -> None:
    document = {
        "model": "codex",
        "stream": True,
        "input": [
            {
                "id": "rs_stream",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "YQ==",
            },
            {
                "id": "msg_history",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "earlier", "annotations": []}],
            },
            {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
    }

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_reasoning_events(encrypted_content="Yg=="), document=document)


def test_synthesized_stream_fields_are_required_by_the_installed_sdk() -> None:
    assert ResponseTextDeltaEvent.model_fields["logprobs"].is_required()
    assert ResponseErrorEvent.model_fields["sequence_number"].is_required()


@pytest.mark.asyncio
async def test_stream_true_routes_only_to_stream_upstream_and_sdk_gets_final_response(
    tmp_path: Path,
) -> None:
    events = _text_events()
    wire = b"".join(_event(event) for event in events) + b"data: [DONE]\n\n"
    upstream = _StreamingOnlyUpstream(wire)
    manager = _CredentialManager()
    settings = _settings(tmp_path)
    app = create_app(settings, manager, upstream=upstream)

    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key=TOKEN,
            base_url=str(server.make_url("/v1")),
            max_retries=0,
        ) as sdk:
            seen: list[str] = []
            async with sdk.responses.stream(model="codex", input="hello") as stream:
                async for event in stream:
                    seen.append(event.type)
                final_response = await stream.get_final_response()

    assert seen[0] == "response.created"
    assert "response.output_text.delta" in seen
    assert seen[-1] == "response.completed"
    assert final_response.model == "codex"
    assert final_response.output_text == "hello"
    assert upstream.nonstream_calls == 0
    assert upstream.stream_calls == [
        (
            manager.credential,
            {
                "model": settings.upstream_model,
                "input": "hello",
                "store": False,
                "stream": True,
                "include": ["reasoning.encrypted_content"],
            },
        )
    ]
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_sdk_stream_exposes_function_call_and_encrypted_reasoning_events(
    tmp_path: Path,
) -> None:
    events = _reasoning_function_events()
    wire = b"".join(_event(event) for event in events) + b"data: [DONE]\n\n"
    upstream = _StreamingOnlyUpstream(wire)
    app = create_app(_settings(tmp_path), _CredentialManager(), upstream=upstream)

    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key=TOKEN,
            base_url=str(server.make_url("/v1")),
            max_retries=0,
        ) as sdk:
            done_item_types: list[str] = []
            async with sdk.responses.stream(
                model="codex",
                input="hello",
                tools=cast(
                    Any,
                    [
                        {
                            "type": "function",
                            "name": "weather",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                ),
            ) as stream:
                async for event in stream:
                    if event.type == "response.output_item.done":
                        done_item_types.append(event.item.type)
                final_response = await stream.get_final_response()

    assert done_item_types == ["reasoning", "function_call"]
    assert [item.type for item in final_response.output] == ["reasoning", "function_call"]
    assert final_response.output[0].encrypted_content == "YQ=="  # type: ignore[union-attr]
    assert final_response.model == "codex"
    assert upstream.nonstream_calls == 0
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_failure_before_first_public_event_returns_sanitized_json_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = _StreamingOnlyUpstream(
        _event(
            {
                "type": "response.failed",
                "response": {"secret": "SENSITIVE_UPSTREAM_FAILURE"},
                "sequence_number": 0,
            }
        )
    )
    app = create_app(_settings(tmp_path), _CredentialManager(), upstream=upstream)

    async def read_document(_request: object, **_kwargs: object) -> object:
        return {"model": "codex", "input": "hello", "stream": True}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(SimpleNamespace(app=app))  # type: ignore[arg-type]
    assert isinstance(response.body, bytes)

    assert response.status == 502
    assert json.loads(response.body)["error"]["code"] == "upstream_error"
    assert b"SENSITIVE" not in response.body
    assert upstream.nonstream_calls == 0
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_failure_after_first_byte_emits_one_responses_error_without_success_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _text_events()[0]
    upstream = _StreamingOnlyUpstream(
        _event(created) + b'data: {"type":"response.output_item.added"}\n\n'
    )
    app = create_app(_settings(tmp_path), _CredentialManager(), upstream=upstream)
    writes: list[bytes] = []
    prepared_headers: dict[str, str] = {}

    class CollectingResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            assert status == 200
            self.headers = headers

        async def prepare(self, request: object) -> None:
            del request
            prepared_headers.update(self.headers)

        async def write(self, frame: bytes) -> None:
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", CollectingResponse)
    request = _parse({"model": "codex", "input": "hello", "stream": True})

    await app_module._stream_responses_response(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        credential=_CredentialManager().credential,
        payload={"model": "server-model", "stream": True},
        responses_request=request,
        deadline=time.monotonic() + 1,
        request_id="b" * 32,
    )

    body = b"".join(writes)
    assert body.startswith(b"event: response.created\n")
    assert body.count(b"event: error\n") == 1
    assert body.count(b'"code":"upstream_stream_error"') == 1
    assert b"response.completed" not in body
    assert b"data: [DONE]" not in body
    assert prepared_headers["X-Request-ID"] == "b" * 32
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_slow_body_write_uses_terminal_reserve_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.1)
    wire = b"".join(_event(event) for event in _text_events()) + b"data: [DONE]\n\n"
    upstream = _StreamingOnlyUpstream(wire)
    app = create_app(settings, _CredentialManager(), upstream=upstream)
    writes: list[bytes] = []
    write_calls = 0

    class BlockSecondWriteResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            nonlocal write_calls
            write_calls += 1
            if write_calls == 2:
                await asyncio.Event().wait()
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", BlockSecondWriteResponse)
    request = _parse({"model": "codex", "input": "hello", "stream": True})

    await app_module._stream_responses_response(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        credential=_CredentialManager().credential,
        payload={"model": "server-model", "stream": True},
        responses_request=request,
        deadline=time.monotonic() + settings.total_request_deadline_seconds,
    )

    body = b"".join(writes)
    assert body.count(b"event: error\n") == 1
    assert b"data: [DONE]" not in body
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_done_write_failure_never_appends_error_after_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = b"".join(_event(event) for event in _text_events()) + b"data: [DONE]\n\n"
    upstream = _StreamingOnlyUpstream(wire)
    app = create_app(_settings(tmp_path), _CredentialManager(), upstream=upstream)
    writes: list[bytes] = []

    class FailDoneWriteResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            if frame == b"data: [DONE]\n\n":
                raise TimeoutError
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", FailDoneWriteResponse)
    request = _parse({"model": "codex", "input": "hello", "stream": True})

    await app_module._stream_responses_response(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        credential=_CredentialManager().credential,
        payload={"model": "server-model", "stream": True},
        responses_request=request,
        deadline=time.monotonic() + 1,
    )

    body = b"".join(writes)
    assert body.count(b"event: response.completed\n") == 1
    assert b"event: error\n" not in body
    assert b"data: [DONE]" not in body
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_completed_write_crossing_body_deadline_never_appends_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.2)
    wire = b"".join(_event(event) for event in _text_events()) + b"data: [DONE]\n\n"
    upstream = _StreamingOnlyUpstream(wire)
    app = create_app(settings, _CredentialManager(), upstream=upstream)
    writes: list[bytes] = []
    deadline = time.monotonic() + settings.total_request_deadline_seconds
    terminal_reserve = min(0.25, settings.total_request_deadline_seconds / 10)
    body_deadline = deadline - terminal_reserve

    class CrossDeadlineAfterWriteResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            writes.append(frame)
            if frame.startswith(b"event: response.completed\n"):
                time.sleep(max(0.0, body_deadline - time.monotonic() + 0.005))

    monkeypatch.setattr(web, "StreamResponse", CrossDeadlineAfterWriteResponse)
    request = _parse({"model": "codex", "input": "hello", "stream": True})

    await app_module._stream_responses_response(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        credential=_CredentialManager().credential,
        payload={"model": "server-model", "stream": True},
        responses_request=request,
        deadline=deadline,
    )

    body = b"".join(writes)
    assert body.count(b"event: response.completed\n") == 1
    assert b"event: error\n" not in body
    assert b"data: [DONE]" not in body
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_completed_write_raising_before_return_emits_only_error_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = b"".join(_event(event) for event in _text_events()) + b"data: [DONE]\n\n"
    upstream = _StreamingOnlyUpstream(wire)
    app = create_app(_settings(tmp_path), _CredentialManager(), upstream=upstream)
    writes: list[bytes] = []

    class FailCompletedBeforeReturnResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            if frame.startswith(b"event: response.completed\n"):
                raise TimeoutError
            writes.append(frame)

    monkeypatch.setattr(web, "StreamResponse", FailCompletedBeforeReturnResponse)
    request = _parse({"model": "codex", "input": "hello", "stream": True})

    await app_module._stream_responses_response(
        SimpleNamespace(app=app),  # type: ignore[arg-type]
        credential=_CredentialManager().credential,
        payload={"model": "server-model", "stream": True},
        responses_request=request,
        deadline=time.monotonic() + 1,
    )

    body = b"".join(writes)
    assert b"event: response.completed\n" not in body
    assert body.count(b"event: error\n") == 1
    assert body.count(b'"code":"upstream_stream_error"') == 1
    assert b"data: [DONE]" not in body
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_prepare_deadline_returns_sanitized_timeout_before_body_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.1)
    upstream = _StreamingOnlyUpstream(_event(_text_events()[0]))
    app = create_app(settings, _CredentialManager(), upstream=upstream)

    async def read_document(_request: object, **_kwargs: object) -> object:
        return {"model": "codex", "input": "hello", "stream": True}

    class BlockPrepareResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request
            await asyncio.Event().wait()

        async def write(self, frame: bytes) -> None:
            raise AssertionError(f"body must not be written: {frame!r}")

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    monkeypatch.setattr(web, "StreamResponse", BlockPrepareResponse)

    response = await app_module._responses(SimpleNamespace(app=app))  # type: ignore[arg-type]
    assert isinstance(response.body, bytes)

    assert response.status == 504
    assert json.loads(response.body)["error"]["code"] == "upstream_timeout"
    assert upstream.stream.close_calls == 1


@pytest.mark.asyncio
async def test_disconnect_cancellation_closes_once_and_releases_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = _StreamingOnlyUpstream(_event(_text_events()[0]))
    app = create_app(_settings(tmp_path), _CredentialManager(), upstream=upstream)

    class CancelWriteResponse:
        def __init__(self, *, status: int, headers: dict[str, str]) -> None:
            del status, headers

        async def prepare(self, request: object) -> None:
            del request

        async def write(self, frame: bytes) -> None:
            del frame
            raise asyncio.CancelledError

    monkeypatch.setattr(web, "StreamResponse", CancelWriteResponse)
    request = make_mocked_request("POST", "/v1/responses", headers=AUTH, app=app)
    parsed = _parse({"model": "codex", "input": "hello", "stream": True})

    async def handler(stream_request: web.Request) -> web.StreamResponse:
        return await app_module._stream_responses_response(
            stream_request,
            credential=_CredentialManager().credential,
            payload={"model": "server-model", "stream": True},
            responses_request=parsed,
            deadline=time.monotonic() + 1,
        )

    with pytest.raises(asyncio.CancelledError):
        await app_module._admission_middleware(request, handler)

    controller = app[app_module._ADMISSION_KEY]
    assert upstream.stream.close_calls == 1
    assert controller.active_count == 0
    assert controller.waiting_count == 0
