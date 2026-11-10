"""Synthetic fixtures for observed Codex metadata shapes; no captured live payloads."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from codex_openai_bridge.config import Settings
from codex_openai_bridge.models import ParsedSseEvent
from codex_openai_bridge.responses import responses_to_public
from codex_openai_bridge.translation import (
    ResponsesStreamValidator,
    UpstreamResponseError,
    parse_responses_sse,
    responses_to_chat_completion,
)
from codex_openai_bridge.upstream import BufferedResponsesUpstream
from codex_openai_bridge.wire import encrypted_reasoning_data_digest
from tests.contract._support import contract_server
from tests.test_responses_streaming import (
    _chunks,
    _CredentialManager,
    _event,
    _parse,
    _reasoning_events,
    _StreamingOnlyUpstream,
    _text_events,
)
from tests.test_responses_streaming import _translate as _responses_stream
from tests.test_streaming import _tool_events
from tests.test_streaming import _translate as _chat_stream

SURFACES = ("chat_stream", "responses_stream", "chat_buffered", "responses_buffered")
SAFETY_EVENTS = (
    "response.created",
    "response.in_progress",
    "response.output_text.delta",
    "response.completed",
)
COUNTERS = ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens")
PRIVATE_ITEM = "msg_SYNTHETIC_PRIVATE_ACCOUNTING"
PRIVATE_METADATA = "SYNTHETIC_PRIVATE_METADATA"
LIMITS = {"max_json_depth": 16, "max_json_nodes": 256, "max_string_bytes": 4096}


def _attribution() -> dict[str, Any]:
    counts = dict(zip(COUNTERS, (8, 0, 0, 0), strict=True))
    return {
        "items": {
            PRIVATE_ITEM: {**counts, "content": [dict(counts)]},
            "msg_synthetic_other": {**counts, "content": []},
        },
        "request_fields": {"instructions": {**counts, "input_tokens": 7}},
    }


def _resequence(events: list[dict[str, Any]]) -> None:
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence


def _events(*, safety: bool | None = False, attribution: bool = True) -> list[dict[str, Any]]:
    events = _text_events()
    events.insert(1, {"type": "response.in_progress", "response": deepcopy(events[0]["response"])})
    events.insert(
        2,
        {
            "type": "response.metadata",
            "response_id": "resp_stream",
            "metadata": {"synthetic": PRIVATE_METADATA},
        },
    )
    for event in events:
        if safety is not None and event["type"] in SAFETY_EVENTS:
            event["safety_buffering"] = safety
    usage = events[-1]["response"]["usage"]
    usage["input_tokens_details"]["cache_write_tokens"] = 0
    if attribution:
        usage["attribution"] = _attribution()
    _resequence(events)
    return events


def _public(response: object, surface: str, **limits: int) -> dict[str, Any]:
    bounds = LIMITS | limits
    if surface.startswith("chat"):
        return responses_to_chat_completion(
            response,
            public_model="codex",
            binding_key="synthetic-binding",
            max_json_depth=bounds["max_json_depth"],
            max_json_nodes=bounds["max_json_nodes"],
            max_string_bytes=bounds["max_string_bytes"],
        )
    return responses_to_public(
        response,
        request=_parse({"model": "codex", "input": "hello"}),
        public_model="codex",
        max_items=16,
        max_tools=8,
        max_json_depth=bounds["max_json_depth"],
        max_json_nodes=bounds["max_json_nodes"],
        max_string_bytes=bounds["max_string_bytes"],
    )


async def _project(events: list[dict[str, Any]], surface: str) -> bytes:
    wire = b"".join(_event(event) for event in events)
    if surface == "chat_stream":
        return b"".join(await _chat_stream(_chunks(wire), include_usage=True))
    if surface == "responses_stream":
        return b"".join(await _responses_stream(events, upstream_done=False))
    upstream = _StreamingOnlyUpstream(wire)
    buffered = BufferedResponsesUpstream(upstream, Settings.from_env())
    try:
        response = await buffered.create_response(_CredentialManager().credential, {})
        # Buffering must strip accounting before the public converter or continuation sees it.
        assert isinstance(response, dict)
        assert "attribution" not in response["usage"]
        return json.dumps(_public(response, surface)).encode()
    finally:
        assert upstream.stream.close_calls == 1


def _assert_private_absent(value: bytes) -> None:
    for private in ("safety_buffering", "attribution", PRIVATE_ITEM, PRIVATE_METADATA):
        assert private.encode() not in value


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    ("safety", "attribution"),
    [
        (None, False),
        (False, False),
        (True, False),
        (None, True),
        (False, True),
        (True, True),
    ],
)
async def test_synthetic_metadata_is_private_on_every_surface(
    surface: str,
    safety: bool | None,
    attribution: bool,
) -> None:
    events = _events(safety=safety, attribution=attribution)
    before = deepcopy(events)
    result = await _project(events, surface)
    _assert_private_absent(result)
    assert b"hello" in result
    assert events == before
    # Internal attribution deliberately does not sum to terminal totals: it is not authority.
    if surface.endswith("stream"):
        assert result.endswith(b"data: [DONE]\n\n")
        payloads = [
            json.loads(line[6:])
            for line in result.splitlines()
            if line.startswith(b"data: ") and line != b"data: [DONE]"
        ]
        public = payloads[-1].get("response", payloads[-1])
    else:
        public = json.loads(result)
    assert public["usage"]["total_tokens"] == 3
    if surface.startswith("responses"):
        assert public["usage"]["input_tokens_details"]["cache_write_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("event_type", SAFETY_EVENTS)
@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
async def test_safety_buffering_rejects_non_bool(
    surface: str,
    event_type: str,
    invalid: object,
) -> None:
    events = _events()
    next(event for event in events if event["type"] == event_type)["safety_buffering"] = invalid
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "event_type",
    [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.metadata",
    ],
)
async def test_safety_buffering_is_not_a_generic_optional_event_field(
    surface: str,
    event_type: str,
) -> None:
    events = _events()
    next(event for event in events if event["type"] == event_type)["safety_buffering"] = False
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)


def _invalid_attributions() -> list[object]:
    values: list[object] = [None, [], True, {}, {**_attribution(), "account_id": "private"}]
    for key in ("items", "request_fields"):
        values.extend([{**_attribution(), key: None}, {**_attribution(), key: []}])
    missing = _attribution()
    del missing["items"]
    values.append(missing)
    for key in ("", "bad.id", "x" * 129):
        value = _attribution()
        value["items"][key] = value["items"].pop(PRIVATE_ITEM)
        values.append(value)
    for target in ("item", "content", "instructions", "tools", "text.format"):
        for change in ("missing", "extra", "negative", "bool", "float", "string"):
            value = _attribution()
            if target in {"tools", "text.format"}:
                value["request_fields"][target] = value["request_fields"].pop("instructions")
            record = (
                value["items"][PRIVATE_ITEM]
                if target == "item"
                else value["items"][PRIVATE_ITEM]["content"][0]
                if target == "content"
                else value["request_fields"][target]
            )
            if change == "missing":
                del record["cached_tokens"]
            elif change == "extra":
                record["account_id"] = "private"
            else:
                record["input_tokens"] = {
                    "negative": -1,
                    "bool": True,
                    "float": 1.0,
                    "string": "1",
                }[change]
            values.append(value)
    invalid_contents: tuple[object, ...] = (None, {}, True, [None], [{"input_tokens": 1}])
    for bad in invalid_contents:
        value = _attribution()
        value["items"][PRIVATE_ITEM]["content"] = bad
        values.append(value)
    for key in ("unobserved", "model", "authorization"):
        value = _attribution()
        value["request_fields"][key] = value["request_fields"].pop("instructions")
        values.append(value)
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "variant", ["no_request_fields", "no_content", "tools", "tool_combined", "text.format"]
)
async def test_observed_optional_accounting_and_tool_shapes(surface: str, variant: str) -> None:
    events = _events()
    attribution = events[-1]["response"]["usage"]["attribution"]
    if variant in {"no_content", "tool_combined"}:
        del attribution["items"][PRIVATE_ITEM]["content"]
    if variant in {"tools", "tool_combined"}:
        attribution["request_fields"] = {"tools": attribution["request_fields"]["instructions"]}
    if variant == "no_request_fields":
        del attribution["request_fields"]
    if variant == "text.format":
        attribution["request_fields"] = {
            "text.format": attribution["request_fields"]["instructions"]
        }
    result = await _project(events, surface)
    _assert_private_absent(result)
    assert b"hello" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_same_item_reasoning_ciphertext_can_remain_unchanged(surface: str) -> None:
    events = _reasoning_events(provider_statusless=True)
    events[2]["item"]["encrypted_content"] = events[1]["item"]["encrypted_content"]
    before = deepcopy(events)
    result = await _project(events, surface)
    assert b"hello" in result
    assert events == before


@pytest.mark.parametrize(
    "invalid", ["other_pending", "historical", "duplicate_done", "wrong_id", "missing_done"]
)
def test_unchanged_reasoning_keeps_cross_item_and_completion_guards(invalid: str) -> None:
    events = _reasoning_events(provider_statusless=True)
    historical = encrypted_reasoning_data_digest("Yg==", max_string_bytes=4096)
    assert historical is not None
    validator = ResponsesStreamValidator(
        public_model="codex",
        max_unknown_events=256,
        max_string_bytes=4096,
        initial_encrypted_digests=frozenset({historical})
        if invalid == "historical"
        else frozenset(),
    )
    validator.feed(ParsedSseEvent(None, events[0]))
    validator.feed(ParsedSseEvent(None, events[1]))
    done = deepcopy(events[2])
    done["item"]["encrypted_content"] = events[1]["item"]["encrypted_content"]
    if invalid == "other_pending":
        added = deepcopy(events[1])
        added.update(output_index=1, sequence_number=2)
        added["item"].update(id="rs_other", encrypted_content="Yg==")
        validator.feed(ParsedSseEvent(None, added))
        done["sequence_number"] = 3
        done["item"]["encrypted_content"] = "Yg=="
    elif invalid == "historical":
        done["item"]["encrypted_content"] = "Yg=="
    elif invalid == "duplicate_done":
        validator.feed(ParsedSseEvent(None, done))
        done["sequence_number"] = 3
    elif invalid == "wrong_id":
        done["item"]["id"] = "rs_other"
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        if invalid == "missing_done":
            validator.finish()
        else:
            validator.feed(ParsedSseEvent(None, done))
    assert validator.completed_response is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("invalid", _invalid_attributions())
async def test_attribution_rejects_unobserved_shapes(surface: str, invalid: object) -> None:
    events = _events()
    events[-1]["response"]["usage"]["attribution"] = invalid
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)


@pytest.mark.parametrize("surface", ["chat_direct", "responses_direct"])
@pytest.mark.parametrize("invalid", _invalid_attributions())
def test_direct_projection_cannot_bypass_attribution_validation(
    surface: str,
    invalid: object,
) -> None:
    response = _events()[-1]["response"]
    response["usage"]["attribution"] = invalid
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public(response, surface)


@pytest.mark.parametrize("surface", ["chat_direct", "responses_direct"])
def test_direct_projection_strips_valid_attribution(surface: str) -> None:
    response = _events()[-1]["response"]
    before = deepcopy(response)
    result = _public(response, surface)
    assert result["usage"]["total_tokens"] == 3
    _assert_private_absent(json.dumps(result).encode())
    assert response == before


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "failure",
    [
        "response_id",
        "item_id",
        "sequence",
        "terminal_output",
        "missing_done_item",
        "missing_terminal",
        "duplicate_terminal",
        "late_metadata",
        "unknown_terminal",
        "unknown_field",
        "unknown_usage",
        "negative_usage",
        "bool_usage",
        "partial_details",
    ],
)
async def test_metadata_does_not_weaken_lifecycle_or_usage_authority(
    surface: str,
    failure: str,
) -> None:
    events = _events()
    if failure == "response_id":
        events[-1]["response"]["id"] = "resp_other"
    elif failure == "item_id":
        events[5]["item_id"] = "msg_other"
    elif failure == "sequence":
        events[-1]["sequence_number"] = 0
    elif failure == "terminal_output":
        events[-1]["response"]["output"] = [deepcopy(events[-1]["response"]["output"][0])]
        events[-1]["response"]["output"][0]["id"] = "msg_other"
    elif failure == "missing_done_item":
        events = [event for event in events if event["type"] != "response.output_item.done"]
    elif failure == "missing_terminal":
        events.pop()
    elif failure == "duplicate_terminal":
        events.append(deepcopy(events[-1]))
    elif failure == "late_metadata":
        events.append({"type": "response.metadata", "metadata": {}})
    elif failure == "unknown_terminal":
        events[2]["type"] = "response.metadata.completed"
    elif failure == "unknown_field":
        events[0]["authorization"] = "SYNTHETIC_SECRET"
    elif failure == "unknown_usage":
        events[-1]["response"]["usage"]["account_id"] = "SYNTHETIC_SECRET"
    elif failure == "partial_details":
        del events[-1]["response"]["usage"]["output_tokens_details"]
    else:
        events[-1]["response"]["usage"]["total_tokens"] = (
            -1 if failure == "negative_usage" else True
        )
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "call_id", "name", "safety"])
async def test_tool_identity_remains_strict_with_metadata(failure: str | None) -> None:
    events: list[dict[str, Any]] = deepcopy(_tool_events())
    events[0]["safety_buffering"] = False
    events[-1]["safety_buffering"] = False
    events[-1]["response"]["usage"].update(
        {
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
            "attribution": _attribution(),
        }
    )
    if failure == "safety":
        events[3]["safety_buffering"] = False
    elif failure:
        events[-2]["item"][failure] = "different"
    if failure:
        with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
            await _project(events, "chat_stream")
    else:
        result = await _project(events, "chat_stream")
        _assert_private_absent(result)
        assert b"tool_calls" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_sdk_http_metadata_and_continuation_stay_private(
    tmp_path: Path,
    surface: str,
) -> None:
    wire = b"".join(_event(event) for event in _events())
    second_wire = wire.replace(b"resp_stream", b"resp_second").replace(b"msg_stream", b"msg_second")
    async with contract_server(
        tmp_path,
        streams=[wire, second_wire],
        buffered_nonstream=True,
    ) as running:
        stream = surface.endswith("stream")
        dumped: object
        text: str | None
        if surface.startswith("chat"):
            messages: list[Any] = [{"role": "user", "content": "hello"}]
            if stream:
                chat_stream = await running.client.chat.completions.create(
                    model="codex",
                    messages=messages,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                chat_frames = [chunk async for chunk in chat_stream]
                dumped = [chunk.model_dump() for chunk in chat_frames]
                text = "".join(
                    chunk.choices[0].delta.content or "" for chunk in chat_frames if chunk.choices
                )
                assert chat_frames[-1].usage is not None
                assert chat_frames[-1].usage.total_tokens == 3
            else:
                completion = await running.client.chat.completions.create(
                    model="codex", messages=messages, stream=False
                )
                dumped = completion.model_dump()
                text = completion.choices[0].message.content
                assert completion.usage is not None
                assert completion.usage.total_tokens == 3
            messages.extend(
                [{"role": "assistant", "content": text}, {"role": "user", "content": "again"}]
            )
            await running.client.chat.completions.create(model="codex", messages=messages)
        else:
            if stream:
                response_stream = await running.client.responses.create(
                    model="codex", input="hello", stream=True
                )
                response_events = [event async for event in response_stream]
                dumped = [event.model_dump() for event in response_events]
                terminal = response_events[-1]
                assert terminal.type == "response.completed"
                response = terminal.response
            else:
                response = await running.client.responses.create(
                    model="codex", input="hello", stream=False
                )
                dumped = response.model_dump()
            assert response.usage is not None
            assert response.usage.total_tokens == 3
            history: list[Any] = [
                {"role": "user", "content": "hello"},
                *[item.model_dump(exclude_none=True) for item in response.output],
                {"role": "user", "content": "again"},
            ]
            await running.client.responses.create(model="codex", input=history)
        _assert_private_absent(json.dumps(dumped).encode())
        _assert_private_absent(json.dumps(running.upstream.calls).encode())
        assert len(running.upstream.calls) == 2
        assert all(call["stream"] is True for call in running.upstream.calls)
        assert all(byte_stream.close_calls == 1 for byte_stream in running.upstream.byte_streams)


def _tree_bounds(root: object, *, cumulative_strings: bool = False) -> dict[str, int]:
    stack = [(root, 1)]
    nodes = depth = 0
    strings = [0]
    while stack:
        value, level = stack.pop()
        nodes += 1
        depth = max(depth, level)
        if isinstance(value, dict):
            strings.extend(len(key.encode()) for key in value)
            stack.extend((child, level + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, level + 1) for child in value)
        elif isinstance(value, str):
            strings.append(len(value.encode()))
    string_bytes = sum(strings) if cumulative_strings else max(strings)
    return {"max_json_depth": depth, "max_json_nodes": nodes, "max_string_bytes": string_bytes}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit",
    [
        "max_sse_event_bytes",
        "max_stream_bytes",
        "max_json_depth",
        "max_json_nodes",
        "max_string_bytes",
    ],
)
async def test_private_metadata_still_counts_against_exact_sse_bounds(limit: str) -> None:
    events = _events()
    frames = [_event(event) for event in events]
    wire = b"".join(frames)
    bounds = {key: max(_tree_bounds(event)[key] for event in events) for key in LIMITS}
    bounds.update(
        max_sse_event_bytes=max(len(frame) - 1 for frame in frames), max_stream_bytes=len(wire)
    )
    parsed = [event async for event in parse_responses_sse(_chunks(wire), **bounds)]
    assert len(parsed) == len(events)
    bounds[limit] -= 1
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        async for _ in parse_responses_sse(_chunks(wire), **bounds):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["max_json_depth", "max_json_nodes", "max_string_bytes"])
async def test_buffered_metadata_is_bounded_before_it_is_stripped(limit: str) -> None:
    events = _events()
    bounds = {key: max(_tree_bounds(event)[key] for event in events) for key in LIMITS}
    bounds[limit] -= 1
    upstream = _StreamingOnlyUpstream(b"".join(_event(event) for event in events))
    settings = replace(
        Settings.from_env(),
        max_json_depth=bounds["max_json_depth"],
        max_json_nodes=bounds["max_json_nodes"],
        max_string_bytes=bounds["max_string_bytes"],
    )
    buffered = BufferedResponsesUpstream(upstream, settings)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await buffered.create_response(_CredentialManager().credential, {})
    assert upstream.stream.close_calls == 1


@pytest.mark.parametrize("surface", ["chat_direct", "responses_direct"])
@pytest.mark.parametrize("limit", ["max_json_depth", "max_json_nodes", "max_string_bytes"])
def test_direct_accounting_respects_exact_tree_bounds(surface: str, limit: str) -> None:
    response = _events()[-1]["response"]
    bounds = _tree_bounds(
        response["usage"] if surface.startswith("chat") else response,
        cumulative_strings=surface.startswith("responses"),
    )
    _public(response, surface, **bounds)
    bounds[limit] -= 1
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public(response, surface, **bounds)


@pytest.mark.asyncio
@pytest.mark.parametrize("counter", COUNTERS)
@pytest.mark.parametrize("record", ["item", "content", "instructions"])
@pytest.mark.parametrize("invalid", [-1, True])
async def test_every_private_counter_rejects_negative_and_bool(
    counter: str,
    record: str,
    invalid: object,
) -> None:
    events = _events()
    attribution = events[-1]["response"]["usage"]["attribution"]
    counts = attribution["items"][PRIVATE_ITEM]
    if record == "content":
        counts = counts["content"][0]
    elif record == "instructions":
        counts = attribution["request_fields"]["instructions"]
    counts[counter] = invalid
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, "chat_stream")
