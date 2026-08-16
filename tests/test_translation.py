from __future__ import annotations

import pytest

from codex_openai_bridge.translation import (
    UpstreamResponseError,
    chat_request_to_responses,
    responses_to_chat_completion,
)
from codex_openai_bridge.wire import ChatCompletionRequest, ChatMessage


def test_translation_uses_upstream_model_and_forces_non_persistence_and_non_streaming() -> None:
    request = ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="hello"),),
        max_output_tokens=None,
    )

    assert chat_request_to_responses(request, upstream_model="upstream-model") == {
        "model": "upstream-model",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "store": False,
        "stream": False,
    }


def test_system_and_developer_text_become_ordered_deterministic_instructions() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(role="system", content="system text"),
            ChatMessage(role="user", content="question"),
            ChatMessage(role="developer", content="developer text"),
        ),
        max_output_tokens=None,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["instructions"] == "system text\n\ndeveloper text"
    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "question"}]}
    ]


def test_ordered_user_and_assistant_history_uses_role_appropriate_text_parts() -> None:
    request = ChatCompletionRequest(
        messages=(
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="second"),
            ChatMessage(role="user", content="third"),
        ),
        max_output_tokens=23,
    )

    payload = chat_request_to_responses(request, upstream_model="upstream-model")

    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "second"}],
        },
        {"role": "user", "content": [{"type": "input_text", "text": "third"}]},
    ]
    assert payload["max_output_tokens"] == 23


def _completed_response() -> dict[str, object]:
    return {
        "id": "resp_test",
        "status": "completed",
        "created_at": 1_723_456_789,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "id": "msg_test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
        ],
        "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
    }


def test_completed_assistant_response_maps_to_openai_chat_completion() -> None:
    assert responses_to_chat_completion(_completed_response(), public_model="codex") == {
        "id": "resp_test",
        "object": "chat.completion",
        "created": 1_723_456_789,
        "model": "codex",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        ("id", None),
        ("id", ""),
        ("status", "in_progress"),
        ("created_at", True),
        ("created_at", -1),
        ("output", None),
        ("output", []),
        ("usage", None),
    ],
)
def test_malformed_response_core_is_rejected_generically(mutation: tuple[str, object]) -> None:
    response = _completed_response()
    response[mutation[0]] = mutation[1]

    with pytest.raises(UpstreamResponseError) as caught:
        responses_to_chat_completion(response, public_model="codex")

    assert caught.value.args == ("invalid upstream response",)


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "1", None])
def test_usage_requires_exact_nonnegative_integers(field: str, value: object) -> None:
    response = _completed_response()
    usage = response["usage"]
    assert isinstance(usage, dict)
    usage[field] = value

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


def test_assistant_message_item_must_be_completed() -> None:
    response = _completed_response()
    output = response["output"]
    assert isinstance(output, list)
    message = output[1]
    assert isinstance(message, dict)
    message["status"] = "in_progress"

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


@pytest.mark.parametrize(
    "content",
    [
        [],
        None,
        [{"type": "input_text", "text": "wrong kind"}],
        [{"type": "output_text", "text": None}],
    ],
)
def test_assistant_output_requires_output_text_parts(content: object) -> None:
    response = _completed_response()
    output = response["output"]
    assert isinstance(output, list)
    message = output[1]
    assert isinstance(message, dict)
    message["content"] = content

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")


@pytest.mark.parametrize(
    "unsupported_item",
    [
        None,
        "invalid",
        {"type": "function_call", "name": "unsupported"},
        {"type": "message", "role": "user", "content": []},
    ],
)
def test_rejects_malformed_or_unsupported_output_items(unsupported_item: object) -> None:
    response = _completed_response()
    output = response["output"]
    assert isinstance(output, list)
    output.insert(0, unsupported_item)

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        responses_to_chat_completion(response, public_model="codex")
