from __future__ import annotations

import asyncio
import hashlib
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
    ResponseCustomToolCall,
    ResponseCustomToolCallInputDeltaEvent,
    ResponseCustomToolCallInputDoneEvent,
    ResponseErrorEvent,
    ResponseOutputItemDoneEvent,
    ResponseTextDeltaEvent,
)

import codex_openai_bridge.app as app_module
import codex_openai_bridge.upstream as upstream_module
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
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ],
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
        self.close_calls = 0

    async def create_stream(self, credential: Credential, payload: dict[str, Any]) -> _ByteStream:
        self.stream_calls.append((credential, payload))
        return self.stream

    async def create_response(self, credential: Credential, payload: dict[str, Any]) -> object:
        del credential, payload
        self.nonstream_calls += 1
        raise AssertionError("stream:true must never use create_response")

    async def aclose(self) -> None:
        self.close_calls += 1


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
    continuation_key_file = tmp_path / "continuation-key"
    continuation_key_file.write_text("b" * 43 + "\n", encoding="ascii")
    continuation_key_file.chmod(0o600)
    return replace(
        Settings.from_env(),
        client_token_file=token_file,
        continuation_key_file=continuation_key_file,
    )


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


def _compaction_events() -> list[dict[str, Any]]:
    first_compaction = {
        "id": "cmp_stream_1",
        "type": "compaction",
        "encrypted_content": "Y29tcGFjdC0x",
    }
    message_added = {
        "id": "msg_compaction_stream",
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
        "phase": "final_answer",
    }
    message_done = {
        **message_added,
        "status": "completed",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }
    second_compaction = {
        "id": "cmp_stream_2",
        "type": "compaction",
        "encrypted_content": "Y29tcGFjdC0y",
    }
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_compaction_stream",
                "created_at": 11,
                "status": "in_progress",
                "model": "SENSITIVE_UPSTREAM_MODEL",
                "internal_account": "SENSITIVE_INTERNAL_ACCOUNT",
            },
        },
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_compaction_stream",
                "created_at": 11,
                "status": "in_progress",
                "model": "SENSITIVE_SECOND_UPSTREAM_MODEL",
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": first_compaction,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": dict(first_compaction),
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": message_added,
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_compaction_stream",
            "output_index": 1,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_compaction_stream",
            "output_index": 1,
            "content_index": 0,
            "delta": "hello",
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_compaction_stream",
            "output_index": 1,
            "content_index": 0,
            "text": "hello",
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_compaction_stream",
            "output_index": 1,
            "content_index": 0,
            "part": {"type": "output_text", "text": "hello", "annotations": []},
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": message_done,
        },
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": second_compaction,
        },
        {
            "type": "response.output_item.done",
            "output_index": 2,
            "item": dict(second_compaction),
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_compaction_stream",
                "object": "response",
                "created_at": 11,
                "status": "completed",
                "model": "SENSITIVE_TERMINAL_UPSTREAM_MODEL",
                "output": [],
                "usage": {
                    "input_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 3,
                },
                "internal_account": "SENSITIVE_TERMINAL_ACCOUNT",
            },
        },
    ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
    return events


def _compaction_document(*, stream: bool) -> dict[str, object]:
    return {
        "model": "codex",
        "input": "hello",
        "stream": stream,
        "context_management": [{"type": "compaction", "compact_threshold": 1024}],
    }


async def _translate(
    events: list[dict[str, Any]],
    *,
    document: dict[str, object] | None = None,
    max_sse_event_bytes: int = 4096,
    max_stream_bytes: int = 65536,
    upstream_done: bool = True,
) -> list[bytes]:
    request = _parse(document or {"model": "codex", "input": "hello", "stream": True})
    wire = b"".join(_event(event) for event in events)
    if upstream_done:
        wire += b"data: [DONE]\n\n"
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
async def test_direct_stream_completed_eof_generates_sdk_done_marker() -> None:
    frames = await _translate(_text_events(), upstream_done=False)

    assert frames[-1] == b"data: [DONE]\n\n"
    name, completed = _decode(frames[-2])
    assert name == "response.completed"
    assert completed["response"]["status"] == "completed"


@pytest.mark.asyncio
async def test_direct_stream_projects_native_compaction_lifecycle_in_live_order() -> None:
    frames = await _translate(
        _compaction_events(),
        document=_compaction_document(stream=True),
        upstream_done=False,
    )

    assert frames[-1] == b"data: [DONE]\n\n"
    decoded = [_decode(frame) for frame in frames[:-1]]
    assert [name for name, _ in decoded] == [event["type"] for event in _compaction_events()]
    streamed_items = [
        data["item"]
        for name, data in decoded
        if name in {"response.output_item.added", "response.output_item.done"}
    ]
    assert [item["type"] for item in streamed_items] == [
        "compaction",
        "compaction",
        "message",
        "message",
        "compaction",
        "compaction",
    ]
    assert (
        streamed_items[0]
        == streamed_items[1]
        == {
            "id": "cmp_stream_1",
            "type": "compaction",
            "encrypted_content": "Y29tcGFjdC0x",
        }
    )
    assert (
        streamed_items[4]
        == streamed_items[5]
        == {
            "id": "cmp_stream_2",
            "type": "compaction",
            "encrypted_content": "Y29tcGFjdC0y",
        }
    )
    completed = decoded[-1][1]["response"]
    assert completed["model"] == "codex"
    assert [item["type"] for item in completed["output"]] == [
        "compaction",
        "message",
        "compaction",
    ]
    assert completed["output"] == [streamed_items[1], streamed_items[3], streamed_items[5]]
    assert "SENSITIVE" not in repr(decoded)

    replayed_events = _compaction_events()
    terminal = replayed_events[-1]["response"]
    assert isinstance(terminal, dict)
    terminal["output"] = [
        dict(replayed_events[3]["item"]),
        dict(replayed_events[9]["item"]),
        dict(replayed_events[11]["item"]),
    ]
    replayed_frames = await _translate(
        replayed_events,
        document=_compaction_document(stream=True),
        upstream_done=False,
    )
    _, replayed_completed = _decode(replayed_frames[-2])
    assert replayed_completed["response"]["output"] == completed["output"]


@pytest.mark.asyncio
async def test_direct_stream_compaction_and_message_ids_are_model_alias_bound() -> None:
    document = _compaction_document(stream=True)
    document["model"] = "codex-sol"
    request = parse_responses_request(
        document,
        public_model=("codex", "codex-sol"),
        max_items=16,
        max_tools=8,
        max_json_depth=16,
        max_json_nodes=256,
        max_string_bytes=4096,
        binding_key=TOKEN,
    )
    wire = b"".join(_event(event) for event in _compaction_events())
    frames = [
        frame
        async for frame in translate_responses_sse_to_public(
            _chunks(wire),
            request=request,
            public_model="codex-sol",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
            binding_key=TOKEN,
            model_scoped=True,
        )
    ]
    decoded = [_decode(frame) for frame in frames[:-1]]
    items = [
        data["item"]
        for name, data in decoded
        if name in {"response.output_item.added", "response.output_item.done"}
    ]
    message_id = items[2]["id"]
    text_item_ids = [
        data["item_id"]
        for name, data in decoded
        if name
        in {
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
        }
    ]
    completed = next(data["response"] for name, data in decoded if name == "response.completed")

    assert all(item["id"].startswith("cobr_c1_") for item in items)
    assert items[0]["encrypted_content"].startswith("cobr_s1_")
    assert items[4]["encrypted_content"].startswith("cobr_s1_")
    assert items[0] == items[1]
    assert items[2]["id"] == items[3]["id"]
    assert items[4] == items[5]
    assert text_item_ids == [message_id, message_id, message_id, message_id]
    assert completed["output"] == [items[1], items[3], items[5]]
    serialized = b"".join(frames).decode()
    for raw in (
        "cmp_stream_1",
        "cmp_stream_2",
        "msg_compaction_stream",
        "Y29tcGFjdC0x",
        "Y29tcGFjdC0y",
    ):
        assert raw not in serialized


@pytest.mark.asyncio
async def test_nonstream_transport_uses_one_stream_request_and_collects_completed_response(
    tmp_path: Path,
) -> None:
    wire = b"".join(_event(event) for event in _text_events())
    transport = _StreamingOnlyUpstream(wire)
    adapter = upstream_module.BufferedResponsesUpstream(transport, _settings(tmp_path))
    credential = _CredentialManager().credential
    payload = {
        "model": "server-model",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
    }

    response = await adapter.create_response(credential, payload)

    assert isinstance(response, dict)
    assert response["status"] == "completed"
    assert response["output"][0]["content"][0]["text"] == "hello"
    assert transport.nonstream_calls == 0
    assert transport.stream_calls == [
        (
            credential,
            {
                **payload,
                "stream": True,
                "store": False,
                "include": ["reasoning.encrypted_content"],
            },
        )
    ]
    assert transport.stream.close_calls == 1


@pytest.mark.asyncio
async def test_buffered_nonstream_reconstructs_native_compaction_done_items(
    tmp_path: Path,
) -> None:
    wire = b"".join(_event(event) for event in _compaction_events())
    transport = _StreamingOnlyUpstream(wire)
    adapter = upstream_module.BufferedResponsesUpstream(transport, _settings(tmp_path))
    credential = _CredentialManager().credential
    request = _parse(_compaction_document(stream=False))
    payload = responses_request_to_upstream(request, upstream_model="server-model")

    response = await adapter.create_response(credential, payload)

    assert isinstance(response, dict)
    assert [item["type"] for item in response["output"]] == [
        "compaction",
        "message",
        "compaction",
    ]
    assert response["output"][0] == {
        "id": "cmp_stream_1",
        "type": "compaction",
        "encrypted_content": "Y29tcGFjdC0x",
    }
    assert response["output"][2] == {
        "id": "cmp_stream_2",
        "type": "compaction",
        "encrypted_content": "Y29tcGFjdC0y",
    }
    assert transport.nonstream_calls == 0
    assert transport.stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_case",
    [
        "without_context_management",
        "missing_in_progress_setup",
        "missing_final_answer_phase",
        "consecutive_compactions",
        "duplicate_item_id",
        "duplicate_encrypted_digest",
        "historical_item_id",
        "historical_reasoning_digest",
        "extra_plaintext_field",
        "malformed_encrypted_content",
        "oversized_item_id",
        "oversized_encrypted_content",
        "wrong_output_index",
        "done_before_added",
        "missing_done",
        "missing_closing_checkpoint",
        "compaction_only",
        "added_done_mismatch",
        "terminal_output_conflict",
    ],
)
async def test_direct_compaction_stream_fails_closed_outside_live_authority(
    invalid_case: str,
) -> None:
    events = _compaction_events()
    document = _compaction_document(stream=True)
    if invalid_case == "without_context_management":
        document.pop("context_management")
    elif invalid_case == "missing_in_progress_setup":
        events.pop(1)
    elif invalid_case == "missing_final_answer_phase":
        for index in (4, 9):
            item = events[index]["item"]
            assert isinstance(item, dict)
            item.pop("phase")
    elif invalid_case == "consecutive_compactions":
        second_added = events.pop(10)
        second_done = events.pop(10)
        for event in events[4:10]:
            if "output_index" in event:
                event["output_index"] = 2
        second_added["output_index"] = 1
        second_done["output_index"] = 1
        events[4:4] = [second_added, second_done]
    elif invalid_case in {"duplicate_item_id", "duplicate_encrypted_digest"}:
        first = events[2]["item"]
        second_added = events[10]["item"]
        second_done = events[11]["item"]
        assert isinstance(first, dict)
        assert isinstance(second_added, dict)
        assert isinstance(second_done, dict)
        field = "id" if invalid_case == "duplicate_item_id" else "encrypted_content"
        second_added[field] = first[field]
        second_done[field] = first[field]
    elif invalid_case in {"historical_item_id", "historical_reasoning_digest"}:
        historical_id = "cmp_stream_1" if invalid_case == "historical_item_id" else "rs_history"
        historical_content = (
            "aGlzdG9yaWNhbA==" if invalid_case == "historical_item_id" else "Y29tcGFjdC0x"
        )
        document["input"] = [
            {
                "id": historical_id,
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": historical_content,
            },
            {
                "id": "msg_history",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "earlier", "annotations": []}],
            },
            {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ]
    elif invalid_case in {"extra_plaintext_field", "malformed_encrypted_content"}:
        for index in (2, 3):
            item = events[index]["item"]
            assert isinstance(item, dict)
            if invalid_case == "extra_plaintext_field":
                item["summary"] = "SENSITIVE PLAINTEXT"
            else:
                item["encrypted_content"] = "not base64!"
    elif invalid_case in {"oversized_item_id", "oversized_encrypted_content"}:
        field = "id" if invalid_case == "oversized_item_id" else "encrypted_content"
        value = "x" * (129 if invalid_case == "oversized_item_id" else 4097)
        for index in (2, 3):
            item = events[index]["item"]
            assert isinstance(item, dict)
            item[field] = value
    elif invalid_case == "wrong_output_index":
        events[10]["output_index"] = 3
        events[11]["output_index"] = 3
    elif invalid_case == "done_before_added":
        events[2], events[3] = events[3], events[2]
    elif invalid_case == "missing_done":
        events.pop(3)
    elif invalid_case == "missing_closing_checkpoint":
        events = [*events[:10], events[-1]]
    elif invalid_case == "compaction_only":
        events = [*events[:4], events[-1]]
    elif invalid_case == "added_done_mismatch":
        item = events[3]["item"]
        assert isinstance(item, dict)
        item["encrypted_content"] = "bWlzbWF0Y2g="
    else:
        response = events[-1]["response"]
        assert isinstance(response, dict)
        conflicting = [
            dict(events[3]["item"]),
            dict(events[9]["item"]),
            dict(events[11]["item"]),
        ]
        conflicting[2]["encrypted_content"] = "Y29uZmxpY3Q="
        response["output"] = conflicting
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$") as caught:
        await _translate(events, document=document, upstream_done=False)

    assert "SENSITIVE" not in str(caught.value)


@pytest.mark.parametrize("invalid_metadata", ["nonempty_logprobs", "negative_cache_write_tokens"])
@pytest.mark.asyncio
async def test_buffered_nonstream_rejects_malformed_live_metadata(
    invalid_metadata: str,
    tmp_path: Path,
) -> None:
    events = _text_events()
    if invalid_metadata == "nonempty_logprobs":
        delta = next(event for event in events if event["type"] == "response.output_text.delta")
        delta["logprobs"] = [{"token": "private"}]
    else:
        completed = next(event for event in events if event["type"] == "response.completed")
        response = completed["response"]
        assert isinstance(response, dict)
        usage = response["usage"]
        assert isinstance(usage, dict)
        input_details = usage["input_tokens_details"]
        assert isinstance(input_details, dict)
        input_details["cache_write_tokens"] = -1
    transport = _StreamingOnlyUpstream(b"".join(_event(event) for event in events))
    adapter = upstream_module.BufferedResponsesUpstream(transport, _settings(tmp_path))

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await adapter.create_response(
            _CredentialManager().credential,
            {"model": "server-model", "input": [], "store": False, "stream": False},
        )

    assert transport.stream.close_calls == 1


@pytest.mark.asyncio
async def test_direct_live_codex_metadata_preserves_phase_and_confirmed_usage() -> None:
    events = _text_events()
    for event in events:
        if event["type"] in {"response.output_item.added", "response.output_item.done"}:
            item = event["item"]
            assert isinstance(item, dict)
            item["phase"] = "final_answer"
            for part in item["content"]:
                part["logprobs"] = []
        if event["type"] in {"response.content_part.added", "response.content_part.done"}:
            part = event["part"]
            assert isinstance(part, dict)
            part["logprobs"] = []
        if event["type"] == "response.output_text.delta":
            event["obfuscation"] = "bounded-padding"
        if event["type"] == "response.completed":
            response = event["response"]
            assert isinstance(response, dict)
            response["output"] = []
            usage = response["usage"]
            assert isinstance(usage, dict)
            input_details = usage["input_tokens_details"]
            assert isinstance(input_details, dict)
            input_details["cache_write_tokens"] = 0

    frames = await _translate(events, upstream_done=False)

    serialized = b"".join(frames)
    assert b'"phase":"final_answer"' in serialized
    assert b"obfuscation" not in serialized
    assert b"bounded-padding" not in serialized
    _, completed = _decode(frames[-2])
    assert completed["response"]["output"][0]["content"][0]["text"] == "hello"
    assert completed["response"]["output"][0]["phase"] == "final_answer"
    assert completed["response"]["usage"]["input_tokens_details"] == {
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }


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


@pytest.mark.asyncio
async def test_direct_stream_function_call_id_is_model_alias_bound() -> None:
    document = _tool_document()
    document["model"] = "codex-sol"
    request = parse_responses_request(
        document,
        public_model=("codex", "codex-sol"),
        max_items=16,
        max_tools=8,
        max_json_depth=16,
        max_json_nodes=256,
        max_string_bytes=4096,
        binding_key=TOKEN,
    )
    wire = b"".join(_event(event) for event in _function_events()) + b"data: [DONE]\n\n"
    frames = [
        frame
        async for frame in translate_responses_sse_to_public(
            _chunks(wire),
            request=request,
            public_model="codex-sol",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
            binding_key=TOKEN,
            model_scoped=True,
        )
    ]
    decoded = [_decode(frame) for frame in frames[:-1]]
    added = next(data for name, data in decoded if name == "response.output_item.added")
    done = next(data for name, data in decoded if name == "response.output_item.done")
    completed = next(data for name, data in decoded if name == "response.completed")
    public_call_id = added["item"]["call_id"]
    public_item_id = added["item"]["id"]
    argument_item_ids = [
        data["item_id"]
        for name, data in decoded
        if name
        in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }
    ]

    assert public_call_id.startswith("cobr_c1_")
    assert public_item_id.startswith("cobr_c1_")
    assert argument_item_ids == [public_item_id, public_item_id, public_item_id]
    assert done["item"]["id"] == public_item_id
    assert done["item"]["call_id"] == public_call_id
    assert completed["response"]["output"][0]["id"] == public_item_id
    assert completed["response"]["output"][0]["call_id"] == public_call_id
    assert "call_stream" not in b"".join(frames).decode()
    assert "fc_stream" not in b"".join(frames).decode()


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


def _custom_tool_document(*, input_value: object = "hello") -> dict[str, object]:
    return {
        "model": "codex",
        "input": input_value,
        "stream": True,
        "tools": [{"type": "custom", "name": "emit_probe"}],
        "tool_choice": {"type": "custom", "name": "emit_probe"},
        "parallel_tool_calls": False,
    }


def _custom_tool_events(
    *,
    item_id: str | None = "ctc_stream",
    call_id: str | None = "call_stream",
) -> list[dict[str, Any]]:
    added = {
        "id": item_id,
        "type": "custom_tool_call",
        "status": "in_progress",
        "call_id": call_id,
        "name": "emit_probe",
        "input": "",
    }
    done = {**added, "status": "completed", "input": '{"city":"Tokyo"}'}
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {
                "id": "resp_custom",
                "created_at": 13,
                "status": "in_progress",
                "model": "SENSITIVE_UPSTREAM_MODEL",
            },
        },
        {
            "type": "response.in_progress",
            "response": {
                "id": "resp_custom",
                "created_at": 13,
                "status": "in_progress",
                "model": "SENSITIVE_SECOND_UPSTREAM_MODEL",
            },
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.custom_tool_call_input.delta",
            "output_index": 0,
            "item_id": item_id,
            "delta": '{"city":"Tokyo"}',
            "obfuscation": "SENSITIVE_OBFUSCATION",
        },
        {
            "type": "response.custom_tool_call_input.done",
            "output_index": 0,
            "item_id": item_id,
            "input": '{"city":"Tokyo"}',
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_custom",
                "object": "response",
                "created_at": 13,
                "status": "completed",
                "model": "SENSITIVE_TERMINAL_UPSTREAM_MODEL",
                "output": [],
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


@pytest.mark.asyncio
async def test_custom_tool_stream_projects_exact_live_lifecycle_and_restores_output() -> None:
    frames = await _translate(
        _custom_tool_events(),
        document=_custom_tool_document(),
        upstream_done=False,
    )

    assert frames[-1] == b"data: [DONE]\n\n"
    decoded = [_decode(frame) for frame in frames[:-1]]
    assert [name for name, _ in decoded] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
        "response.output_item.done",
        "response.completed",
    ]
    created = decoded[0][1]
    added = decoded[2][1]["item"]
    delta = decoded[3][1]
    input_done = decoded[4][1]
    done = decoded[5][1]["item"]
    expected_added = {
        "id": "ctc_stream",
        "type": "custom_tool_call",
        "call_id": "call_stream",
        "name": "emit_probe",
        "input": "",
    }
    expected_done = {**expected_added, "input": '{"city":"Tokyo"}'}
    assert created["response"]["tools"] == [{"type": "custom", "name": "emit_probe"}]
    assert created["response"]["tool_choice"] == {
        "type": "custom",
        "name": "emit_probe",
    }
    assert created["response"]["parallel_tool_calls"] is False
    assert added == expected_added
    assert delta == {
        "type": "response.custom_tool_call_input.delta",
        "sequence_number": 3,
        "output_index": 0,
        "item_id": "ctc_stream",
        "delta": '{"city":"Tokyo"}',
    }
    assert input_done == {
        "type": "response.custom_tool_call_input.done",
        "sequence_number": 4,
        "output_index": 0,
        "item_id": "ctc_stream",
        "input": '{"city":"Tokyo"}',
    }
    assert done == expected_done
    completed = decoded[-1][1]["response"]
    assert completed["output"] == [expected_done]
    assert "SENSITIVE" not in repr(decoded)

    ResponseCustomToolCall.model_validate(added)
    ResponseCustomToolCallInputDeltaEvent.model_validate(delta)
    ResponseCustomToolCallInputDoneEvent.model_validate(input_done)
    ResponseCustomToolCall.model_validate(done)
    ResponseCompletedEvent.model_validate(decoded[-1][1])
    ResponseOutputItemDoneEvent.model_validate(decoded[5][1])
    ResponseCreatedEvent.model_validate(created)


@pytest.mark.asyncio
async def test_direct_stream_custom_tool_ids_are_model_alias_bound() -> None:
    document = _custom_tool_document()
    document["model"] = "codex-sol"
    request = parse_responses_request(
        document,
        public_model=("codex", "codex-sol"),
        max_items=16,
        max_tools=8,
        max_json_depth=16,
        max_json_nodes=256,
        max_string_bytes=4096,
        binding_key=TOKEN,
    )
    wire = b"".join(_event(event) for event in _custom_tool_events())
    frames = [
        frame
        async for frame in translate_responses_sse_to_public(
            _chunks(wire),
            request=request,
            public_model="codex-sol",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
            binding_key=TOKEN,
            model_scoped=True,
        )
    ]
    decoded = [_decode(frame) for frame in frames[:-1]]
    added = next(data["item"] for name, data in decoded if name == "response.output_item.added")
    done = next(data["item"] for name, data in decoded if name == "response.output_item.done")
    input_item_ids = [
        data["item_id"]
        for name, data in decoded
        if name
        in {
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
        }
    ]
    completed = next(data["response"] for name, data in decoded if name == "response.completed")

    assert added["id"].startswith("cobr_c1_")
    assert added["call_id"].startswith("cobr_c1_")
    assert input_item_ids == [added["id"], added["id"]]
    assert done["id"] == added["id"]
    assert done["call_id"] == added["call_id"]
    assert completed["output"] == [done]
    assert "ctc_stream" not in b"".join(frames).decode()
    assert "call_stream" not in b"".join(frames).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_case",
    [
        "wrong_event_order",
        "missing_in_progress",
        "wrong_output_index",
        "wrong_item_id",
        "wrong_done_item_id",
        "wrong_done_call_id",
        "wrong_name",
        "wrong_added_status",
        "wrong_added_input",
        "extra_added_field",
        "input_done_mismatch",
        "done_input_mismatch",
        "oversized_delta",
        "invalid_obfuscation",
        "extra_delta_field",
        "extra_input_done_field",
        "extra_done_field",
        "mixed_message",
        "mixed_function",
        "mixed_compaction",
        "unknown_nonterminal_event",
        "nonempty_completed_output",
        "contradictory_completed_output",
        "duplicate_history_item_id",
        "duplicate_history_call_id",
    ],
)
async def test_custom_tool_stream_rejects_malformed_lifecycle(invalid_case: str) -> None:
    events = _custom_tool_events()
    document = _custom_tool_document()
    if invalid_case == "wrong_event_order":
        events[3], events[4] = events[4], events[3]
    elif invalid_case == "missing_in_progress":
        events.pop(1)
    elif invalid_case == "wrong_output_index":
        events[3]["output_index"] = 1
    elif invalid_case == "wrong_item_id":
        events[3]["item_id"] = "ctc_other"
    elif invalid_case in {"wrong_done_item_id", "wrong_done_call_id"}:
        item = events[5]["item"]
        assert isinstance(item, dict)
        field = "id" if invalid_case == "wrong_done_item_id" else "call_id"
        item[field] = "wrong_identity"
    elif invalid_case == "wrong_name":
        for index in (2, 5):
            item = events[index]["item"]
            assert isinstance(item, dict)
            item["name"] = "other"
    elif invalid_case == "wrong_added_status":
        item = events[2]["item"]
        assert isinstance(item, dict)
        item["status"] = "completed"
    elif invalid_case == "wrong_added_input":
        item = events[2]["item"]
        assert isinstance(item, dict)
        item["input"] = "premature"
    elif invalid_case == "extra_added_field":
        item = events[2]["item"]
        assert isinstance(item, dict)
        item["extra"] = "SENSITIVE_EXTRA"
    elif invalid_case == "input_done_mismatch":
        events[4]["input"] = "mismatch"
    elif invalid_case == "done_input_mismatch":
        item = events[5]["item"]
        assert isinstance(item, dict)
        item["input"] = "mismatch"
    elif invalid_case == "oversized_delta":
        oversized = "x" * 4097
        events[3]["delta"] = oversized
        events[4]["input"] = oversized
        item = events[5]["item"]
        assert isinstance(item, dict)
        item["input"] = oversized
    elif invalid_case == "invalid_obfuscation":
        events[3]["obfuscation"] = 1
    elif invalid_case == "extra_delta_field":
        events[3]["extra"] = "SENSITIVE_EXTRA"
    elif invalid_case == "extra_input_done_field":
        events[4]["extra"] = "SENSITIVE_EXTRA"
    elif invalid_case == "extra_done_field":
        item = events[5]["item"]
        assert isinstance(item, dict)
        item["extra"] = "SENSITIVE_EXTRA"
    elif invalid_case == "mixed_message":
        events.insert(
            2,
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "msg_mixed",
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
        )
        for event in events[3:7]:
            if "output_index" in event:
                event["output_index"] = 1
    elif invalid_case == "mixed_function":
        item = events[2]["item"]
        assert isinstance(item, dict)
        item.clear()
        item.update(
            {
                "id": "fc_mixed",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_mixed",
                "name": "emit_probe",
                "arguments": "",
            }
        )
    elif invalid_case == "mixed_compaction":
        item = events[2]["item"]
        assert isinstance(item, dict)
        item.clear()
        item.update(
            {
                "id": "cmp_mixed",
                "type": "compaction",
                "encrypted_content": "bWl4ZWQ=",
            }
        )
    elif invalid_case == "unknown_nonterminal_event":
        events.insert(3, {"type": "response.future.delta", "secret": "SENSITIVE_UNKNOWN"})
    elif invalid_case in {"nonempty_completed_output", "contradictory_completed_output"}:
        response = events[-1]["response"]
        done_item = events[5]["item"]
        assert isinstance(response, dict)
        assert isinstance(done_item, dict)
        response["output"] = [dict(done_item)]
        if invalid_case == "contradictory_completed_output":
            response["output"][0]["input"] = "contradiction"
    elif invalid_case == "duplicate_history_item_id":
        document["input"] = [
            {
                "id": "ctc_stream",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "aGlzdG9yeQ==",
            },
            {"role": "user", "content": "continue"},
        ]
    else:
        document["input"] = [
            {
                "id": "ctc_history",
                "type": "custom_tool_call",
                "call_id": "call_stream",
                "name": "emit_probe",
                "input": "old input",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_stream",
                "output": "old output",
            },
            {"role": "user", "content": "continue"},
        ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$") as caught:
        await _translate(events, document=document, upstream_done=False)
    assert "SENSITIVE" not in repr(caught.value)


def _synthesized_custom_ids(*, response_id: str, output_index: int) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"codex-openai-bridge:custom-tool-call\0{response_id}\0{output_index}".encode()
    ).hexdigest()
    return f"ctc_sha256_{digest}", f"call_sha256_{digest}"


@pytest.mark.asyncio
async def test_custom_tool_stream_synthesizes_stable_ids_for_exact_both_null_variant() -> None:
    first_frames = await _translate(
        _custom_tool_events(item_id=None, call_id=None),
        document=_custom_tool_document(),
        upstream_done=False,
    )
    second_frames = await _translate(
        _custom_tool_events(item_id=None, call_id=None),
        document=_custom_tool_document(),
        upstream_done=False,
    )

    assert first_frames == second_frames
    decoded = [_decode(frame) for frame in first_frames[:-1]]
    item_id, call_id = _synthesized_custom_ids(response_id="resp_custom", output_index=0)
    added = decoded[2][1]["item"]
    delta = decoded[3][1]
    input_done = decoded[4][1]
    done = decoded[5][1]["item"]
    completed = decoded[6][1]["response"]
    assert added == {
        "id": item_id,
        "type": "custom_tool_call",
        "call_id": call_id,
        "name": "emit_probe",
        "input": "",
    }
    assert done == {**added, "input": '{"city":"Tokyo"}'}
    assert delta["item_id"] == input_done["item_id"] == item_id
    assert completed["output"] == [done]
    assert None not in (added["id"], added["call_id"], delta["item_id"], input_done["item_id"])
    ResponseCustomToolCall.model_validate(done)
    ResponseCompletedEvent.model_validate(decoded[6][1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_case",
    [
        "partial_null_item_id",
        "partial_null_call_id",
        "done_switches_to_nonnull",
        "done_switches_to_null",
        "nullable_with_nonnull_delta_item_id",
        "nonnull_with_null_delta_item_id",
        "nullable_with_nonnull_done_item_id",
        "nullable_synthesized_item_collision",
        "nullable_synthesized_call_collision",
    ],
)
async def test_custom_tool_stream_rejects_partial_mixed_or_colliding_nullable_identity(
    invalid_case: str,
) -> None:
    nullable = invalid_case not in {"done_switches_to_null", "nonnull_with_null_delta_item_id"}
    events = _custom_tool_events(
        item_id=None if nullable else "ctc_stream",
        call_id=None if nullable else "call_stream",
    )
    document = _custom_tool_document()
    if invalid_case == "partial_null_item_id":
        for index in (2, 5):
            item = events[index]["item"]
            assert isinstance(item, dict)
            item["call_id"] = "call_stream"
    elif invalid_case == "partial_null_call_id":
        for index in (2, 5):
            item = events[index]["item"]
            assert isinstance(item, dict)
            item["id"] = "ctc_stream"
        events[3]["item_id"] = "ctc_stream"
        events[4]["item_id"] = "ctc_stream"
    elif invalid_case == "done_switches_to_nonnull":
        item = events[5]["item"]
        assert isinstance(item, dict)
        item["id"] = "ctc_stream"
        item["call_id"] = "call_stream"
    elif invalid_case == "done_switches_to_null":
        item = events[5]["item"]
        assert isinstance(item, dict)
        item["id"] = None
        item["call_id"] = None
    elif invalid_case == "nullable_with_nonnull_delta_item_id":
        events[3]["item_id"] = "ctc_stream"
    elif invalid_case == "nonnull_with_null_delta_item_id":
        events[3]["item_id"] = None
    elif invalid_case == "nullable_with_nonnull_done_item_id":
        events[4]["item_id"] = "ctc_stream"
    else:
        item_id, call_id = _synthesized_custom_ids(response_id="resp_custom", output_index=0)
        if invalid_case == "nullable_synthesized_item_collision":
            document["input"] = [
                {
                    "id": item_id,
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [],
                    "encrypted_content": "aGlzdG9yeQ==",
                },
                {"role": "user", "content": "continue"},
            ]
        else:
            document["input"] = [
                {
                    "id": "ctc_history",
                    "type": "custom_tool_call",
                    "call_id": call_id,
                    "name": "emit_probe",
                    "input": "old input",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": call_id,
                    "output": "old output",
                },
                {"role": "user", "content": "continue"},
            ]
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(events, document=document, upstream_done=False)


@pytest.mark.asyncio
async def test_statusless_provider_reasoning_may_precede_custom_tool_stream() -> None:
    events = _custom_tool_events()
    reasoning_added = {
        "id": "rs_before_custom",
        "type": "reasoning",
        "summary": [],
        "content": [],
        "encrypted_content": "dHJhbnNpZW50",
    }
    reasoning_done = {
        **reasoning_added,
        "encrypted_content": "Y2Fub25pY2Fs",
    }
    events[2:2] = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": reasoning_added,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": reasoning_done,
        },
    ]
    for event in events[4:8]:
        if "output_index" in event:
            event["output_index"] = 1
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence

    frames = await _translate(
        events,
        document=_custom_tool_document(),
        upstream_done=False,
    )

    decoded = [_decode(frame) for frame in frames[:-1]]
    completed = decoded[-1][1]["response"]
    assert [item["type"] for item in completed["output"]] == [
        "reasoning",
        "custom_tool_call",
    ]
    assert completed["output"][0] == {
        "id": "rs_before_custom",
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "encrypted_content": "Y2Fub25pY2Fs",
    }
    assert completed["output"][1]["input"] == '{"city":"Tokyo"}'
    assert "dHJhbnNpZW50" not in repr(decoded)


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


def _reasoning_events(
    *,
    encrypted_content: str = "YQ==",
    provider_statusless: bool = False,
) -> list[dict[str, Any]]:
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
    if provider_statusless:
        added.pop("status")
        added["content"] = []
        added["encrypted_content"] = "YWRkZWQtdHJhbnNpZW50"
        done.pop("status")
        done["content"] = []
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
async def test_direct_stream_reasoning_state_is_model_alias_bound() -> None:
    document = {"model": "codex-sol", "input": "hello", "stream": True}
    request = parse_responses_request(
        document,
        public_model=("codex", "codex-sol"),
        max_items=16,
        max_tools=8,
        max_json_depth=16,
        max_json_nodes=256,
        max_string_bytes=4096,
        binding_key=TOKEN,
    )
    wire = b"".join(_event(event) for event in _reasoning_events()) + b"data: [DONE]\n\n"
    frames = [
        frame
        async for frame in translate_responses_sse_to_public(
            _chunks(wire),
            request=request,
            public_model="codex-sol",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
            binding_key=TOKEN,
            model_scoped=True,
        )
    ]
    decoded = [_decode(frame) for frame in frames[:-1]]
    items = [
        data["item"]
        for name, data in decoded
        if name in {"response.output_item.added", "response.output_item.done"}
        and data["item"]["type"] == "reasoning"
    ]
    completed = next(data for name, data in decoded if name == "response.completed")

    assert items[0]["id"].startswith("cobr_c1_")
    assert items[1]["id"] == items[0]["id"]
    assert items[1]["encrypted_content"].startswith("cobr_s1_")
    assert completed["response"]["output"][0] == items[1]
    assert "rs_stream" not in b"".join(frames).decode()
    assert '"encrypted_content":"YQ=="' not in b"".join(frames).decode()


@pytest.mark.asyncio
async def test_statusless_provider_reasoning_is_normalized_for_direct_stream() -> None:
    frames = await _translate(_reasoning_events(provider_statusless=True))
    decoded = [_decode(frame) for frame in frames[:-1]]
    reasoning_items = [
        data["item"]
        for name, data in decoded
        if name in {"response.output_item.added", "response.output_item.done"}
        and data["item"]["type"] == "reasoning"
    ]

    assert reasoning_items == [
        {
            "id": "rs_stream",
            "type": "reasoning",
            "status": "in_progress",
            "summary": [],
        },
        {
            "id": "rs_stream",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": "YQ==",
        },
    ]
    completed = decoded[-1][1]["response"]
    assert completed["output"][0] == reasoning_items[1]
    assert b"YWRkZWQtdHJhbnNpZW50" not in b"".join(frames)


@pytest.mark.asyncio
async def test_buffered_nonstream_normalizes_statusless_provider_reasoning(
    tmp_path: Path,
) -> None:
    wire = b"".join(_event(event) for event in _reasoning_events(provider_statusless=True))
    transport = _StreamingOnlyUpstream(wire)
    adapter = upstream_module.BufferedResponsesUpstream(transport, _settings(tmp_path))

    response = await adapter.create_response(
        _CredentialManager().credential,
        {"model": "server-model", "input": [], "store": False, "stream": False},
    )

    assert isinstance(response, dict)
    assert response["output"][0] == {
        "id": "rs_stream",
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "encrypted_content": "YQ==",
    }
    assert transport.stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_case", ["nonempty_content", "missing_added_blob"])
async def test_statusless_provider_reasoning_rejects_unconfirmed_shapes(
    invalid_case: str,
) -> None:
    events = _reasoning_events(provider_statusless=True)
    added = events[1]["item"]
    assert isinstance(added, dict)
    if invalid_case == "nonempty_content":
        added["content"] = [{"type": "reasoning_text", "text": "SENSITIVE"}]
    else:
        added.pop("encrypted_content")

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$") as caught:
        await _translate(events)
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.asyncio
async def test_reasoning_summary_is_strictly_validated_then_preserved() -> None:
    events = _reasoning_events()
    summary = [{"type": "summary_text", "text": "SENSITIVE SUMMARY"}]
    for event in events:
        if event["type"] in {"response.output_item.added", "response.output_item.done"}:
            item = event["item"]
            assert isinstance(item, dict)
            if item["type"] == "reasoning":
                item["summary"] = summary

    frames = await _translate(
        events,
        document={
            "model": "codex",
            "input": "hello",
            "stream": True,
            "reasoning": {"summary": "auto"},
        },
    )
    decoded = [_decode(frame) for frame in frames[:-1]]
    reasoning_items = [
        data["item"]
        for name, data in decoded
        if name in {"response.output_item.added", "response.output_item.done"}
        and data["item"]["type"] == "reasoning"
    ]
    assert reasoning_items
    assert all(item["summary"] == summary for item in reasoning_items)
    assert b"SENSITIVE SUMMARY" in b"".join(frames)


@pytest.mark.asyncio
async def test_stream_rejects_unsolicited_reasoning_summary() -> None:
    events = _reasoning_events()
    for event in events:
        if event["type"] in {"response.output_item.added", "response.output_item.done"}:
            item = event["item"]
            assert isinstance(item, dict)
            if item["type"] == "reasoning":
                item["summary"] = [{"type": "summary_text", "text": "UNSOLICITED STREAM SUMMARY"}]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$") as caught:
        await _translate(events)

    assert "UNSOLICITED STREAM SUMMARY" not in repr(caught.value)


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
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
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
