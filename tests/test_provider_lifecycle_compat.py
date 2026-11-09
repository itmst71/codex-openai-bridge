"""Synthetic regressions for observed provider lifecycle changes, without live captures."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from typing import Any

import pytest

from codex_openai_bridge.config import Settings
from codex_openai_bridge.responses import (
    ResponsesRequestError,
    responses_request_to_upstream,
    responses_to_public,
)
from codex_openai_bridge.translation import UpstreamResponseError
from codex_openai_bridge.upstream import BufferedResponsesUpstream
from tests.test_responses_streaming import (
    _compaction_document,
    _compaction_events,
    _CredentialManager,
    _custom_tool_document,
    _custom_tool_events,
    _event,
    _parse,
    _StreamingOnlyUpstream,
    _translate,
)


def _resequence(events: list[dict[str, Any]]) -> None:
    for index, event in enumerate(events):
        event["sequence_number"] = index


async def _project(
    events: list[dict[str, Any]], document: dict[str, object], surface: str
) -> tuple[dict[str, Any], bytes]:
    if surface == "stream":
        frames = await _translate(events, document=document, upstream_done=False)
        assert frames[-1] == b"data: [DONE]\n\n"
        payloads = [json.loads(frame.split(b"data: ", 1)[1]) for frame in frames[:-1]]
        public = next(item["response"] for item in payloads if item["type"] == "response.completed")
        return public, b"".join(frames)
    request = _parse({**document, "stream": False})
    transport = _StreamingOnlyUpstream(b"".join(_event(event) for event in events))
    adapter = BufferedResponsesUpstream(transport, Settings.from_env())
    try:
        response = await adapter.create_response(
            _CredentialManager().credential,
            responses_request_to_upstream(request, upstream_model="synthetic-model"),
        )
        public = responses_to_public(
            response,
            request=request,
            public_model="codex",
            max_items=16,
            max_tools=8,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
        )
        return public, json.dumps(public).encode()
    finally:
        await adapter.aclose()
        assert transport.stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "buffered"])
@pytest.mark.parametrize("nullable_ids", [False, True])
@pytest.mark.parametrize("metadata_position", [None, "before_call", "after_done"])
async def test_custom_terminal_snapshot_matches_done_items_and_keeps_metadata_private(
    surface: str, nullable_ids: bool, metadata_position: str | None
) -> None:
    events = (
        _custom_tool_events(item_id=None, call_id=None) if nullable_ids else _custom_tool_events()
    )
    events[-1]["response"]["output"] = [deepcopy(events[-2]["item"])]
    if metadata_position is not None:
        position = 2 if metadata_position == "before_call" else len(events) - 1
        events.insert(
            position,
            {
                "type": "response.metadata",
                "response_id": "resp_custom",
                "metadata": {"provider_private": "SYNTHETIC_PRIVATE_METADATA"},
            },
        )
    _resequence(events)

    public, wire = await _project(events, _custom_tool_document(), surface)

    assert [item["type"] for item in public["output"]] == ["custom_tool_call"]
    assert public["output"][0]["input"] == '{"city":"Tokyo"}'
    assert b"response.metadata" not in wire
    assert b"SYNTHETIC_PRIVATE_METADATA" not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "buffered"])
@pytest.mark.parametrize(
    "invalid",
    ["wrong_response", "wrong_metadata_type", "extra_field", "before_created", "after_completed"],
)
async def test_custom_metadata_has_closed_identity_schema_and_lifecycle(
    surface: str, invalid: str
) -> None:
    events = _custom_tool_events()
    metadata: dict[str, Any] = {
        "type": "response.metadata",
        "response_id": "resp_custom",
        "metadata": {},
    }
    position = len(events) - 1
    if invalid == "wrong_response":
        metadata["response_id"] = "resp_other"
    elif invalid == "wrong_metadata_type":
        metadata["metadata"] = []
    elif invalid == "extra_field":
        metadata["plaintext"] = "SYNTHETIC_PRIVATE_METADATA"
    elif invalid == "before_created":
        position = 0
    else:
        position = len(events)
    events.insert(position, metadata)
    _resequence(events)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, _custom_tool_document(), surface)


def _updated_compaction_events() -> list[dict[str, Any]]:
    events = _compaction_events()
    result: list[dict[str, Any]] = []
    for event in events:
        if event["type"] == "response.output_item.done" and event["item"]["type"] == "compaction":
            item = event["item"]
            item["encrypted_content"] = base64.b64encode(
                f"synthetic-final-{event['output_index']}".encode()
            ).decode()
            result.append(
                {
                    "type": "response.compaction.compacting",
                    "item_id": item["id"],
                    "output_index": event["output_index"],
                }
            )
        result.append(event)
    result[-1]["response"]["output"] = [
        deepcopy(event["item"]) for event in result if event["type"] == "response.output_item.done"
    ]
    _resequence(result)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "buffered"])
async def test_compacting_transition_accepts_final_state_not_initial_checkpoint(
    surface: str,
) -> None:
    events = _updated_compaction_events()
    public, _wire = await _project(events, _compaction_document(stream=True), surface)
    expected = events[-1]["response"]["output"]
    assert public["output"][0]["encrypted_content"] == expected[0]["encrypted_content"]
    assert public["output"][2]["encrypted_content"] == expected[2]["encrypted_content"]
    assert public["output"][0]["encrypted_content"] != events[2]["item"]["encrypted_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "buffered"])
@pytest.mark.parametrize(
    "invalid",
    [
        "missing_transition",
        "wrong_id",
        "wrong_index",
        "duplicate_transition",
        "extra_field",
        "changed_done_id",
        "reused_final_state",
        "historical_final_state",
        "historical_initial_state",
        "historical_compaction_initial_state",
        "other_initial_state",
        "terminal_conflict",
    ],
)
async def test_compacting_transition_preserves_identity_and_final_state_authority(
    surface: str, invalid: str
) -> None:
    events = _updated_compaction_events()
    document = _compaction_document(stream=True)
    transition = next(
        event for event in events if event["type"] == "response.compaction.compacting"
    )
    done = [
        event
        for event in events
        if event["type"] == "response.output_item.done" and event["item"]["type"] == "compaction"
    ]
    if invalid == "missing_transition":
        events.remove(transition)
    elif invalid == "wrong_id":
        transition["item_id"] = "cmp_other"
    elif invalid == "wrong_index":
        transition["output_index"] = 2
    elif invalid == "duplicate_transition":
        events.insert(events.index(transition), deepcopy(transition))
    elif invalid == "extra_field":
        transition["plaintext"] = "SYNTHETIC_PRIVATE_METADATA"
    elif invalid == "changed_done_id":
        done[0]["item"]["id"] = "cmp_other"
    elif invalid == "reused_final_state":
        done[1]["item"]["encrypted_content"] = done[0]["item"]["encrypted_content"]
        events[-1]["response"]["output"][2] = deepcopy(done[1]["item"])
    elif invalid in {
        "historical_final_state",
        "historical_initial_state",
        "historical_compaction_initial_state",
    }:
        historical_content = (
            done[0]["item"]["encrypted_content"]
            if invalid == "historical_final_state"
            else events[2]["item"]["encrypted_content"]
        )
        historical_item = (
            {
                "id": "cmp_history",
                "type": "compaction",
                "encrypted_content": historical_content,
            }
            if invalid == "historical_compaction_initial_state"
            else {
                "id": "rs_history",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": historical_content,
            }
        )
        document["input"] = [
            historical_item,
            {"role": "user", "content": "continue"},
        ]
    elif invalid == "other_initial_state":
        done[1]["item"]["encrypted_content"] = events[2]["item"]["encrypted_content"]
        events[-1]["response"]["output"][2] = deepcopy(done[1]["item"])
    else:
        events[-1]["response"]["output"][0]["encrypted_content"] = events[2]["item"][
            "encrypted_content"
        ]
    _resequence(events)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, document, surface)


@pytest.mark.parametrize("threshold", [1, 999])
def test_compaction_threshold_below_provider_minimum_is_local_invalid_request(
    threshold: int,
) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "input": "hello",
                "context_management": [{"type": "compaction", "compact_threshold": threshold}],
            }
        )
