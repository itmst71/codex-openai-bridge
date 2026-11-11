"""Family selectors are explicit policies, not inferred model capability rankings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codex_openai_bridge.model_catalog import CatalogModel, build_catalog


def test_family_selectors_choose_numeric_latest_independently_of_default() -> None:
    rows = [
        CatalogModel("gpt-6.9-sol", True),
        CatalogModel("gpt-6.10-sol", False),
        CatalogModel("gpt-10-astra", False),
        CatalogModel("gpt-9-astra", False),
        CatalogModel("gpt-6-luna", False),
        CatalogModel("gpt-5.6-luna", False),
        CatalogModel("gpt-99-sol", False, hidden=True),
        CatalogModel("gpt-100-sol-preview", False),
    ]
    snapshot = build_catalog(rows, overrides={"codex": "gpt-6.9-sol"}, refreshed_at=10.0)
    assert snapshot.routes["default"] == "gpt-6.9-sol"
    assert snapshot.routes["standard"] == "gpt-6.10-sol"
    assert snapshot.routes["advanced"] == "gpt-10-astra"
    assert snapshot.routes["fast"] == "gpt-6-luna"
    assert snapshot.routes["codex"] == "gpt-6.9-sol"
    assert "codex-6-9-sol" in snapshot.routes
    assert "codex-100-sol-preview" in snapshot.routes
    assert "gpt-99-sol" not in snapshot.models
    reverse = build_catalog(list(reversed(rows)), overrides={}, refreshed_at=10.0)
    for alias in ("default", "standard", "advanced", "fast"):
        assert reverse.routes[alias] == snapshot.routes[alias]


@pytest.mark.parametrize("family", ["sol", "astra", "luna"])
def test_equal_latest_numeric_versions_are_ambiguous(family: str) -> None:
    from codex_openai_bridge.model_catalog import CatalogUnavailable

    rows = [CatalogModel(f"gpt-6.1-{family}", True), CatalogModel(f"gpt-6.1.0-{family}", False)]
    with pytest.raises(CatalogUnavailable):
        build_catalog(rows, overrides={}, refreshed_at=10.0)


@pytest.mark.parametrize("selector", ["default", "standard", "advanced", "fast"])
def test_floating_aliases_are_reserved_even_for_same_model(selector: str) -> None:
    from codex_openai_bridge.model_catalog import CatalogUnavailable

    with pytest.raises(CatalogUnavailable):
        build_catalog(
            [CatalogModel("gpt-6-sol", True)],
            overrides={selector: "gpt-6-sol"},
            refreshed_at=10.0,
        )


@pytest.mark.parametrize("selector", ["default", "standard", "advanced", "fast"])
def test_reservation_is_dynamic_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    from codex_openai_bridge.config import ConfigError, Settings

    config = tmp_path / "models.toml"
    config.write_text(f'version = 1\n[models]\ncodex = "gpt-6-sol"\n{selector} = "gpt-6-sol"\n')
    config.chmod(0o600)
    monkeypatch.setenv("CODEX_BRIDGE_MODEL_CONFIG_FILE", str(config))
    assert Settings.from_env().model_map[selector] == "gpt-6-sol"
    monkeypatch.setenv("CODEX_BRIDGE_MODEL_DISCOVERY", "codex")
    with pytest.raises(ConfigError, match="reserved"):
        Settings.from_env()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,family", [("standard", "sol"), ("advanced", "astra"), ("fast", "luna")]
)
@pytest.mark.parametrize(
    "surface", ["chat_buffered", "chat_stream", "responses_buffered", "responses_stream"]
)
@pytest.mark.parametrize(
    "fault", ["none", "removed", "tamper", "strip", "cross_alias", "mixed", "inner"]
)
async def test_family_chains_pin_original_route_and_keep_all_signature_checks(
    tmp_path: Path, selector: str, family: str, surface: str, fault: str
) -> None:
    from codex_openai_bridge.continuation import derive_route_binding_key, encode_continuation_id
    from codex_openai_bridge.route_receipts import RouteReceipt, receipt_scope, wrap_route_token
    from tests.test_default_routes import _completion, _server, _wire
    from tests.test_responses_streaming import (
        _reasoning_events,
        _reasoning_function_events,
        _text_events,
    )

    old, new = f"gpt-6-{family}", f"gpt-7-{family}"
    streaming = surface.endswith("stream")
    chat = surface.startswith("chat")
    path = "/v1/chat/completions" if chat else "/v1/responses"
    streams = [
        _wire(_reasoning_function_events()),
        _wire(_reasoning_events(encrypted_content="Yg==")),
        _wire(_text_events()),
    ]
    async with _server(
        tmp_path, initial_rows=[CatalogModel(old, True)], streams=streams, buffered=not streaming
    ) as (client, upstream, provider, rows, registry):
        function = {
            "name": "weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
        body = {
            "model": selector,
            "stream": streaming,
            "tools": [
                {"type": "function", "function": function}
                if chat
                else {"type": "function", **function}
            ],
            "messages" if chat else "input": [{"role": "user", "content": "hello"}],
        }
        first = await _completion(client, path, body)
        assert upstream.calls[-1]["model"] == old
        if chat:
            assistant = first["choices"][0]["message"]
            call = assistant["tool_calls"][0]
            result = {"role": "tool", "tool_call_id": call["id"], "content": "ok"}
            body["messages"] = [{"role": "user", "content": "hello"}, assistant, result]
            call_field, result_field = "id", "tool_call_id"
        else:
            call = next(item for item in first["output"] if item["type"] == "function_call")
            result = {"type": "function_call_output", "call_id": call["call_id"], "output": "ok"}
            body["input"] = [{"role": "user", "content": "hello"}, *first["output"], result]
            call_field = result_field = "call_id"
        original = call[call_field]
        assert original.startswith("cobr_t1_")
        rows[:] = [CatalogModel(old, False), CatalogModel(new, True)]
        if fault == "removed":
            rows.pop(0)
        assert await registry.refresh()
        if fault == "tamper":
            call[call_field] = original[:-2] + ("A" if original[-2] != "A" else "B") + original[-1]
        elif fault == "strip":
            call[call_field] = original.split(".", 2)[2].rsplit(".", 1)[0]
        elif fault == "cross_alias":
            body["model"] = "default"
        elif fault == "inner":
            inner = original.split(".", 2)[2].rsplit(".", 1)[0]
            inner = inner[:-2] + ("A" if inner[-2] != "A" else "B") + inner[-1]
            with receipt_scope(RouteReceipt("d" * 43, selector, old)):
                call[call_field] = wrap_route_token(inner, public_model=selector, purpose="i")
        result[result_field] = call[call_field]
        if fault == "mixed":
            key = "d" * 43
            with receipt_scope(RouteReceipt(key, selector, new)):
                result[result_field] = encode_continuation_id(
                    raw_id="call_stream",
                    public_model=selector,
                    kind="chat_call" if chat else "responses_call",
                    binding_key=derive_route_binding_key(
                        key, public_model=selector, upstream_model=new
                    ),
                )
        if fault != "none":
            before = len(provider.calls), len(upstream.calls)
            response = await client.post(path, json=body)
            assert response.status == 400
            assert (len(provider.calls), len(upstream.calls)) == before
        else:
            await _completion(client, path, body)
            assert upstream.calls[-1]["model"] == old
            body["messages" if chat else "input"] = [{"role": "user", "content": "fresh"}]
            await _completion(client, path, body)
            assert upstream.calls[-1]["model"] == new


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,family", [("standard", "sol"), ("advanced", "astra"), ("fast", "luna")]
)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("kind", ["compaction", "custom"])
async def test_family_compaction_and_custom_replay(
    tmp_path: Path, selector: str, family: str, streaming: bool, kind: str
) -> None:
    from tests.test_default_routes import _completion, _server, _wire
    from tests.test_responses_streaming import _compaction_events, _custom_tool_events, _text_events

    old, new = f"gpt-6-{family}", f"gpt-7-{family}"
    events = _compaction_events() if kind == "compaction" else _custom_tool_events()
    async with _server(
        tmp_path,
        initial_rows=[CatalogModel(old, True)],
        streams=[_wire(events), _wire(_text_events())],
        buffered=not streaming,
    ) as (client, upstream, _provider, rows, registry):
        body: dict[str, Any] = {"model": selector, "stream": streaming, "input": "hello"}
        if kind == "compaction":
            body["context_management"] = [{"type": "compaction", "compact_threshold": 1000}]
        else:
            body.update(
                tools=[{"type": "custom", "name": "emit_probe"}],
                tool_choice={"type": "custom", "name": "emit_probe"},
                parallel_tool_calls=False,
            )
        first = await _completion(client, "/v1/responses", body)
        assert all(item["id"].startswith("cobr_t1_") for item in first["output"])
        rows[:] = [CatalogModel(old, False), CatalogModel(new, True)]
        assert await registry.refresh()
        history = [*first["output"]]
        if kind == "custom":
            call = next(item for item in history if item["type"] == "custom_tool_call")
            history.append(
                {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "ok"}
            )
            body["tool_choice"] = "none"
        else:
            assert any(item["type"] == "compaction" for item in history)
            history.append({"role": "user", "content": "continue"})
        body["input"] = history
        await _completion(client, "/v1/responses", body)
        assert [call["model"] for call in upstream.calls] == [old, old]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,family", [("standard", "sol"), ("advanced", "astra"), ("fast", "luna")]
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_family_sdk_reasoning_and_same_model_cross_alias_rejection(
    tmp_path: Path, selector: str, family: str, streaming: bool
) -> None:
    from openai import AsyncOpenAI
    from openai.types.responses import Response

    from tests.test_default_routes import _server, _wire
    from tests.test_responses_streaming import _reasoning_events

    old, new = f"gpt-6-{family}", f"gpt-7-{family}"
    next_wire = (
        _wire(_reasoning_events(encrypted_content="Yg=="))
        .replace(b"rs_stream", b"rs_next")
        .replace(b"msg_stream", b"msg_next")
        .replace(b"resp_stream", b"resp_next")
    )
    async with _server(
        tmp_path,
        initial_rows=[CatalogModel(old, True)],
        streams=[_wire(_reasoning_events()), next_wire],
        buffered=not streaming,
    ) as (http, upstream, provider, rows, registry):
        async with AsyncOpenAI(
            base_url=str(http.server.make_url("/v1/")), api_key="c" * 43, max_retries=0
        ) as sdk:
            first: Response
            second: Response
            if streaming:
                async with sdk.responses.stream(model=selector, input="hello") as events:
                    async for _ in events:
                        pass
                    first = await events.get_final_response()
            else:
                first = await sdk.responses.create(model=selector, input="hello", stream=False)
            history: list[Any] = [item.model_dump(exclude_none=True) for item in first.output]
            history.append({"role": "user", "content": "continue"})
            before = len(provider.calls), len(upstream.calls)
            rejected = await http.post("/v1/responses", json={"model": "default", "input": history})
            assert rejected.status == 400
            assert (len(provider.calls), len(upstream.calls)) == before
            rows[:] = [CatalogModel(old, False), CatalogModel(new, True)]
            assert await registry.refresh()
            if streaming:
                async with sdk.responses.stream(model=selector, input=history) as events:
                    async for _ in events:
                        pass
                    second = await events.get_final_response()
            else:
                second = await sdk.responses.create(model=selector, input=history, stream=False)
            assert first.model == second.model == selector and second.output_text == "hello"
            assert [call["model"] for call in upstream.calls] == [old, old]


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_missing_families_are_unlisted_and_rejected_without_fallback(
    tmp_path: Path, cold: bool
) -> None:
    from tests.test_default_routes import _server

    rows = [CatalogModel("gpt-6-terra", True)] if cold else [CatalogModel("gpt-6-sol", True)]
    async with _server(tmp_path, initial_rows=rows) as (client, upstream, provider, rows, registry):
        if not cold:
            rows[:] = [CatalogModel("gpt-6-terra", True)]
            assert await registry.refresh()
        listing = await client.get("/v1/models")
        assert listing.status == 200
        aliases = {item["id"] for item in (await listing.json())["data"]}
        assert "default" in aliases and aliases.isdisjoint({"standard", "advanced", "fast"})
        assert (await client.get("/readyz")).status == 200
        before = len(provider.calls), len(upstream.calls)
        for alias in ("standard", "advanced", "fast"):
            result = await client.post("/v1/responses", json={"model": alias, "input": "hello"})
            assert result.status == 400
        assert (len(provider.calls), len(upstream.calls)) == before


@pytest.mark.asyncio
async def test_ambiguous_family_refresh_keeps_old_generation_without_renewing_age() -> None:
    from codex_openai_bridge.model_catalog import CatalogRegistry, CatalogUnavailable

    now = [10.0]
    rows = [CatalogModel("gpt-6-sol", True)]

    async def load() -> list[CatalogModel]:
        return rows

    registry = CatalogRegistry(
        load, overrides={}, refresh_seconds=5.0, max_age_seconds=20.0, clock=lambda: now[0]
    )
    try:
        assert await registry.refresh()
        original = registry.current()
        now[0] = 15.0
        rows[:] = [CatalogModel("gpt-7-sol", True), CatalogModel("gpt-7.0-sol", False)]
        assert not await registry.refresh()
        assert registry.current() is original
        assert registry.current().routes["standard"] == "gpt-6-sol"
        now[0] = 30.0
        with pytest.raises(CatalogUnavailable):
            registry.current()
    finally:
        await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,family", [("standard", "sol"), ("advanced", "astra"), ("fast", "luna")]
)
async def test_family_request_keeps_snapshot_across_credential_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str, family: str
) -> None:
    from codex_openai_bridge.auth import Credential
    from tests.test_default_routes import _server

    old, new = f"gpt-6-{family}", f"gpt-7-{family}"
    async with _server(tmp_path, initial_rows=[CatalogModel(old, True)]) as (
        client,
        upstream,
        provider,
        rows,
        registry,
    ):

        async def credentials(*, force_refresh: bool = False) -> Credential:
            provider.calls.append(force_refresh)
            rows[:] = [CatalogModel(old, False), CatalogModel(new, True)]
            assert await registry.refresh()
            value = provider.credential
            assert isinstance(value, Credential)
            return value

        monkeypatch.setattr(provider, "get_credentials", credentials)
        for _ in range(2):
            response = await client.post(
                "/v1/responses", json={"model": selector, "input": "hello"}
            )
            assert response.status == 200
        assert [call["model"] for call in upstream.calls] == [old, new]
