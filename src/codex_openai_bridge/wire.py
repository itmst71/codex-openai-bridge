"""Strict Chat Completions request wire model."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
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
class ChatMessage:
    """One validated Chat Completions message."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


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
class ChatCompletionRequest:
    """The closed Chat Completions request contract."""

    messages: tuple[ChatMessage, ...]
    max_output_tokens: int | None
    tools: tuple[FunctionTool, ...] = ()
    tool_choice: Literal["auto", "required", "none"] | NamedFunctionToolChoice | None = None
    parallel_tool_calls: bool | None = None


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
        raise ChatRequestError("invalid request")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
            parse_float=_parse_finite_json_float,
        )
    except (ValueError, OverflowError, RecursionError):
        raise ChatRequestError("invalid request") from None
    if type(parsed) is not dict:
        raise ChatRequestError("invalid request")
    return value


def parse_chat_completion_request(
    value: object,
    *,
    public_model: str,
    max_messages: int,
    max_tools: int,
) -> ChatCompletionRequest:
    """Parse without coercion and reject every unsupported field."""
    if (
        type(value) is not dict
        or type(public_model) is not str
        or type(max_messages) is not int
        or type(max_tools) is not int
        or max_tools <= 0
    ):
        raise ChatRequestError("invalid request")
    document: dict[str, Any] = value
    allowed = {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
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
        if role == "assistant" and "tool_calls" in raw_message:
            if set(raw_message) != {"role", "content", "tool_calls"}:
                raise ChatRequestError("invalid request")
            content = raw_message["content"]
            raw_calls = raw_message["tool_calls"]
            if (
                (content is not None and type(content) is not str)
                or type(raw_calls) is not list
                or not 1 <= len(raw_calls) <= max_tools
            ):
                raise ChatRequestError("invalid request")
            calls: list[ToolCall] = []
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
            messages.append(ChatMessage(role="assistant", content=content, tool_calls=tuple(calls)))
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

    if "stream" in document and document["stream"] is not False:
        raise ChatRequestError("invalid request")
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
    )
