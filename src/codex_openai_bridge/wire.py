"""Strict Task-6 Chat Completions request wire model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


class ChatRequestError(ValueError):
    """Raised when a public Chat Completions request is unsupported or malformed."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One validated text-only Chat Completions message."""

    role: Literal["system", "developer", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ChatCompletionRequest:
    """The closed Task-6 request contract."""

    messages: tuple[ChatMessage, ...]
    max_output_tokens: int | None


def parse_chat_completion_request(
    value: object,
    *,
    public_model: str,
    max_messages: int,
) -> ChatCompletionRequest:
    """Parse without coercion and reject every field outside the Task-6 contract."""
    if type(value) is not dict or type(public_model) is not str or type(max_messages) is not int:
        raise ChatRequestError("invalid request")
    document: dict[str, Any] = value
    allowed = {"model", "messages", "max_tokens", "max_completion_tokens", "stream"}
    if not set(document) <= allowed or "model" not in document or "messages" not in document:
        raise ChatRequestError("invalid request")
    if type(document["model"]) is not str or document["model"] != public_model:
        raise ChatRequestError("invalid request")
    raw_messages = document["messages"]
    if type(raw_messages) is not list or not 1 <= len(raw_messages) <= max_messages:
        raise ChatRequestError("invalid request")

    messages: list[ChatMessage] = []
    valid_roles = {"system", "developer", "user", "assistant"}
    for raw_message in raw_messages:
        if type(raw_message) is not dict or set(raw_message) != {"role", "content"}:
            raise ChatRequestError("invalid request")
        role = raw_message["role"]
        content = raw_message["content"]
        if type(role) is not str or role not in valid_roles or type(content) is not str:
            raise ChatRequestError("invalid request")
        messages.append(ChatMessage(role=role, content=content))  # type: ignore[arg-type]

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
    return ChatCompletionRequest(messages=tuple(messages), max_output_tokens=max_output_tokens)
