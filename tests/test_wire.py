from __future__ import annotations

import pytest

from codex_openai_bridge.wire import (
    ChatCompletionRequest,
    ChatMessage,
    ChatRequestError,
    parse_chat_completion_request,
)


def _parse(value: object) -> ChatCompletionRequest:
    return parse_chat_completion_request(value, public_model="codex", max_messages=8)


def test_parses_exact_public_model_and_minimal_text_message() -> None:
    assert _parse(
        {"model": "codex", "messages": [{"role": "user", "content": "hello"}]}
    ) == ChatCompletionRequest(
        messages=(ChatMessage(role="user", content="hello"),),
        max_output_tokens=None,
    )


@pytest.mark.parametrize("model", ["other", "Codex", "codex ", True, None])
def test_rejects_every_model_other_than_the_exact_public_alias(model: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": model, "messages": [{"role": "user", "content": "text"}]})


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"model": "codex"},
        {"messages": [{"role": "user", "content": "text"}]},
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "temperature": 0,
        },
    ],
)
def test_rejects_missing_or_extra_core_fields_without_echoing_values(value: object) -> None:
    marker = "SENSITIVE_REQUEST_VALUE"
    with pytest.raises(ChatRequestError) as caught:
        _parse(value)

    assert caught.value.args == ("invalid request",)
    assert marker not in repr(caught.value)


@pytest.mark.parametrize("role", ["tool", "function", "User", "", True, None])
def test_rejects_unknown_or_non_string_roles(role: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": "codex", "messages": [{"role": role, "content": "text"}]})


@pytest.mark.parametrize("content", [None, True, 7, ["text"], {"type": "text"}])
def test_rejects_non_string_and_multimodal_content(content: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse({"model": "codex", "messages": [{"role": "user", "content": content}]})


@pytest.mark.parametrize(
    "messages",
    [
        [],
        "not-a-list",
        [None],
        [{"role": "user"}],
        [{"role": "user", "content": "text", "name": "extra"}],
        [
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
            {"role": "user", "content": "3"},
        ],
    ],
)
def test_rejects_malformed_messages_and_configured_count_overflow(messages: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        parse_chat_completion_request(
            {"model": "codex", "messages": messages},
            public_model="codex",
            max_messages=2,
        )


@pytest.mark.parametrize(
    "field",
    ["max_tokens", "max_completion_tokens"],
)
def test_maps_one_supported_token_limit(field: str) -> None:
    request = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            field: 17,
        }
    )

    assert request.max_output_tokens == 17


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, 0, -1])
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_rejects_non_positive_or_non_exact_integer_token_limits(field: str, value: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                field: value,
            }
        )


def test_rejects_both_token_limit_aliases_together() -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                "max_tokens": 1,
                "max_completion_tokens": 2,
            }
        )


def test_stream_may_be_absent_or_exact_false() -> None:
    without_stream = _parse({"model": "codex", "messages": [{"role": "user", "content": "text"}]})
    with_false = _parse(
        {
            "model": "codex",
            "messages": [{"role": "user", "content": "text"}],
            "stream": False,
        }
    )

    assert with_false == without_stream


@pytest.mark.parametrize("stream", [True, 0, 1, None, "false"])
def test_rejects_streaming_and_non_boolean_stream_values(stream: object) -> None:
    with pytest.raises(ChatRequestError, match=r"^invalid request$"):
        _parse(
            {
                "model": "codex",
                "messages": [{"role": "user", "content": "text"}],
                "stream": stream,
            }
        )
