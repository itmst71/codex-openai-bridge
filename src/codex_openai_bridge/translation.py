"""Deterministic, secret-free Task-6 protocol conversion."""

from __future__ import annotations

from typing import Any

from codex_openai_bridge.wire import ChatCompletionRequest


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
        message.content for message in request.messages if message.role in {"system", "developer"}
    ]
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    payload["input"] = [
        {
            "role": message.role,
            "content": [
                {
                    "type": "input_text" if message.role == "user" else "output_text",
                    "text": message.content,
                }
            ],
        }
        for message in request.messages
        if message.role in {"user", "assistant"}
    ]
    if request.max_output_tokens is not None:
        payload["max_output_tokens"] = request.max_output_tokens
    return payload


def _exact_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
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
        for item in output:
            if type(item) is not dict:
                raise UpstreamResponseError("invalid upstream response")
            if item.get("type") == "reasoning":
                continue
            if (
                item.get("type") != "message"
                or item.get("role") != "assistant"
                or item.get("status") != "completed"
            ):
                raise UpstreamResponseError("invalid upstream response")
            assistant_messages.append(item)
        if len(assistant_messages) != 1:
            raise UpstreamResponseError("invalid upstream response")
        content = assistant_messages[0].get("content")
        if type(content) is not list or not content:
            raise UpstreamResponseError("invalid upstream response")
        text_parts: list[str] = []
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

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created_at,
        "model": public_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(text_parts)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }
