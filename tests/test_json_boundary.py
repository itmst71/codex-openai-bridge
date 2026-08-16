from __future__ import annotations

import tracemalloc
from collections.abc import AsyncIterator
from typing import Any

import pytest
from multidict import CIMultiDict

import codex_openai_bridge.json_boundary as json_boundary_module
from codex_openai_bridge.json_boundary import (
    JsonBoundaryError,
    parse_json_bytes,
    read_json_request,
)


class FailingContent:
    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        del size
        yield b"{"
        raise ConnectionResetError("SENSITIVE_TRANSPORT_DETAIL")


class FailingRequest:
    content_type = "application/json"
    charset = None
    content_length = None
    content = FailingContent()
    headers = CIMultiDict({"Content-Type": "application/json"})


class StaticContent:
    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        del size
        yield b"{}"


class DuplicateContentTypeRequest:
    content_type = "application/json"
    charset = None
    content_length = 2
    content = StaticContent()
    headers = CIMultiDict(
        [("Content-Type", "application/json"), ("Content-Type", "application/json")]
    )


@pytest.mark.asyncio
async def test_duplicate_content_type_values_are_rejected() -> None:
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        await read_json_request(
            DuplicateContentTypeRequest(),  # type: ignore[arg-type]
            max_body_bytes=1024,
            max_depth=32,
            max_nodes=20_000,
            max_string_bytes=1024,
        )


@pytest.mark.asyncio
async def test_payload_read_failure_becomes_sanitized_boundary_error() -> None:
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid") as caught:
        await read_json_request(
            FailingRequest(),  # type: ignore[arg-type]
            max_body_bytes=1024,
            max_depth=32,
            max_nodes=20_000,
            max_string_bytes=1024,
        )

    assert caught.value.__cause__ is None
    assert "SENSITIVE_TRANSPORT_DETAIL" not in repr(caught.value)


def _nested_array(depth: int) -> bytes:
    return ("[" * (depth - 1) + "0" + "]" * (depth - 1)).encode("ascii")


def test_accepts_exact_depth_and_rejects_one_over() -> None:
    assert (
        parse_json_bytes(
            _nested_array(32),
            max_depth=32,
            max_nodes=100,
            max_string_bytes=100,
        )
        is not None
    )

    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            _nested_array(33),
            max_depth=32,
            max_nodes=100,
            max_string_bytes=100,
        )


def test_node_limit_rejects_wide_container_without_bulk_pending_stack() -> None:
    wide_value = [None] * 200_000
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            json_boundary_module._validate_json_tree(
                wide_value,
                max_depth=2,
                max_nodes=10,
                max_string_bytes=10,
            )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 1024 * 1024


def test_accepts_exact_node_count_and_rejects_one_over() -> None:
    exact = ("[" + ",".join("0" for _ in range(19_999)) + "]").encode("ascii")
    one_over = ("[" + ",".join("0" for _ in range(20_000)) + "]").encode("ascii")

    assert (
        len(
            parse_json_bytes(
                exact,
                max_depth=2,
                max_nodes=20_000,
                max_string_bytes=10,
            )
        )
        == 19_999
    )
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            one_over,
            max_depth=2,
            max_nodes=20_000,
            max_string_bytes=10,
        )


def test_accepts_exact_utf8_string_bytes_and_rejects_one_over() -> None:
    assert (
        parse_json_bytes(
            '"éé"'.encode(),
            max_depth=1,
            max_nodes=1,
            max_string_bytes=4,
        )
        == "éé"
    )
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            '"ééa"'.encode(),
            max_depth=1,
            max_nodes=1,
            max_string_bytes=4,
        )


@pytest.mark.parametrize("raw", [b'"\\ud800"', b'{"\\ud800":1}', b"1e9999"])
def test_rejects_non_encodable_strings_and_nonfinite_numeric_result(raw: bytes) -> None:
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            raw,
            max_depth=2,
            max_nodes=2,
            max_string_bytes=100,
        )


@pytest.mark.parametrize(
    "raw,max_depth,max_nodes,max_string_bytes",
    [
        (bytearray(b"{}"), 32, 20_000, 100),
        (b"{}", True, 20_000, 100),
        (b"{}", 32, True, 100),
        (b'"x"', 32, 20_000, True),
        (b"{}", 1.0, 20_000, 100),
        (b"{}", 32, 20_000.0, 100),
        (b'"x"', 32, 20_000, 100.0),
        (b"{}", 0, 20_000, 100),
        (b"{}", 32, 0, 100),
        (b"{}", 32, 20_000, 0),
    ],
)
def test_rejects_nonexact_or_nonpositive_parser_inputs(
    raw: Any,
    max_depth: Any,
    max_nodes: Any,
    max_string_bytes: Any,
) -> None:
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            raw,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_string_bytes=max_string_bytes,
        )


def test_parses_valid_utf8_json_object() -> None:
    assert parse_json_bytes(
        b'{"model":"codex","messages":[]}',
        max_depth=32,
        max_nodes=20_000,
        max_string_bytes=256 * 1024,
    ) == {"model": "codex", "messages": []}


def test_accepts_exact_utf8_object_key_bytes_and_rejects_one_over() -> None:
    assert parse_json_bytes(
        '{"éé":0}'.encode(),
        max_depth=2,
        max_nodes=2,
        max_string_bytes=4,
    ) == {"éé": 0}
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            '{"ééa":0}'.encode(),
            max_depth=2,
            max_nodes=2,
            max_string_bytes=4,
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"outer":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":"\xff"}',
        b'{"x":1} trailing',
        b'{"x":',
    ],
)
def test_rejects_non_strict_json_decoder_inputs(raw: bytes) -> None:
    with pytest.raises(JsonBoundaryError, match="request JSON is invalid"):
        parse_json_bytes(
            raw,
            max_depth=32,
            max_nodes=20_000,
            max_string_bytes=256 * 1024,
        )


def test_public_error_is_fixed_and_sanitized() -> None:
    secret = b'{"prompt":"SENSITIVE_BODY"'

    try:
        parse_json_bytes(
            secret,
            max_depth=32,
            max_nodes=20_000,
            max_string_bytes=256 * 1024,
        )
    except JsonBoundaryError as exc:
        assert exc.args == ("request JSON is invalid",)
        assert exc.__cause__ is None
        assert "SENSITIVE_BODY" not in repr(exc)
    else:
        raise AssertionError("invalid JSON was accepted")
