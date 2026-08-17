"""Deterministic, secret-free protocol conversion."""

from __future__ import annotations

import json
import math
import secrets
import time
from collections.abc import AsyncIterable, AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from codex_openai_bridge.models import ParsedSseEvent, StreamIdentity, StreamUsage
from codex_openai_bridge.wire import (
    ChatCompletionRequest,
    JsonObjectResponseFormat,
    NamedFunctionToolChoice,
    ToolCall,
    create_reasoning_binding_id,
    encrypted_reasoning_data_digest,
    json_schema_for_upstream,
    json_schema_name_for_upstream,
)


class UpstreamResponseError(ValueError):
    """Raised when an upstream response cannot be represented safely."""


def _invalid_stream() -> UpstreamResponseError:
    return UpstreamResponseError("invalid upstream response")


def _validate_stream_json_tree(
    root: object,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise _invalid_stream()
        if type(value) is dict:
            if nodes + len(stack) + len(value) > max_nodes:
                raise _invalid_stream()
            for key, child in value.items():
                if type(key) is not str:
                    raise _invalid_stream()
                try:
                    key_bytes = key.encode("utf-8", errors="strict")
                except UnicodeError:
                    raise _invalid_stream() from None
                if len(key_bytes) > max_string_bytes:
                    raise _invalid_stream()
                stack.append((child, depth + 1))
        elif type(value) is list:
            if nodes + len(stack) + len(value) > max_nodes:
                raise _invalid_stream()
            stack.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeError:
                raise _invalid_stream() from None
            if len(encoded) > max_string_bytes:
                raise _invalid_stream()
        elif type(value) is float:
            if not math.isfinite(value):
                raise _invalid_stream()
        elif value is not None and type(value) not in (bool, int):
            raise _invalid_stream()


def _decode_sse_lines(
    lines: list[bytes],
    *,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> ParsedSseEvent:
    event_name: str | None = None
    data_payload: bytes | None = None
    for index, line in enumerate(lines):
        if line.startswith(b"event:"):
            if event_name is not None or data_payload is not None or index != 0:
                raise _invalid_stream()
            raw_name = line[6:]
            if raw_name.startswith(b" "):
                raw_name = raw_name[1:]
            if not raw_name:
                raise _invalid_stream()
            try:
                event_name = raw_name.decode("ascii", errors="strict")
            except UnicodeError:
                raise _invalid_stream() from None
            if any(character <= " " or character > "~" for character in event_name):
                raise _invalid_stream()
            continue
        if line.startswith(b"data:"):
            if data_payload is not None:
                raise _invalid_stream()
            data_payload = line[5:]
            if data_payload.startswith(b" "):
                data_payload = data_payload[1:]
            if not data_payload:
                raise _invalid_stream()
            continue
        raise _invalid_stream()
    if data_payload is None:
        raise _invalid_stream()
    if data_payload == b"[DONE]":
        if event_name is not None:
            raise _invalid_stream()
        return ParsedSseEvent(event=None, data=None, done=True)
    try:
        text = data_payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_json_float,
        )
    except (UnicodeError, ValueError, OverflowError, RecursionError):
        raise _invalid_stream() from None
    if type(value) is not dict:
        raise _invalid_stream()
    _validate_stream_json_tree(
        value,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    event_type = value.get("type")
    if type(event_type) is not str or not event_type:
        raise _invalid_stream()
    if event_name is not None and event_name != event_type:
        raise _invalid_stream()
    return ParsedSseEvent(event=event_name, data=value)


async def parse_responses_sse(
    chunks: AsyncIterable[bytes],
    *,
    max_sse_event_bytes: int,
    max_stream_bytes: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> AsyncIterator[ParsedSseEvent]:
    """Parse strict single-data-field SSE over arbitrarily fragmented bytes."""
    limits = (
        max_sse_event_bytes,
        max_stream_bytes,
        max_json_depth,
        max_json_nodes,
        max_string_bytes,
    )
    if any(type(limit) is not int or limit <= 0 for limit in limits):
        raise _invalid_stream()
    total_bytes = 0
    event_size = 0
    lines: list[bytes] = []
    line = bytearray()
    pending_cr = False
    async for chunk in chunks:
        if type(chunk) is not bytes or not chunk:
            if type(chunk) is not bytes:
                raise _invalid_stream()
            continue
        if len(chunk) > max_stream_bytes - total_bytes:
            raise _invalid_stream()
        total_bytes += len(chunk)
        for byte in chunk:
            if byte == 0:
                raise _invalid_stream()
            if pending_cr:
                if byte != 10:
                    raise _invalid_stream()
                pending_cr = False
                if not line:
                    if not lines:
                        raise _invalid_stream()
                    yield _decode_sse_lines(
                        lines,
                        max_json_depth=max_json_depth,
                        max_json_nodes=max_json_nodes,
                        max_string_bytes=max_string_bytes,
                    )
                    lines = []
                    event_size = 0
                    continue
                if event_size + len(line) + 2 > max_sse_event_bytes:
                    raise _invalid_stream()
                event_size += len(line) + 2
                lines.append(bytes(line))
                line.clear()
                continue
            if byte == 13:
                pending_cr = True
                continue
            if byte == 10:
                if not line:
                    if not lines:
                        raise _invalid_stream()
                    yield _decode_sse_lines(
                        lines,
                        max_json_depth=max_json_depth,
                        max_json_nodes=max_json_nodes,
                        max_string_bytes=max_string_bytes,
                    )
                    lines = []
                    event_size = 0
                    continue
                if event_size + len(line) + 1 > max_sse_event_bytes:
                    raise _invalid_stream()
                event_size += len(line) + 1
                lines.append(bytes(line))
                line.clear()
                continue
            if event_size + len(line) + 1 > max_sse_event_bytes:
                raise _invalid_stream()
            line.append(byte)
    if pending_cr or line or lines:
        raise _invalid_stream()


@dataclass(slots=True)
class _TextStreamItem:
    output_index: int
    item_id: str
    text: str = ""
    content_added: bool = False
    text_done: bool = False
    part_done: bool = False
    item_done: bool = False


@dataclass(slots=True)
class _ToolStreamItem:
    output_index: int
    tool_index: int
    item_id: str
    call_id: str
    name: str
    arguments: str = ""
    arguments_done: bool = False
    item_done: bool = False


@dataclass(slots=True)
class _ReasoningStreamItem:
    output_index: int
    item_id: str


def _json_type_exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        left_dict = cast(dict[str, object], left)
        right_dict = cast(dict[str, object], right)
        if set(left_dict) != set(right_dict):
            return False
        return all(_json_type_exact_equal(left_dict[key], right_dict[key]) for key in left_dict)
    if type(left) is list:
        left_list = cast(list[object], left)
        right_list = cast(list[object], right)
        return len(left_list) == len(right_list) and all(
            _json_type_exact_equal(left_item, right_item)
            for left_item, right_item in zip(left_list, right_list, strict=True)
        )
    return left == right


def _exact_fields(
    event: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    allowed = required | (optional or set())
    if not required <= set(event) or not set(event) <= allowed:
        raise _invalid_stream()


def _nonempty_string(value: object) -> str:
    if type(value) is not str or not value:
        raise _invalid_stream()
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid_stream()
    return value


def _empty_logprobs(value: object) -> bool:
    return value is None or (type(value) is list and not value)


def _validate_stream_usage(value: object) -> StreamUsage:
    if type(value) is not dict:
        raise _invalid_stream()
    base_fields = {"input_tokens", "output_tokens", "total_tokens"}
    detail_fields = {"input_tokens_details", "output_tokens_details"}
    if set(value) not in (base_fields, base_fields | detail_fields):
        raise _invalid_stream()
    prompt = _nonnegative_int(value["input_tokens"])
    completion = _nonnegative_int(value["output_tokens"])
    total = _nonnegative_int(value["total_tokens"])
    if detail_fields <= set(value):
        input_details = value["input_tokens_details"]
        output_details = value["output_tokens_details"]
        if (
            type(input_details) is not dict
            or set(input_details)
            not in (
                {"cached_tokens"},
                {"cached_tokens", "cache_write_tokens"},
            )
            or type(output_details) is not dict
            or set(output_details) != {"reasoning_tokens"}
        ):
            raise _invalid_stream()
        _nonnegative_int(input_details["cached_tokens"])
        if "cache_write_tokens" in input_details:
            _nonnegative_int(input_details["cache_write_tokens"])
        _nonnegative_int(output_details["reasoning_tokens"])
    return StreamUsage(prompt, completion, total)


class _ResponsesStreamTranslator:
    def __init__(
        self,
        *,
        public_model: str,
        include_usage: bool,
        max_unknown_events: int,
        max_string_bytes: int,
    ) -> None:
        if (
            type(public_model) is not str
            or not public_model
            or type(include_usage) is not bool
            or type(max_unknown_events) is not int
            or max_unknown_events <= 0
            or type(max_string_bytes) is not int
            or max_string_bytes <= 0
        ):
            raise _invalid_stream()
        self.public_model = public_model
        self.include_usage = include_usage
        self.max_unknown_events = max_unknown_events
        self.max_string_bytes = max_string_bytes
        self.identity = StreamIdentity(
            response_id="chatcmpl-" + secrets.token_hex(12), created=int(time.time())
        )
        self.identity_from_upstream = False
        self.visible = False
        self.next_output_index = 0
        self.next_tool_index = 0
        self.text_item: _TextStreamItem | None = None
        self.tools: dict[str, _ToolStreamItem] = {}
        self.items_by_index: dict[
            int, _TextStreamItem | _ToolStreamItem | _ReasoningStreamItem
        ] = {}
        self.item_ids: set[str] = set()
        self.done_items: dict[int, dict[str, Any]] = {}
        self.projected_done_items: dict[int, dict[str, Any]] = {}
        self.last_sequence: int | None = None
        self.setup_events: set[str] = set()
        self.unknown_events = 0
        self.completed = False
        self.completed_response: dict[str, Any] | None = None
        self.saw_done = False
        self.usage: StreamUsage | None = None

    def _base_chunk(self, choices: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": self.identity.response_id,
            "object": "chat.completion.chunk",
            "created": self.identity.created,
            "model": self.public_model,
            "choices": choices,
        }

    def _frame(self, value: dict[str, Any]) -> bytes:
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeError):
            raise _invalid_stream() from None
        return b"data: " + encoded + b"\n\n"

    def _role_frame(self) -> bytes:
        return self._frame(
            self._base_chunk(
                [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ]
            )
        )

    def _visible_frames(self, frame: bytes) -> tuple[bytes, ...]:
        if self.visible:
            return (frame,)
        self.visible = True
        return (self._role_frame(), frame)

    def _validate_sequence(self, event: dict[str, Any]) -> None:
        if "sequence_number" not in event:
            return
        sequence = _nonnegative_int(event["sequence_number"])
        if self.last_sequence is not None and sequence <= self.last_sequence:
            raise _invalid_stream()
        self.last_sequence = sequence

    def _validate_identity(self, response: object, *, status: str) -> dict[str, Any]:
        if type(response) is not dict:
            raise _invalid_stream()
        response_id = _nonempty_string(response.get("id"))
        created = _nonnegative_int(response.get("created_at"))
        if response.get("status") != status:
            raise _invalid_stream()
        if self.identity_from_upstream:
            if response_id != self.identity.response_id or created != self.identity.created:
                raise _invalid_stream()
        elif not self.visible:
            self.identity = StreamIdentity(response_id=response_id, created=created)
            self.identity_from_upstream = True
        return response

    def _validate_output_index(self, event: dict[str, Any]) -> int:
        return _nonnegative_int(event.get("output_index"))

    def _message_item(self, value: object, *, status: str) -> dict[str, Any]:
        if type(value) is not dict:
            raise _invalid_stream()
        required = {"id", "type", "status", "role", "content"}
        if not required <= set(value) or not set(value) <= required | {"phase"}:
            raise _invalid_stream()
        content = value["content"]
        if (
            value["type"] != "message"
            or value["status"] != status
            or value["role"] != "assistant"
            or type(content) is not list
            or ("phase" in value and value["phase"] != "final_answer")
        ):
            raise _invalid_stream()
        _nonempty_string(value["id"])
        canonical_content: list[dict[str, Any]] = []
        for part in content:
            if type(part) is not dict or type(part.get("text")) is not str:
                raise _invalid_stream()
            canonical_content.append(self._text_part(part, expected_text=part["text"]))
        return {
            "id": value["id"],
            "type": value["type"],
            "status": value["status"],
            "role": value["role"],
            "content": canonical_content,
        }

    def _text_part(self, value: object, *, expected_text: str) -> dict[str, Any]:
        if type(value) is not dict:
            raise _invalid_stream()
        required = {"type", "text"}
        allowed = required | {"annotations", "logprobs"}
        if not required <= set(value) or not set(value) <= allowed:
            raise _invalid_stream()
        if value["type"] != "output_text" or value["text"] != expected_text:
            raise _invalid_stream()
        if "annotations" in value and value["annotations"] != []:
            raise _invalid_stream()
        if "logprobs" in value and not _empty_logprobs(value["logprobs"]):
            raise _invalid_stream()
        return {"type": "output_text", "text": expected_text, "annotations": []}

    def _function_item(self, value: object, *, status: str) -> dict[str, Any]:
        if type(value) is not dict:
            raise _invalid_stream()
        required = {"id", "type", "status", "call_id", "name", "arguments"}
        if set(value) != required:
            raise _invalid_stream()
        if value["type"] != "function_call" or value["status"] != status:
            raise _invalid_stream()
        _nonempty_string(value["id"])
        _nonempty_string(value["call_id"])
        _nonempty_string(value["name"])
        if type(value["arguments"]) is not str:
            raise _invalid_stream()
        return value

    def _reasoning_item(self, value: object, *, status: str) -> dict[str, Any]:
        if type(value) is not dict:
            raise _invalid_stream()
        if (
            value.get("type") != "reasoning"
            or value.get("status") != status
            or type(value.get("id")) is not str
            or not value["id"]
            or ("summary" in value and type(value["summary"]) is not list)
            or (
                "encrypted_content" in value
                and (type(value["encrypted_content"]) is not str or not value["encrypted_content"])
            )
        ):
            raise _invalid_stream()
        return value

    def _on_output_added(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "output_index", "item"},
            {"sequence_number"},
        )
        output_index = self._validate_output_index(event)
        if output_index != self.next_output_index:
            raise _invalid_stream()
        self.next_output_index += 1
        item = event["item"]
        if type(item) is not dict:
            raise _invalid_stream()
        item_type = item.get("type")
        if item_type == "message":
            message = self._message_item(item, status="in_progress")
            item_id = _nonempty_string(message["id"])
            if self.text_item is not None or item_id in self.item_ids or message["content"] != []:
                raise _invalid_stream()
            self.item_ids.add(item_id)
            text_state = _TextStreamItem(output_index, item_id)
            self.text_item = text_state
            self.items_by_index[output_index] = text_state
            return ()
        if item_type == "function_call":
            function = self._function_item(item, status="in_progress")
            if function["arguments"] != "":
                raise _invalid_stream()
            item_id = _nonempty_string(function["id"])
            call_id = _nonempty_string(function["call_id"])
            if item_id in self.item_ids or any(
                tool.call_id == call_id for tool in self.tools.values()
            ):
                raise _invalid_stream()
            self.item_ids.add(item_id)
            tool_state = _ToolStreamItem(
                output_index=output_index,
                tool_index=self.next_tool_index,
                item_id=item_id,
                call_id=call_id,
                name=_nonempty_string(function["name"]),
            )
            self.next_tool_index += 1
            self.tools[item_id] = tool_state
            self.items_by_index[output_index] = tool_state
            frame = self._frame(
                self._base_chunk(
                    [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": tool_state.tool_index,
                                        "id": tool_state.call_id,
                                        "type": "function",
                                        "function": {"name": tool_state.name, "arguments": ""},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                )
            )
            return self._visible_frames(frame)
        if item_type == "reasoning":
            reasoning = self._reasoning_item(item, status="in_progress")
            reasoning_id = _nonempty_string(reasoning["id"])
            if reasoning_id in self.item_ids:
                raise _invalid_stream()
            self.item_ids.add(reasoning_id)
            self.items_by_index[output_index] = _ReasoningStreamItem(
                output_index=output_index,
                item_id=reasoning_id,
            )
            return ()
        raise _invalid_stream()

    def _matching_text_event(self, event: dict[str, Any]) -> _TextStreamItem:
        state = self.text_item
        if state is None:
            raise _invalid_stream()
        if (
            _nonempty_string(event.get("item_id")) != state.item_id
            or self._validate_output_index(event) != state.output_index
            or _nonnegative_int(event.get("content_index")) != 0
        ):
            raise _invalid_stream()
        return state

    def _on_content_added(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "item_id", "output_index", "content_index", "part"},
            {"sequence_number"},
        )
        state = self._matching_text_event(event)
        if state.content_added:
            raise _invalid_stream()
        self._text_part(event["part"], expected_text="")
        state.content_added = True
        return ()

    def _on_text_delta(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "item_id", "output_index", "content_index", "delta"},
            {"sequence_number", "logprobs", "obfuscation"},
        )
        state = self._matching_text_event(event)
        delta = event["delta"]
        if not state.content_added or state.text_done or type(delta) is not str:
            raise _invalid_stream()
        if "logprobs" in event and not _empty_logprobs(event["logprobs"]):
            raise _invalid_stream()
        if "obfuscation" in event and (
            type(event["obfuscation"]) is not str
            or len(event["obfuscation"].encode("utf-8", errors="strict")) > self.max_string_bytes
        ):
            raise _invalid_stream()
        if len((state.text + delta).encode("utf-8", errors="strict")) > self.max_string_bytes:
            raise _invalid_stream()
        state.text += delta
        if not delta:
            return ()
        frame = self._frame(
            self._base_chunk(
                [
                    {
                        "index": 0,
                        "delta": {"content": delta},
                        "finish_reason": None,
                    }
                ]
            )
        )
        return self._visible_frames(frame)

    def _on_text_done(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "item_id", "output_index", "content_index", "text"},
            {"sequence_number", "logprobs"},
        )
        state = self._matching_text_event(event)
        if not state.content_added or state.text_done or event["text"] != state.text:
            raise _invalid_stream()
        if "logprobs" in event and not _empty_logprobs(event["logprobs"]):
            raise _invalid_stream()
        state.text_done = True
        return ()

    def _on_content_done(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "item_id", "output_index", "content_index", "part"},
            {"sequence_number"},
        )
        state = self._matching_text_event(event)
        if not state.text_done or state.part_done:
            raise _invalid_stream()
        self._text_part(event["part"], expected_text=state.text)
        state.part_done = True
        return ()

    def _on_arguments_delta(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "item_id", "output_index", "delta"},
            {"sequence_number", "obfuscation"},
        )
        item_id = _nonempty_string(event["item_id"])
        state = self.tools.get(item_id)
        delta = event["delta"]
        if (
            state is None
            or self._validate_output_index(event) != state.output_index
            or state.arguments_done
            or type(delta) is not str
            or (
                "obfuscation" in event
                and (
                    type(event["obfuscation"]) is not str
                    or len(event["obfuscation"].encode("utf-8", errors="strict"))
                    > self.max_string_bytes
                )
            )
        ):
            raise _invalid_stream()
        if len((state.arguments + delta).encode("utf-8", errors="strict")) > self.max_string_bytes:
            raise _invalid_stream()
        state.arguments += delta
        if not delta:
            return ()
        frame = self._frame(
            self._base_chunk(
                [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": state.tool_index,
                                    "function": {"arguments": delta},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            )
        )
        return self._visible_frames(frame)

    def _on_arguments_done(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(
            event,
            {"type", "item_id", "output_index", "arguments"},
            {"sequence_number"},
        )
        item_id = _nonempty_string(event["item_id"])
        state = self.tools.get(item_id)
        if (
            state is None
            or self._validate_output_index(event) != state.output_index
            or state.arguments_done
            or event["arguments"] != state.arguments
        ):
            raise _invalid_stream()
        _validate_arguments_object(state.arguments)
        state.arguments_done = True
        return ()

    def _on_item_done(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(event, {"type", "output_index", "item"}, {"sequence_number"})
        output_index = self._validate_output_index(event)
        if output_index not in self.items_by_index or output_index in self.done_items:
            raise _invalid_stream()
        state = self.items_by_index[output_index]
        item = event["item"]
        if isinstance(state, _ReasoningStreamItem):
            projected_item = self._reasoning_item(item, status="completed")
            if projected_item["id"] != state.item_id:
                raise _invalid_stream()
        elif isinstance(state, _TextStreamItem):
            projected_item = self._message_item(item, status="completed")
            if not state.part_done or state.item_done or projected_item["id"] != state.item_id:
                raise _invalid_stream()
            content = projected_item["content"]
            if len(content) != 1:
                raise _invalid_stream()
            self._text_part(content[0], expected_text=state.text)
            state.item_done = True
        elif isinstance(state, _ToolStreamItem):
            projected_item = self._function_item(item, status="completed")
            if (
                not state.arguments_done
                or state.item_done
                or projected_item["id"] != state.item_id
                or projected_item["call_id"] != state.call_id
                or projected_item["name"] != state.name
                or projected_item["arguments"] != state.arguments
            ):
                raise _invalid_stream()
            state.item_done = True
        else:
            raise _invalid_stream()
        self.done_items[output_index] = item
        self.projected_done_items[output_index] = projected_item
        return ()

    def _on_completed(self, event: dict[str, Any]) -> tuple[bytes, ...]:
        _exact_fields(event, {"type", "response"}, {"sequence_number"})
        if self.completed:
            raise _invalid_stream()
        response = self._validate_identity(event["response"], status="completed")
        output = response.get("output")
        usage = response.get("usage")
        if (
            type(output) is not list
            or type(usage) is not dict
            or set(self.done_items) != set(range(self.next_output_index))
            or set(self.projected_done_items) != set(range(self.next_output_index))
            or (output and len(output) != self.next_output_index)
        ):
            raise _invalid_stream()
        if output:
            for index, item in enumerate(output):
                if not _json_type_exact_equal(item, self.done_items[index]):
                    raise _invalid_stream()
        self.usage = _validate_stream_usage(usage)
        if self.text_item is None and not self.tools:
            raise _invalid_stream()
        self.completed_response = dict(response)
        self.completed_response["output"] = [
            deepcopy(self.projected_done_items[index]) for index in range(self.next_output_index)
        ]
        self.completed = True
        return ()

    def finish(self) -> tuple[bytes, ...]:
        if self.saw_done or not self.completed or self.usage is None:
            raise _invalid_stream()
        self.saw_done = True
        frames: list[bytes] = []
        if not self.visible:
            self.visible = True
            frames.append(self._role_frame())
        finish_reason = "tool_calls" if self.tools else "stop"
        frames.append(
            self._frame(
                self._base_chunk([{"index": 0, "delta": {}, "finish_reason": finish_reason}])
            )
        )
        if self.include_usage:
            usage_chunk = self._base_chunk([])
            usage_chunk["usage"] = self.usage.as_dict()
            frames.append(self._frame(usage_chunk))
        frames.append(b"data: [DONE]\n\n")
        return tuple(frames)

    def feed(self, parsed: ParsedSseEvent) -> tuple[bytes, ...]:
        if self.saw_done:
            raise _invalid_stream()
        if parsed.done:
            return self.finish()
        if self.completed or parsed.data is None:
            raise _invalid_stream()
        event = parsed.data
        self._validate_sequence(event)
        event_type = event["type"]
        handlers = {
            "response.output_item.added": self._on_output_added,
            "response.content_part.added": self._on_content_added,
            "response.output_text.delta": self._on_text_delta,
            "response.output_text.done": self._on_text_done,
            "response.content_part.done": self._on_content_done,
            "response.function_call_arguments.delta": self._on_arguments_delta,
            "response.function_call_arguments.done": self._on_arguments_done,
            "response.output_item.done": self._on_item_done,
            "response.completed": self._on_completed,
        }
        if event_type in {"response.created", "response.in_progress"}:
            _exact_fields(event, {"type", "response"}, {"sequence_number"})
            if (
                self.next_output_index != 0
                or self.visible
                or event_type in self.setup_events
                or (
                    event_type == "response.created" and "response.in_progress" in self.setup_events
                )
            ):
                raise _invalid_stream()
            status = "in_progress"
            self._validate_identity(event["response"], status=status)
            self.setup_events.add(event_type)
            return ()
        if event_type in {"response.failed", "response.incomplete"}:
            raise _invalid_stream()
        handler = handlers.get(event_type)
        if handler is not None:
            return handler(event)
        if any(token in event_type for token in ("completed", "failed", "incomplete", ".done")):
            raise _invalid_stream()
        self.unknown_events += 1
        if self.unknown_events > self.max_unknown_events:
            raise _invalid_stream()
        return ()


class ResponsesStreamValidator:
    """Narrow reusable validator for one strict Responses event stream."""

    def __init__(
        self,
        *,
        public_model: str,
        max_unknown_events: int,
        max_string_bytes: int,
    ) -> None:
        self._translator = _ResponsesStreamTranslator(
            public_model=public_model,
            include_usage=False,
            max_unknown_events=max_unknown_events,
            max_string_bytes=max_string_bytes,
        )

    def feed(self, event: ParsedSseEvent) -> None:
        """Validate an event while deliberately discarding Chat wire frames."""
        self._translator.feed(event)

    def finish(self) -> None:
        """Validate completed authority at a clean upstream EOF."""
        self._translator.finish()

    @property
    def saw_done(self) -> bool:
        return self._translator.saw_done

    @property
    def last_sequence(self) -> int | None:
        return self._translator.last_sequence

    @property
    def completed_response(self) -> dict[str, Any] | None:
        return self._translator.completed_response


async def translate_responses_sse(
    chunks: AsyncIterator[bytes],
    *,
    public_model: str,
    include_usage: bool,
    max_sse_event_bytes: int,
    max_stream_bytes: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> AsyncIterator[bytes]:
    """Translate a strict Codex Responses SSE stream into ChatCompletionChunk SSE."""
    translator = _ResponsesStreamTranslator(
        public_model=public_model,
        include_usage=include_usage,
        max_unknown_events=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    terminal_frames: tuple[bytes, ...] | None = None
    async for event in parse_responses_sse(
        chunks,
        max_sse_event_bytes=max_sse_event_bytes,
        max_stream_bytes=max_stream_bytes,
        max_json_depth=max_json_depth,
        max_json_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    ):
        frames = translator.feed(event)
        if event.done:
            terminal_frames = frames
            continue
        for frame in frames:
            yield frame
    if terminal_frames is None:
        terminal_frames = translator.finish()
    for frame in terminal_frames:
        yield frame


def chat_request_to_responses(
    request: ChatCompletionRequest,
    *,
    upstream_model: str,
) -> dict[str, Any]:
    """Translate one validated text-only request to Responses format."""
    payload: dict[str, Any] = {
        "model": upstream_model,
        "input": [],
        "store": False,
        "stream": request.stream,
        "include": ["reasoning.encrypted_content"],
    }
    instructions = [
        message.content
        for message in request.messages
        if message.role in {"system", "developer"} and message.content is not None
    ]
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    translated_input: list[dict[str, Any]] = []
    for message in request.messages:
        for detail in message.reasoning_details:
            translated_input.append({"type": "reasoning", "encrypted_content": detail.data})
        if message.role in {"user", "assistant"} and message.content is not None:
            translated_input.append(
                {
                    "role": message.role,
                    "content": [
                        {
                            "type": "input_text" if message.role == "user" else "output_text",
                            "text": message.content,
                        }
                    ],
                }
            )
        elif message.role == "assistant" and message.reasoning_details and not message.tool_calls:
            translated_input.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ""}],
                }
            )
        for call in message.tool_calls:
            translated_input.append(
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
            )
        if message.role == "tool":
            translated_input.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
    payload["input"] = translated_input
    # The ChatGPT Codex Responses endpoint rejects max_output_tokens. Keep the
    # public aliases parse-compatible for Honcho/OpenAI SDK callers, but rely on
    # the bridge's response-byte and deadline bounds instead of forwarding it.
    if request.tools:
        payload["tools"] = []
        for tool in request.tools:
            translated_tool: dict[str, Any] = {
                "type": "function",
                "name": tool.name,
                "parameters": deepcopy(tool.parameters),
            }
            if tool.description is not None:
                translated_tool["description"] = tool.description
            if tool.strict is not None:
                translated_tool["strict"] = tool.strict
            payload["tools"].append(translated_tool)
    if request.tool_choice is not None:
        if isinstance(request.tool_choice, NamedFunctionToolChoice):
            payload["tool_choice"] = {"type": "function", "name": request.tool_choice.name}
        else:
            payload["tool_choice"] = request.tool_choice
    if request.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = request.parallel_tool_calls
    if request.response_format is not None:
        if isinstance(request.response_format, JsonObjectResponseFormat):
            response_format: dict[str, Any] = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "name": json_schema_name_for_upstream(request.response_format.name),
                "schema": json_schema_for_upstream(request.response_format.schema),
            }
            if request.response_format.description is not None:
                response_format["description"] = request.response_format.description
            if request.response_format.strict is not None:
                response_format["strict"] = request.response_format.strict
        payload["text"] = {"format": response_format}
    return payload


def _exact_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise UpstreamResponseError("invalid upstream response")
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _validate_arguments_object(value: object) -> str:
    if type(value) is not str or not value:
        raise UpstreamResponseError("invalid upstream response")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_json_float,
        )
    except (ValueError, OverflowError, RecursionError):
        raise UpstreamResponseError("invalid upstream response") from None
    if type(parsed) is not dict:
        raise UpstreamResponseError("invalid upstream response")
    return value


def _validate_bounded_reasoning_tree(
    root: object,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise UpstreamResponseError("invalid upstream response")
        if type(item) is dict:
            if nodes + len(stack) + len(item) > max_nodes:
                raise UpstreamResponseError("invalid upstream response")
            for key, child in item.items():
                if type(key) is not str:
                    raise UpstreamResponseError("invalid upstream response")
                try:
                    encoded_key = key.encode("utf-8", errors="strict")
                except UnicodeError:
                    raise UpstreamResponseError("invalid upstream response") from None
                if len(encoded_key) > max_string_bytes:
                    raise UpstreamResponseError("invalid upstream response")
                stack.append((child, depth + 1))
        elif type(item) is list:
            if nodes + len(stack) + len(item) > max_nodes:
                raise UpstreamResponseError("invalid upstream response")
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            try:
                encoded_item = item.encode("utf-8", errors="strict")
            except UnicodeError:
                raise UpstreamResponseError("invalid upstream response") from None
            if len(encoded_item) > max_string_bytes:
                raise UpstreamResponseError("invalid upstream response")
        elif type(item) is float:
            if not math.isfinite(item):
                raise UpstreamResponseError("invalid upstream response")
        elif item is not None and type(item) not in (bool, int):
            raise UpstreamResponseError("invalid upstream response")


def responses_to_chat_completion(
    value: object,
    *,
    public_model: str,
    binding_key: str,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> dict[str, Any]:
    """Convert one completed assistant Responses result to Chat Completions format."""
    try:
        if (
            type(value) is not dict
            or type(public_model) is not str
            or not public_model
            or type(binding_key) is not str
            or not binding_key
            or type(max_json_depth) is not int
            or max_json_depth <= 0
            or type(max_json_nodes) is not int
            or max_json_nodes <= 0
            or type(max_string_bytes) is not int
            or max_string_bytes <= 0
        ):
            raise UpstreamResponseError("invalid upstream response")
        response: dict[str, Any] = value
        response_id = response["id"]
        created_at = response["created_at"]
        output = response["output"]
        usage = response["usage"]
        if (
            type(response_id) is not str
            or not response_id
            or response.get("status") != "completed"
            or type(created_at) is not int
            or created_at < 0
            or type(output) is not list
            or type(usage) is not dict
        ):
            raise UpstreamResponseError("invalid upstream response")

        assistant_messages: list[dict[str, Any]] = []
        function_calls: list[dict[str, str]] = []
        reasoning_items: list[dict[str, Any]] = []
        call_ids: set[str] = set()
        for item in output:
            if type(item) is not dict:
                raise UpstreamResponseError("invalid upstream response")
            if item.get("type") == "reasoning":
                if len(reasoning_items) >= max_json_nodes:
                    raise UpstreamResponseError("invalid upstream response")
                reasoning_items.append(item)
                continue
            if item.get("type") == "function_call":
                required_call_fields = {"type", "status", "call_id", "name", "arguments"}
                if not required_call_fields <= set(item) or not set(item) <= (
                    required_call_fields | {"id"}
                ):
                    raise UpstreamResponseError("invalid upstream response")
                call_id = item["call_id"]
                name = item["name"]
                upstream_item_id = item.get("id")
                if (
                    type(item["type"]) is not str
                    or item["type"] != "function_call"
                    or type(item["status"]) is not str
                    or item["status"] != "completed"
                    or type(call_id) is not str
                    or not call_id
                    or call_id in call_ids
                    or type(name) is not str
                    or not name
                    or (
                        "id" in item and (type(upstream_item_id) is not str or not upstream_item_id)
                    )
                ):
                    raise UpstreamResponseError("invalid upstream response")
                arguments = _validate_arguments_object(item["arguments"])
                call_ids.add(call_id)
                function_calls.append({"call_id": call_id, "name": name, "arguments": arguments})
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
            ):
                raise UpstreamResponseError("invalid upstream response")
            assistant_messages.append(item)
        if len(assistant_messages) > 1 or (not assistant_messages and not function_calls):
            raise UpstreamResponseError("invalid upstream response")
        _validate_bounded_reasoning_tree(
            reasoning_items,
            max_depth=max_json_depth,
            max_nodes=max_json_nodes,
            max_string_bytes=max_string_bytes,
        )
        encrypted_items: list[str] = []
        encrypted_digests: set[bytes] = set()
        for item in reasoning_items:
            if "encrypted_content" not in item:
                continue
            encrypted_content = item["encrypted_content"]
            encrypted_digest = encrypted_reasoning_data_digest(
                encrypted_content,
                max_string_bytes=max_string_bytes,
            )
            if (
                ("status" in item and item["status"] != "completed")
                or encrypted_digest is None
                or (encrypted_digest in encrypted_digests)
            ):
                raise UpstreamResponseError("invalid upstream response")
            encrypted_digests.add(encrypted_digest)
            encrypted_items.append(encrypted_content)
        text_parts: list[str] = []
        if assistant_messages:
            content = assistant_messages[0].get("content")
            if type(content) is not list or not content:
                raise UpstreamResponseError("invalid upstream response")
            for part in content:
                if type(part) is not dict or part.get("type") != "output_text":
                    raise UpstreamResponseError("invalid upstream response")
                text = part.get("text")
                if type(text) is not str:
                    raise UpstreamResponseError("invalid upstream response")
                text_parts.append(text)

        prompt_tokens = _exact_nonnegative_int(usage.get("input_tokens"))
        completion_tokens = _exact_nonnegative_int(usage.get("output_tokens"))
        total_tokens = _exact_nonnegative_int(usage.get("total_tokens"))
    except (KeyError, TypeError):
        raise UpstreamResponseError("invalid upstream response") from None

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) if assistant_messages else None,
    }
    finish_reason = "stop"
    if function_calls:
        message["tool_calls"] = [
            {
                "id": call["call_id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in function_calls
        ]
        finish_reason = "tool_calls"
    if encrypted_items:
        bound_calls = tuple(
            ToolCall(
                call_id=call["call_id"],
                name=call["name"],
                arguments=call["arguments"],
            )
            for call in function_calls
        )
        try:
            public_reasoning_details = [
                {
                    "type": "reasoning.encrypted",
                    "data": data,
                    "format": "openai-responses-v1",
                    "id": create_reasoning_binding_id(
                        binding_key=binding_key,
                        content=message["content"],
                        tool_calls=bound_calls,
                        index=index,
                        data=data,
                    ),
                    "index": index,
                }
                for index, data in enumerate(encrypted_items)
            ]
        except ValueError:
            raise UpstreamResponseError("invalid upstream response") from None
        _validate_bounded_reasoning_tree(
            public_reasoning_details,
            max_depth=max_json_depth,
            max_nodes=max_json_nodes,
            max_string_bytes=max_string_bytes,
        )
        message["reasoning_details"] = public_reasoning_details

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created_at,
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }
