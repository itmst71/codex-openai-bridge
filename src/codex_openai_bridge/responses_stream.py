"""Validated direct Responses SSE projection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from codex_openai_bridge.models import ParsedSseEvent
from codex_openai_bridge.responses import (
    ResponsesRequest,
    responses_public_snapshot,
    responses_to_public,
    validate_encrypted_reasoning_content,
    validate_responses_identifier,
)
from codex_openai_bridge.translation import (
    ResponsesStreamValidator,
    UpstreamResponseError,
    parse_responses_sse,
)
from codex_openai_bridge.wire import NamedFunctionToolChoice, encrypted_reasoning_data_digest

_KNOWN_EVENTS = frozenset(
    {
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    }
)


def _invalid() -> UpstreamResponseError:
    return UpstreamResponseError("invalid upstream response")


def _frame(event_type: str, value: dict[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise _invalid() from None
    return b"event: " + event_type.encode("ascii") + b"\ndata: " + encoded + b"\n\n"


def _sequence(event: dict[str, Any]) -> int:
    value = event.get("sequence_number")
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def _project_text_part(value: object, *, expected_text: str) -> dict[str, Any]:
    if (
        type(value) is not dict
        or not {"type", "text"} <= set(value)
        or not set(value) <= {"type", "text", "annotations", "logprobs"}
        or value["type"] != "output_text"
        or value["text"] != expected_text
        or value.get("annotations", []) != []
        or ("logprobs" in value and value["logprobs"] not in (None, []))
    ):
        raise _invalid()
    return {"type": "output_text", "text": expected_text, "annotations": []}


def _project_item(
    value: object,
    *,
    status: str,
    max_string_bytes: int,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise _invalid()
    item_type = value.get("type")
    if item_type == "message":
        if set(value) != {"id", "type", "status", "role", "content"}:
            raise _invalid()
        content = value["content"]
        if value["status"] != status or value["role"] != "assistant" or type(content) is not list:
            raise _invalid()
        parts: list[dict[str, Any]] = []
        for part in content:
            if type(part) is not dict or type(part.get("text")) is not str:
                raise _invalid()
            parts.append(_project_text_part(part, expected_text=part["text"]))
        return {
            "id": value["id"],
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": parts,
        }
    if item_type == "function_call":
        if set(value) != {"id", "type", "status", "call_id", "name", "arguments"}:
            raise _invalid()
        if value["status"] != status:
            raise _invalid()
        return {
            "id": value["id"],
            "type": "function_call",
            "status": status,
            "call_id": value["call_id"],
            "name": value["name"],
            "arguments": value["arguments"],
        }
    if item_type == "reasoning":
        required = {"id", "type", "status", "summary"}
        optional = {"encrypted_content"} if status == "completed" else set()
        if not required <= set(value) or not set(value) <= required | optional:
            raise _invalid()
        summary = value["summary"]
        if (
            value["status"] != status
            or type(summary) is not list
            or any(
                type(part) is not dict
                or set(part) != {"type", "text"}
                or part["type"] != "summary_text"
                or type(part["text"]) is not str
                for part in summary
            )
        ):
            raise _invalid()
        result = {
            "id": value["id"],
            "type": "reasoning",
            "status": status,
            "summary": [],
        }
        if status == "completed":
            if "encrypted_content" not in value:
                raise _invalid()
            result["encrypted_content"] = validate_encrypted_reasoning_content(
                value["encrypted_content"], max_string_bytes=max_string_bytes
            )
        return result
    raise _invalid()


class ResponsesSseTranslator:
    """Stateful validated upstream-to-public Responses SSE iterator."""

    def __init__(
        self,
        chunks: AsyncIterator[bytes],
        *,
        request: ResponsesRequest,
        public_model: str,
        max_items: int,
        max_tools: int,
        max_sse_event_bytes: int,
        max_stream_bytes: int,
        max_json_depth: int,
        max_json_nodes: int,
        max_string_bytes: int,
    ) -> None:
        self._request = request
        self._public_model = public_model
        self._max_items = max_items
        self._max_tools = max_tools
        self._max_json_depth = max_json_depth
        self._max_json_nodes = max_json_nodes
        self._max_string_bytes = max_string_bytes
        self._validator = ResponsesStreamValidator(
            public_model=public_model,
            max_unknown_events=max_json_nodes,
            max_string_bytes=max_string_bytes,
        )
        self._parsed = parse_responses_sse(
            chunks,
            max_sse_event_bytes=max_sse_event_bytes,
            max_stream_bytes=max_stream_bytes,
            max_json_depth=max_json_depth,
            max_json_nodes=max_json_nodes,
            max_string_bytes=max_string_bytes,
        )
        self._created = False
        self._response_id: str | None = None
        self._item_count = 0
        self._message_count = 0
        self._function_count = 0
        self._function_phase = False
        self._visible_output = False
        self._declared_tool_names = {tool.name for tool in request.tools}
        self._required_tool_name = (
            request.tool_choice.name
            if isinstance(request.tool_choice, NamedFunctionToolChoice)
            else None
        )
        self._item_ids: set[str] = set(request.historical_item_ids)
        self._call_ids: set[str] = set(request.historical_call_ids)
        self._reasoning_digests: set[bytes] = set(request.historical_reasoning_digests)
        self._iterator = self._translate()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        return await anext(self._iterator)

    def terminal_error_frame(self) -> bytes:
        """Build the SDK-required bridge terminal error after a prepared response."""
        last = self._validator.last_sequence
        sequence = 0 if last is None else last + 1
        value: dict[str, Any] = {
            "type": "error",
            "code": "upstream_stream_error",
            "message": "Upstream stream failed",
            "param": None,
            "sequence_number": sequence,
        }
        return _frame("error", value)

    def _project(self, parsed: ParsedSseEvent) -> bytes | None:
        if parsed.done:
            return None
        event = parsed.data
        if event is None:
            raise _invalid()
        event_type = event["type"]
        sequence = _sequence(event)
        if event_type not in _KNOWN_EVENTS:
            return None
        if not self._created and event_type != "response.created":
            raise _invalid()
        if event_type in {"response.created", "response.in_progress"}:
            response = event["response"]
            if type(response) is not dict:
                raise _invalid()
            if event_type == "response.created":
                if self._created:
                    raise _invalid()
                self._created = True
            snapshot = responses_public_snapshot(
                self._request,
                response_id=response.get("id"),
                created_at=response.get("created_at"),
                public_model=self._public_model,
            )
            if self._response_id is None:
                if snapshot["id"] in self._item_ids:
                    raise _invalid()
                self._response_id = snapshot["id"]
            return _frame(
                event_type,
                {"type": event_type, "response": snapshot, "sequence_number": sequence},
            )
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            status = "in_progress" if event_type.endswith("added") else "completed"
            item = _project_item(
                event["item"],
                status=status,
                max_string_bytes=self._max_string_bytes,
            )
            item_id = validate_responses_identifier(item.get("id"))
            if item_id == self._response_id:
                raise _invalid()
            if status == "in_progress":
                if item_id in self._item_ids:
                    raise _invalid()
                self._item_ids.add(item_id)
                self._item_count += 1
                if self._item_count > self._max_items:
                    raise _invalid()
                if item["type"] == "message":
                    self._message_count += 1
                    if self._function_phase or self._message_count > 1:
                        raise _invalid()
                    self._visible_output = True
                elif item["type"] == "function_call":
                    call_id = validate_responses_identifier(item.get("call_id"))
                    name = validate_responses_identifier(item.get("name"))
                    self._function_count += 1
                    if (
                        call_id in self._call_ids
                        or name not in self._declared_tool_names
                        or self._request.tool_choice == "none"
                        or (
                            self._required_tool_name is not None
                            and name != self._required_tool_name
                        )
                        or self._function_count > self._max_tools
                        or (self._request.parallel_tool_calls is False and self._function_count > 1)
                    ):
                        raise _invalid()
                    self._call_ids.add(call_id)
                    self._function_phase = True
                    self._visible_output = True
                elif item["type"] == "reasoning":
                    if self._function_phase or self._visible_output:
                        raise _invalid()
            elif item["type"] == "reasoning":
                encrypted = item.get("encrypted_content")
                digest = encrypted_reasoning_data_digest(
                    encrypted,
                    max_string_bytes=self._max_string_bytes,
                )
                if digest is None or digest in self._reasoning_digests:
                    raise _invalid()
                self._reasoning_digests.add(digest)
            return _frame(
                event_type,
                {
                    "type": event_type,
                    "output_index": event["output_index"],
                    "item": item,
                    "sequence_number": sequence,
                },
            )
        if event_type in {"response.content_part.added", "response.content_part.done"}:
            part = event["part"]
            if type(part) is not dict or type(part.get("text")) is not str:
                raise _invalid()
            public_part = _project_text_part(part, expected_text=part["text"])
            return _frame(
                event_type,
                {
                    "type": event_type,
                    "item_id": event["item_id"],
                    "output_index": event["output_index"],
                    "content_index": event["content_index"],
                    "part": public_part,
                    "sequence_number": sequence,
                },
            )
        if event_type in {"response.output_text.delta", "response.output_text.done"}:
            if "logprobs" in event and event["logprobs"] != []:
                raise _invalid()
            text_field = "delta" if event_type.endswith("delta") else "text"
            return _frame(
                event_type,
                {
                    "type": event_type,
                    "item_id": event["item_id"],
                    "output_index": event["output_index"],
                    "content_index": event["content_index"],
                    text_field: event[text_field],
                    "logprobs": [],
                    "sequence_number": sequence,
                },
            )
        if event_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            value_field = "delta" if event_type.endswith("delta") else "arguments"
            return _frame(
                event_type,
                {
                    "type": event_type,
                    "item_id": event["item_id"],
                    "output_index": event["output_index"],
                    value_field: event[value_field],
                    "sequence_number": sequence,
                },
            )
        if event_type == "response.completed":
            public_response = responses_to_public(
                event["response"],
                request=self._request,
                public_model=self._public_model,
                max_items=self._max_items,
                max_tools=self._max_tools,
                max_json_depth=self._max_json_depth,
                max_json_nodes=self._max_json_nodes,
                max_string_bytes=self._max_string_bytes,
            )
            return _frame(
                event_type,
                {
                    "type": event_type,
                    "response": public_response,
                    "sequence_number": sequence,
                },
            )
        raise _invalid()

    async def _translate(self) -> AsyncIterator[bytes]:
        terminal: bytes | None = None
        async for parsed in self._parsed:
            self._validator.feed(parsed)
            projected = self._project(parsed)
            if parsed.done:
                continue
            if parsed.data is not None and parsed.data["type"] == "response.completed":
                terminal = projected
                continue
            if projected is not None:
                yield projected
        if not self._validator.saw_done or terminal is None:
            raise _invalid()
        yield terminal
        yield b"data: [DONE]\n\n"


async def translate_responses_sse_to_public(
    chunks: AsyncIterator[bytes],
    *,
    request: ResponsesRequest,
    public_model: str,
    max_items: int,
    max_tools: int,
    max_sse_event_bytes: int,
    max_stream_bytes: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> AsyncIterator[bytes]:
    """Translate strict upstream SSE into sanitized direct Responses SSE."""
    translator = ResponsesSseTranslator(
        chunks,
        request=request,
        public_model=public_model,
        max_items=max_items,
        max_tools=max_tools,
        max_sse_event_bytes=max_sse_event_bytes,
        max_stream_bytes=max_stream_bytes,
        max_json_depth=max_json_depth,
        max_json_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    async for frame in translator:
        yield frame
