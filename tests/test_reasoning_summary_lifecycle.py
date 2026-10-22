"""Synthetic reasoning-summary SSE fixtures; never captured provider content."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codex_openai_bridge.config import Settings
from codex_openai_bridge.models import ParsedSseEvent
from codex_openai_bridge.responses import (
    ResponsesRequestError,
    parse_responses_request,
    responses_to_public,
)
from codex_openai_bridge.responses_stream import translate_responses_sse_to_public
from codex_openai_bridge.translation import (
    ResponsesStreamValidator,
    UpstreamResponseError,
    responses_to_chat_completion,
)
from codex_openai_bridge.upstream import BufferedResponsesUpstream
from tests.contract._support import contract_server
from tests.test_responses_streaming import (
    _chunks,
    _CredentialManager,
    _decode,
    _event,
    _function_events,
    _parse,
    _reasoning_events,
    _StreamingOnlyUpstream,
    _tool_document,
)
from tests.test_responses_streaming import _translate as _responses_stream
from tests.test_streaming import _translate as _chat_stream
from tests.test_upstream_metadata import _project as _metadata_project

SUMMARY = "SYNTHETIC SUMMARY 日本語"
SUMMARY_TYPES = (
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_summary_part.done",
)
LIMITS: dict[str, Any] = {"max_json_depth": 16, "max_json_nodes": 256, "max_string_bytes": 4096}


def _resequence(events: list[dict[str, Any]]) -> None:
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence


def _summary_events(
    *, statusless: bool = True, summaries: tuple[str, ...] = (SUMMARY,)
) -> list[dict[str, Any]]:
    events = _reasoning_events(provider_statusless=statusless)
    if statusless:
        # Observed same-item unchanged encrypted state remains valid.
        events[1]["item"]["encrypted_content"] = events[2]["item"]["encrypted_content"]
    events[2]["item"]["summary"] = [{"type": "summary_text", "text": text} for text in summaries]
    lifecycle: list[dict[str, Any]] = []
    for index, text in enumerate(summaries):
        base = {"item_id": "rs_stream", "output_index": 0, "summary_index": index}
        lifecycle.append(
            {**base, "type": SUMMARY_TYPES[0], "part": {"type": "summary_text", "text": ""}}
        )
        for delta in (text[:3], text[3:]):
            lifecycle.append({**base, "type": SUMMARY_TYPES[1], "delta": delta})
        lifecycle.extend(
            [
                {**base, "type": SUMMARY_TYPES[2], "text": text},
                {**base, "type": SUMMARY_TYPES[3], "part": {"type": "summary_text", "text": text}},
            ]
        )
    events[2:2] = lifecycle
    _resequence(events)
    return events


def _document(*, requested: bool = True) -> dict[str, Any]:
    document: dict[str, Any] = {"model": "codex", "input": "hello", "stream": True}
    if requested:
        document["reasoning"] = {"effort": "high", "summary": "detailed"}
    return document


async def _buffered(
    events: list[dict[str, Any]],
    settings: Settings | None = None,
) -> dict[str, Any]:
    transport = _StreamingOnlyUpstream(b"".join(_event(event) for event in events))
    adapter = BufferedResponsesUpstream(transport, settings or Settings.from_env())
    try:
        response = await adapter.create_response(_CredentialManager().credential, {})
        assert isinstance(response, dict)
        return response
    finally:
        assert transport.stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("statusless", [False, True])
@pytest.mark.parametrize(
    "surface", ["responses_stream", "responses_buffered", "chat_stream", "chat_buffered"]
)
async def test_summary_lifecycle_preserves_requested_responses_and_chat_privacy(
    statusless: bool, surface: str
) -> None:
    events = _summary_events(statusless=statusless, summaries=(SUMMARY, "", "second"))
    before = deepcopy(events)
    summary = [{"type": "summary_text", "text": text} for text in (SUMMARY, "", "second")]
    if surface == "responses_stream":
        frames = await _responses_stream(events, document=_document())
        decoded = [_decode(frame) for frame in frames[:-1]]
        emitted = [data for name, data in decoded if name in SUMMARY_TYPES]
        assert emitted == [event for event in events if event["type"] in SUMMARY_TYPES]
        final = decoded[-1][1]["response"]
        done_item = next(
            data["item"]
            for name, data in decoded
            if name == "response.output_item.done" and data["item"]["type"] == "reasoning"
        )
        assert done_item == final["output"][0]
        assert done_item["summary"] == summary
        assert "content" not in done_item
    elif surface == "responses_buffered":
        final = responses_to_public(
            await _buffered(events),
            request=_parse(_document()),
            public_model="codex",
            max_items=16,
            max_tools=8,
            **LIMITS,
        )
        assert final["output"][0]["summary"] == summary
    else:
        if surface == "chat_stream":
            wire = b"".join(_event(event) for event in events)
            body = b"".join(await _chat_stream(_chunks(wire)))
            assert body.endswith(b"data: [DONE]\n\n")
        else:
            final = responses_to_chat_completion(
                await _buffered(events),
                public_model="codex",
                binding_key="synthetic",
                **LIMITS,
            )
            body = json.dumps(final).encode()
        assert b"SYNTHETIC SUMMARY" not in body
        assert b"summary_text" not in body
        assert b"hello" in body
    assert events == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["responses_stream", "responses_buffered", "chat_stream", "chat_buffered"]
)
async def test_mixed_form_reasoning_cannot_reuse_another_items_transient_blob(surface: str) -> None:
    events = _reasoning_events(provider_statusless=True)
    transient = events[1]["item"]["encrypted_content"]
    added = {"id": "rs_second", "type": "reasoning", "status": "in_progress", "summary": []}
    done = {**added, "status": "completed", "encrypted_content": "Yg=="}
    for event in events[3:-1]:
        if "output_index" in event:
            event["output_index"] = 2
    events[3:3] = [
        {"type": "response.output_item.added", "output_index": 1, "item": added},
        {"type": "response.output_item.done", "output_index": 1, "item": done},
    ]
    events[-1]["response"]["output"].insert(1, done)
    _resequence(events)
    # Prove this exact mixed-form lifecycle is otherwise valid on each real path.
    await _metadata_project(events, surface)
    done["encrypted_content"] = transient
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _metadata_project(events, surface)


async def _project_summary(events: list[dict[str, Any]], surface: str) -> bytes:
    if surface == "responses_stream":
        return b"".join(await _responses_stream(events, document=_document()))
    if surface == "chat_stream":
        return b"".join(await _chat_stream(_chunks(b"".join(_event(event) for event in events))))
    response = await _buffered(events)
    if surface == "responses_buffered":
        result = responses_to_public(
            response,
            request=_parse(_document()),
            public_model="codex",
            max_items=16,
            max_tools=8,
            **LIMITS,
        )
    else:
        result = responses_to_chat_completion(
            response,
            public_model="codex",
            binding_key="synthetic",
            **LIMITS,
        )
    return json.dumps(result).encode()


def _validator(**overrides: Any) -> ResponsesStreamValidator:
    return ResponsesStreamValidator(
        **(
            {"public_model": "codex", "max_unknown_events": 256, "max_string_bytes": 4096}
            | overrides
        )
    )


def _feed(validator: ResponsesStreamValidator, events: list[dict[str, Any]]) -> None:
    for event in events:
        validator.feed(ParsedSseEvent(event=event["type"], data=event))


@pytest.mark.parametrize("event_type", SUMMARY_TYPES)
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("item_id", "rs_other"),
        ("item_id", None),
        ("output_index", 1),
        ("output_index", True),
        ("output_index", 0.0),
        ("output_index", -1),
        ("summary_index", 1),
        ("summary_index", True),
        ("summary_index", 0.0),
        ("summary_index", -1),
        ("summary_index", 10**100),
        ("unexpected", "private"),
        ("safety_buffering", True),
        ("sequence_number", True),
    ],
)
def test_summary_event_identity_and_closed_fields(event_type: str, key: str, value: object) -> None:
    events = _summary_events()
    index = next(i for i, event in enumerate(events) if event["type"] == event_type)
    validator = _validator()
    _feed(validator, events[:index])
    events[index][key] = value
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _feed(validator, [events[index]])


@pytest.mark.parametrize("event_type", SUMMARY_TYPES)
def test_summary_event_requires_each_field(event_type: str) -> None:
    events = _summary_events()
    index = next(i for i, event in enumerate(events) if event["type"] == event_type)
    for key in events[index].keys() - {"type"}:
        validator = _validator()
        _feed(validator, events[:index])
        bad = dict(events[index])
        del bad[key]
        with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
            _feed(validator, [bad])


@pytest.mark.parametrize("event_type", SUMMARY_TYPES)
@pytest.mark.parametrize("invalid", [None, True, 1, [], {}, "\ud800", "x" * 4097])
def test_summary_event_text_is_bounded_utf8(event_type: str, invalid: object) -> None:
    events = _summary_events()
    index = next(i for i, event in enumerate(events) if event["type"] == event_type)
    validator = _validator()
    _feed(validator, events[:index])
    event = deepcopy(events[index])
    if "part" in event:
        event["part"]["text"] = invalid
    else:
        event["delta" if "delta" in event else "text"] = invalid
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _feed(validator, [event])


@pytest.mark.parametrize("event_type", [SUMMARY_TYPES[0], SUMMARY_TYPES[3]])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        {"type": "reasoning_text", "text": ""},
        {"type": "summary_text", "text": "", "extra": 1},
    ],
)
def test_summary_part_shape_is_closed(event_type: str, invalid: object) -> None:
    events = _summary_events()
    index = next(i for i, event in enumerate(events) if event["type"] == event_type)
    validator = _validator()
    _feed(validator, events[:index])
    events[index]["part"] = invalid
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _feed(validator, [events[index]])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["responses_stream", "responses_buffered", "chat_stream", "chat_buffered"]
)
@pytest.mark.parametrize(
    "case",
    [
        "missing_added",
        "missing_text_done",
        "missing_part_done",
        "duplicate_added",
        "duplicate_text_done",
        "duplicate_part_done",
        "done_before_delta",
        "part_before_text_done",
        "delta_after_text_done",
        "late_added",
        "late_delta",
        "late_after_completed",
        "nonempty_added",
        "done_mismatch",
        "part_mismatch",
        "item_mismatch",
        "item_missing",
        "terminal_mismatch",
        "content_leak",
        "unknown_summary_event",
        "unverified_snapshot",
        "overlapping_parts",
        "skipped_part_index",
        "mixed_snapshot",
    ],
)
async def test_invalid_summary_lifecycle_fails_closed_on_every_path(
    surface: str, case: str
) -> None:
    events = _summary_events(statusless=case != "mixed_snapshot")
    # Layout: created, item.added, part.added, delta, delta, text.done, part.done, item.done.
    if case.startswith("missing_"):
        del events[{"missing_added": 2, "missing_text_done": 5, "missing_part_done": 6}[case]]
    elif case.startswith("duplicate_"):
        index = {"duplicate_added": 2, "duplicate_text_done": 5, "duplicate_part_done": 6}[case]
        events.insert(index + 1, deepcopy(events[index]))
    elif case == "done_before_delta":
        events.insert(3, events.pop(5))
    elif case == "part_before_text_done":
        events[5], events[6] = events[6], events[5]
    elif case == "delta_after_text_done":
        events.insert(6, deepcopy(events[3]))
    elif case in {"late_added", "late_delta", "late_after_completed"}:
        event = deepcopy(events[2 if case == "late_added" else 3])
        events.insert(len(events) if case == "late_after_completed" else 8, event)
    elif case == "nonempty_added":
        events[2]["part"]["text"] = SUMMARY
    elif case == "done_mismatch":
        events[5]["text"] += "different"
    elif case == "part_mismatch":
        events[6]["part"]["text"] += "different"
    elif case == "item_mismatch":
        events[7]["item"]["summary"][0]["text"] += "different"
    elif case == "item_missing":
        events[7]["item"]["summary"] = []
    elif case == "terminal_mismatch":
        events[-1] = deepcopy(events[-1])
        events[-1]["response"]["output"][0]["summary"][0]["text"] += "different"
    elif case == "content_leak":
        events[7]["item"]["content"] = [{"type": "reasoning_text", "text": SUMMARY}]
    elif case == "unknown_summary_event":
        events[3]["type"] = "response.reasoning_summary_text.unrecognized"
    elif case == "unverified_snapshot":
        del events[2:7]
    elif case == "overlapping_parts":
        event = deepcopy(events[2])
        event["summary_index"] = 1
        events.insert(3, event)
    elif case == "skipped_part_index":
        events[2]["summary_index"] = 1
    elif case == "mixed_snapshot":
        events[1]["item"]["summary"] = [{"type": "summary_text", "text": SUMMARY}]
    _resequence(events)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$") as caught:
        await _project_summary(events, surface)
    assert SUMMARY not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("statusless", [False, True])
async def test_unsolicited_summary_rejected_before_any_plaintext_is_public(
    statusless: bool,
) -> None:
    events = _summary_events(statusless=statusless)
    frames: list[bytes] = []
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        async for frame in translate_responses_sse_to_public(
            _chunks(b"".join(_event(event) for event in events)),
            request=_parse(_document(requested=False)),
            public_model="codex",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            **LIMITS,
        ):
            frames.append(frame)
    assert not any(
        b"reasoning_summary" in frame or b"SYNTHETIC SUMMARY" in frame for frame in frames
    )
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_public(
            await _buffered(events),
            request=_parse(_document(requested=False)),
            public_model="codex",
            max_items=16,
            max_tools=8,
            **LIMITS,
        )


def test_summary_accumulation_uses_utf8_byte_budget_before_growth() -> None:
    events = _summary_events(summaries=("界" * 21 + "x",))
    validator = _validator(max_string_bytes=64)
    _feed(validator, events[:5])
    extra_delta = {**events[4], "delta": "x", "sequence_number": 5}
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _feed(validator, [extra_delta])
    validator = _validator(max_string_bytes=64)
    _feed(validator, events)
    validator.finish()


def test_summary_retained_part_count_has_a_cross_event_budget() -> None:
    events = _summary_events(summaries=("", "", ""))
    validator = _validator(max_unknown_events=2)
    _feed(validator, events[:12])  # two fully completed parts
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _feed(validator, [events[12]])
    validator = _validator(max_unknown_events=2)
    _feed(validator, _summary_events(summaries=("", "")))
    validator.finish()


@pytest.mark.asyncio
async def test_summary_obfuscation_is_bounded_private_delta_metadata() -> None:
    events = _summary_events()
    events[3]["obfuscation"] = "PRIVATE SYNTHETIC OBFUSCATION"
    frames = await _responses_stream(events, document=_document())
    assert b"PRIVATE SYNTHETIC OBFUSCATION" not in b"".join(frames)
    for invalid in (None, True, 1, [], "x" * 4097, "\ud800"):
        validator = _validator()
        _feed(validator, events[:3])
        bad = {**events[3], "obfuscation": invalid}
        with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
            _feed(validator, [bad])
    for index in (2, 5, 6):
        validator = _validator()
        _feed(validator, events[:index])
        with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
            _feed(validator, [{**events[index], "obfuscation": "private"}])


@pytest.mark.asyncio
async def test_summary_events_use_model_bound_item_ids_and_replay_preserves_summary() -> None:
    events = _summary_events()
    frames = [
        frame
        async for frame in translate_responses_sse_to_public(
            _chunks(b"".join(_event(event) for event in events)),
            request=_parse(_document()),
            public_model="codex",
            max_items=16,
            max_tools=8,
            max_sse_event_bytes=4096,
            max_stream_bytes=65536,
            binding_key="synthetic-binding",
            model_scoped=True,
            **LIMITS,
        )
    ]
    decoded = [_decode(frame) for frame in frames[:-1]]
    final_item = decoded[-1][1]["response"]["output"][0]
    assert final_item["id"].startswith("cobr_c1_")
    assert final_item["encrypted_content"].startswith("cobr_s1_")
    assert final_item["summary"] == [{"type": "summary_text", "text": SUMMARY}]
    assert all(
        data["item_id"] == final_item["id"] for name, data in decoded if name in SUMMARY_TYPES
    )
    assert b"rs_stream" not in b"".join(frames)
    history = [
        {"role": "user", "content": "hello"},
        *decoded[-1][1]["response"]["output"],
        {"role": "user", "content": "again"},
    ]
    request = parse_responses_request(
        {"model": "codex", "input": history},
        public_model=("codex", "codex-sol"),
        max_items=16,
        max_tools=8,
        binding_key="synthetic-binding",
        **LIMITS,
    )
    assert isinstance(request.input, tuple)
    assert request.input[1]["summary"] == final_item["summary"]
    assert "rs_stream" in request.historical_item_ids
    for mutation in ("alias", "item"):
        document: dict[str, Any] = {"model": "codex", "input": deepcopy(history)}
        if mutation == "alias":
            document["model"] = "codex-sol"
        else:
            document["input"][1]["id"] = "rs_other"
        with pytest.raises(ResponsesRequestError):
            parse_responses_request(
                document,
                public_model=("codex", "codex-sol"),
                max_items=16,
                max_tools=8,
                binding_key="synthetic-binding",
                **LIMITS,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("max_json_depth", 5),
        ("max_json_nodes", 30),
        ("max_string_bytes", 511),
        ("max_sse_event_bytes", 256),
        ("max_stream_bytes", 512),
    ],
)
async def test_summary_does_not_bypass_existing_parser_budgets(limit: str, value: int) -> None:
    events = _summary_events(summaries=("s" * 512,))
    await _buffered(events)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _buffered(events, replace(Settings.from_env(), **{limit: value}))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_sequential_tool_roundtrip_with_requested_summary(
    tmp_path: Path, stream: bool
) -> None:
    first = _function_events()
    second = _summary_events()
    first_wire = b"".join(_event(event) for event in first)
    second_wire = b"".join(_event(event) for event in second).replace(
        b"resp_stream", b"resp_second"
    )
    async with contract_server(
        tmp_path,
        streams=[first_wire, second_wire],
        buffered_nonstream=True,
    ) as running:
        tools: Any = _tool_document()["tools"]
        result = await running.client.responses.create(model="codex", input="hello", tools=tools)
        call = next(item for item in result.output if item.type == "function_call")
        history: list[Any] = [
            {"role": "user", "content": "hello"},
            *[item.model_dump(exclude_none=True) for item in result.output],
            {"type": "function_call_output", "call_id": call.call_id, "output": "synthetic result"},
        ]
        kwargs: dict[str, Any] = {
            "model": "codex",
            "input": history,
            "tools": tools,
            "reasoning": {"effort": "high", "summary": "detailed"},
        }
        if stream:
            async with running.client.responses.stream(**kwargs) as response_stream:
                seen = [event.type async for event in response_stream]
                result = await response_stream.get_final_response()
            assert all(event_type in seen for event_type in SUMMARY_TYPES)
        else:
            result = await running.client.responses.create(**kwargs)
        assert result.output_text == "hello"
        assert result.output[0].type == "reasoning"
        assert result.output[0].summary[0].text == SUMMARY
        assert running.upstream.calls[1]["reasoning"] == {"effort": "high", "summary": "detailed"}
        assert running.upstream.calls[1]["input"][-1]["type"] == "function_call_output"
        assert len(running.upstream.calls) == 2
        assert all(byte_stream.close_calls == 1 for byte_stream in running.upstream.byte_streams)
