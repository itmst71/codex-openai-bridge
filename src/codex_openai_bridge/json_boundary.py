"""Strict bounded JSON decoding for authenticated request bodies."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from aiohttp import hdrs, web

_READ_CHUNK_BYTES = 64 * 1024
_JSON_CONTENT_TYPE = re.compile(
    r'[ \t]*application/json(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?[ \t]*',
    re.IGNORECASE,
)


class JsonBoundaryError(ValueError):
    """Raised when a request JSON document is malformed or exceeds a bound."""


class JsonBodyTooLarge(JsonBoundaryError):
    """Raised when the authenticated HTTP body exceeds its byte budget."""


def _reject_constant(_value: str) -> None:
    raise ValueError


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _validate_string(value: str, *, max_string_bytes: int) -> None:
    if len(value.encode("utf-8", errors="strict")) > max_string_bytes:
        raise ValueError


def _validate_json_tree(
    root: Any,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ValueError
        if type(value) is dict:
            if nodes + len(stack) + len(value) > max_nodes:
                raise ValueError
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError
                _validate_string(key, max_string_bytes=max_string_bytes)
                stack.append((item, depth + 1))
        elif type(value) is list:
            if nodes + len(stack) + len(value) > max_nodes:
                raise ValueError
            stack.extend((item, depth + 1) for item in value)
        elif type(value) is str:
            _validate_string(value, max_string_bytes=max_string_bytes)
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError
        elif value is not None and type(value) not in (bool, int):
            raise ValueError


def parse_json_bytes(
    raw: bytes,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> Any:
    """Decode one strict UTF-8 JSON document."""
    if type(raw) is not bytes or any(
        type(bound) is not int or bound <= 0 for bound in (max_depth, max_nodes, max_string_bytes)
    ):
        raise JsonBoundaryError("request JSON is invalid")
    try:
        document = raw.decode("utf-8", errors="strict")
        value = json.loads(
            document,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        _validate_json_tree(
            value,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_string_bytes=max_string_bytes,
        )
        return value
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        raise JsonBoundaryError("request JSON is invalid") from None


def validate_json_request_headers(request: web.Request, *, max_body_bytes: int) -> None:
    """Validate media type and declared size without consuming the body."""
    if type(max_body_bytes) is not int or max_body_bytes <= 0:
        raise JsonBoundaryError("request JSON is invalid")
    content_types = request.headers.getall(hdrs.CONTENT_TYPE, [])
    if len(content_types) != 1 or _JSON_CONTENT_TYPE.fullmatch(content_types[0]) is None:
        raise JsonBoundaryError("request JSON is invalid")
    if request.content_length is not None and request.content_length > max_body_bytes:
        raise JsonBodyTooLarge("request body is too large")


async def read_json_request(
    request: web.Request,
    *,
    max_body_bytes: int,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> Any:
    """Collect at most the configured body budget, then strictly decode it."""
    validate_json_request_headers(request, max_body_bytes=max_body_bytes)

    body = bytearray()
    try:
        async for chunk in request.content.iter_chunked(_READ_CHUNK_BYTES):
            remaining = max_body_bytes + 1 - len(body)
            body.extend(chunk[: max(0, remaining)])
            if len(body) > max_body_bytes or len(chunk) > remaining:
                raise JsonBodyTooLarge("request body is too large")
    except JsonBodyTooLarge:
        raise
    except Exception:
        raise JsonBoundaryError("request JSON is invalid") from None
    return parse_json_bytes(
        bytes(body),
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_string_bytes=max_string_bytes,
    )
