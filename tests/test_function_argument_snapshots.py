"""Synthetic coverage for function arguments present only in their done event."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

import pytest

from codex_openai_bridge.config import Settings
from codex_openai_bridge.models import ParsedSseEvent
from codex_openai_bridge.responses import responses_to_public
from codex_openai_bridge.translation import ResponsesStreamValidator, UpstreamResponseError
from codex_openai_bridge.upstream import BufferedResponsesUpstream
from tests.test_responses_streaming import (
    _CredentialManager,
    _event,
    _parse,
    _StreamingOnlyUpstream,
)
from tests.test_responses_streaming import _translate as _responses_stream
from tests.test_streaming import _tool_events
from tests.test_upstream_metadata import SURFACES
from tests.test_upstream_metadata import _project as _metadata_project


def _events(skipped: tuple[str, ...] = ("fc_1", "fc_2")) -> list[dict[str, Any]]:
    events = cast(list[dict[str, Any]], deepcopy(_tool_events()))
    events[-1]["response"]["object"] = "response"
    events[-1]["response"]["usage"]["input_tokens_details"] = {"cached_tokens": 0}
    events[-1]["response"]["usage"]["output_tokens_details"] = {"reasoning_tokens": 0}
    return [
        event
        for event in events
        if not (
            event["type"] == "response.function_call_arguments.delta"
            and event["item_id"] in skipped
        )
    ]


async def _project(events: list[dict[str, Any]], surface: str) -> bytes:
    events = [{**event, "sequence_number": sequence} for sequence, event in enumerate(events)]
    if surface.startswith("chat"):
        return await _metadata_project(events, surface)
    document: dict[str, object] = {
        "model": "codex",
        "input": "hello",
        "parallel_tool_calls": True,
        "tools": [
            {
                "type": "function",
                "name": name,
                "description": "Synthetic tool",
                "parameters": {"type": "object", "properties": {}},
            }
            for name in ("weather", "time")
        ],
    }
    if surface == "responses_stream":
        return b"".join(await _responses_stream(events, document=document, upstream_done=False))
    upstream = _StreamingOnlyUpstream(b"".join(_event(event) for event in events))
    buffered = BufferedResponsesUpstream(upstream, Settings.from_env())
    try:
        response = await buffered.create_response(_CredentialManager().credential, {})
        return json.dumps(
            responses_to_public(
                response,
                request=_parse(document),
                public_model="codex",
                max_items=16,
                max_tools=8,
                max_json_depth=16,
                max_json_nodes=256,
                max_string_bytes=4096,
            )
        ).encode()
    finally:
        assert upstream.stream.close_calls == 1


def _done(events: list[dict[str, Any]], item_id: str = "fc_1") -> dict[str, Any]:
    return next(
        event
        for event in events
        if event["type"] == "response.function_call_arguments.done" and event["item_id"] == item_id
    )


def _final(events: list[dict[str, Any]], arguments: object) -> None:
    _done(events)["arguments"] = arguments
    for event in events:
        if event["type"] == "response.output_item.done" and event["item"]["id"] == "fc_1":
            event["item"]["arguments"] = arguments
    events[-1]["response"]["output"][0]["arguments"] = arguments


def _assert_result(wire: bytes, surface: str, expected: str = '{"city":"Tokyo"}') -> None:
    if surface == "chat_stream":
        chunks = [
            json.loads(line[6:])
            for line in wire.splitlines()
            if line.startswith(b"data: ") and line != b"data: [DONE]"
        ]
        arguments = {0: "", 1: ""}
        names: dict[int, str] = {}
        for chunk in chunks:
            for choice in chunk["choices"]:
                for call in choice["delta"].get("tool_calls", []):
                    index = call["index"]
                    assert index in arguments
                    function = call["function"]
                    arguments[index] += function.get("arguments", "")
                    if "name" in function:
                        names[index] = function["name"]
        assert arguments == {0: expected, 1: "{}"}
        assert names == {0: "weather", 1: "time"}
        assert any(
            chunk["choices"] and chunk["choices"][0]["finish_reason"] == "tool_calls"
            for chunk in chunks
        )
        assert wire.endswith(b"data: [DONE]\n\n")
        return
    if surface == "responses_stream":
        chunks = [
            json.loads(line[6:])
            for line in wire.splitlines()
            if line.startswith(b"data: ") and line != b"data: [DONE]"
        ]
        public = next(
            event["response"] for event in chunks if event["type"] == "response.completed"
        )
    else:
        public = json.loads(wire)
    if surface.startswith("chat"):
        calls = public["choices"][0]["message"]["tool_calls"]
        assert [(call["function"]["name"], call["function"]["arguments"]) for call in calls] == [
            ("weather", expected),
            ("time", "{}"),
        ]
        assert public["choices"][0]["message"]["content"] is None
    else:
        assert [(item["name"], item["arguments"]) for item in public["output"]] == [
            ("weather", expected),
            ("time", "{}"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("skipped", [("fc_1",), ("fc_2",), ("fc_1", "fc_2")])
async def test_snapshot_only_arguments_preserve_parallel_calls_on_every_surface(
    surface: str, skipped: tuple[str, ...]
) -> None:
    events = _events(skipped)
    before = deepcopy(events)
    _assert_result(await _project(events, surface), surface)
    assert events == before


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_empty_delta_does_not_preclude_a_complete_unicode_argument_snapshot(
    surface: str,
) -> None:
    events = _events()
    events.insert(
        3,
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": "",
        },
    )
    final = '{"city":"雪"}'
    _final(events, final)
    _assert_result(await _project(events, surface), surface, final)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "mutation",
    [
        "prefix",
        "semantic_rewrite",
        "duplicate_done",
        "late_delta",
        "wrong_item",
        "wrong_index",
        "unknown_field",
        "missing_done",
        "item_conflict",
        "terminal_conflict",
    ],
)
async def test_snapshot_acceptance_retains_identity_lifecycle_and_exactness_guards(
    surface: str, mutation: str
) -> None:
    events = _events()
    done = _done(events)
    position = events.index(done)
    if mutation in {"prefix", "semantic_rewrite"}:
        delta = '{"city":' if mutation == "prefix" else '{"city": "Tokyo"}'
        events.insert(
            position,
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "output_index": 0,
                "delta": delta,
            },
        )
    elif mutation == "duplicate_done":
        events.insert(position, deepcopy(done))
    elif mutation == "late_delta":
        events.insert(
            position + 1,
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_1",
                "output_index": 0,
                "delta": " ",
            },
        )
    elif mutation == "wrong_item":
        done["item_id"] = "fc_unknown"
    elif mutation == "wrong_index":
        done["output_index"] = 1
    elif mutation == "unknown_field":
        done["unrecognized"] = 0
    elif mutation == "missing_done":
        events.remove(done)
    elif mutation == "item_conflict":
        item = next(
            event
            for event in events
            if event["type"] == "response.output_item.done" and event["item"]["id"] == "fc_1"
        )
        item["item"] = {**item["item"], "arguments": "{}"}
    else:
        events[-1]["response"]["output"][0] = {
            **events[-1]["response"]["output"][0],
            "arguments": "{}",
        }
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "invalid", ["", "{", "[]", "null", "42", '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', None]
)
async def test_snapshot_arguments_must_be_a_finite_unambiguous_json_object(
    surface: str, invalid: object
) -> None:
    events = _events()
    _final(events, invalid)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)


@pytest.mark.parametrize("over_limit", [False, True])
def test_snapshot_arguments_use_the_existing_accumulated_utf8_byte_bound(over_limit: bool) -> None:
    events = _events()
    final = '{"city":"雪"}' + " " * 20
    _final(events, final)
    validator = ResponsesStreamValidator(
        public_model="codex",
        max_unknown_events=256,
        max_string_bytes=len(final.encode()) - int(over_limit),
    )
    if over_limit:
        with pytest.raises(UpstreamResponseError):
            for event in events:
                validator.feed(ParsedSseEvent(event=None, data=event))
    else:
        for event in events:
            validator.feed(ParsedSseEvent(event=None, data=event))
        validator.finish()
        assert validator.completed_response is not None
