from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
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
from codex_openai_bridge.continuation import encode_continuation_id, encode_continuation_state
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
    continuation_key_file = tmp_path / "continuation-key"
    continuation_key_file.write_text("b" * 43 + "\n", encoding="ascii")
    continuation_key_file.chmod(0o600)
    return replace(
        Settings.from_env(),
        client_token_file=token_file,
        continuation_key_file=continuation_key_file,
    )


def _credential() -> Credential:
    return Credential(
        access_token="upstream-token",
        base_url="https://chatgpt.com/backend-api/codex",
        account_id="account-1",
        expires_at=4_102_444_800,
    )


def test_direct_reasoning_rejects_same_route_id_state_splicing() -> None:
    binding_key = "k" * 43
    public_id_a = encode_continuation_id(
        raw_id="rs_a",
        public_model="codex-sol",
        kind="responses_reasoning",
        binding_key=binding_key,
    )
    state_b = encode_continuation_state(
        "Yg==",
        public_model="codex-sol",
        kind="responses_reasoning_state",
        binding_key=binding_key,
        max_value_bytes=4096,
        state="rs_b",
    )
    document = {
        "model": "codex-sol",
        "input": [
            {
                "id": public_id_a,
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": state_b,
            }
        ],
    }

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        parse_responses_request(
            document,
            public_model=("codex", "codex-sol"),
            max_items=16,
            max_tools=8,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
            binding_key=binding_key,
            model_scoped=True,
        )


def test_model_scoped_compaction_requires_item_id_for_state_linkage() -> None:
    state = encode_continuation_state(
        "YQ==",
        public_model="codex-sol",
        kind="responses_compaction_state",
        binding_key="k" * 43,
        max_value_bytes=4096,
        state="no-id",
    )

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        parse_responses_request(
            {
                "model": "codex-sol",
                "input": [{"type": "compaction", "encrypted_content": state}],
                "context_management": [{"type": "compaction", "compact_threshold": 1024}],
            },
            public_model=("codex", "codex-sol"),
            max_items=16,
            max_tools=8,
            max_json_depth=16,
            max_json_nodes=256,
            max_string_bytes=4096,
            binding_key="k" * 43,
            model_scoped=True,
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
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    }
                ],
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
async def test_responses_routes_approved_alias_and_returns_only_that_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        _settings(tmp_path),
        model_config_file=tmp_path / "models.toml",
        model_map=MappingProxyType(
            {"codex": "gpt-5.6-terra", "codex-sol": "SENSITIVE_REAL_SOL_MODEL"}
        ),
    )
    credential = _credential()
    upstream_response = _text_response()
    upstream_response["model"] = "SENSITIVE_REAL_SOL_MODEL"
    upstream = FakeUpstream([upstream_response])
    app = create_app(settings, StaticCredentialManager(credential), upstream=upstream)

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex-sol", "input": "hello"}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))
    body = json.loads(response.body)

    assert response.status == 200
    assert upstream.calls[0][1]["model"] == "SENSITIVE_REAL_SOL_MODEL"
    assert body["model"] == "codex-sol"
    assert "SENSITIVE_REAL_SOL_MODEL" not in json.dumps(body)


@pytest.mark.asyncio
async def test_one_entry_model_map_scopes_direct_responses_continuations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        _settings(tmp_path),
        model_config_file=tmp_path / "models.toml",
        model_map=MappingProxyType({"codex": "gpt-5.6-terra"}),
    )
    manager = StaticCredentialManager(_credential())
    tool = {
        "type": "function",
        "name": "lookup",
        "parameters": {"type": "object", "properties": {}},
    }
    first_upstream = _text_response()
    first_upstream["output"] = [
        {
            "id": "fc_one_map",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_one_map",
            "name": "lookup",
            "arguments": "{}",
        }
    ]
    upstream = FakeUpstream([first_upstream])
    app = create_app(settings, manager, upstream=upstream)
    documents: list[dict[str, object]] = [{"model": "codex", "input": "lookup", "tools": [tool]}]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    first = await app_module._responses(request)
    assert isinstance(first.body, (bytes, bytearray))
    public_call = json.loads(first.body)["output"][0]
    assert public_call["call_id"].startswith("cobr_c1_")

    history = [
        public_call,
        {
            "type": "function_call_output",
            "call_id": public_call["call_id"],
            "output": "result",
        },
    ]
    app[app_module._SETTINGS_KEY] = replace(
        settings,
        model_map=MappingProxyType({"codex": "gpt-5.6-terra-remapped"}),
    )
    documents.append({"model": "codex", "input": history, "tools": [tool]})
    remapped = await app_module._responses(request)
    assert remapped.status == 400
    assert manager.calls == [False]
    assert len(upstream.calls) == 1


@pytest.mark.asyncio
async def test_responses_tool_continuation_is_model_alias_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        _settings(tmp_path),
        model_config_file=tmp_path / "models.toml",
        model_map=MappingProxyType({"codex": "gpt-5.6-terra", "codex-sol": "gpt-5.6-sol"}),
    )
    credential = _credential()
    manager = StaticCredentialManager(credential)
    tool = {
        "type": "function",
        "name": "lookup",
        "parameters": {"type": "object", "properties": {}},
    }
    first_upstream = _text_response()
    first_upstream["id"] = "resp_direct_call"
    first_upstream["output"] = [
        {
            "id": "fc_direct_call",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_direct_raw",
            "name": "lookup",
            "arguments": "{}",
        }
    ]
    upstream = FakeUpstream([first_upstream, _text_response("done")])
    app = create_app(settings, manager, upstream=upstream)
    documents: list[dict[str, object]] = [
        {"model": "codex-sol", "input": "lookup", "tools": [tool]}
    ]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    first = await app_module._responses(request)
    assert isinstance(first.body, (bytes, bytearray))
    public_call = json.loads(first.body)["output"][0]
    public_call_id = public_call["call_id"]
    assert public_call_id.startswith("cobr_c1_")
    history = [
        public_call,
        {"type": "function_call_output", "call_id": public_call_id, "output": "result"},
    ]

    documents.append({"model": "codex", "input": history, "tools": [tool]})
    cross_alias = await app_module._responses(request)
    assert cross_alias.status == 400
    assert manager.calls == [False]
    assert len(upstream.calls) == 1

    raw_history = [
        {**public_call, "call_id": "call_direct_raw"},
        {"type": "function_call_output", "call_id": "call_direct_raw", "output": "result"},
    ]
    documents.append({"model": "codex", "input": raw_history, "tools": [tool]})
    raw_legacy = await app_module._responses(request)
    assert raw_legacy.status == 400
    assert manager.calls == [False]
    assert len(upstream.calls) == 1

    app[app_module._SETTINGS_KEY] = replace(
        settings,
        model_map=MappingProxyType({"codex": "gpt-5.6-terra", "codex-sol": "gpt-5.6-sol-remapped"}),
    )
    documents.append({"model": "codex-sol", "input": history, "tools": [tool]})
    remapped_alias = await app_module._responses(request)
    assert remapped_alias.status == 400
    assert manager.calls == [False]
    assert len(upstream.calls) == 1
    app[app_module._SETTINGS_KEY] = settings

    output_item_id_history = [
        public_call,
        {
            "id": "raw_output_item",
            "type": "function_call_output",
            "call_id": public_call_id,
            "output": "result",
        },
    ]
    documents.append({"model": "codex-sol", "input": output_item_id_history, "tools": [tool]})
    unsigned_output_id = await app_module._responses(request)
    assert unsigned_output_id.status == 400
    assert manager.calls == [False]
    assert len(upstream.calls) == 1

    documents.append({"model": "codex-sol", "input": history, "tools": [tool]})
    same_alias = await app_module._responses(request)
    assert same_alias.status == 200
    assert upstream.calls[1][1]["input"][0]["call_id"] == "call_direct_raw"
    assert upstream.calls[1][1]["input"][1]["call_id"] == "call_direct_raw"


@pytest.mark.asyncio
async def test_responses_reasoning_continuation_is_model_alias_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        _settings(tmp_path),
        model_config_file=tmp_path / "models.toml",
        model_map=MappingProxyType({"codex": "gpt-5.6-terra", "codex-sol": "gpt-5.6-sol"}),
    )
    manager = StaticCredentialManager(_credential())
    first_upstream = _text_response("first")
    first_upstream["id"] = "resp_direct_reason"
    first_upstream["output"].insert(
        0,
        {
            "id": "rs_direct_raw",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": "YQ==",
        },
    )
    upstream = FakeUpstream([first_upstream, _text_response("second")])
    app = create_app(settings, manager, upstream=upstream)
    documents: list[dict[str, object]] = [{"model": "codex-sol", "input": "first"}]

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return documents.pop(0)

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    request = cast(Any, SimpleNamespace(app=app))
    first = await app_module._responses(request)
    assert isinstance(first.body, (bytes, bytearray))
    reasoning = json.loads(first.body)["output"][0]
    assert reasoning["id"].startswith("cobr_c1_")
    history = [reasoning, {"role": "user", "content": "continue"}]

    documents.append({"model": "codex", "input": history})
    cross_alias = await app_module._responses(request)
    assert cross_alias.status == 400
    assert len(upstream.calls) == 1

    documents.append({"model": "codex-sol", "input": history})
    same_alias = await app_module._responses(request)
    assert same_alias.status == 200
    assert upstream.calls[1][1]["input"][0] == {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "YQ==",
    }


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
async def test_max_output_tokens_is_rejected_generically_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream([]))

    async def read_document(_request: Any, **_kwargs: Any) -> object:
        return {"model": "codex", "input": "hello", "max_output_tokens": 42}

    monkeypatch.setattr(app_module, "read_json_request", read_document)
    response = await app_module._responses(cast(Any, SimpleNamespace(app=app)))
    assert isinstance(response.body, (bytes, bytearray))

    assert response.status == 400
    assert json.loads(response.body) == {
        "error": {
            "message": "Request is invalid",
            "type": "invalid_request_error",
            "param": None,
            "code": "invalid_request",
        }
    }


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


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_reasoning_effort_accepts_only_confirmed_values_and_is_preserved(effort: str) -> None:
    parsed = _parse_request({"model": "codex", "input": "hello", "reasoning": {"effort": effort}})

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["reasoning"] == {"effort": effort}
    for rejected in ("minimal", "", "MAX", "unknown", True, None):
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": "hello", "reasoning": {"effort": rejected}})


@pytest.mark.parametrize("summary", ["auto", "concise", "detailed"])
def test_reasoning_summary_accepts_only_confirmed_values_and_is_preserved(summary: str) -> None:
    parsed = _parse_request({"model": "codex", "input": "hello", "reasoning": {"summary": summary}})

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["reasoning"] == {"summary": summary}
    for rejected in ("none", "", "DETAILED", "unknown", True, None):
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": "hello", "reasoning": {"summary": rejected}})


def test_prompt_cache_key_is_a_bounded_exact_string_and_is_preserved() -> None:
    parsed = _parse_request({"model": "codex", "input": "hello", "prompt_cache_key": "cache-東京"})

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["prompt_cache_key"] == "cache-東京"
    rejected_cache_keys: tuple[object, ...] = (True, 1, None, [], {})
    for rejected in rejected_cache_keys:
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": "hello", "prompt_cache_key": rejected})
    sensitive_cache_key = "SENSITIVE_CACHE_KEY_" + "x" * 4096
    with pytest.raises(ResponsesRequestError) as caught:
        _parse_request(
            {"model": "codex", "input": "hello", "prompt_cache_key": sensitive_cache_key}
        )
    assert caught.value.args == ("invalid request",)
    assert "SENSITIVE" not in repr(caught.value)


@pytest.mark.parametrize("include_obfuscation", [False, True])
def test_stream_options_accepts_only_exact_include_obfuscation_boolean(
    include_obfuscation: bool,
) -> None:
    parsed = _parse_request(
        {
            "model": "codex",
            "input": "hello",
            "stream": True,
            "stream_options": {"include_obfuscation": include_obfuscation},
        }
    )

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["stream_options"] == {"include_obfuscation": include_obfuscation}
    rejected_stream_options: tuple[object, ...] = (
        {},
        {"include_obfuscation": 1},
        {"include_obfuscation": None},
        {"include_obfuscation": True, "include_usage": True},
        True,
        None,
    )
    for rejected in rejected_stream_options:
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request(
                {
                    "model": "codex",
                    "input": "hello",
                    "stream": True,
                    "stream_options": rejected,
                }
            )


@pytest.mark.parametrize("stream", [None, False])
def test_stream_options_requires_stream_true(stream: bool | None) -> None:
    document: dict[str, object] = {
        "model": "codex",
        "input": "hello",
        "stream_options": {"include_obfuscation": True},
    }
    if stream is not None:
        document["stream"] = stream

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(document)


@pytest.mark.parametrize("verbosity", ["low", "medium", "high"])
def test_text_verbosity_accepts_only_confirmed_values_and_is_preserved(verbosity: str) -> None:
    parsed = _parse_request({"model": "codex", "input": "hello", "text": {"verbosity": verbosity}})

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["text"] == {"verbosity": verbosity}
    for rejected in ("minimal", "", "HIGH", "unknown", True, None):
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": "hello", "text": {"verbosity": rejected}})


def test_service_tier_accepts_only_default_and_is_preserved() -> None:
    parsed = _parse_request({"model": "codex", "input": "hello", "service_tier": "default"})

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["service_tier"] == "default"
    for rejected in ("auto", "flex", "priority", "", "DEFAULT", True, None):
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": "hello", "service_tier": rejected})


def test_context_management_accepts_one_bounded_compaction_entry_and_forwards_exactly() -> None:
    for threshold in (1, 1024, 1_000_000):
        context_management = [{"type": "compaction", "compact_threshold": threshold}]
        parsed = _parse_request(
            {
                "model": "codex",
                "input": "hello",
                "context_management": context_management,
            }
        )

        payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

        assert payload["context_management"] == context_management
        assert payload["model"] == "gpt-upstream"
        assert payload["store"] is False
        assert payload["stream"] is False
        assert payload["include"] == ["reasoning.encrypted_content"]

    rejected_context_management: tuple[object, ...] = (
        None,
        True,
        {},
        [],
        [{"type": "compaction", "compact_threshold": True}],
        [{"type": "compaction", "compact_threshold": 0}],
        [{"type": "compaction", "compact_threshold": -1}],
        [{"type": "compaction", "compact_threshold": 1_000_001}],
        [{"type": "compaction", "compact_threshold": "1024"}],
        [{"type": "other", "compact_threshold": 1024}],
        [{"type": "compaction"}],
        [{"type": "compaction", "compact_threshold": 1024, "secret": "SENSITIVE"}],
        [
            {"type": "compaction", "compact_threshold": 1024},
            {"type": "compaction", "compact_threshold": 2048},
        ],
    )
    for rejected in rejected_context_management:
        with pytest.raises(ResponsesRequestError) as caught:
            _parse_request({"model": "codex", "input": "hello", "context_management": rejected})
        assert caught.value.args == ("invalid request",)
        assert "SENSITIVE" not in repr(caught.value)


def test_developer_input_text_is_preserved_separately_from_top_level_instructions() -> None:
    parsed = _parse_request(
        {
            "model": "codex",
            "instructions": "top-level policy",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "developer policy"}],
                },
                {"role": "user", "content": "question"},
            ],
        }
    )

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["instructions"] == "top-level policy"
    assert payload["input"] == [
        {
            "role": "developer",
            "content": [{"type": "input_text", "text": "developer policy"}],
        },
        {"role": "user", "content": [{"type": "input_text", "text": "question"}]},
    ]
    for rejected in (
        {"role": "system", "content": [{"type": "input_text", "text": "policy"}]},
        {"role": "developer", "content": "policy"},
        {"role": "developer", "content": [{"type": "output_text", "text": "policy"}]},
    ):
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": [rejected]})


@pytest.mark.parametrize("status", ["completed", "in_progress", "incomplete"])
def test_assistant_message_status_accepts_only_confirmed_values_and_is_preserved(
    status: str,
) -> None:
    message = {
        "id": "msg_history",
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": "prior answer", "annotations": []}],
    }
    parsed = _parse_request(
        {"model": "codex", "input": [message, {"role": "user", "content": "continue"}]}
    )

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["input"][0] == {
        "role": "assistant",
        "status": status,
        "content": [{"type": "output_text", "text": "prior answer"}],
    }
    for rejected in ("failed", "queued", "", "COMPLETED", True, None):
        invalid = {**message, "status": rejected}
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": [invalid]})


def test_assistant_message_accepts_live_shape_without_id_or_annotations() -> None:
    parsed = _parse_request(
        {
            "model": "codex",
            "input": [
                {
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "prior note"}],
                },
                {"role": "user", "content": "continue"},
            ],
        }
    )

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["input"][0] == {
        "role": "assistant",
        "status": "completed",
        "phase": "commentary",
        "content": [{"type": "output_text", "text": "prior note"}],
    }


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_assistant_message_phase_accepts_only_confirmed_values_and_is_preserved(
    phase: str,
) -> None:
    message = {
        "id": "msg_history",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "phase": phase,
        "content": [{"type": "output_text", "text": "prior answer", "annotations": []}],
    }
    parsed = _parse_request({"model": "codex", "input": [message]})

    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["input"][0] == {
        "role": "assistant",
        "status": "completed",
        "phase": phase,
        "content": [{"type": "output_text", "text": "prior answer"}],
    }
    for rejected in ("analysis", "final", "", "COMMENTARY", True, None):
        invalid = {**message, "phase": rejected}
        with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
            _parse_request({"model": "codex", "input": [invalid]})


def test_request_options_reuse_closed_chat_contracts_and_reconstruct_exact_policy() -> None:
    schema = {
        "title": "AnswerResult",
        "type": "object",
        "properties": {"answer": {"title": "Answer", "type": "string"}},
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
                "name": "Answer",
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


def test_custom_tool_request_forwards_exact_confirmed_named_policy() -> None:
    document = {
        "model": "codex",
        "input": "emit a probe",
        "tools": [
            {
                "type": "custom",
                "name": "emit_probe",
                "description": "Emit the supplied probe text",
            }
        ],
        "tool_choice": {"type": "custom", "name": "emit_probe"},
        "parallel_tool_calls": False,
    }

    parsed = _parse_request(document)
    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload == {
        "model": "gpt-upstream",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "emit a probe"}],
            }
        ],
        "store": False,
        "stream": False,
        "include": ["reasoning.encrypted_content"],
        "tools": [
            {
                "type": "custom",
                "name": "emit_probe",
                "description": "Emit the supplied probe text",
            }
        ],
        "tool_choice": {"type": "custom", "name": "emit_probe"},
        "parallel_tool_calls": False,
    }


def test_custom_tool_description_is_optional_without_broadening_its_shape() -> None:
    policy = _custom_tool_policy()
    policy["tools"] = [{"type": "custom", "name": "emit_probe"}]

    parsed = _parse_request({"model": "codex", "input": "hello", **policy})
    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert payload["tools"] == [{"type": "custom", "name": "emit_probe"}]


def _custom_tool_policy(name: str = "emit_probe") -> dict[str, object]:
    return {
        "tools": [{"type": "custom", "name": name, "description": "Emit probe text"}],
        "tool_choice": {"type": "custom", "name": name},
        "parallel_tool_calls": False,
    }


def test_custom_tool_output_continuation_forwards_none_choice_and_accepts_message() -> None:
    policy = _custom_tool_policy()
    policy["tool_choice"] = "none"
    document = {
        "model": "codex",
        "input": [
            {
                "id": "ctc_history",
                "type": "custom_tool_call",
                "call_id": "call_history",
                "name": "emit_probe",
                "input": "probe text",
            },
            {
                "id": "ctco_history",
                "type": "custom_tool_call_output",
                "call_id": "call_history",
                "output": "probe result",
            },
        ],
        **policy,
    }

    parsed = _parse_request(document)
    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]
    public = _public_response_for(document, _text_response())

    assert payload["input"] == [
        {
            "type": "custom_tool_call",
            "call_id": "call_history",
            "name": "emit_probe",
            "input": "probe text",
        },
        {
            "type": "custom_tool_call_output",
            "call_id": "call_history",
            "output": "probe result",
        },
    ]
    assert payload["tool_choice"] == "none"
    assert public["tool_choice"] == "none"
    assert public["output"][0]["type"] == "message"


@pytest.mark.parametrize(
    "policy",
    [
        {"tools": [{"type": "custom", "name": "emit_probe"}]},
        {
            "tools": [{"type": "custom", "name": "emit_probe"}],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
        {
            "tools": [{"type": "custom", "name": "emit_probe"}],
            "tool_choice": {"type": "custom", "name": "other"},
            "parallel_tool_calls": False,
        },
        {
            "tools": [{"type": "custom", "name": "emit_probe"}],
            "tool_choice": {"type": "custom", "name": "emit_probe"},
        },
        {
            "tools": [{"type": "custom", "name": "emit_probe"}],
            "tool_choice": {"type": "custom", "name": "emit_probe"},
            "parallel_tool_calls": True,
        },
        {
            **_custom_tool_policy(),
            "tools": [
                {"type": "custom", "name": "emit_probe"},
                {"type": "custom", "name": "other"},
            ],
        },
        {
            **_custom_tool_policy(),
            "tools": [
                {"type": "custom", "name": "emit_probe"},
                {
                    "type": "function",
                    "name": "allowed",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        },
        {
            **_custom_tool_policy(),
            "tools": [{"type": "custom", "name": "emit_probe", "format": {}}],
        },
        {
            **_custom_tool_policy(),
            "tools": [{"type": "custom", "name": "emit_probe", "allowed_callers": ["code"]}],
        },
        {
            **_custom_tool_policy(),
            "tools": [{"type": "custom", "name": "emit_probe", "defer_loading": False}],
        },
    ],
)
def test_custom_tool_declaration_rejects_authority_expansion(
    policy: dict[str, object],
) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request({"model": "codex", "input": "hello", **policy})


@pytest.mark.parametrize(
    "items",
    [
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            }
        ],
        [
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            }
        ],
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "other",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            },
        ],
        [
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            },
        ],
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "status": "completed",
                "call_id": "call_one",
                "output": "result",
            },
        ],
        [
            {
                "id": "duplicate",
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "id": "duplicate",
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            },
        ],
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call",
                "call_id": "call_two",
                "name": "emit_probe",
                "input": "probe two",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_two",
                "output": "result two",
            },
        ],
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_two",
                "output": "result",
            },
        ],
        [
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            },
            {"role": "user", "content": "continue"},
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe again",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "second result",
            },
        ],
        [
            {
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
            {
                "type": "custom_tool_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_one",
                "output": "result",
            },
        ],
        [
            {
                "type": "function_call",
                "call_id": "call_one",
                "name": "emit_probe",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_one", "output": "result"},
        ],
    ],
)
def test_custom_tool_replay_rejects_malformed_or_ambiguous_pairing(items: object) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(
            {
                "model": "codex",
                "input": items,
                **_custom_tool_policy(),
            }
        )


def test_custom_tool_replay_requires_its_exact_declaration() -> None:
    items = [
        {
            "type": "custom_tool_call",
            "call_id": "call_one",
            "name": "emit_probe",
            "input": "probe",
        },
        {"type": "custom_tool_call_output", "call_id": "call_one", "output": "result"},
    ]

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request({"model": "codex", "input": items})


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
                    "summary": [{"type": "summary_text", "text": "safe summary"}],
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
    assert payload["input"][0] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "safe summary"}],
        "encrypted_content": data,
    }


def test_compaction_checkpoint_replay_is_exact_bounded_and_shares_digest_authority() -> None:
    context_management = [{"type": "compaction", "compact_threshold": 1024}]
    for item, expected in (
        (
            {"type": "compaction", "encrypted_content": "YQ=="},
            {"type": "compaction", "encrypted_content": "YQ=="},
        ),
        (
            {"type": "compaction", "id": None, "encrypted_content": "YQ=="},
            {"type": "compaction", "encrypted_content": "YQ=="},
        ),
        (
            {"type": "compaction", "id": "cmp_history", "encrypted_content": "YQ=="},
            {
                "type": "compaction",
                "id": "cmp_history",
                "encrypted_content": "YQ==",
            },
        ),
    ):
        parsed = _parse_request(
            {
                "model": "codex",
                "input": [item, {"role": "user", "content": "continue"}],
                "context_management": context_management,
            }
        )

        payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

        assert payload["input"][0] == expected
        assert payload["context_management"] == context_management

    malformed_items: tuple[object, ...] = (
        {"type": "compaction"},
        {"type": "compaction", "encrypted_content": None},
        {"type": "compaction", "encrypted_content": "AB"},
        {"type": "compaction", "encrypted_content": "YQ==", "id": True},
        {"type": "compaction", "encrypted_content": "YQ==", "id": ""},
        {"type": "compaction", "encrypted_content": "YQ==", "id": "x" * 129},
        {
            "type": "compaction",
            "encrypted_content": "YQ==",
            "plaintext": "SENSITIVE_CHECKPOINT",
        },
    )
    for malformed in malformed_items:
        with pytest.raises(ResponsesRequestError) as caught:
            _parse_request(
                {
                    "model": "codex",
                    "input": [malformed],
                    "context_management": context_management,
                }
            )
        assert caught.value.args == ("invalid request",)
        assert "SENSITIVE" not in repr(caught.value)

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(
            {
                "model": "codex",
                "input": [{"type": "compaction", "encrypted_content": "YQ=="}],
            }
        )
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(
            {
                "model": "codex",
                "input": [
                    {
                        "id": "rs_history",
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [],
                        "encrypted_content": "++8=",
                    },
                    {"type": "compaction", "encrypted_content": "--8"},
                ],
                "context_management": context_management,
            }
        )
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(
            {
                "model": "codex",
                "input": [
                    {
                        "id": "msg_history",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "answer", "annotations": []}],
                    },
                    {"type": "compaction", "encrypted_content": "YQ=="},
                ],
                "context_management": context_management,
            }
        )


def test_compaction_replay_accepts_live_checkpoint_message_checkpoint_sequence() -> None:
    context_management = [{"type": "compaction", "compact_threshold": 1024}]
    document = {
        "model": "codex",
        "input": [
            {"type": "compaction", "id": "cmp_before", "encrypted_content": "YQ=="},
            {
                "id": "msg_history",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
            {"type": "compaction", "id": "cmp_after", "encrypted_content": "Yg=="},
            {"role": "user", "content": "continue"},
        ],
        "context_management": context_management,
    }

    parsed = _parse_request(document)
    payload = responses_request_to_upstream(parsed, upstream_model="gpt-upstream")  # type: ignore[arg-type]

    assert [item.get("type", "message") for item in payload["input"]] == [
        "compaction",
        "message",
        "compaction",
        "message",
    ]
    assert payload["context_management"] == context_management

    consecutive = {**document, "input": [document["input"][0], document["input"][2]]}
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(consecutive)


@pytest.mark.parametrize(
    ("status", "phase"),
    [("in_progress", "final_answer"), ("completed", "commentary")],
)
def test_compaction_replay_requires_completed_final_answer_before_closing_checkpoint(
    status: str,
    phase: str,
) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(
            {
                "model": "codex",
                "input": [
                    {"type": "compaction", "id": "cmp_before", "encrypted_content": "YQ=="},
                    {
                        "id": "msg_history",
                        "type": "message",
                        "status": status,
                        "role": "assistant",
                        "phase": phase,
                        "content": [{"type": "output_text", "text": "answer", "annotations": []}],
                    },
                    {"type": "compaction", "id": "cmp_after", "encrypted_content": "Yg=="},
                    {"role": "user", "content": "continue"},
                ],
                "context_management": [{"type": "compaction", "compact_threshold": 1024}],
            }
        )


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
        {"model": "codex", "input": "x", "truncation": "auto"},
        {"model": "codex", "input": "x", "reasoning": {}},
        {"model": "codex", "input": "x", "reasoning": {"effort": "minimal"}},
        {
            "model": "codex",
            "input": "x",
            "reasoning": {"effort": "low", "unknown": "SENSITIVE"},
        },
        {
            "model": "codex",
            "input": "x",
            "tools": [{"type": "web_search_preview"}],
        },
        {"model": "codex", "input": "x", "text": {"format": {"type": "text"}}},
        {"model": "codex", "input": "x", "text": {}},
        {
            "model": "codex",
            "input": "x",
            "text": {"verbosity": "low", "unknown": "SENSITIVE"},
        },
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


@pytest.mark.parametrize(
    ("tools", "tool_choice", "parallel_tool_calls"),
    [
        (
            [
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object", "additionalProperties": False},
                    "strict": True,
                }
            ],
            "auto",
            True,
        ),
        (
            [{"type": "custom", "name": "emit_probe"}],
            {"type": "custom", "name": "emit_probe"},
            False,
        ),
    ],
)
def test_context_management_rejects_unproven_tool_combinations(
    tools: list[dict[str, object]],
    tool_choice: object,
    parallel_tool_calls: bool,
) -> None:
    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request(
            {
                "model": "codex",
                "input": "hello",
                "tools": tools,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "context_management": [{"type": "compaction", "compact_threshold": 1024}],
            }
        )


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


def test_live_proven_sequential_function_rounds_are_forwarded_in_order() -> None:
    document = {
        "model": "codex",
        "input": [
            {"role": "user", "content": "Run both stages."},
            {
                "id": "rs_first",
                "type": "reasoning",
                "status": "completed",
                "summary": [],
                "encrypted_content": "YQ==",
            },
            {
                "id": "fc_first",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_first",
                "name": "first_probe",
                "arguments": '{"value":"first"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_first",
                "output": '{"accepted":true,"stage":1}',
            },
            {
                "id": "rs_second",
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "Second stage"}],
                "encrypted_content": "Yg==",
            },
            {
                "id": "fc_second",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_second",
                "name": "second_probe",
                "arguments": '{"value":"second"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_second",
                "output": '{"accepted":true,"stage":2}',
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
            for name in ("first_probe", "second_probe")
        ],
        "parallel_tool_calls": False,
    }

    request = cast(Any, _parse_request(document))
    payload = responses_request_to_upstream(request, upstream_model="private-model")

    assert payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Run both stages."}]},
        {"type": "reasoning", "summary": [], "encrypted_content": "YQ=="},
        {
            "type": "function_call",
            "call_id": "call_first",
            "name": "first_probe",
            "arguments": '{"value":"first"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_first",
            "output": '{"accepted":true,"stage":1}',
        },
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "Second stage"}],
            "encrypted_content": "Yg==",
        },
        {
            "type": "function_call",
            "call_id": "call_second",
            "name": "second_probe",
            "arguments": '{"value":"second"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_second",
            "output": '{"accepted":true,"stage":2}',
        },
    ]
    assert request.historical_call_ids == frozenset({"call_first", "call_second"})


@pytest.mark.parametrize(
    "next_item",
    [
        {
            "type": "function_call",
            "call_id": "call_next_round",
            "name": "allowed",
            "arguments": "{}",
        },
        {
            "id": "rs_next_round",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": "YQ==",
        },
    ],
)
def test_next_model_round_waits_for_every_parallel_output(next_item: dict[str, Any]) -> None:
    items = [
        {
            "type": "function_call",
            "call_id": "call_parallel_one",
            "name": "allowed",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "call_id": "call_parallel_two",
            "name": "allowed",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_parallel_one",
            "output": "done",
        },
        next_item,
    ]

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _parse_request({"model": "codex", "input": items})


@pytest.mark.asyncio
async def test_two_request_output_history_round_trip_replays_summary_and_ciphertext(
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
        {
            "model": "codex",
            "input": "Weather?",
            "tools": [tool],
            "reasoning": {"summary": "auto"},
        },
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
        "summary": [{"type": "summary_text", "text": "PRIVATE REASONING SUMMARY"}],
        "encrypted_content": "YQ==",
    }
    assert second_body["model"] == "codex"
    assert manager.calls == [False, False]
    # Responses IDs satisfy the SDK schema but carry no bridge authenticity and are stripped
    # before replay. The validated summary and canonical ciphertext are forwarded without
    # decryption or interpretation.
    assert upstream.calls[1][1]["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "Weather?"}]},
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "PRIVATE REASONING SUMMARY"}],
            "encrypted_content": "YQ==",
        },
        {
            "role": "assistant",
            "status": "completed",
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


def test_nonstream_rejects_unsolicited_reasoning_summary() -> None:
    upstream = _text_response("done")
    upstream["output"].insert(
        0,
        {
            "id": "rs_unsolicited",
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": "UNSOLICITED SUMMARY"}],
            "encrypted_content": "YQ==",
        },
    )

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$") as caught:
        _public_response(upstream)

    assert "UNSOLICITED SUMMARY" not in repr(caught.value)


def test_nonstream_compaction_output_is_exact_bounded_and_fail_closed() -> None:
    context_management = [{"type": "compaction", "compact_threshold": 1024}]
    document = {
        "model": "codex",
        "input": "hello",
        "context_management": context_management,
    }
    checkpoint = {
        "id": "cmp_public_contract",
        "type": "compaction",
        "encrypted_content": "YQ==",
    }
    value = _text_response()
    value["output"] = [checkpoint]

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, value)
    ordinary = _public_response(_text_response())
    assert Response.model_validate(ordinary).output[0].type == "message"

    malformed_checkpoints: tuple[object, ...] = (
        {"type": "compaction", "encrypted_content": "YQ=="},
        {"id": None, "type": "compaction", "encrypted_content": "YQ=="},
        {"id": "", "type": "compaction", "encrypted_content": "YQ=="},
        {"id": "x" * 129, "type": "compaction", "encrypted_content": "YQ=="},
        {"id": "cmp_bad", "type": "compaction", "encrypted_content": None},
        {"id": "cmp_bad", "type": "compaction", "encrypted_content": "AB"},
        {
            "id": "cmp_bad",
            "type": "compaction",
            "encrypted_content": "YQ==",
            "plaintext": "SENSITIVE_CHECKPOINT",
        },
    )
    for malformed in malformed_checkpoints:
        invalid = _text_response()
        invalid["output"].insert(0, malformed)
        with pytest.raises(UpstreamResponseError) as caught:
            _public_response_for(document, invalid)
        assert caught.value.args == ("invalid upstream response",)
        assert "SENSITIVE" not in repr(caught.value)

    without_opt_in = _text_response()
    without_opt_in["output"].insert(0, checkpoint)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response(without_opt_in)

    duplicate_id = _text_response()
    duplicate_id["output"].insert(0, {**checkpoint, "id": "msg_public_contract"})
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, duplicate_id)

    historical_document = {
        "model": "codex",
        "input": [
            {"type": "compaction", "id": "cmp_history", "encrypted_content": "++8="},
            {"role": "user", "content": "continue"},
        ],
        "context_management": context_management,
    }
    duplicate_digest = _text_response()
    duplicate_digest["output"].insert(
        0,
        {"id": "cmp_new", "type": "compaction", "encrypted_content": "--8"},
    )
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(historical_document, duplicate_digest)

    late = _text_response()
    late["output"].append(checkpoint)
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, late)


def test_nonstream_compaction_output_accepts_live_checkpoint_message_checkpoint_sequence() -> None:
    document = {
        "model": "codex",
        "input": "hello",
        "context_management": [{"type": "compaction", "compact_threshold": 1024}],
    }
    checkpoints = [
        {"id": "cmp_one", "type": "compaction", "encrypted_content": "YQ=="},
        {"id": "cmp_two", "type": "compaction", "encrypted_content": "Yg=="},
    ]
    mixed = _text_response()
    mixed["output"][0]["phase"] = "final_answer"
    mixed["output"] = [checkpoints[0], mixed["output"][0], checkpoints[1]]

    public = _public_response_for(document, mixed)

    assert [item["type"] for item in public["output"]] == [
        "compaction",
        "message",
        "compaction",
    ]
    assert public["output"][1]["phase"] == "final_answer"

    consecutive = _text_response()
    consecutive["output"] = checkpoints
    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, consecutive)

    for truncated in ([mixed["output"][0]], mixed["output"][:2]):
        invalid = _text_response()
        invalid["output"] = truncated
        with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
            _public_response_for(document, invalid)


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


def _custom_item(
    *,
    item_id: object = "ctc_one",
    call_id: object = "call_one",
    name: object = "emit_probe",
    status: object = "completed",
    tool_input: object = "probe text",
) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "custom_tool_call",
        "status": status,
        "call_id": call_id,
        "name": name,
        "input": tool_input,
    }


def test_nonstream_custom_tool_call_projects_sdk_common_public_shape() -> None:
    document = {"model": "codex", "input": "hello", **_custom_tool_policy()}
    value = _text_response()
    value["output"] = [_custom_item()]

    public = _public_response_for(document, value)

    assert public["output"] == [
        {
            "id": "ctc_one",
            "type": "custom_tool_call",
            "call_id": "call_one",
            "name": "emit_probe",
            "input": "probe text",
        }
    ]
    assert public["tools"] == [
        {"type": "custom", "name": "emit_probe", "description": "Emit probe text"}
    ]
    assert public["tool_choice"] == {"type": "custom", "name": "emit_probe"}
    assert public["parallel_tool_calls"] is False
    parsed = Response.model_validate(public)
    assert parsed.output[0].type == "custom_tool_call"


def test_nonstream_reasoning_may_precede_the_single_custom_tool_call() -> None:
    document = {"model": "codex", "input": "hello", **_custom_tool_policy()}
    value = _text_response()
    value["output"] = [
        {
            "id": "rs_custom",
            "type": "reasoning",
            "status": "completed",
            "summary": [],
            "encrypted_content": "YQ==",
        },
        _custom_item(),
    ]

    public = _public_response_for(document, value)

    assert [item["type"] for item in public["output"]] == ["reasoning", "custom_tool_call"]


def test_custom_tool_output_replay_requires_none_choice_for_normal_message() -> None:
    document = {
        "model": "codex",
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "call_history",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_history",
                "output": "result",
            },
        ],
        **_custom_tool_policy(),
    }

    with pytest.raises(ResponsesRequestError, match=r"^invalid request$"):
        _public_response_for(document, _text_response("finished"))


def test_earlier_custom_output_does_not_relax_a_later_named_custom_request() -> None:
    document = {
        "model": "codex",
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "call_history",
                "name": "emit_probe",
                "input": "probe",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_history",
                "output": "result",
            },
            {"role": "user", "content": "run it again"},
        ],
        **_custom_tool_policy(),
    }

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, _text_response("should have called the tool"))


@pytest.mark.parametrize(
    "document,output",
    [
        ({"model": "codex", "input": "hello"}, [_custom_item()]),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(name="other")],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(item_id=None)],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(call_id=None)],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(status="in_progress")],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [{key: value for key, value in _custom_item().items() if key != "status"}],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [{**_custom_item(), "unexpected": "SENSITIVE"}],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(tool_input=None)],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(), _custom_item(item_id="ctc_two", call_id="call_two")],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(), *_text_response()["output"]],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            [_custom_item(), _function_item(item_id="fc_two", call_id="call_two")],
        ),
        (
            {"model": "codex", "input": "hello", **_custom_tool_policy()},
            _text_response()["output"],
        ),
    ],
)
def test_nonstream_custom_output_enforces_declared_request_policy(
    document: object,
    output: list[object],
) -> None:
    value = _text_response()
    value["output"] = output

    with pytest.raises(UpstreamResponseError, match=r"^invalid upstream response$"):
        _public_response_for(document, value)


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
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
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


def test_responses_and_explicitly_unsupported_embeddings_routes_are_registered(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), NeverCredentialManager(), upstream=FakeUpstream([]))
    routes = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }

    assert ("POST", "/v1/responses") in routes
    assert ("POST", "/v1/embeddings") in routes
