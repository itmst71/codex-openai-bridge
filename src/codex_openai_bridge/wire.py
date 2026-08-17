"""Strict Chat Completions request wire model."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Any, Literal


class ChatRequestError(ValueError):
    """Raised when a public Chat Completions request is unsupported or malformed."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One validated assistant function call in request history."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ReasoningDetail:
    """One authenticated encrypted Codex reasoning state item."""

    data: str
    binding_id: str
    index: int


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One validated Chat Completions message."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_details: tuple[ReasoningDetail, ...] = ()


@dataclass(frozen=True, slots=True)
class FunctionTool:
    """One validated OpenAI function tool definition."""

    name: str
    description: str | None
    parameters: dict[str, Any]
    strict: bool | None


@dataclass(frozen=True, slots=True)
class NamedFunctionToolChoice:
    """A validated request to call one declared function."""

    name: str


@dataclass(frozen=True, slots=True)
class JsonObjectResponseFormat:
    """A validated request for native JSON-object output."""


@dataclass(frozen=True, slots=True)
class JsonSchemaResponseFormat:
    """A validated native structured-output schema."""

    name: str
    schema: dict[str, Any]
    description: str | None
    strict: bool | None


@dataclass(frozen=True, slots=True)
class ChatCompletionRequest:
    """The closed Chat Completions request contract."""

    messages: tuple[ChatMessage, ...]
    max_output_tokens: int | None
    tools: tuple[FunctionTool, ...] = ()
    tool_choice: Literal["auto", "required", "none"] | NamedFunctionToolChoice | None = None
    parallel_tool_calls: bool | None = None
    response_format: JsonObjectResponseFormat | JsonSchemaResponseFormat | None = None
    stream: bool = False
    include_usage: bool = False


def json_schema_for_upstream(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy a validated schema while dropping unsupported annotations."""
    try:
        projected = deepcopy(schema)
    except (MemoryError, RecursionError):
        raise ChatRequestError("invalid request") from None
    stack: list[dict[str, Any]] = [projected]
    while stack:
        candidate = stack.pop()
        candidate.pop("title", None)
        candidate.pop("description", None)
        items = candidate.get("items")
        if type(items) is dict:
            stack.append(items)
        any_of = candidate.get("anyOf")
        if type(any_of) is list:
            stack.extend(item for item in any_of if type(item) is dict)
        for keyword in ("properties", "$defs"):
            definitions = candidate.get(keyword)
            if type(definitions) is dict:
                stack.extend(item for item in definitions.values() if type(item) is dict)
    return projected


def json_schema_name_for_upstream(name: str) -> str:
    """Project a validated public schema identifier to Codex's lowercase form."""
    return name.lower()


def model_list_document(public_model: str) -> dict[str, object]:
    """Build the stable, secret-free OpenAI model discovery document."""
    if type(public_model) is not str or not public_model:
        raise ValueError("invalid public model")
    return {
        "object": "list",
        "data": [
            {
                "id": public_model,
                "created": 0,
                "object": "model",
                "owned_by": "codex-openai-bridge",
                "x_codex_bridge": {
                    "chat_completions": True,
                    "responses": True,
                    "function_calling": True,
                    "embeddings": False,
                },
            }
        ],
    }


def _reject_json_constant(_value: str) -> None:
    raise ValueError


@dataclass(frozen=True, slots=True)
class _ExactJsonInteger:
    value: Decimal


@dataclass(frozen=True, slots=True)
class _ExactJsonFloat:
    value: Decimal


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except DecimalException:
        raise ValueError from None
    if not parsed.is_finite():
        raise ValueError
    return parsed


def _parse_exact_json_integer(value: str) -> _ExactJsonInteger:
    return _ExactJsonInteger(_parse_decimal(value))


def _parse_exact_json_float(value: str) -> _ExactJsonFloat:
    try:
        binary_float = float(value)
    except (OverflowError, ValueError):
        raise ValueError from None
    if not math.isfinite(binary_float):
        raise ValueError
    return _ExactJsonFloat(_parse_decimal(value))


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _parse_arguments_object(value: object) -> dict[str, Any]:
    if type(value) is not str or not value:
        raise ChatRequestError("invalid request")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
            parse_float=_parse_exact_json_float,
            parse_int=_parse_exact_json_integer,
        )
    except (ValueError, OverflowError, RecursionError):
        raise ChatRequestError("invalid request") from None
    if type(parsed) is not dict:
        raise ChatRequestError("invalid request")
    return parsed


def _validate_arguments_object(value: object) -> str:
    _parse_arguments_object(value)
    if type(value) is not str:
        raise ChatRequestError("invalid request")
    return value


def _canonical_decimal(value: Decimal, *, preserve_zero_sign: bool) -> str:
    sign, raw_digits, exponent = value.as_tuple()
    if type(exponent) is not int:
        raise ChatRequestError("invalid request")
    digits = list(raw_digits)
    if not digits or all(digit == 0 for digit in digits):
        return f"{'-' if sign and preserve_zero_sign else ''}0e0"
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    return f"{'-' if sign else ''}{coefficient}e{exponent}"


def _json_semantic_projection(value: object) -> object:
    """Represent exact JSON semantics without binary-float or type collisions."""
    if type(value) is dict:
        return [
            "object",
            [[key, _json_semantic_projection(item)] for key, item in sorted(value.items())],
        ]
    if type(value) is list:
        return ["array", [_json_semantic_projection(item) for item in value]]
    if type(value) is str:
        return ["string", value]
    if type(value) is bool:
        return ["boolean", value]
    if value is None:
        return ["null"]
    if type(value) is _ExactJsonInteger:
        return [
            "integer",
            _canonical_decimal(value.value, preserve_zero_sign=False),
        ]
    if type(value) is _ExactJsonFloat:
        return [
            "number",
            _canonical_decimal(value.value, preserve_zero_sign=True),
        ]
    raise ChatRequestError("invalid request")


_SCHEMA_KEYWORDS = {
    "$defs",
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "anyOf",
    "description",
    "title",
}
_SCHEMA_TYPES = {"null", "boolean", "object", "array", "number", "integer", "string"}
_LOCAL_DEFS_REF = re.compile(r"#/(?:\$defs)/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*\Z")
_FORMAT_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
_BASE64_CORE = re.compile(r"[A-Za-z0-9+/_-]+={0,2}\Z", re.ASCII)
_REASONING_BINDING_PREFIX = "cobr_r2_"


def encrypted_reasoning_data_digest(value: object, *, max_string_bytes: int) -> bytes | None:
    """Validate strict base64 and return a digest without retaining decoded bytes."""
    if type(value) is not str or type(max_string_bytes) is not int or max_string_bytes <= 0:
        return None
    try:
        encoded = value.encode("ascii", errors="strict")
    except UnicodeError:
        return None
    if not encoded or len(encoded) > max_string_bytes or _BASE64_CORE.fullmatch(value) is None:
        return None
    padding = len(value) - len(value.rstrip("="))
    core = value[:-padding] if padding else value
    if not core or padding > 2 or "=" in core:
        return None
    has_standard = "+" in core or "/" in core
    has_urlsafe = "-" in core or "_" in core
    if has_standard and has_urlsafe:
        return None
    remainder = len(core) % 4
    if remainder == 1:
        return None
    required_padding = (4 - remainder) % 4
    if padding and (padding != required_padding or len(value) % 4 != 0):
        return None
    try:
        decoded = base64.b64decode(
            core.encode("ascii") + b"=" * required_padding,
            altchars=b"-_" if has_urlsafe else None,
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None
    if not decoded:
        return None
    canonical = base64.urlsafe_b64encode(decoded) if has_urlsafe else base64.b64encode(decoded)
    if encoded not in (canonical, canonical.rstrip(b"=")):
        return None
    digest = hashlib.sha256(decoded).digest()
    del decoded
    return digest


def encrypted_reasoning_data_is_valid(value: object, *, max_string_bytes: int) -> bool:
    """Return whether opaque encrypted state is bounded strict base64."""
    return encrypted_reasoning_data_digest(value, max_string_bytes=max_string_bytes) is not None


def _reasoning_tool_call_projection(
    tool_calls: tuple[ToolCall, ...],
) -> list[dict[str, object]]:
    return [
        {
            "id": call.call_id,
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": _json_semantic_projection(_parse_arguments_object(call.arguments)),
            },
        }
        for call in tool_calls
    ]


def create_reasoning_binding_id(
    *,
    binding_key: str,
    content: str | None,
    tool_calls: tuple[ToolCall, ...],
    index: int,
    data: str,
) -> str:
    """Bind opaque state to assistant semantics, canonicalizing tool JSON objects."""
    if (
        type(binding_key) is not str
        or not binding_key
        or (content is not None and type(content) is not str)
        or type(index) is not int
        or index < 0
        or type(data) is not str
    ):
        raise ValueError("invalid reasoning binding")
    try:
        key = binding_key.encode("utf-8", errors="strict")
        canonical = json.dumps(
            {
                "version": 2,
                "content": content,
                "tool_calls": _reasoning_tool_call_projection(tool_calls),
                "index": index,
                "data": data,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeError):
        raise ValueError("invalid reasoning binding") from None
    digest = hmac.new(key, canonical, hashlib.sha256).digest()
    return _REASONING_BINDING_PREFIX + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def reasoning_binding_is_valid(
    *,
    binding_key: str,
    content: str | None,
    tool_calls: tuple[ToolCall, ...],
    index: int,
    data: str,
    binding_id: object,
) -> bool:
    """Verify a message-scoped binding in constant time."""
    if type(binding_id) is not str or not binding_id:
        return False
    try:
        expected = create_reasoning_binding_id(
            binding_key=binding_key,
            content=content,
            tool_calls=tool_calls,
            index=index,
            data=data,
        ).encode("ascii")
        candidate = binding_id.encode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return False
    # Rotation of the bridge token intentionally invalidates prior details fail-closed.
    return hmac.compare_digest(expected, candidate)


def _validate_bounded_json_tree(
    root: object,
    *,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> int:
    nodes = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ChatRequestError("invalid request")
        if type(item) is dict:
            if nodes + len(stack) + len(item) > max_nodes:
                raise ChatRequestError("invalid request")
            for key, value in item.items():
                if type(key) is not str:
                    raise ChatRequestError("invalid request")
                try:
                    encoded_key = key.encode("utf-8", errors="strict")
                except UnicodeError:
                    raise ChatRequestError("invalid request") from None
                if len(encoded_key) > max_string_bytes:
                    raise ChatRequestError("invalid request")
                stack.append((value, depth + 1))
        elif type(item) is list:
            if nodes + len(stack) + len(item) > max_nodes:
                raise ChatRequestError("invalid request")
            stack.extend((value, depth + 1) for value in item)
        elif type(item) is str:
            try:
                encoded_item = item.encode("utf-8", errors="strict")
            except UnicodeError:
                raise ChatRequestError("invalid request") from None
            if len(encoded_item) > max_string_bytes:
                raise ChatRequestError("invalid request")
        elif type(item) is float:
            if not math.isfinite(item):
                raise ChatRequestError("invalid request")
        elif item is not None and type(item) not in (bool, int):
            raise ChatRequestError("invalid request")
    return nodes


def _is_json_scalar(value: object) -> bool:
    return value is None or type(value) in (str, bool, int, float)


def _validate_schema_subset(schema: dict[str, Any]) -> None:
    stack: list[dict[str, Any]] = [schema]
    while stack:
        candidate = stack.pop()
        if not set(candidate) <= _SCHEMA_KEYWORDS:
            raise ChatRequestError("invalid request")

        if "$ref" in candidate:
            ref = candidate["$ref"]
            if type(ref) is not str or _LOCAL_DEFS_REF.fullmatch(ref) is None:
                raise ChatRequestError("invalid request")

        if "type" in candidate:
            schema_type = candidate["type"]
            if type(schema_type) is str:
                if schema_type not in _SCHEMA_TYPES:
                    raise ChatRequestError("invalid request")
            elif type(schema_type) is list:
                if not schema_type or any(
                    type(item) is not str or item not in _SCHEMA_TYPES for item in schema_type
                ):
                    raise ChatRequestError("invalid request")
                if len(set(schema_type)) != len(schema_type):
                    raise ChatRequestError("invalid request")
            else:
                raise ChatRequestError("invalid request")

        for keyword in ("description", "title"):
            if keyword in candidate and type(candidate[keyword]) is not str:
                raise ChatRequestError("invalid request")

        if (
            "additionalProperties" in candidate
            and type(candidate["additionalProperties"]) is not bool
        ):
            raise ChatRequestError("invalid request")

        if "required" in candidate:
            required = candidate["required"]
            if type(required) is not list or any(type(item) is not str for item in required):
                raise ChatRequestError("invalid request")
            if len(set(required)) != len(required):
                raise ChatRequestError("invalid request")

        if "enum" in candidate:
            enum = candidate["enum"]
            if (
                type(enum) is not list
                or not enum
                or any(not _is_json_scalar(item) for item in enum)
            ):
                raise ChatRequestError("invalid request")
        if "const" in candidate and not _is_json_scalar(candidate["const"]):
            raise ChatRequestError("invalid request")

        if "items" in candidate:
            items = candidate["items"]
            if type(items) is not dict:
                raise ChatRequestError("invalid request")
            stack.append(items)

        if "anyOf" in candidate:
            any_of = candidate["anyOf"]
            if (
                type(any_of) is not list
                or not any_of
                or any(type(item) is not dict for item in any_of)
            ):
                raise ChatRequestError("invalid request")
            stack.extend(any_of)

        for keyword in ("properties", "$defs"):
            if keyword not in candidate:
                continue
            definitions = candidate[keyword]
            if type(definitions) is not dict or any(
                type(name) is not str or type(value) is not dict
                for name, value in definitions.items()
            ):
                raise ChatRequestError("invalid request")
            stack.extend(definitions.values())


def _parse_response_format(
    value: object,
    *,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
) -> JsonObjectResponseFormat | JsonSchemaResponseFormat:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise ChatRequestError("invalid request")
    if value["type"] == "json_object":
        if set(value) != {"type"}:
            raise ChatRequestError("invalid request")
        return JsonObjectResponseFormat()
    if value["type"] != "json_schema" or set(value) != {"type", "json_schema"}:
        raise ChatRequestError("invalid request")
    raw_format = value["json_schema"]
    if (
        type(raw_format) is not dict
        or not {"name", "schema"} <= set(raw_format)
        or not set(raw_format) <= {"name", "description", "schema", "strict"}
    ):
        raise ChatRequestError("invalid request")
    name = raw_format["name"]
    description = raw_format.get("description")
    strict = raw_format.get("strict")
    schema = raw_format["schema"]
    if (
        type(name) is not str
        or _FORMAT_NAME.fullmatch(name) is None
        or type(schema) is not dict
        or ("description" in raw_format and (type(description) is not str or not description))
        or ("strict" in raw_format and type(strict) is not bool)
    ):
        raise ChatRequestError("invalid request")
    _validate_bounded_json_tree(
        schema,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    _validate_schema_subset(schema)
    try:
        owned_schema = deepcopy(schema)
    except (MemoryError, RecursionError):
        raise ChatRequestError("invalid request") from None
    return JsonSchemaResponseFormat(
        name=name,
        description=description,
        schema=owned_schema,
        strict=strict,
    )


def _parse_reasoning_details(
    value: object,
    *,
    binding_key: str,
    content: str | None,
    tool_calls: tuple[ToolCall, ...],
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
    seen_binding_ids: set[str],
    seen_data_digests: set[bytes],
) -> tuple[tuple[ReasoningDetail, ...], int]:
    if type(value) is not list or not value:
        raise ChatRequestError("invalid request")
    nodes_used = _validate_bounded_json_tree(
        value,
        max_depth=max_json_depth,
        max_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )
    details: list[ReasoningDetail] = []
    for expected_index, raw_detail in enumerate(value):
        if type(raw_detail) is not dict or set(raw_detail) != {
            "type",
            "data",
            "format",
            "id",
            "index",
        }:
            raise ChatRequestError("invalid request")
        data = raw_detail["data"]
        binding_id = raw_detail["id"]
        index = raw_detail["index"]
        data_digest = encrypted_reasoning_data_digest(data, max_string_bytes=max_string_bytes)
        if (
            type(raw_detail["type"]) is not str
            or raw_detail["type"] != "reasoning.encrypted"
            or type(raw_detail["format"]) is not str
            or raw_detail["format"] != "openai-responses-v1"
            or type(binding_id) is not str
            or not binding_id
            or type(index) is not int
            or index != expected_index
            or data_digest is None
            or not reasoning_binding_is_valid(
                binding_key=binding_key,
                content=content,
                tool_calls=tool_calls,
                index=index,
                data=data,
                binding_id=binding_id,
            )
        ):
            raise ChatRequestError("invalid request")
        if binding_id in seen_binding_ids or data_digest in seen_data_digests:
            raise ChatRequestError("invalid request")
        seen_binding_ids.add(binding_id)
        seen_data_digests.add(data_digest)
        details.append(ReasoningDetail(data=data, binding_id=binding_id, index=index))
    return tuple(details), nodes_used


def parse_chat_completion_request(
    value: object,
    *,
    public_model: str,
    max_messages: int,
    max_tools: int,
    max_json_depth: int,
    max_json_nodes: int,
    max_string_bytes: int,
    binding_key: str,
) -> ChatCompletionRequest:
    """Parse without coercion and reject every unsupported field."""
    if (
        type(value) is not dict
        or type(public_model) is not str
        or type(max_messages) is not int
        or type(max_tools) is not int
        or type(max_json_depth) is not int
        or type(max_json_nodes) is not int
        or type(max_string_bytes) is not int
        or type(binding_key) is not str
        or not binding_key
        or max_tools <= 0
        or max_json_depth <= 0
        or max_json_nodes <= 0
        or max_string_bytes <= 0
    ):
        raise ChatRequestError("invalid request")
    document: dict[str, Any] = value
    allowed = {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "stream_options",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
    }
    if not set(document) <= allowed or "model" not in document or "messages" not in document:
        raise ChatRequestError("invalid request")
    if type(document["model"]) is not str or document["model"] != public_model:
        raise ChatRequestError("invalid request")
    raw_messages = document["messages"]
    if type(raw_messages) is not list or not 1 <= len(raw_messages) <= max_messages:
        raise ChatRequestError("invalid request")

    messages: list[ChatMessage] = []
    valid_roles = {"system", "developer", "user", "assistant", "tool"}
    all_call_ids: set[str] = set()
    pending_call_ids: set[str] = set()
    seen_reasoning_binding_ids: set[str] = set()
    seen_reasoning_data_digests: set[bytes] = set()
    remaining_reasoning_nodes = max_json_nodes
    for raw_message in raw_messages:
        if type(raw_message) is not dict or "role" not in raw_message:
            raise ChatRequestError("invalid request")
        role = raw_message["role"]
        if type(role) is not str or role not in valid_roles:
            raise ChatRequestError("invalid request")
        if role == "tool":
            if set(raw_message) != {"role", "content", "tool_call_id"}:
                raise ChatRequestError("invalid request")
            content = raw_message["content"]
            tool_call_id = raw_message["tool_call_id"]
            if (
                not pending_call_ids
                or type(content) is not str
                or type(tool_call_id) is not str
                or not tool_call_id
                or tool_call_id not in pending_call_ids
            ):
                raise ChatRequestError("invalid request")
            pending_call_ids.remove(tool_call_id)
            messages.append(ChatMessage(role="tool", content=content, tool_call_id=tool_call_id))
            continue

        if pending_call_ids:
            raise ChatRequestError("invalid request")
        if role == "assistant":
            allowed_message_fields = {"role", "content", "tool_calls", "reasoning_details"}
            if "content" not in raw_message or not set(raw_message) <= allowed_message_fields:
                raise ChatRequestError("invalid request")
            content = raw_message["content"]
            if content is not None and type(content) is not str:
                raise ChatRequestError("invalid request")
            calls: list[ToolCall] = []
            if "tool_calls" in raw_message:
                raw_calls = raw_message["tool_calls"]
                if type(raw_calls) is not list or not 1 <= len(raw_calls) <= max_tools:
                    raise ChatRequestError("invalid request")
                for raw_call in raw_calls:
                    if type(raw_call) is not dict or set(raw_call) != {"id", "type", "function"}:
                        raise ChatRequestError("invalid request")
                    raw_function = raw_call["function"]
                    if (
                        type(raw_call["type"]) is not str
                        or raw_call["type"] != "function"
                        or type(raw_function) is not dict
                        or set(raw_function) != {"name", "arguments"}
                    ):
                        raise ChatRequestError("invalid request")
                    call_id = raw_call["id"]
                    name = raw_function["name"]
                    if (
                        type(call_id) is not str
                        or not call_id
                        or call_id in all_call_ids
                        or type(name) is not str
                        or not name
                    ):
                        raise ChatRequestError("invalid request")
                    arguments = _validate_arguments_object(raw_function["arguments"])
                    all_call_ids.add(call_id)
                    pending_call_ids.add(call_id)
                    calls.append(ToolCall(call_id=call_id, name=name, arguments=arguments))
            reasoning_details: tuple[ReasoningDetail, ...] = ()
            if "reasoning_details" in raw_message:
                reasoning_details, nodes_used = _parse_reasoning_details(
                    raw_message["reasoning_details"],
                    binding_key=binding_key,
                    content=content,
                    tool_calls=tuple(calls),
                    max_json_depth=max_json_depth,
                    max_json_nodes=remaining_reasoning_nodes,
                    max_string_bytes=max_string_bytes,
                    seen_binding_ids=seen_reasoning_binding_ids,
                    seen_data_digests=seen_reasoning_data_digests,
                )
                remaining_reasoning_nodes -= nodes_used
            if content is None and not calls and not reasoning_details:
                raise ChatRequestError("invalid request")
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=content,
                    tool_calls=tuple(calls),
                    reasoning_details=reasoning_details,
                )
            )
            continue

        if set(raw_message) != {"role", "content"}:
            raise ChatRequestError("invalid request")
        content = raw_message["content"]
        if type(content) is not str:
            raise ChatRequestError("invalid request")
        messages.append(ChatMessage(role=role, content=content))  # type: ignore[arg-type]
    if pending_call_ids:
        raise ChatRequestError("invalid request")

    tools: list[FunctionTool] = []
    if "tools" in document:
        raw_tools = document["tools"]
        if type(raw_tools) is not list or not 1 <= len(raw_tools) <= max_tools:
            raise ChatRequestError("invalid request")
        names: set[str] = set()
        for raw_tool in raw_tools:
            if type(raw_tool) is not dict or set(raw_tool) != {"type", "function"}:
                raise ChatRequestError("invalid request")
            if raw_tool["type"] != "function" or type(raw_tool["type"]) is not str:
                raise ChatRequestError("invalid request")
            raw_function = raw_tool["function"]
            if type(raw_function) is not dict:
                raise ChatRequestError("invalid request")
            if not {"name", "parameters"} <= set(raw_function) or not set(raw_function) <= {
                "name",
                "description",
                "parameters",
                "strict",
            }:
                raise ChatRequestError("invalid request")
            name = raw_function["name"]
            description = raw_function.get("description")
            parameters = raw_function["parameters"]
            strict = raw_function.get("strict")
            if (
                type(name) is not str
                or not name
                or name in names
                or (
                    "description" in raw_function
                    and (type(description) is not str or not description)
                )
                or type(parameters) is not dict
                or ("strict" in raw_function and type(strict) is not bool)
            ):
                raise ChatRequestError("invalid request")
            names.add(name)
            tools.append(
                FunctionTool(
                    name=name,
                    description=description,
                    parameters=deepcopy(parameters),
                    strict=strict,
                )
            )

    tool_choice: Literal["auto", "required", "none"] | NamedFunctionToolChoice | None = None
    if "tool_choice" in document:
        raw_choice = document["tool_choice"]
        if type(raw_choice) is str:
            if raw_choice not in {"auto", "required", "none"}:
                raise ChatRequestError("invalid request")
            if not tools and raw_choice != "none":
                raise ChatRequestError("invalid request")
            tool_choice = raw_choice  # type: ignore[assignment]
        elif type(raw_choice) is dict:
            if set(raw_choice) != {"type", "function"}:
                raise ChatRequestError("invalid request")
            raw_function_choice = raw_choice["function"]
            if (
                type(raw_choice["type"]) is not str
                or raw_choice["type"] != "function"
                or type(raw_function_choice) is not dict
                or set(raw_function_choice) != {"name"}
            ):
                raise ChatRequestError("invalid request")
            choice_name = raw_function_choice["name"]
            if (
                type(choice_name) is not str
                or not choice_name
                or choice_name not in {tool.name for tool in tools}
            ):
                raise ChatRequestError("invalid request")
            tool_choice = NamedFunctionToolChoice(name=choice_name)
        else:
            raise ChatRequestError("invalid request")

    parallel_tool_calls: bool | None = None
    if "parallel_tool_calls" in document:
        candidate_parallel = document["parallel_tool_calls"]
        if not tools or type(candidate_parallel) is not bool:
            raise ChatRequestError("invalid request")
        parallel_tool_calls = candidate_parallel

    response_format: JsonObjectResponseFormat | JsonSchemaResponseFormat | None = None
    if "response_format" in document:
        response_format = _parse_response_format(
            document["response_format"],
            max_json_depth=max_json_depth,
            max_json_nodes=max_json_nodes,
            max_string_bytes=max_string_bytes,
        )

    stream = document.get("stream", False)
    if type(stream) is not bool:
        raise ChatRequestError("invalid request")
    include_usage = False
    if "stream_options" in document:
        stream_options = document["stream_options"]
        if (
            stream is not True
            or type(stream_options) is not dict
            or set(stream_options) != {"include_usage"}
            or type(stream_options["include_usage"]) is not bool
        ):
            raise ChatRequestError("invalid request")
        include_usage = stream_options["include_usage"]
    token_fields = [name for name in ("max_tokens", "max_completion_tokens") if name in document]
    if len(token_fields) > 1:
        raise ChatRequestError("invalid request")
    max_output_tokens: int | None = None
    if token_fields:
        candidate = document[token_fields[0]]
        if type(candidate) is not int or candidate <= 0:
            raise ChatRequestError("invalid request")
        max_output_tokens = candidate
    return ChatCompletionRequest(
        messages=tuple(messages),
        max_output_tokens=max_output_tokens,
        tools=tuple(tools),
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        response_format=response_format,
        stream=stream,
        include_usage=include_usage,
    )
