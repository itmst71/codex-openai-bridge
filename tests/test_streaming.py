from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from codex_openai_bridge.translation import (
    UpstreamResponseError,
    parse_responses_sse,
    translate_responses_sse,
)


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def _event(value: object, *, newline: bytes = b"\n", event_field: bool = False) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    prefix = b""
    if event_field:
        assert isinstance(value, dict)
        prefix = b"event: " + str(value["type"]).encode() + newline
    return prefix + b"data: " + payload + newline + newline


def _text_events() -> list[dict[str, object]]:
    response = {"id": "resp_123", "created_at": 1_700_000_000, "status": "in_progress"}
    return [
        {"type": "response.created", "response": response, "sequence_number": 0},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
            "sequence_number": 1,
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
            "sequence_number": 2,
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "hel",
            "sequence_number": 3,
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "lo",
            "sequence_number": 4,
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "text": "hello",
            "sequence_number": 5,
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "hello", "annotations": []},
            "sequence_number": 6,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            },
            "sequence_number": 7,
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_123",
                "created_at": 1_700_000_000,
                "status": "completed",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
            "sequence_number": 8,
        },
    ]


async def _translate(source: AsyncIterator[bytes], *, include_usage: bool = False) -> list[bytes]:
    return [
        chunk
        async for chunk in translate_responses_sse(
            source,
            public_model="codex",
            include_usage=include_usage,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
        )
    ]


def _decode_chunk(frame: bytes) -> object:
    assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
    return json.loads(frame[6:-2])


@pytest.mark.asyncio
async def test_text_role_deltas_final_usage_and_done_are_exact() -> None:
    wire = b"".join(_event(value) for value in _text_events()) + b"data: [DONE]\n\n"

    frames = await _translate(_chunks(wire), include_usage=True)

    assert len(frames) == 6
    role, first, second, final, usage = [_decode_chunk(frame) for frame in frames[:-1]]
    assert role["id"] == "resp_123"  # type: ignore[index]
    assert role["object"] == "chat.completion.chunk"  # type: ignore[index]
    assert role["created"] == 1_700_000_000  # type: ignore[index]
    assert role["model"] == "codex"  # type: ignore[index]
    assert role["choices"] == [  # type: ignore[index]
        {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
    ]
    assert first["choices"][0]["delta"] == {"content": "hel"}  # type: ignore[index]
    assert second["choices"][0]["delta"] == {"content": "lo"}  # type: ignore[index]
    assert final["choices"] == [  # type: ignore[index]
        {"index": 0, "delta": {}, "finish_reason": "stop"}
    ]
    assert usage["choices"] == []  # type: ignore[index]
    assert usage["usage"] == {  # type: ignore[index]
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }
    assert frames[-1] == b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_fragmented_utf8_multiple_frames_and_crlf_are_incremental() -> None:
    events = _text_events()
    events[3]["delta"] = "雪"
    events[4]["delta"] = "だ"
    events[5]["text"] = "雪だ"
    events[6]["part"] = {"type": "output_text", "text": "雪だ", "annotations": []}
    message = events[7]["item"]
    assert isinstance(message, dict)
    message["content"] = [{"type": "output_text", "text": "雪だ", "annotations": []}]
    completed = events[8]["response"]
    assert isinstance(completed, dict)
    completed["output"] = [message]
    wire = b"".join(_event(value, newline=b"\r\n", event_field=True) for value in events)
    wire += b"data: [DONE]\r\n\r\n"
    snow = wire.index("雪".encode())
    source = _chunks(wire[: snow + 1], wire[snow + 1 : snow + 2], wire[snow + 2 :])

    frames = await _translate(source)

    assert [frame for frame in frames if "雪".encode() in frame]
    assert frames[-1] == b"data: [DONE]\n\n"
    assert all(b"usage" not in frame for frame in frames[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [
        b"data: {}\r\r",
        b"data: \xff\n\n",
        b'data: {"type":"x","type":"y"}\n\n',
        b'data: {"type":NaN}\n\n',
        b"data: {} trailing\n\n",
        b"data: {}\ndata: {}\n\n",
        b":comment\ndata: {}\n\n",
        b"id: secret\ndata: {}\n\n",
        b"data: {}\x00\n\n",
    ],
)
async def test_malformed_or_ambiguous_sse_fails_closed(wire: bytes) -> None:
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_chunks(wire))


@pytest.mark.asyncio
async def test_event_and_cumulative_caps_accept_exact_and_reject_one_over() -> None:
    event = _event(
        {
            "type": "response.created",
            "response": {"id": "r", "created_at": 0, "status": "in_progress"},
        }
    )

    async def first(*, event_limit: int, stream_limit: int) -> object:
        iterator = parse_responses_sse(
            _chunks(event),
            max_sse_event_bytes=event_limit,
            max_stream_bytes=stream_limit,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
        )
        return await anext(iterator)

    # The event cap includes the nonblank data line and its LF, not the blank delimiter LF.
    exact_event_size = len(event) - 1
    parsed = await first(event_limit=exact_event_size, stream_limit=len(event))
    assert parsed.data is not None  # type: ignore[attr-defined]
    with pytest.raises(UpstreamResponseError):
        await first(event_limit=exact_event_size - 1, stream_limit=len(event))
    with pytest.raises(UpstreamResponseError):
        await first(event_limit=exact_event_size, stream_limit=len(event) - 1)


def _tool_events() -> list[dict[str, object]]:
    response = {"id": "resp_tools", "created_at": 5, "status": "in_progress"}
    first_added = {
        "id": "fc_1",
        "type": "function_call",
        "status": "in_progress",
        "call_id": "call_public_1",
        "name": "weather",
        "arguments": "",
    }
    second_added = {
        "id": "fc_2",
        "type": "function_call",
        "status": "in_progress",
        "call_id": "call_public_2",
        "name": "time",
        "arguments": "",
    }
    first_done = {**first_added, "status": "completed", "arguments": '{"city":"Tokyo"}'}
    second_done = {**second_added, "status": "completed", "arguments": "{}"}
    return [
        {"type": "response.created", "response": response},
        {"type": "response.output_item.added", "output_index": 0, "item": first_added},
        {"type": "response.output_item.added", "output_index": 1, "item": second_added},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": '{"city":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_2",
            "output_index": 1,
            "delta": "{}",
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": '"Tokyo"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_2",
            "output_index": 1,
            "arguments": "{}",
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "output_index": 0,
            "arguments": '{"city":"Tokyo"}',
        },
        {"type": "response.output_item.done", "output_index": 1, "item": second_done},
        {"type": "response.output_item.done", "output_index": 0, "item": first_done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_tools",
                "created_at": 5,
                "status": "completed",
                "output": [first_done, second_done],
                "usage": {"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
            },
        },
    ]


@pytest.mark.asyncio
async def test_parallel_tool_declarations_arguments_and_indexes_preserve_event_order() -> None:
    wire = b"".join(_event(value) for value in _tool_events()) + b"data: [DONE]\n\n"

    frames = await _translate(_chunks(wire))
    chunks = [_decode_chunk(frame) for frame in frames[:-1]]

    assert chunks[1]["choices"][0]["delta"]["tool_calls"] == [  # type: ignore[index]
        {
            "index": 0,
            "id": "call_public_1",
            "type": "function",
            "function": {"name": "weather", "arguments": ""},
        }
    ]
    assert chunks[2]["choices"][0]["delta"]["tool_calls"][0]["index"] == 1  # type: ignore[index]
    argument_updates = [
        chunk["choices"][0]["delta"]["tool_calls"][0]  # type: ignore[index]
        for chunk in chunks[3:-1]
    ]
    assert [(update["index"], update["function"]["arguments"]) for update in argument_updates] == [
        (0, '{"city":'),
        (1, "{}"),
        (0, '"Tokyo"}'),
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"  # type: ignore[index]
    serialized = b"".join(frames)
    assert b"fc_1" not in serialized
    assert b"fc_2" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["[]", "null", '{"x":NaN}', '{"x":1,"x":2}'])
async def test_tool_arguments_must_finish_as_one_strict_json_object(arguments: str) -> None:
    events = _tool_events()
    for event in events:
        if event["type"] == "response.function_call_arguments.delta" and event["item_id"] == "fc_2":
            event["delta"] = arguments
        if event["type"] == "response.function_call_arguments.done" and event["item_id"] == "fc_2":
            event["arguments"] = arguments
    wire = b"".join(_event(value) for value in events)

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_chunks(wire))


@pytest.mark.asyncio
async def test_unknown_nonterminal_is_ignored_but_unknown_terminal_and_early_done_fail() -> None:
    events = _text_events()
    events.insert(1, {"type": "response harmless telemetry", "value": "not exposed"})
    good = b"".join(_event(value) for value in events) + b"data: [DONE]\n\n"
    assert (await _translate(_chunks(good)))[-1] == b"data: [DONE]\n\n"

    terminal = _event({"type": "response.future.completed", "secret": "SENSITIVE"})
    with pytest.raises(UpstreamResponseError) as caught:
        await _translate(_chunks(terminal))
    assert "SENSITIVE" not in repr(caught.value)

    with pytest.raises(UpstreamResponseError):
        await _translate(_chunks(b"data: [DONE]\n\n"))


@pytest.mark.asyncio
async def test_event_after_done_fails_before_success_terminal_frames_are_released() -> None:
    wire = b"".join(_event(value) for value in _text_events())
    wire += b"data: [DONE]\n\n" + _event({"type": "response.future.delta", "value": 1})
    emitted: list[bytes] = []

    with pytest.raises(UpstreamResponseError):
        async for frame in translate_responses_sse(
            _chunks(wire),
            public_model="codex",
            include_usage=False,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
        ):
            emitted.append(frame)

    assert emitted
    assert b"data: [DONE]" not in b"".join(emitted)
    assert b'"finish_reason":"stop"' not in b"".join(emitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete"])
async def test_failed_or_incomplete_terminal_fails_without_emitting_a_frame(terminal: str) -> None:
    source = _chunks(_event({"type": terminal, "response": {"secret": "SENSITIVE"}}))
    emitted: list[bytes] = []
    with pytest.raises(UpstreamResponseError):
        async for frame in translate_responses_sse(
            source,
            public_model="codex",
            include_usage=False,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
        ):
            emitted.append(frame)
    assert emitted == []


@pytest.mark.asyncio
async def test_empty_text_still_requires_content_part_added_before_done() -> None:
    events = [
        event
        for event in _text_events()
        if event["type"] not in {"response.content_part.added", "response.output_text.delta"}
    ]
    for event in events:
        if event["type"] == "response.output_text.done":
            event["text"] = ""
        elif event["type"] == "response.content_part.done":
            part = event["part"]
            assert isinstance(part, dict)
            part["text"] = ""
        elif event["type"] == "response.output_item.done":
            item = event["item"]
            assert isinstance(item, dict)
            content = item["content"]
            assert isinstance(content, list)
            assert isinstance(content[0], dict)
            content[0]["text"] = ""
        elif event["type"] == "response.completed":
            response = event["response"]
            assert isinstance(response, dict)
            output = response["output"]
            assert isinstance(output, list)
            assert isinstance(output[0], dict)
            content = output[0]["content"]
            assert isinstance(content, list)
            assert isinstance(content[0], dict)
            content[0]["text"] = ""
    wire = b"".join(_event(value) for value in events) + b"data: [DONE]\n\n"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_chunks(wire))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [[{"type": "output_text", "text": "hidden"}], [1]])
async def test_in_progress_message_requires_empty_initial_content(content: list[object]) -> None:
    events = _text_events()
    added = events[1]["item"]
    assert isinstance(added, dict)
    added["content"] = content
    wire = b"".join(_event(value) for value in events) + b"data: [DONE]\n\n"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_chunks(wire))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["identity", "failed_status"])
async def test_reasoning_added_done_identity_and_status_are_authoritative(mutation: str) -> None:
    events = _text_events()
    completed_event = events.pop()
    for event in events:
        event.pop("sequence_number", None)
    reasoning_added = {
        "id": "reason_1",
        "type": "reasoning",
        "status": "in_progress",
        "summary": [],
    }
    reasoning_done = {
        **reasoning_added,
        "status": "completed",
        "encrypted_content": "opaque",
    }
    if mutation == "identity":
        reasoning_done["id"] = "reason_other"
    else:
        reasoning_done["status"] = "failed"
    events.extend(
        [
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": reasoning_added,
            },
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": reasoning_done,
            },
        ]
    )
    completed = completed_event["response"]
    assert isinstance(completed, dict)
    output = completed["output"]
    assert isinstance(output, list)
    output.append(reasoning_done)
    completed_event.pop("sequence_number", None)
    events.append(completed_event)
    wire = b"".join(_event(value) for value in events) + b"data: [DONE]\n\n"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_chunks(wire))


@pytest.mark.asyncio
async def test_completed_output_authority_comparison_is_type_exact() -> None:
    events = _text_events()
    done_event = events[7]
    done_item = done_event["item"]
    assert isinstance(done_item, dict)
    done_content = done_item["content"]
    assert isinstance(done_content, list)
    done_part = done_content[0]
    assert isinstance(done_part, dict)
    done_part["annotations"] = [1]

    completed_event = events[8]
    completed = completed_event["response"]
    assert isinstance(completed, dict)
    output = completed["output"]
    assert isinstance(output, list)
    authority_item = output[0]
    assert isinstance(authority_item, dict)
    authority_content = authority_item["content"]
    assert isinstance(authority_content, list)
    authority_part = authority_content[0]
    assert isinstance(authority_part, dict)
    authority_part["annotations"] = [True]
    wire = b"".join(_event(value) for value in events) + b"data: [DONE]\n\n"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _translate(_chunks(wire))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["duplicate", "late"])
async def test_setup_events_must_be_unique_and_precede_output(mutation: str) -> None:
    events = _text_events()
    created = events.pop(0)
    created.pop("sequence_number")
    if mutation == "duplicate":
        events[0:0] = [created, created]
    else:
        events.insert(1, created)
    wire = b"".join(_event(value) for value in events) + b"data: [DONE]\n\n"

    with pytest.raises(UpstreamResponseError):
        await _translate(_chunks(wire))
