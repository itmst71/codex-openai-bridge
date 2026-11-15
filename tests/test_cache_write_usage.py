"""Synthetic fixtures for the observed private Codex cache-write TTL breakdown."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from codex_openai_bridge.translation import UpstreamResponseError
from tests.contract._support import completed_text_response
from tests.test_upstream_metadata import SURFACES, _events, _project, _public


def _usage() -> dict[str, Any]:
    return {
        "input_tokens": 8,
        "input_tokens_details": {
            "cached_tokens": 1,
            "cache_write_tokens": 5,
            "cache_write_tokens_details": {"ttl_30m_tokens": 2, "ttl_12h_tokens": 3},
        },
        "output_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 11,
    }


def _assert_public(wire: bytes, surface: str) -> None:
    assert b"cache_write_tokens_details" not in wire
    assert b"ttl_30m_tokens" not in wire
    assert b"ttl_12h_tokens" not in wire
    assert b"hello" in wire
    if surface.endswith("stream"):
        assert wire.endswith(b"data: [DONE]\n\n")
        payloads = [
            json.loads(line[6:])
            for line in wire.splitlines()
            if line.startswith(b"data: ") and line != b"data: [DONE]"
        ]
        public = payloads[-1].get("response", payloads[-1])
    else:
        public = json.loads(wire)
    usage = public["usage"]
    assert usage["total_tokens"] == 11
    if surface.startswith("responses"):
        assert usage["input_tokens_details"] == {"cached_tokens": 1, "cache_write_tokens": 5}
        assert usage["input_tokens"] == 8
        assert usage["output_tokens"] == 3
    else:
        assert usage["prompt_tokens"] == 8
        assert usage["completion_tokens"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_cache_write_breakdown_is_validated_but_private_on_every_surface(
    surface: str,
) -> None:
    events = _events()
    events[-1]["response"]["usage"] = _usage()
    before = deepcopy(events)
    wire = await _project(events, surface)
    _assert_public(wire, surface)
    assert events == before


@pytest.mark.parametrize("surface", ["chat_buffered", "responses_buffered"])
def test_direct_public_converters_drop_cache_write_breakdown(surface: str) -> None:
    response = completed_text_response()
    response["usage"] = _usage()
    before = deepcopy(response)
    wire = json.dumps(_public(response, surface)).encode()
    _assert_public(wire, surface)
    assert response == before


def _invalid_usages() -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    invalid: Any
    for invalid in (None, True, 0, "private", [], {}, {"ttl_30m_tokens": 0}):
        value = _usage()
        value["input_tokens_details"]["cache_write_tokens_details"] = invalid
        values.append(value)
    for key in ("ttl_30m_tokens", "ttl_12h_tokens"):
        for invalid in (None, True, -1, 1.5, "0", [], {}):
            value = _usage()
            value["input_tokens_details"]["cache_write_tokens_details"][key] = invalid
            values.append(value)
    for missing in ("cached_tokens", "cache_write_tokens"):
        value = _usage()
        del value["input_tokens_details"][missing]
        values.append(value)
    for level in ("usage", "input", "output", "breakdown"):
        value = _usage()
        target = (
            value
            if level == "usage"
            else value["input_tokens_details"]
            if level == "input"
            else value["output_tokens_details"]
            if level == "output"
            else value["input_tokens_details"]["cache_write_tokens_details"]
        )
        target["unexpected_private_field"] = 0
        values.append(value)
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("usage", _invalid_usages())
async def test_cache_write_breakdown_fails_closed(surface: str, usage: dict[str, Any]) -> None:
    events = _events()
    events[-1]["response"]["usage"] = deepcopy(usage)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        await _project(events, surface)
