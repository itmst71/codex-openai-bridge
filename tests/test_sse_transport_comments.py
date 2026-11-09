"""Standard SSE comments are transport-only and retain every existing stream bound."""

from __future__ import annotations

import json
from typing import Any

import pytest

from codex_openai_bridge.config import Settings
from codex_openai_bridge.responses_stream import translate_responses_sse_to_public
from codex_openai_bridge.translation import UpstreamResponseError, parse_responses_sse
from codex_openai_bridge.upstream import BufferedResponsesUpstream
from tests.test_responses_streaming import (
    _chunks,
    _CredentialManager,
    _event,
    _parse,
    _StreamingOnlyUpstream,
    _text_events,
)
from tests.test_streaming import _translate as _chat_stream
from tests.test_upstream_metadata import _public

LIMITS: dict[str, Any] = {
    "max_sse_event_bytes": 4096,
    "max_stream_bytes": 65536,
    "max_json_depth": 16,
    "max_json_nodes": 256,
    "max_string_bytes": 4096,
}
SURFACES = ("chat_stream", "responses_stream", "chat_buffered", "responses_buffered")
COMMENT = b": SYNTHETIC_PRIVATE_HEARTBEAT"


def _wire(newline: bytes, upstream_done: bool) -> bytes:
    heartbeat = COMMENT + newline + newline
    result = bytearray(heartbeat)
    for event in _text_events():
        encoded = _event(event).replace(b"\n", newline)
        header, rest = encoded.split(newline, 1)
        result.extend(COMMENT + newline + header + newline + COMMENT + newline + rest)
        result.extend(heartbeat)
    if upstream_done:
        result.extend(b"data: [DONE]" + newline + newline)
    result.extend(heartbeat)
    return bytes(result)


async def _project(wire: bytes, surface: str, chunk_size: int) -> bytes:
    pieces = [wire[index : index + chunk_size] for index in range(0, len(wire), chunk_size)]
    if surface == "chat_stream":
        return b"".join(await _chat_stream(_chunks(*pieces), include_usage=True))
    if surface == "responses_stream":
        return b"".join(
            [
                frame
                async for frame in translate_responses_sse_to_public(
                    _chunks(*pieces),
                    request=_parse({"model": "codex", "input": "hello", "stream": True}),
                    public_model="codex",
                    max_items=16,
                    max_tools=8,
                    **LIMITS,
                )
            ]
        )
    transport = _StreamingOnlyUpstream(wire)
    adapter = BufferedResponsesUpstream(transport, Settings.from_env())
    try:
        response = await adapter.create_response(_CredentialManager().credential, {})
        return json.dumps(_public(response, surface)).encode()
    finally:
        await adapter.aclose()
        assert transport.stream.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("chunk_size", [1, 65536])
@pytest.mark.parametrize("upstream_done", [False, True])
async def test_comments_before_between_and_after_events_are_private_transport_only(
    surface: str, newline: bytes, chunk_size: int, upstream_done: bool
) -> None:
    result = await _project(_wire(newline, upstream_done), surface, chunk_size)
    assert b"hello" in result
    assert b"SYNTHETIC_PRIVATE_HEARTBEAT" not in result
    if surface.endswith("stream"):
        assert result.endswith(b"data: [DONE]\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_comments_alone_cannot_authorize_a_successful_response(surface: str) -> None:
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(COMMENT + b"\n\n", surface, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_comment_only_blocks_have_no_semantic_events(newline: bytes) -> None:
    wire = COMMENT + newline + newline + b":" + newline + newline
    assert [event async for event in parse_responses_sse(_chunks(wire), **LIMITS)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["max_sse_event_bytes", "max_stream_bytes"])
async def test_comments_still_consume_transport_byte_budgets(budget: str) -> None:
    limits = {**LIMITS, budget: 4}
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        [event async for event in parse_responses_sse(_chunks(COMMENT + b"\n\n"), **limits)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [
        b": invalid-utf8-\xff\n\n",
        b": nul-\x00\n\n",
        b"id: provider-id\n\n",
        b"retry: 1000\n\n",
        b": comment\nevent: response.created\n\n",
        b': comment\ndata: {"type":"response.created"}\ndata: {}\n\n',
        b': comment\nevent: response.completed\ndata: {"type":"response.created"}\n\n',
        b": unterminated comment",
    ],
)
async def test_comment_support_does_not_relax_other_framing_rules(wire: bytes) -> None:
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        [event async for event in parse_responses_sse(_chunks(wire), **LIMITS)]
