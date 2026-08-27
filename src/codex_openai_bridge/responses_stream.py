"""Validated direct Responses SSE projection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from codex_openai_bridge.models import ParsedSseEvent
from codex_openai_bridge.responses import (
    NamedCustomToolChoice,
    ResponsesRequest,
    _model_scoped_encrypted_state,
    _public_model_scoped_call_id,
    _public_model_scoped_state_id,
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
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
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
    allow_compaction: bool,
    allow_reasoning_summary: bool,
    max_string_bytes: int,
    public_model: str,
    binding_key: str | None,
    model_scoped: bool,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise _invalid()
    item_type = value.get("type")
    if item_type == "message":
        required = {"id", "type", "status", "role", "content"}
        if not required <= set(value) or not set(value) <= required | {"phase"}:
            raise _invalid()
        content = value["content"]
        if (
            value["status"] != status
            or value["role"] != "assistant"
            or type(content) is not list
            or ("phase" in value and value["phase"] != "final_answer")
        ):
            raise _invalid()
        parts: list[dict[str, Any]] = []
        for part in content:
            if type(part) is not dict or type(part.get("text")) is not str:
                raise _invalid()
            parts.append(_project_text_part(part, expected_text=part["text"]))
        message_result = {
            "id": value["id"],
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": parts,
        }
        if "phase" in value:
            message_result["phase"] = value["phase"]
        return message_result
    if item_type == "function_call":
        if set(value) != {"id", "type", "status", "call_id", "name", "arguments"}:
            raise _invalid()
        if value["status"] != status:
            raise _invalid()
        return {
            "id": value["id"],
            "type": "function_call",
            "status": status,
            "call_id": _public_model_scoped_call_id(
                value["call_id"],
                public_model=public_model,
                binding_key=binding_key,
                model_scoped=model_scoped,
            ),
            "name": value["name"],
            "arguments": value["arguments"],
        }
    if item_type == "reasoning":
        provider_fields = {"id", "type", "summary", "content", "encrypted_content"}
        if set(value) == provider_fields:
            if value["summary"] != [] or value["content"] != []:
                raise _invalid()
            raw_item_id = validate_responses_identifier(value["id"])
            result: dict[str, Any] = {
                "id": _public_model_scoped_state_id(
                    raw_item_id,
                    public_model=public_model,
                    kind="responses_reasoning",
                    binding_key=binding_key,
                    model_scoped=model_scoped,
                ),
                "type": "reasoning",
                "status": status,
                "summary": [],
            }
            encrypted_content = validate_encrypted_reasoning_content(
                value["encrypted_content"], max_string_bytes=max_string_bytes
            )
            if status == "completed":
                result["encrypted_content"] = _model_scoped_encrypted_state(
                    encrypted_content,
                    public_model=public_model,
                    kind="responses_reasoning_state",
                    binding_key=binding_key,
                    max_string_bytes=max_string_bytes,
                    model_scoped=model_scoped,
                    item_id=raw_item_id,
                    upstream=True,
                )
            return result
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
        if summary and not allow_reasoning_summary:
            raise _invalid()
        raw_item_id = validate_responses_identifier(value["id"])
        result = {
            "id": _public_model_scoped_state_id(
                raw_item_id,
                public_model=public_model,
                kind="responses_reasoning",
                binding_key=binding_key,
                model_scoped=model_scoped,
            ),
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": part["text"]} for part in summary],
        }
        if status == "completed":
            if "encrypted_content" not in value:
                raise _invalid()
            encrypted_content = validate_encrypted_reasoning_content(
                value["encrypted_content"], max_string_bytes=max_string_bytes
            )
            result["encrypted_content"] = _model_scoped_encrypted_state(
                encrypted_content,
                public_model=public_model,
                kind="responses_reasoning_state",
                binding_key=binding_key,
                max_string_bytes=max_string_bytes,
                model_scoped=model_scoped,
                item_id=raw_item_id,
                upstream=True,
            )
        return result
    if item_type == "compaction":
        if not allow_compaction or set(value) != {"id", "type", "encrypted_content"}:
            raise _invalid()
        encrypted_content = validate_encrypted_reasoning_content(
            value["encrypted_content"], max_string_bytes=max_string_bytes
        )
        raw_item_id = validate_responses_identifier(value["id"])
        return {
            "id": _public_model_scoped_state_id(
                raw_item_id,
                public_model=public_model,
                kind="responses_compaction",
                binding_key=binding_key,
                model_scoped=model_scoped,
            ),
            "type": "compaction",
            "encrypted_content": _model_scoped_encrypted_state(
                encrypted_content,
                public_model=public_model,
                kind="responses_compaction_state",
                binding_key=binding_key,
                max_string_bytes=max_string_bytes,
                model_scoped=model_scoped,
                item_id=raw_item_id,
                upstream=True,
            ),
        }
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
        binding_key: str | None = None,
        model_scoped: bool = False,
    ) -> None:
        self._request = request
        self._public_model = public_model
        self._binding_key = binding_key
        self._model_scoped = model_scoped
        self._max_items = max_items
        self._max_tools = max_tools
        self._max_json_depth = max_json_depth
        self._max_json_nodes = max_json_nodes
        self._max_string_bytes = max_string_bytes
        self._validator = ResponsesStreamValidator(
            public_model=public_model,
            allow_compaction=request.context_management is not None,
            max_unknown_events=max_json_nodes,
            max_string_bytes=max_string_bytes,
            initial_item_ids=request.historical_item_ids,
            initial_encrypted_digests=request.historical_reasoning_digests,
            custom_tool_name=(
                request.custom_tool.name
                if request.custom_tool is not None
                and isinstance(request.custom_tool_choice, NamedCustomToolChoice)
                else None
            ),
            max_items=max_items,
            max_tools=max_tools,
            initial_call_ids=request.historical_call_ids,
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
        self._custom_count = 0
        self._function_phase = False
        self._visible_output = False
        self._declared_tool_names = {tool.name for tool in request.tools}
        self._required_tool_name = (
            request.tool_choice.name
            if isinstance(request.tool_choice, NamedFunctionToolChoice)
            else None
        )
        self._item_ids: set[str] = set(request.historical_item_ids)
        self._public_item_ids: dict[str, str] = {}
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

    def _public_item_id(self, value: object) -> str:
        raw_item_id = validate_responses_identifier(value)
        try:
            return self._public_item_ids[raw_item_id]
        except KeyError:
            raise _invalid() from None

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
            raw_item = event["item"]
            custom_raw_call_id: str | None = None
            if type(raw_item) is dict and raw_item.get("type") == "custom_tool_call":
                item = self._validator.custom_public_item(
                    event["output_index"], completed=status == "completed"
                )
                custom_raw_call_id = validate_responses_identifier(item["call_id"])
                item["call_id"] = _public_model_scoped_call_id(
                    item["call_id"],
                    public_model=self._public_model,
                    binding_key=self._binding_key,
                    model_scoped=self._model_scoped,
                )
            else:
                item = _project_item(
                    raw_item,
                    status=status,
                    allow_compaction=self._request.context_management is not None,
                    allow_reasoning_summary=(
                        self._request.reasoning is not None
                        and self._request.reasoning.summary is not None
                    ),
                    max_string_bytes=self._max_string_bytes,
                    public_model=self._public_model,
                    binding_key=self._binding_key,
                    model_scoped=self._model_scoped,
                )
            raw_item_id = (
                validate_responses_identifier(item.get("id"))
                if type(raw_item) is dict and raw_item.get("type") == "custom_tool_call"
                else validate_responses_identifier(
                    raw_item.get("id") if type(raw_item) is dict else None
                )
            )
            if item["type"] not in {"reasoning", "compaction"}:
                item["id"] = _public_model_scoped_state_id(
                    raw_item_id,
                    public_model=self._public_model,
                    kind="responses_item",
                    binding_key=self._binding_key,
                    model_scoped=self._model_scoped,
                )
            public_item_id = item["id"]
            if type(public_item_id) is not str or not public_item_id:
                raise _invalid()
            if raw_item_id == self._response_id:
                raise _invalid()
            if status == "in_progress":
                if raw_item_id in self._item_ids:
                    raise _invalid()
                self._item_ids.add(raw_item_id)
                self._public_item_ids[raw_item_id] = public_item_id
                self._item_count += 1
                if self._item_count > self._max_items:
                    raise _invalid()
                if item["type"] == "message":
                    self._message_count += 1
                    if self._function_phase or self._message_count > 1:
                        raise _invalid()
                    self._visible_output = True
                elif item["type"] == "function_call":
                    assert type(raw_item) is dict
                    call_id = validate_responses_identifier(raw_item.get("call_id"))
                    name = validate_responses_identifier(raw_item.get("name"))
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
                elif item["type"] == "custom_tool_call":
                    assert type(raw_item) is dict
                    assert custom_raw_call_id is not None
                    call_id = custom_raw_call_id
                    name = validate_responses_identifier(item.get("name"))
                    self._custom_count += 1
                    if (
                        self._request.custom_tool is None
                        or not isinstance(self._request.custom_tool_choice, NamedCustomToolChoice)
                        or name != self._request.custom_tool.name
                        or name != self._request.custom_tool_choice.name
                        or call_id in self._call_ids
                        or self._custom_count > 1
                        or self._custom_count > self._max_tools
                        or self._visible_output
                    ):
                        raise _invalid()
                    self._call_ids.add(call_id)
                    self._visible_output = True
            elif self._public_item_ids.get(raw_item_id) != public_item_id:
                raise _invalid()
            elif item["type"] == "reasoning":
                assert type(raw_item) is dict
                encrypted = raw_item.get("encrypted_content")
                digest = encrypted_reasoning_data_digest(
                    encrypted,
                    max_string_bytes=self._max_string_bytes,
                )
                if digest is None or digest in self._reasoning_digests:
                    raise _invalid()
                self._reasoning_digests.add(digest)
            elif item["type"] == "compaction":
                assert type(raw_item) is dict
                digest = encrypted_reasoning_data_digest(
                    raw_item["encrypted_content"],
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
                    "item_id": self._public_item_id(event["item_id"]),
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
                    "item_id": self._public_item_id(event["item_id"]),
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
                    "item_id": self._public_item_id(event["item_id"]),
                    "output_index": event["output_index"],
                    value_field: event[value_field],
                    "sequence_number": sequence,
                },
            )
        if event_type in {
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
        }:
            output_index = event["output_index"]
            value_field = "delta" if event_type.endswith("delta") else "input"
            return _frame(
                event_type,
                {
                    "type": event_type,
                    "sequence_number": sequence,
                    "output_index": output_index,
                    "item_id": self._public_item_id(
                        self._validator.custom_public_item_id(output_index)
                    ),
                    value_field: event[value_field],
                },
            )
        if event_type == "response.completed":
            completed_response = self._validator.completed_response
            if completed_response is None:
                raise _invalid()
            public_response = responses_to_public(
                completed_response,
                request=self._request,
                public_model=self._public_model,
                max_items=self._max_items,
                max_tools=self._max_tools,
                max_json_depth=self._max_json_depth,
                max_json_nodes=self._max_json_nodes,
                max_string_bytes=self._max_string_bytes,
                binding_key=self._binding_key,
                model_scoped=self._model_scoped,
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
        if terminal is None:
            raise _invalid()
        if not self._validator.saw_done:
            self._validator.finish()
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
    binding_key: str | None = None,
    model_scoped: bool = False,
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
        binding_key=binding_key,
        model_scoped=model_scoped,
    )
    async for frame in translator:
        yield frame
