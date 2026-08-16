"""Deterministic, secret-free protocol conversion."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from codex_openai_bridge.wire import ChatCompletionRequest, NamedFunctionToolChoice


class UpstreamResponseError(ValueError):
    """Raised when an upstream response cannot be represented safely."""


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
        "stream": False,
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
    if request.max_output_tokens is not None:
        payload["max_output_tokens"] = request.max_output_tokens
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


def responses_to_chat_completion(value: object, *, public_model: str) -> dict[str, Any]:
    """Convert one completed assistant Responses result to Chat Completions format."""
    try:
        if type(value) is not dict:
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
        call_ids: set[str] = set()
        for item in output:
            if type(item) is not dict:
                raise UpstreamResponseError("invalid upstream response")
            if item.get("type") == "reasoning":
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
