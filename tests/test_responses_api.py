from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from aiohttp import web
from multidict import CIMultiDict
from openai.types.responses import Response

import codex_openai_bridge.app as app_module
import codex_openai_bridge.upstream as upstream_module
from codex_openai_bridge.app import create_app
from codex_openai_bridge.auth import Credential
from codex_openai_bridge.config import Settings
from codex_openai_bridge.json_boundary import JsonBodyTooLarge, JsonBoundaryError
from codex_openai_bridge.responses import (
    ResponsesRequestError,
    parse_responses_request,
    responses_request_to_upstream,
    responses_to_public,
)
from codex_openai_bridge.translation import UpstreamResponseError
from codex_openai_bridge.upstream import HttpxResponsesUpstream

TOKEN = "a" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class NeverCredentialManager:
    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        raise AssertionError("credential resolver must not be called")


class StaticCredentialManager:
    def __init__(self, credential: Credential) -> None:
        self.credential = credential
        self.calls: list[bool] = []

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        self.calls.append(force_refresh)
        return self.credential


class FailingCredentialManager:
    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        del force_refresh
        raise RuntimeError("SENSITIVE CREDENTIAL FAILURE")


class FakeUpstream:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[Credential, dict[str, Any]]] = []

    async def create_response(
        self,
        credential: Credential,
        payload: dict[str, Any],
    ) -> object:
        self.calls.append((credential, payload))
        return self.responses[len(self.calls) - 1]


def _settings(tmp_path: Path) -> Settings:
    token_file = tmp_path / "client-token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    token_file.chmod(0o600)
    return replace(Settings.from_env(), client_token_file=token_file)


def _credential() -> Credential:
    return Credential(
        access_token="upstream-token",
        base_url="https://chatgpt.com/backend-api/codex",
        account_id="account-1",
        expires_at=4_102_444_800,
    )


def _text_response(text: str = "hello back") -> dict[str, Any]:
    return {
        "id": "resp_public_contract",
        "object": "response",
        "created_at": 1_723_456_789,
        "status": "completed",
        "model": "SENSITIVE_UPSTREAM_MODEL",
        "output": [
            {
                "id": "msg_public_contract",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 5,
        },
        "internal_account": "SENSITIVE_INTERNAL_ACCOUNT",
    }


@pytest.mark.asyncio
async def test_authenticated_nonstream_string_response_is_reconstructed_and_sdk_parses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    credential = _credential()
    manager = StaticCredentialManager(credential)
    upstream = FakeUpstream([_text_response()])
    app = create_app(settings, manager, upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello"}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))
    body = json.loads(response.body)

    assert response.status == 200
    assert manager.calls == [False]
    assert upstream.calls == [
        (
            credential,
            {
                "model": settings.upstream_model,
                "input": "hello",
                "store": False,
                "stream": False,
                "include": ["reasoning.encrypted_content"],
            },
        )
    ]
    assert body == {
        "id": "resp_public_contract",
        "object": "response",
        "created_at": 1_723_456_789,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": "codex",
        "output": [
            {
                "id": "msg_public_contract",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello back", "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 5,
        },
    }
    parsed = Response.model_validate(body)
    assert parsed.model == "codex"
    assert parsed.output_text == "hello back"
    assert "SENSITIVE" not in repr(body)


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        {"model": "wrong", "input": "x"},
        {"model": "codex", "input": "x", "unknown": "SENSITIVE REQUEST"},
        {"model": "codex", "input": "x", "max_output_tokens": True},
        {"model": "codex", "input": "x", "store": True},
        {
            "model": "codex",
            "input": [{"type": "function_call_output", "call_id": "missing", "output": "x"}],
        },
    ],
)
async def test_malformed_request_matrix_never_resolves_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: object,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream([]))

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return document

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_request"
    assert "SENSITIVE" not in response.body.decode()


@pytest.mark.asyncio
async def test_credential_failure_is_sanitized_without_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = FakeUpstream([])
    app = create_app(_settings(tmp_path), FailingCredentialManager(), upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello"}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 503
    assert json.loads(response.body)["error"]["code"] == "credentials_unavailable"
    assert "SENSITIVE" not in response.body.decode()
    assert upstream.calls == []


def _parse_request(
    value: object,
    *,
    max_items: int = 16,
    max_tools: int = 8,
    max_json_depth: int = 16,
    max_json_nodes: int = 256,
    max_string_bytes: int = 4096,
) -> object:
    return parse_responses_request(
        value,
        public_model="codex",
        max_items=max_items,
        max_tools=max_tools,
        max_json_depth=max_json_depth,
        max_json_nodes=max_json_nodes,
        max_string_bytes=max_string_bytes,
    )


def test_request_options_reuse_closed_chat_contracts_and_reconstruct_exact_policy() -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    document = {
        "model": "codex",
        "input": [{"role": "user", "content": "hello"}],
        "instructions": "Be concise",
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 42,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": "Look up a value",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
                "strict": True,
            }
        ],
        "tool_choice": {"type": "function", "name": "lookup"},
        "parallel_tool_calls": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "answer",
                "schema": schema,
                "strict": True,
            }
        },
    }

    parsed = _parse_request(document)
    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]
    document["input"][0]["content"] = "mutated"  # type: ignore[index]
    schema["properties"]["answer"]["type"] = "number"  # type: ignore[index]

    assert payload == {
        "model": "gpt-upstream",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "instructions": "Be concise",
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 42,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": "Look up a value",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
                "strict": True,
            }
        ],
        "tool_choice": {"type": "function", "name": "lookup"},
        "parallel_tool_calls": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "answer",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        },
    }


@pytest.mark.parametrize("data", ["YQ==", "YQ", "++8=", "--8"])
def test_direct_responses_reasoning_accepts_strict_base64_variants(data: str) -> None:
    parsed = _parse_request(
        {
            "model": "codex",
            "input": [
                {
                    "id": "rs_one",
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [],
                    "encrypted_content": data,
                },
                {
                    "id": "msg_one",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer", "annotations": []}],
                },
            ],
        }
    )

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]
    assert payload["input"][0] == {"type": "reasoning", "encrypted_content": data}


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"model": "codex"},
        {"input": "x"},
        {"model": "other", "input": "x"},
        {"model": True, "input": "x"},
        {"model": "codex", "input": None},
        {"model": "codex", "input": []},
        {"model": "codex", "input": "x", "unknown": "SENSITIVE"},
        {"model": "codex", "input": "x", "url": "https://trap.invalid"},
        {"model": "codex", "input": "x", "authorization": "secret"},
        {"model": "codex", "input": "x", "account_id": "secret"},
        {"model": "codex", "input": "x", "store": True},
        {"model": "codex", "input": "x", "store": 0},
        {"model": "codex", "input": "x", "stream": 0},
        {"model": "codex", "input": "x", "include": []},
        {"model": "codex", "input": "x", "include": "reasoning.encrypted_content"},
        {"model": "codex", "input": "x", "instructions": None},
        {"model": "codex", "input": "x", "max_output_tokens": True},
        {"model": "codex", "input": "x", "max_output_tokens": 0},
        {
            "model": "codex",
            "input": "x",
            "tools": [{"type": "web_search_preview"}],
        },
        {"model": "codex", "input": "x", "text": {"format": {"type": "text"}}},
        {
            "model": "codex",
            "input": [{"role": "assistant", "content": "not an output item"}],
        },
        {
            "model": "codex",
            "input": [{"role": "user", "content": [{"type": "output_text", "text": "x"}]}],
        },
    ],
)
def test_malformed_or_authority_expanding_requests_fail_generically(document: object) -> None:
    with pytest.raises(ResponsesRequestError) as caught:
        _parse_request(document)

    assert caught.value.args == ("invalid request",)
    assert "SENSITIVE" not in repr(caught.value)


def test_request_depth_nodes_and_cumulative_strings_use_exact_existing_budgets() -> None:
    document = {"model": "codex", "input": "é"}
    _parse_request(document, max_json_depth=2, max_json_nodes=3, max_string_bytes=17)

    for limits in (
        {"max_json_depth": 1, "max_json_nodes": 3, "max_string_bytes": 17},
        {"max_json_depth": 2, "max_json_nodes": 2, "max_string_bytes": 17},
        {"max_json_depth": 2, "max_json_nodes": 3, "max_string_bytes": 16},
    ):
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request(document, **limits)


@pytest.mark.parametrize(
    "items",
    [
        [
            {
                "id": "rs_one",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "AB",
            }
        ],
        [
            {
                "id": "rs_one",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "++8=",
            },
            {
                "id": "rs_two",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "--8=",
            },
        ],
        [
            {
                "id": "fc_one",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_one",
                "name": "tool",
                "arguments": "{}",
            }
        ],
        [
            {
                "type": "function_call_output",
                "call_id": "missing",
                "output": "result",
            }
        ],
        [
            {
                "id": "fc_one",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_one",
                "name": "tool",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_one", "output": "one"},
            {"type": "function_call_output", "call_id": "call_one", "output": "two"},
        ],
        [
            {
                "id": "fc_one",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_one",
                "name": "tool",
                "arguments": "[]",
            },
            {"type": "function_call_output", "call_id": "call_one", "output": "one"},
        ],
    ],
)
def test_reasoning_and_tool_history_ambiguity_or_bad_association_fails(items: object) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request({"model": "codex", "input": items})


@pytest.mark.asyncio
async def test_two_request_output_history_round_trip_is_deterministic_and_not_authenticated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    credential = _credential()
    manager = StaticCredentialManager(credential)
    first_upstream = {
        "id": "resp_first",
        "object": "response",
        "created_at": 10,
        "status": "completed",
        "model": "private-model",
        "output": [
            {
                "id": "rs_first",
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "PRIVATE REASONING SUMMARY"}],
                "encrypted_content": "YQ==",
            },
            {
                "id": "msg_first",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Checking.", "annotations": []}],
            },
            {
                "id": "fc_first",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_weather",
                "name": "weather",
                "arguments": '{"city":"Tokyo"}',
            },
        ],
        "usage": {
            "input_tokens": 4,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 9,
        },
    }
    upstream = FakeUpstream([first_upstream, _text_response("Sunny.")])
    app = create_app(settings, manager, upstream=upstream)
    tool = {
        "type": "function",
        "name": "weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        "strict": True,
    }
    documents: list[dict[str, object]] = [
        {"model": "codex", "input": "Weather?", "tools": [tool]},
    ]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    first_response = await app_module._responses(request)
    assert isinstance(first_response.body, (bytes, bytearray))
    first_body = json.loads(first_response.body)
    documents.append(
        {
            "model": "codex",
            "input": [
                {"role": "user", "content": "Weather?"},
                *first_body["output"],
                {
                    "type": "function_call_output",
                    "call_id": "call_weather",
                    "output": '{"condition":"sunny"}',
                },
            ],
            "tools": [tool],
        }
    )
    second_response = await app_module._responses(request)
    assert isinstance(second_response.body, (bytes, bytearray))
    second_body = json.loads(second_response.body)

    assert first_response.status == second_response.status == 200
    assert Response.model_validate(first_body).output[0].type == "reasoning"
    assert first_body["output"][0] == {
        "id": "rs_first",
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "encrypted_content": "YQ==",
    }
    assert "PRIVATE" not in repr(first_body)
    assert second_body["model"] == "codex"
    assert manager.calls == [False, False]
    # Responses IDs satisfy the SDK schema but carry no bridge authenticity and are stripped
    # before replay. The canonical ciphertext is opaque and is forwarded without decryption.
    assert upstream.calls[1][1]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Weather?"}]},
        {"type": "reasoning", "encrypted_content": "YQ=="},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking."}],
        },
        {
            "type": "function_call",
            "call_id": "call_weather",
            "name": "weather",
            "arguments": '{"city":"Tokyo"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_weather",
            "output": '{"condition":"sunny"}',
        },
    ]
    assert "rs_first" not in repr(upstream.calls[1][1])
    assert "msg_first" not in repr(upstream.calls[1][1])
    assert "fc_first" not in repr(upstream.calls[1][1])


def _public_response_for(
    document: object,
    value: object,
    **limits: int,
) -> dict[str, Any]:
    request = _parse_request(document)
    return responses_to_public(
        value,
        request=request,  # type: ignore[arg-type]
        public_model="codex",
        max_items=limits.get("max_items", 16),
        max_tools=limits.get("max_tools", 8),
        max_json_depth=limits.get("max_json_depth", 16),
        max_json_nodes=limits.get("max_json_nodes", 256),
        max_string_bytes=limits.get("max_string_bytes", 4096),
    )


def _public_response(value: object, **limits: int) -> dict[str, Any]:
    return _public_response_for({"model": "codex", "input": "hello"}, value, **limits)


def _function_item(
    *,
    item_id: str = "fc_one",
    call_id: str = "call_one",
    name: str = "allowed",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": "{}",
    }


def _function_tool(name: str = "allowed") -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "parameters": {"type": "object", "properties": {}},
    }


def test_upstream_function_call_requires_a_declared_request_tool() -> None:
    value = _text_response()
    value["output"] = [_function_item()]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response(value)


def test_upstream_function_call_name_must_be_declared() -> None:
    value = _text_response()
    value["output"] = [_function_item(name="undeclared")]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(
            {"model": "codex", "input": "hello", "tools": [_function_tool()]},
            value,
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"tool_choice": "required"},
        {"tool_choice": {"type": "function", "name": "allowed"}},
        {"parallel_tool_calls": False},
    ],
)
def test_matching_single_function_call_satisfies_request_tool_policy(
    policy: dict[str, object],
) -> None:
    document: dict[str, object] = {
        "model": "codex",
        "input": "hello",
        "tools": [_function_tool()],
        **policy,
    }
    value = _text_response()
    value["output"] = [_function_item()]

    public = _public_response_for(document, value)
    assert public["output"] == [_function_item()]


@pytest.mark.parametrize(
    "document,output",
    [
        (
            {
                "model": "codex",
                "input": "hello",
                "tools": [_function_tool()],
                "tool_choice": "none",
            },
            [_function_item()],
        ),
        (
            {
                "model": "codex",
                "input": "hello",
                "tools": [_function_tool()],
                "tool_choice": "required",
            },
            _text_response()["output"],
        ),
        (
            {
                "model": "codex",
                "input": "hello",
                "tools": [_function_tool(), _function_tool("other")],
                "tool_choice": {"type": "function", "name": "allowed"},
            },
            [_function_item(name="other")],
        ),
        (
            {
                "model": "codex",
                "input": "hello",
                "tools": [_function_tool()],
                "parallel_tool_calls": False,
            },
            [
                _function_item(),
                _function_item(item_id="fc_two", call_id="call_two"),
            ],
        ),
    ],
)
def test_upstream_output_must_honor_request_tool_policy(
    document: object,
    output: list[object],
) -> None:
    value = _text_response()
    value["output"] = output

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, value)


def test_upstream_response_cannot_reuse_a_historical_reasoning_item_id() -> None:
    document = {
        "model": "codex",
        "input": [
            {
                "id": "rs_history",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "YQ==",
            },
            {
                "id": "msg_history",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "earlier", "annotations": []}],
            },
            {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
    }
    value = _text_response()
    value["output"].insert(
        0,
        {
            "id": "rs_history",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": "Yg==",
        },
    )

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, value)


def test_upstream_response_cannot_reuse_a_historical_call_id() -> None:
    document = {
        "model": "codex",
        "input": [
            _function_item(call_id="call_history"),
            {
                "type": "function_call_output",
                "call_id": "call_history",
                "output": "done",
            },
            {"role": "user", "content": "continue"},
        ],
        "tools": [_function_tool()],
    }
    value = _text_response()
    value["output"] = [_function_item(call_id="call_history")]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, value)


def test_upstream_response_cannot_reuse_equivalent_historical_reasoning() -> None:
    document = {
        "model": "codex",
        "input": [
            {
                "id": "rs_history",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "++8=",
            },
            {
                "id": "msg_history",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "history", "annotations": []}],
            },
            {"role": "user", "content": "continue"},
        ],
    }
    value = _text_response()
    value["output"] = [
        {
            "id": "rs_new",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": "--8",
        },
        value["output"][0],
    ]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(status="in_progress"),
        lambda value: value.update(object="chat.completion"),
        lambda value: value.update(created_at=True),
        lambda value: value.update(error={"message": "SENSITIVE RAW ERROR"}),
        lambda value: value.update(incomplete_details={"reason": "max_output_tokens"}),
        lambda value: value["usage"].update(input_tokens=True),
        lambda value: value["usage"].update(total_tokens=-1),
        lambda value: value["usage"].update(extra=1),
        lambda value: value["output"][0].update(id="resp_public_contract"),
        lambda value: value["output"][0].update(secret="SENSITIVE_OUTPUT_FIELD"),
        lambda value: value["output"][0]["content"][0].update(text=True),
    ],
)
def test_malformed_or_nonallowlisted_upstream_response_is_generic_502_input(
    mutation: Any,
) -> None:
    value = _text_response()
    mutation(value)

    with pytest.raises(UpstreamResponseError) as caught:
        _public_response(value)

    assert caught.value.args == ("invalid upstream response",)
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.parametrize(
    "output",
    [
        [
            {
                "id": "fc_one",
                "type": "function_call",
                "status": "completed",
                "call_id": "same",
                "name": "one",
                "arguments": "{}",
            },
            {
                "id": "fc_two",
                "type": "function_call",
                "status": "completed",
                "call_id": "same",
                "name": "two",
                "arguments": "{}",
            },
        ],
        [
            {
                "id": "fc_one",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_one",
                "name": "one",
                "arguments": "[]",
            }
        ],
        [
            {
                "id": "rs_one",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "++8=",
            },
            {
                "id": "rs_two",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "--8=",
            },
            _text_response()["output"][0],
        ],
        [
            {
                "id": "rs_one",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "AB",
            },
            _text_response()["output"][0],
        ],
        [
            {
                "id": "rs_one",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "YQ==",
                "content": [{"type": "reasoning_text", "text": "PRIVATE PLAINTEXT"}],
            },
            _text_response()["output"][0],
        ],
    ],
)
def test_duplicate_or_unsafe_output_items_fail_closed(output: list[object]) -> None:
    value = _text_response()
    value["output"] = output

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "duplicate_id", "secret_field"])
async def test_malformed_upstream_matrix_is_sanitized_to_generic_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    value = _text_response()
    if failure == "status":
        value["status"] = "failed"
    elif failure == "duplicate_id":
        value["output"][0]["id"] = value["id"]
    else:
        value["output"][0]["secret"] = "SENSITIVE UPSTREAM OUTPUT"
    manager = StaticCredentialManager(_credential())
    app = create_app(_settings(tmp_path), manager, upstream=FakeUpstream([value]))

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello"}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 502
    assert json.loads(response.body)["error"]["code"] == "upstream_error"
    assert "SENSITIVE" not in response.body.decode()
    assert manager.calls == [False]


def test_reasoning_cannot_move_after_visible_assistant_output() -> None:
    message = _text_response()["output"][0]
    reasoning = {
        "id": "rs_late",
        "type": "reasoning",
        "status": "completed",
        "summary": [],
        "encrypted_content": "YQ==",
    }

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request({"model": "codex", "input": [message, reasoning]})

    value = _text_response()
    value["output"] = [message, reasoning]
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,status,code",
    [
        (JsonBoundaryError("SENSITIVE JSON"), 400, "invalid_json"),
        (JsonBodyTooLarge("SENSITIVE BODY"), 413, "request_too_large"),
    ],
)
async def test_json_boundary_failures_precede_credentials_and_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: JsonBoundaryError,
    status: int,
    code: str,
) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, NeverCredentialManager(), upstream=FakeUpstream([]))
    captured: dict[str, object] = {}

    async def fail_read(_request: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        raise failure

    monkeypatch.setattr(app_module, "read_json_request", fail_read)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == status
    assert json.loads(response.body)["error"]["code"] == code
    assert "SENSITIVE" not in response.body.decode()
    assert captured == {
        "max_body_bytes": settings.max_request_body_bytes,
        "max_depth": settings.max_json_depth,
        "max_nodes": settings.max_json_nodes,
        "max_string_bytes": settings.max_string_bytes,
    }


@pytest.mark.asyncio
async def test_total_request_deadline_starts_before_body_and_precedes_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), total_request_deadline_seconds=0.01)
    app = create_app(settings, NeverCredentialManager(), upstream=FakeUpstream([]))
    never = asyncio.Event()

    async def blocked_read(_request: Any, **_kwargs: Any) -> object:
        await never.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(app_module, "read_json_request", blocked_read)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 504
    assert json.loads(response.body)["error"]["code"] == "upstream_timeout"


@pytest.mark.asyncio
async def test_total_request_deadline_is_checked_after_response_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = StaticCredentialManager(_credential())
    app = create_app(settings, manager, upstream=FakeUpstream([_text_response()]))
    expired = False

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello"}

    untyped_app = cast(Any, app_module)
    real_converter = untyped_app.responses_to_public

    def convert_and_expire(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal expired
        result = cast(dict[str, Any], real_converter(*args, **kwargs))
        expired = True
        return result

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    monkeypatch.setattr(app_module, "responses_to_public", convert_and_expire)
    monkeypatch.setattr(
        untyped_app.time,
        "monotonic",
        lambda: settings.total_request_deadline_seconds + 1 if expired else 0.0,
    )

    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 504
    assert json.loads(response.body)["error"]["code"] == "upstream_timeout"


@pytest.mark.asyncio
async def test_real_upstream_retries_exactly_one_401_for_responses_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    initial = _credential()
    refreshed = replace(initial, access_token="refreshed-token", account_id="account-2")
    manager = StaticCredentialManager(initial)
    requests: list[httpx.Request] = []
    refresh_calls = 0

    async def decode_in_loop(function: Any, *args: Any, **kwargs: Any) -> object:
        return function(*args, **kwargs)

    # This test owns retry semantics; dedicated upstream tests own threaded decoding.
    # Avoid creating a default-executor thread, whose teardown is unavailable here.
    monkeypatch.setattr(cast(Any, upstream_module).asyncio, "to_thread", decode_in_loop)

    async def refresh() -> Credential:
        nonlocal refresh_calls
        refresh_calls += 1
        return refreshed

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, content=b"SENSITIVE AUTH BODY")
        return httpx.Response(200, json=_text_response())

    upstream = HttpxResponsesUpstream(
        settings,
        transport=httpx.MockTransport(handler),
        credential_refresher=refresh,
    )
    app = create_app(settings, manager, upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello", "store": False}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    try:
        response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    finally:
        await upstream.aclose()

    assert response.status == 200
    assert manager.calls == [False]
    assert refresh_calls == 1
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == "Bearer upstream-token"
    assert requests[1].headers["Authorization"] == "Bearer refreshed-token"
    assert (
        json.loads(requests[0].content)
        == json.loads(requests[1].content)
        == {
            "model": settings.upstream_model,
            "input": "hello",
            "store": False,
            "stream": False,
            "include": ["reasoning.encrypted_content"],
        }
    )


@pytest.mark.asyncio
async def test_real_upstream_body_cap_is_sanitized_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(_settings(tmp_path), max_upstream_body_bytes=16)
    manager = StaticCredentialManager(_credential())
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b'{"secret":"SENSITIVE UPSTREAM BODY"}')

    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    app = create_app(settings, manager, upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello"}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    try:
        response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    finally:
        await upstream.aclose()
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 502
    assert calls == 1
    assert json.loads(response.body)["error"]["code"] == "upstream_error"
    assert "SENSITIVE" not in response.body.decode()


@pytest.mark.asyncio
async def test_raw_upstream_duplicate_json_key_is_rejected_before_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    manager = StaticCredentialManager(_credential())
    wire = json.dumps(_text_response(), separators=(",", ":")).replace(
        '"id":"resp_public_contract"',
        '"id":"SENSITIVE_SHADOW","id":"resp_public_contract"',
        1,
    )

    async def decode_in_loop(function: Any, *args: Any, **kwargs: Any) -> object:
        return function(*args, **kwargs)

    monkeypatch.setattr(cast(Any, upstream_module).asyncio, "to_thread", decode_in_loop)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=wire.encode())

    upstream = HttpxResponsesUpstream(settings, transport=httpx.MockTransport(handler))
    app = create_app(settings, manager, upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello"}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    try:
        response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    finally:
        await upstream.aclose()
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 502
    assert json.loads(response.body)["error"]["code"] == "upstream_error"
    assert "SENSITIVE" not in response.body.decode()


class _FakeRequest(dict[object, object]):
    def __init__(self, app: web.Application, headers: list[tuple[str, str]]) -> None:
        super().__init__()
        self.app = app
        self.path = "/v1/responses"
        self.headers: CIMultiDict[str] = CIMultiDict(headers)


@pytest.mark.asyncio
async def test_responses_auth_and_request_id_use_shared_middlewares(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream([]))
    reached = 0

    async def handler(_request: Any) -> web.StreamResponse:
        nonlocal reached
        reached += 1
        return web.json_response({"ok": True})

    async def protected(request: Any) -> web.StreamResponse:
        return await app_module._client_auth_middleware(request, handler)

    unauthorized = await app_module._request_id_middleware(
        cast(Any, _FakeRequest(app, [])), protected
    )
    authorized = await app_module._request_id_middleware(
        cast(Any, _FakeRequest(app, [("Authorization", f"Bearer {TOKEN}")])), protected
    )

    assert unauthorized.status == 401
    assert authorized.status == 200
    assert reached == 1
    for response in (unauthorized, authorized):
        assert __import__("re").fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


def test_responses_route_is_registered_and_embeddings_remain_absent(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream([]))
    routes = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }

    assert ("POST", "/v1/responses") in routes
    assert all(path != "/v1/embeddings" for _method, path in routes)
