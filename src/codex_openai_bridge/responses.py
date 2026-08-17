"""Strict Responses request and non-streaming response boundary."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

from codex_openai_bridge.translation import UpstreamResponseError
from codex_openai_bridge.wire import (
    ChatRequestError,
    FunctionTool,
    JsonObjectResponseFormat,
    JsonSchemaResponseFormat,
    NamedFunctionToolChoice,
    encrypted_reasoning_data_digest,
    json_schema_for_upstream,
    json_schema_name_for_upstream,
    parse_chat_completion_request,
)

_PUBLIC_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)


class ResponsesRequestError(ValueError):
    """Raised when a public Responses request is unsupported or malformed."""


@dataclass(frozen=True, slots=True)
class ResponsesRequest:
    """The closed public Responses request contract."""

    input: str | tuple[dict[str, Any], ...]
    instructions: str | None
    max_output_tokens: int | None
    tools: tuple[FunctionTool, ...]
    tool_choice: Literal["auto", "required", "none"] | NamedFunctionToolChoice | None
    parallel_tool_calls: bool | None
    response_format: JsonObjectResponseFormat | JsonSchemaResponseFormat | None
    stream: bool
    historical_item_ids: frozenset[str]
    historical_call_ids: frozenset[str]
    historical_reasoning_digests: frozenset[bytes]


def _invalid_request() -> ResponsesRequestError:
    return ResponsesRequestError("invalid request")


def _validate_tree(
    root: object,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
    upstream: bool = False,
) -> None:
    error: ValueError
    if upstream:
        error = UpstreamResponseError("invalid upstream response")
    else:
        error = _invalid_request()
    nodes = 0
    string_bytes = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise error
        if type(value) is dict:
            if nodes + len(stack) + len(value) > max_nodes:
                raise error
            for key, item in value.items():
                if type(key) is not str:
                    raise error
                try:
                    string_bytes += len(key.encode("utf-8", errors="strict"))
                except UnicodeError:
                    raise error from None
                if string_bytes > max_string_bytes:
                    raise error
                stack.append((item, depth + 1))
        elif type(value) is list:
            if nodes + len(stack) + len(value) > max_nodes:
                raise error
            stack.extend((item, depth + 1) for item in value)
        elif type(value) is str:
            try:
                string_bytes += len(value.encode("utf-8", errors="strict"))
            except UnicodeError:
                raise error from None
            if string_bytes > max_string_bytes:
                raise error
        elif type(value) is float:
            if not math.isfinite(value):
                raise error
        elif value is not None and type(value) not in (bool, int):
            raise error


def _identifier(value: object, *, upstream: bool = False) -> str:
    if type(value) is str and _PUBLIC_ID.fullmatch(value) is not None:
        return value
    if upstream:
        raise UpstreamResponseError("invalid upstream response")
    raise _invalid_request()


def _strict_arguments(
    value: object,
    *,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
    upstream: bool = False,
) -> str:
    error: ValueError = (
        UpstreamResponseError("invalid upstream response") if upstream else _invalid_request()
    )
    if type(value) is not str or not value:
        raise error

    def reject_constant(_value: str) -> None:
        raise ValueError

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (ValueError, OverflowError, RecursionError):
        raise error from None
    if type(parsed) is not dict:
        raise error
    _validate_tree(
        parsed,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
        upstream=upstream,
    )
    return value


def _canonical_reasoning_digest(
    value: object,
    *,
    max_string_bytes: int,
    upstream: bool = False,
) -> bytes:
    digest = encrypted_reasoning_data_digest(value, max_string_bytes=max_string_bytes)
    if digest is not None:
        return digest
    if upstream:
        raise UpstreamResponseError("invalid upstream response")
    raise _invalid_request()


def _parse_options(
    document: dict[str, Any],
    *,
    public_model: str,
    max_tools: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> tuple[
    int | None,
    tuple[FunctionTool, ...],
    Literal["auto", "required", "none"] | NamedFunctionToolChoice | None,
    bool | None,
    JsonObjectResponseFormat | JsonSchemaResponseFormat | None,
]:
    synthetic: dict[str, Any] = {
        "model": public_model,
        "messages": [{"role": "user", "content": ""}],
    }
    if "max_output_tokens" in document:
        synthetic["max_completion_tokens"] = document["max_output_tokens"]
    if "parallel_tool_calls" in document:
        synthetic["parallel_tool_calls"] = document["parallel_tool_calls"]
    if "tools" in document:
        raw_tools = document["tools"]
        if type(raw_tools) is not list:
            raise _invalid_request()
        translated_tools: list[dict[str, Any]] = []
        for tool in raw_tools:
            if (
                type(tool) is not dict
                or not {"type", "name", "parameters"} <= set(tool)
                or not set(tool) <= {"type", "name", "description", "parameters", "strict"}
                or type(tool.get("type")) is not str
                or tool["type"] != "function"
            ):
                raise _invalid_request()
            function = {key: deepcopy(value) for key, value in tool.items() if key != "type"}
            translated_tools.append({"type": "function", "function": function})
        synthetic["tools"] = translated_tools
    if "tool_choice" in document:
        choice = document["tool_choice"]
        if type(choice) is dict:
            if set(choice) != {"type", "name"}:
                raise _invalid_request()
            synthetic["tool_choice"] = {
                "type": choice.get("type"),
                "function": {"name": choice.get("name")},
            }
        else:
            synthetic["tool_choice"] = choice
    if "text" in document:
        text = document["text"]
        if type(text) is not dict or set(text) != {"format"}:
            raise _invalid_request()
        response_format = text["format"]
        if type(response_format) is not dict or type(response_format.get("type")) is not str:
            raise _invalid_request()
        if response_format["type"] == "json_object":
            synthetic["response_format"] = deepcopy(response_format)
        elif response_format["type"] == "json_schema":
            if set(response_format) - {"type", "name", "description", "schema", "strict"}:
                raise _invalid_request()
            synthetic["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    key: deepcopy(value) for key, value in response_format.items() if key != "type"
                },
            }
        else:
            raise _invalid_request()
    try:
        parsed = parse_chat_completion_request(
            synthetic,
            public_model=public_model,
            max_messages=1,
            max_tools=max_tools,
            max_json_depth=max_json_depth,
            max_json_nodes=max_json_nodes,
            max_string_bytes=max_string_bytes,
            binding_key="direct-responses-options",
        )
    except ChatRequestError:
        raise _invalid_request() from None
    return (
        parsed.max_output_tokens,
        parsed.tools,
        parsed.tool_choice,
        parsed.parallel_tool_calls,
        parsed.response_format,
    )


def _parse_input(
    value: object,
    *,
    max_items: int,
    max_tools: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> tuple[
    str | tuple[dict[str, Any], ...],
    frozenset[str],
    frozenset[str],
    frozenset[bytes],
]:
    if type(value) is str:
        return value, frozenset(), frozenset(), frozenset()
    if type(value) is not list or not 1 <= len(value) <= max_items:
        raise _invalid_request()
    translated: list[dict[str, Any]] = []
    item_ids: set[str] = set()
    call_ids: set[str] = set()
    pending_call_ids: set[str] = set()
    completed_call_ids: set[str] = set()
    reasoning_digests: set[bytes] = set()
    reasoning_count = 0
    tool_call_count = 0
    output_phase = False
    visible_assistant_output = False
    for item in value:
        if type(item) is not dict:
            raise _invalid_request()
        item_type = item.get("type")
        if item_type == "reasoning":
            # Direct Responses history has no Chat compatibility HMAC. These checks
            # establish a bounded canonical opaque blob, never provenance/authenticity.
            if (
                output_phase
                or visible_assistant_output
                or pending_call_ids
                or set(item)
                != {
                    "id",
                    "type",
                    "status",
                    "summary",
                    "encrypted_content",
                }
            ):
                raise _invalid_request()
            item_id = _identifier(item["id"])
            if item_id in item_ids or item["status"] != "completed" or item["summary"] != []:
                raise _invalid_request()
            digest = _canonical_reasoning_digest(
                item["encrypted_content"], max_string_bytes=max_string_bytes
            )
            if digest in reasoning_digests:
                raise _invalid_request()
            reasoning_count += 1
            if reasoning_count > max_items:
                raise _invalid_request()
            item_ids.add(item_id)
            reasoning_digests.add(digest)
            translated.append({"type": "reasoning", "encrypted_content": item["encrypted_content"]})
            continue
        if item_type == "function_call":
            if (
                output_phase
                or not {"type", "call_id", "name", "arguments"} <= set(item)
                or not set(item) <= {"id", "type", "status", "call_id", "name", "arguments"}
            ):
                raise _invalid_request()
            if "status" in item and item["status"] != "completed":
                raise _invalid_request()
            if "id" in item:
                item_id = _identifier(item["id"])
                if item_id in item_ids:
                    raise _invalid_request()
                item_ids.add(item_id)
            call_id = _identifier(item["call_id"])
            name = _identifier(item["name"])
            if call_id in call_ids:
                raise _invalid_request()
            arguments = _strict_arguments(
                item["arguments"],
                max_json_depth=max_json_depth,
                max_json_nodes=max_json_nodes,
                max_string_bytes=max_string_bytes,
            )
            tool_call_count += 1
            if tool_call_count > max_tools:
                raise _invalid_request()
            call_ids.add(call_id)
            pending_call_ids.add(call_id)
            translated.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
            continue
        if item_type == "function_call_output":
            if not {"type", "call_id", "output"} <= set(item) or not set(item) <= {
                "id",
                "type",
                "status",
                "call_id",
                "output",
            }:
                raise _invalid_request()
            if "status" in item and item["status"] != "completed":
                raise _invalid_request()
            if "id" in item:
                item_id = _identifier(item["id"])
                if item_id in item_ids:
                    raise _invalid_request()
                item_ids.add(item_id)
            call_id = _identifier(item["call_id"])
            output = item["output"]
            if (
                type(output) is not str
                or call_id not in pending_call_ids
                or call_id in completed_call_ids
            ):
                raise _invalid_request()
            pending_call_ids.remove(call_id)
            completed_call_ids.add(call_id)
            output_phase = True
            translated.append(
                {"type": "function_call_output", "call_id": call_id, "output": output}
            )
            continue

        if pending_call_ids:
            raise _invalid_request()
        role = item.get("role")
        if role == "user":
            if not set(item) <= {"type", "role", "content"} or set(item) < {
                "role",
                "content",
            }:
                raise _invalid_request()
            if "type" in item and item["type"] != "message":
                raise _invalid_request()
            content = item["content"]
            if type(content) is str:
                parts = [{"type": "input_text", "text": content}]
            elif type(content) is list and 1 <= len(content) <= max_items:
                parts = []
                for part in content:
                    if type(part) is not dict or set(part) != {"type", "text"}:
                        raise _invalid_request()
                    if part["type"] != "input_text" or type(part["text"]) is not str:
                        raise _invalid_request()
                    parts.append({"type": "input_text", "text": part["text"]})
            else:
                raise _invalid_request()
            output_phase = False
            visible_assistant_output = False
            translated.append({"role": "user", "content": parts})
            continue
        if role == "assistant":
            if set(item) != {"id", "type", "status", "role", "content"}:
                raise _invalid_request()
            item_id = _identifier(item["id"])
            content = item["content"]
            if (
                item["type"] != "message"
                or item["status"] != "completed"
                or item_id in item_ids
                or type(content) is not list
                or not 1 <= len(content) <= max_items
            ):
                raise _invalid_request()
            parts = []
            for part in content:
                if (
                    type(part) is not dict
                    or set(part) != {"type", "text", "annotations"}
                    or part["type"] != "output_text"
                    or type(part["text"]) is not str
                    or part["annotations"] != []
                ):
                    raise _invalid_request()
                parts.append({"type": "output_text", "text": part["text"]})
            item_ids.add(item_id)
            output_phase = False
            visible_assistant_output = True
            translated.append({"role": "assistant", "content": parts})
            continue
        raise _invalid_request()
    if pending_call_ids:
        raise _invalid_request()
    return (
        tuple(translated),
        frozenset(item_ids),
        frozenset(call_ids),
        frozenset(reasoning_digests),
    )


def parse_responses_request(
    value: object,
    *,
    public_model: str,
    max_items: int,
    max_tools: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> ResponsesRequest:
    """Parse a direct Responses request without coercion or passthrough fields."""
    if (
        type(value) is not dict
        or type(public_model) is not str
        or not public_model
        or any(
            type(limit) is not int or limit <= 0
            for limit in (
                max_items,
                max_tools,
                max_json_depth,
                max_json_nodes,
                max_string_bytes,
            )
        )
    ):
        raise _invalid_request()
    document: dict[str, Any] = value
    allowed = {
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "max_output_tokens",
        "text",
        "store",
        "stream",
        "include",
    }
    if not {"model", "input"} <= set(document) or not set(document) <= allowed:
        raise _invalid_request()
    _validate_tree(
        document,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    if type(document["model"]) is not str or document["model"] != public_model:
        raise _invalid_request()
    if "store" in document and document["store"] is not False:
        raise _invalid_request()
    stream = document.get("stream", False)
    if type(stream) is not bool:
        raise _invalid_request()
    if "include" in document and document["include"] != ["reasoning.encrypted_content"]:
        raise _invalid_request()
    instructions = document.get("instructions")
    if "instructions" in document and type(instructions) is not str:
        raise _invalid_request()
    (
        parsed_input,
        historical_item_ids,
        historical_call_ids,
        historical_reasoning_digests,
    ) = _parse_input(
        document["input"],
        max_items=max_items,
        max_tools=max_tools,
        max_json_depth=max_json_depth,
        max_json_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    max_output_tokens, tools, tool_choice, parallel_tool_calls, response_format = _parse_options(
        document,
        public_model=public_model,
        max_tools=max_tools,
        max_json_depth=max_json_depth,
        max_json_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    return ResponsesRequest(
        input=parsed_input,
        instructions=instructions,
        max_output_tokens=max_output_tokens,
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        response_format=response_format,
        stream=stream,
        historical_item_ids=historical_item_ids,
        historical_call_ids=historical_call_ids,
        historical_reasoning_digests=historical_reasoning_digests,
    )


def _tool_payload(tool: FunctionTool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "function",
        "name": tool.name,
        "parameters": deepcopy(tool.parameters),
    }
    if tool.description is not None:
        result["description"] = tool.description
    if tool.strict is not None:
        result["strict"] = tool.strict
    return result


def _tool_choice_payload(
    choice: Literal["auto", "required", "none"] | NamedFunctionToolChoice | None,
) -> str | dict[str, str] | None:
    if isinstance(choice, NamedFunctionToolChoice):
        return {"type": "function", "name": choice.name}
    return choice


def _text_payload(
    response_format: JsonObjectResponseFormat | JsonSchemaResponseFormat | None,
) -> dict[str, Any] | None:
    if response_format is None:
        return None
    if isinstance(response_format, JsonObjectResponseFormat):
        return {"format": {"type": "json_object"}}
    value: dict[str, Any] = {
        "type": "json_schema",
        "name": json_schema_name_for_upstream(response_format.name),
        "schema": json_schema_for_upstream(response_format.schema),
    }
    if response_format.description is not None:
        value["description"] = response_format.description
    if response_format.strict is not None:
        value["strict"] = response_format.strict
    return {"format": value}


def responses_request_to_upstream(
    request: ResponsesRequest,
    *,
    upstream_model: str,
) -> dict[str, Any]:
    """Reconstruct a fixed-policy upstream Responses payload."""
    upstream_input: list[dict[str, Any]]
    if type(request.input) is str:
        upstream_input = [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": request.input}],
            }
        ]
    else:
        upstream_input = deepcopy(list(cast(tuple[dict[str, Any], ...], request.input)))
    payload: dict[str, Any] = {
        "model": upstream_model,
        "input": upstream_input,
        "store": False,
        "stream": request.stream,
        "include": ["reasoning.encrypted_content"],
    }
    if request.instructions is not None:
        payload["instructions"] = request.instructions
    # The upstream Codex route rejects max_output_tokens; it remains a validated
    # public compatibility field and is bounded locally by response bytes/time.
    if request.tools:
        payload["tools"] = [_tool_payload(tool) for tool in request.tools]
    choice = _tool_choice_payload(request.tool_choice)
    if choice is not None:
        payload["tool_choice"] = choice
    if request.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = request.parallel_tool_calls
    text = _text_payload(request.response_format)
    if text is not None:
        payload["text"] = text
    return payload


def responses_public_snapshot(
    request: ResponsesRequest,
    *,
    response_id: object,
    created_at: object,
    public_model: str,
) -> dict[str, Any]:
    """Build the closed public in-progress snapshot used by Responses SSE."""
    response_id = _identifier(response_id, upstream=True)
    if (
        type(created_at) is not int
        or created_at < 0
        or type(public_model) is not str
        or not public_model
    ):
        raise UpstreamResponseError("invalid upstream response")
    tools = [_tool_payload(tool) for tool in request.tools]
    choice = _tool_choice_payload(request.tool_choice)
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "model": public_model,
        "output": [],
        "parallel_tool_calls": (
            request.parallel_tool_calls if request.parallel_tool_calls is not None else True
        ),
        "tool_choice": choice if choice is not None else "auto",
        "tools": tools,
        "usage": None,
    }


def validate_encrypted_reasoning_content(value: object, *, max_string_bytes: int) -> str:
    """Validate one canonical opaque reasoning payload for public SSE projection."""
    _canonical_reasoning_digest(value, max_string_bytes=max_string_bytes, upstream=True)
    assert type(value) is str
    return value


def validate_responses_identifier(value: object) -> str:
    """Validate one upstream identifier before it enters a public SSE frame."""
    return _identifier(value, upstream=True)


def _usage_detail(
    value: object,
    field: str,
    *,
    optional_fields: frozenset[str] = frozenset(),
) -> int:
    if type(value) is not dict or field not in value or not set(value) <= {field, *optional_fields}:
        raise UpstreamResponseError("invalid upstream response")
    for optional_field in optional_fields:
        if optional_field in value:
            optional_value = value[optional_field]
            if type(optional_value) is not int or optional_value < 0:
                raise UpstreamResponseError("invalid upstream response")
    candidate = value[field]
    if type(candidate) is not int or candidate < 0:
        raise UpstreamResponseError("invalid upstream response")
    return candidate


def responses_to_public(
    value: object,
    *,
    request: ResponsesRequest,
    public_model: str,
    max_items: int,
    max_tools: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> dict[str, Any]:
    """Validate and project one completed upstream Responses object."""
    if (
        type(value) is not dict
        or type(public_model) is not str
        or not public_model
        or any(
            type(limit) is not int or limit <= 0
            for limit in (
                max_items,
                max_tools,
                max_json_depth,
                max_json_nodes,
                max_string_bytes,
            )
        )
    ):
        raise UpstreamResponseError("invalid upstream response")
    _validate_tree(
        value,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
        upstream=True,
    )
    response: dict[str, Any] = value
    response_id = _identifier(response.get("id"), upstream=True)
    if response_id in request.historical_item_ids:
        raise UpstreamResponseError("invalid upstream response")
    created_at = response.get("created_at")
    output = response.get("output")
    usage = response.get("usage")
    if (
        response.get("object") != "response"
        or response.get("status") != "completed"
        or ("error" in response and response["error"] is not None)
        or ("incomplete_details" in response and response["incomplete_details"] is not None)
        or type(created_at) is not int
        or created_at < 0
        or type(output) is not list
        or not 1 <= len(output) <= max_items
        or type(usage) is not dict
    ):
        raise UpstreamResponseError("invalid upstream response")

    public_output: list[dict[str, Any]] = []
    declared_tool_names = {tool.name for tool in request.tools}
    required_tool_name = (
        request.tool_choice.name
        if isinstance(request.tool_choice, NamedFunctionToolChoice)
        else None
    )
    item_ids: set[str] = {response_id, *request.historical_item_ids}
    call_ids: set[str] = set(request.historical_call_ids)
    reasoning_digests: set[bytes] = set(request.historical_reasoning_digests)
    reasoning_count = 0
    function_count = 0
    message_count = 0
    function_phase = False
    visible_output = False
    for raw_item in output:
        if type(raw_item) is not dict:
            raise UpstreamResponseError("invalid upstream response")
        item_id = _identifier(raw_item.get("id"), upstream=True)
        if item_id in item_ids:
            raise UpstreamResponseError("invalid upstream response")
        item_ids.add(item_id)
        item_type = raw_item.get("type")
        if item_type == "reasoning":
            if (
                function_phase
                or visible_output
                or set(raw_item)
                != {
                    "id",
                    "type",
                    "status",
                    "summary",
                    "encrypted_content",
                }
            ):
                raise UpstreamResponseError("invalid upstream response")
            summary = raw_item["summary"]
            if type(summary) is not list or any(
                type(part) is not dict
                or set(part) != {"type", "text"}
                or part["type"] != "summary_text"
                or type(part["text"]) is not str
                for part in summary
            ):
                raise UpstreamResponseError("invalid upstream response")
            digest = _canonical_reasoning_digest(
                raw_item["encrypted_content"],
                max_string_bytes=max_string_bytes,
                upstream=True,
            )
            if raw_item["status"] != "completed" or digest in reasoning_digests:
                raise UpstreamResponseError("invalid upstream response")
            reasoning_count += 1
            if reasoning_count > max_items:
                raise UpstreamResponseError("invalid upstream response")
            reasoning_digests.add(digest)
            public_output.append(
                {
                    "id": item_id,
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [],
                    "encrypted_content": raw_item["encrypted_content"],
                }
            )
            continue
        if item_type == "message":
            if function_phase or set(raw_item) != {"id", "type", "status", "role", "content"}:
                raise UpstreamResponseError("invalid upstream response")
            content = raw_item["content"]
            if (
                raw_item["status"] != "completed"
                or raw_item["role"] != "assistant"
                or type(content) is not list
                or not 1 <= len(content) <= max_items
            ):
                raise UpstreamResponseError("invalid upstream response")
            message_count += 1
            if message_count > 1:
                raise UpstreamResponseError("invalid upstream response")
            parts: list[dict[str, Any]] = []
            for part in content:
                if (
                    type(part) is not dict
                    or set(part) != {"type", "text", "annotations"}
                    or part["type"] != "output_text"
                    or type(part["text"]) is not str
                    or part["annotations"] != []
                ):
                    raise UpstreamResponseError("invalid upstream response")
                parts.append({"type": "output_text", "text": part["text"], "annotations": []})
            public_output.append(
                {
                    "id": item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": parts,
                }
            )
            visible_output = True
            continue
        if item_type == "function_call":
            if set(raw_item) != {"id", "type", "status", "call_id", "name", "arguments"}:
                raise UpstreamResponseError("invalid upstream response")
            call_id = _identifier(raw_item["call_id"], upstream=True)
            name = _identifier(raw_item["name"], upstream=True)
            if (
                raw_item["status"] != "completed"
                or call_id in call_ids
                or name not in declared_tool_names
                or request.tool_choice == "none"
                or (required_tool_name is not None and name != required_tool_name)
            ):
                raise UpstreamResponseError("invalid upstream response")
            arguments = _strict_arguments(
                raw_item["arguments"],
                max_json_depth=max_json_depth,
                max_json_nodes=max_json_nodes,
                max_string_bytes=max_string_bytes,
                upstream=True,
            )
            function_count += 1
            if function_count > max_tools or (
                request.parallel_tool_calls is False and function_count > 1
            ):
                raise UpstreamResponseError("invalid upstream response")
            function_phase = True
            visible_output = True
            call_ids.add(call_id)
            public_output.append(
                {
                    "id": item_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
            continue
        raise UpstreamResponseError("invalid upstream response")
    if message_count == 0 and function_count == 0:
        raise UpstreamResponseError("invalid upstream response")
    if (
        request.tool_choice == "required" or required_tool_name is not None
    ) and function_count == 0:
        raise UpstreamResponseError("invalid upstream response")

    allowed_usage = {
        "input_tokens",
        "input_tokens_details",
        "output_tokens",
        "output_tokens_details",
        "total_tokens",
    }
    if set(usage) != allowed_usage:
        raise UpstreamResponseError("invalid upstream response")
    exact_usage: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        candidate = usage[field]
        if type(candidate) is not int or candidate < 0:
            raise UpstreamResponseError("invalid upstream response")
        exact_usage[field] = candidate
    cached_tokens = _usage_detail(
        usage["input_tokens_details"],
        "cached_tokens",
        optional_fields=frozenset({"cache_write_tokens"}),
    )
    reasoning_tokens = _usage_detail(usage["output_tokens_details"], "reasoning_tokens")

    tools = [_tool_payload(tool) for tool in request.tools]
    choice = _tool_choice_payload(request.tool_choice)
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": public_model,
        "output": public_output,
        "parallel_tool_calls": (
            request.parallel_tool_calls if request.parallel_tool_calls is not None else True
        ),
        "tool_choice": choice if choice is not None else "auto",
        "tools": tools,
        "usage": {
            "input_tokens": exact_usage["input_tokens"],
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens": exact_usage["output_tokens"],
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": exact_usage["total_tokens"],
        },
    }
