"""The same catalog generation governs discovery, parsing and effective routing."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from openai.types.responses import Response

from codex_openai_bridge.app import create_app
from codex_openai_bridge.config import Settings
from codex_openai_bridge.model_catalog import CatalogModel
from codex_openai_bridge.upstream import BufferedResponsesUpstream
from tests.contract._support import (
    RecordingUpstream,
    StaticCredentialProvider,
    completed_text_response,
)


@asynccontextmanager
async def _server(
    tmp_path: Path,
    *,
    fail: bool = False,
    streams: list[bytes] | None = None,
    buffered: bool = False,
    initial_rows: list[CatalogModel] | None = None,
) -> AsyncIterator[Any]:
    import codex_openai_bridge.app as app_module

    token = tmp_path / "client"
    key = tmp_path / "continuation"
    token.write_text("c" * 43)
    key.write_text("d" * 43)
    token.chmod(0o600)
    key.chmod(0o600)
    settings = replace(
        Settings.from_env(),
        client_token_file=token,
        continuation_key_file=key,
        model_discovery="codex",
        model_map=MappingProxyType({"codex": "gpt-a"}),
    )
    rows = (
        [CatalogModel("gpt-a", True), CatalogModel("gpt-b", False)]
        if initial_rows is None
        else initial_rows
    )

    async def load() -> list[CatalogModel]:
        if fail:
            raise RuntimeError("synthetic private diagnostic")
        return rows

    provider = StaticCredentialProvider()
    responses: list[object] = [completed_text_response()] * 10
    upstream = RecordingUpstream(responses=responses, streams=streams)
    app_upstream = BufferedResponsesUpstream(upstream, settings) if buffered else upstream
    app = create_app(settings, provider, upstream=app_upstream, catalog_loader=load)
    async with TestClient(
        TestServer(app), headers={"Authorization": "Bearer " + "c" * 43}
    ) as client:
        yield client, upstream, provider, rows, app[app_module._CATALOG_KEY]


@pytest.mark.asyncio
async def test_default_tracks_new_catalog_without_rebinding_fixed_alias(tmp_path: Path) -> None:
    async with _server(tmp_path) as (client, upstream, _provider, rows, registry):
        response = await client.get("/v1/models")
        assert response.status == 200
        assert [row["id"] for row in (await response.json())["data"]] == [
            "codex",
            "codex-a",
            "codex-b",
            "default",
        ]
        for selector in ("default", "codex"):
            result = await client.post(
                "/v1/chat/completions",
                json={
                    "model": selector,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            assert result.status == 200
            assert (await result.json())["model"] == selector
        assert [call["model"] for call in upstream.calls] == ["gpt-a", "gpt-a"]
        rows[:] = [CatalogModel("gpt-a", False), CatalogModel("gpt-b", True)]
        assert await registry.refresh()
        for selector in ("default", "codex"):
            result = await client.post("/v1/responses", json={"model": selector, "input": "hello"})
            assert result.status == 200
        assert [call["model"] for call in upstream.calls] == ["gpt-a", "gpt-a", "gpt-b", "gpt-a"]
        before = len(upstream.calls)
        result = await client.post("/v1/responses", json={"model": "invented", "input": "hello"})
        assert result.status == 400 and len(upstream.calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("cold_start", [False, True])
async def test_discovery_remains_available_without_withdrawn_pinned_codex(
    tmp_path: Path, cold_start: bool
) -> None:
    initial = [CatalogModel("gpt-b", True)] if cold_start else None
    async with _server(tmp_path, initial_rows=initial) as (
        client,
        upstream,
        provider,
        rows,
        registry,
    ):
        if not cold_start:
            before = await client.get("/v1/models")
            assert before.status == 200
            assert "codex" in {item["id"] for item in (await before.json())["data"]}
            rows[:] = [CatalogModel("gpt-b", True)]
            assert await registry.refresh()
        assert (await client.get("/readyz")).status == 200
        listing = await client.get("/v1/models")
        assert listing.status == 200
        assert [item["id"] for item in (await listing.json())["data"]] == ["codex-b", "default"]
        for path, data in (
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]}),
            ("/v1/responses", {"input": "hello"}),
        ):
            response = await client.post(path, json={"model": "default", **data})
            assert response.status == 200
            assert (await response.json())["model"] == "default"
            assert upstream.calls[-1]["model"] == "gpt-b"
            count = len(provider.calls), len(upstream.calls)
            response = await client.post(path, json={"model": "codex", **data})
            assert response.status == 400
            assert (len(provider.calls), len(upstream.calls)) == count


def test_dynamic_discovery_preserves_static_codec_and_duplicate_guards() -> None:
    from codex_openai_bridge.wire import model_list_document

    with pytest.raises(ValueError):
        model_list_document(("codex-b", "default"))
    for invalid in ((), ("default", "default"), ("codex", "codex"), ("default", "")):
        with pytest.raises(ValueError):
            model_list_document(invalid, require_codex=False)


@pytest.mark.asyncio
async def test_failed_catalog_is_not_silently_replaced_by_static_config(tmp_path: Path) -> None:
    async with _server(tmp_path, fail=True) as (client, upstream, provider, _rows, _registry):
        for endpoint in ("/v1/models", "/readyz"):
            response = await client.get(endpoint)
            assert response.status == 503
            assert "synthetic private" not in await response.text()
        before = len(provider.calls)
        response = await client.post("/v1/responses", json={"model": "default", "input": "hello"})
        assert response.status == 503
        assert not upstream.calls and len(provider.calls) == before


def _wire(events: list[dict[str, Any]]) -> bytes:
    from tests.test_responses_streaming import _event

    return b"".join(_event(event) for event in events)


async def _completion(client: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(path, json=body)
    assert response.status == 200, (response.status, await response.text())
    if not body.get("stream"):
        result: dict[str, Any] = await response.json()
        return result
    frames = [
        json.loads(line[6:])
        for line in (await response.text()).splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    if path == "/v1/responses":
        assert frames[-1]["type"] == "response.completed"
        result = frames[-1]["response"]
        return result
    call: dict[str, Any] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    for frame in frames:
        for choice in frame["choices"]:
            for delta in choice["delta"].get("tool_calls", []):
                if delta.get("id"):
                    call["id"] = delta["id"]
                call["function"]["name"] += delta.get("function", {}).get("name", "")
                call["function"]["arguments"] += delta.get("function", {}).get("arguments", "")
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["chat_buffered", "chat_stream", "responses_buffered", "responses_stream"]
)
@pytest.mark.parametrize("fault", ["none", "removed", "tamper", "strip", "alias", "mixed"])
async def test_default_continuations_remain_pinned_and_fail_closed(
    tmp_path: Path,
    surface: str,
    fault: str,
) -> None:
    from codex_openai_bridge.continuation import derive_route_binding_key, encode_continuation_id
    from codex_openai_bridge.route_receipts import RouteReceipt, receipt_scope
    from tests.test_responses_streaming import (
        _reasoning_events,
        _reasoning_function_events,
        _text_events,
    )

    streaming = surface.endswith("stream")
    chat = surface.startswith("chat")
    path = "/v1/chat/completions" if chat else "/v1/responses"
    streams = [
        _wire(_reasoning_function_events()),
        _wire(_reasoning_events(encrypted_content="Yg==")),
        _wire(_text_events()),
    ]
    async with _server(tmp_path, streams=streams, buffered=not streaming) as (
        client,
        upstream,
        provider,
        rows,
        registry,
    ):
        function = {
            "name": "weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
        body: dict[str, Any] = {
            "model": "default",
            "stream": streaming,
            "tools": [
                {"type": "function", "function": function}
                if chat
                else {"type": "function", **function}
            ],
        }
        body["messages" if chat else "input"] = [{"role": "user", "content": "hello"}]
        first = await _completion(client, path, body)
        assert upstream.calls[-1]["model"] == "gpt-a"
        rows[:] = [CatalogModel("gpt-a", False), CatalogModel("gpt-b", True)]
        if fault == "removed":
            rows.pop(0)
        assert await registry.refresh()
        if chat:
            assistant = first["choices"][0]["message"]
            call = assistant["tool_calls"][0]
            result_row = {"role": "tool", "tool_call_id": call["id"], "content": "ok"}
            history = [{"role": "user", "content": "hello"}, assistant, result_row]
            body["messages"] = history
            call_field, result_field = "id", "tool_call_id"
        else:
            output = first["output"]
            call = next(item for item in output if item["type"] == "function_call")
            result_row = {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": "ok",
            }
            body["input"] = [{"role": "user", "content": "hello"}, *output, result_row]
            call_field = result_field = "call_id"
        original = call[call_field]
        assert original.startswith("cobr_t1_")
        if fault == "tamper":
            call[call_field] = original[:-1] + ("A" if original[-1] != "A" else "B")
        elif fault == "strip":
            call[call_field] = original.split(".", 2)[2].rsplit(".", 1)[0]
        elif fault == "alias":
            body["model"] = "codex"
        result_row[result_field] = call[call_field]
        if fault == "mixed":
            key = "d" * 43
            with receipt_scope(RouteReceipt(key, "default", "gpt-b")):
                result_row[result_field] = encode_continuation_id(
                    raw_id="call_stream",
                    public_model="default",
                    kind="chat_call" if chat else "responses_call",
                    binding_key=derive_route_binding_key(
                        key, public_model="default", upstream_model="gpt-b"
                    ),
                )
        if fault != "none":
            before = len(provider.calls)
            response = await client.post(path, json=body)
            assert response.status == 400
            assert len(upstream.calls) == 1 and len(provider.calls) == before
            return
        await _completion(client, path, body)
        assert upstream.calls[-1]["model"] == "gpt-a"
        body["messages" if chat else "input"] = [{"role": "user", "content": "fresh"}]
        await _completion(client, path, body)
        assert upstream.calls[-1]["model"] == "gpt-b"
        assert "cobr_t1_" not in json.dumps([call["input"] for call in upstream.calls])


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_compaction_state_remains_bound_to_its_original_model(
    tmp_path: Path,
    streaming: bool,
) -> None:
    from tests.test_responses_streaming import _compaction_events, _text_events

    async with _server(
        tmp_path,
        streams=[_wire(_compaction_events()), _wire(_text_events())],
        buffered=not streaming,
    ) as (client, upstream, _provider, rows, registry):
        body = {
            "model": "default",
            "input": "hello",
            "stream": streaming,
            "context_management": [{"type": "compaction", "compact_threshold": 1000}],
        }
        first = await _completion(client, "/v1/responses", body)
        rows[:] = [CatalogModel("gpt-a", False), CatalogModel("gpt-b", True)]
        assert await registry.refresh()
        body["input"] = [*first["output"], {"role": "user", "content": "continue"}]
        await _completion(client, "/v1/responses", body)
        assert [call["model"] for call in upstream.calls] == ["gpt-a", "gpt-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_sdk_reasoning_replay_keeps_the_original_route(
    tmp_path: Path, streaming: bool
) -> None:
    from openai import AsyncOpenAI

    from tests.test_responses_streaming import _reasoning_events

    next_wire = (
        _wire(_reasoning_events(encrypted_content="Yg=="))
        .replace(b"rs_stream", b"rs_next")
        .replace(b"msg_stream", b"msg_next")
        .replace(b"resp_stream", b"resp_next")
    )
    async with _server(
        tmp_path,
        streams=[_wire(_reasoning_events()), next_wire],
        buffered=not streaming,
    ) as (http, upstream, _provider, rows, registry):
        async with AsyncOpenAI(
            base_url=str(http.server.make_url("/v1/")),
            api_key="c" * 43,
            max_retries=0,
        ) as sdk:
            first: Response
            second: Response
            if streaming:
                async with sdk.responses.stream(model="default", input="hello") as events:
                    async for _ in events:
                        pass
                    first = await events.get_final_response()
            else:
                first = await sdk.responses.create(model="default", input="hello", stream=False)
            history: list[Any] = [item.model_dump(exclude_none=True) for item in first.output]
            history.append({"role": "user", "content": "continue"})
            rows[:] = [CatalogModel("gpt-a", False), CatalogModel("gpt-b", True)]
            assert await registry.refresh()
            if streaming:
                async with sdk.responses.stream(model="default", input=history) as events:
                    async for _ in events:
                        pass
                    second = await events.get_final_response()
            else:
                second = await sdk.responses.create(model="default", input=history, stream=False)
            assert first.model == second.model == "default" and second.output_text == "hello"
            assert [call["model"] for call in upstream.calls] == ["gpt-a", "gpt-a"]


@pytest.mark.asyncio
async def test_expired_catalog_refuses_discovery_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _server(tmp_path) as (client, upstream, provider, _rows, registry):
        received = registry.current().refreshed_at
        monkeypatch.setattr(registry, "_clock", lambda: received + 1800.0)
        response = await client.get("/v1/models")
        assert response.status == 503
        before = len(provider.calls)
        response = await client.post("/v1/responses", json={"model": "default", "input": "hello"})
        assert response.status == 503 and not upstream.calls and len(provider.calls) == before


@pytest.mark.asyncio
async def test_request_pins_one_snapshot_even_if_credentials_await_crosses_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_openai_bridge.auth import Credential

    async with _server(tmp_path) as (client, upstream, provider, rows, registry):

        async def credentials(*, force_refresh: bool = False) -> Credential:
            provider.calls.append(force_refresh)
            rows[:] = [CatalogModel("gpt-a", False), CatalogModel("gpt-b", True)]
            assert await registry.refresh()
            value = provider.credential
            assert isinstance(value, Credential)
            return value

        monkeypatch.setattr(provider, "get_credentials", credentials)
        for _ in range(2):
            result = await client.post("/v1/responses", json={"model": "default", "input": "hello"})
            assert result.status == 200
        assert [call["model"] for call in upstream.calls] == ["gpt-a", "gpt-b"]
