"""Unmodified, hash-pinned Unhush 3.3.4 formatting client against loopback fixtures."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.contract._support import (
    CLIENT_TOKEN,
    assert_server_policy,
    completed_text_response,
    contract_server,
)

PREFIX = os.environ.get("UNHUSH_CONTRACT_PREFIX")
if not PREFIX:
    pytest.skip("requires the isolated pinned Unhush consumer", allow_module_level=True)
NODE_COMMAND = shutil.which("node")
assert NODE_COMMAND is not None
NODE = subprocess.run(
    [NODE_COMMAND, "-p", "process.execPath"], check=True, capture_output=True, text=True, timeout=5
).stdout.strip()
RUNNER = Path(__file__).with_name("unhush_env") / "run.mjs"


async def read_bounded(stream: asyncio.StreamReader) -> bytes:
    output = bytearray()
    while chunk := await stream.read(8192):
        output.extend(chunk)
        if len(output) > 64 * 1024:
            raise AssertionError("consumer output bound exceeded")
    return bytes(output)


async def run_client(tmp_path: Path, url: str, **options: Any) -> dict[str, Any]:
    home = tmp_path / "consumer-home"
    home.mkdir(exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        NODE,
        str(RUNNER),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(home), "UNHUSH_CONTRACT_PREFIX": PREFIX or ""},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    output = asyncio.create_task(read_bounded(process.stdout))
    errors = asyncio.create_task(read_bounded(process.stderr))
    try:
        process.stdin.write(json.dumps({"url": url, "token": CLIENT_TOKEN, **options}).encode())
        await process.stdin.drain()
        process.stdin.close()
        code, stdout, stderr = await asyncio.wait_for(
            asyncio.gather(process.wait(), output, errors),
            timeout=15,
        )
        assert code == 0, stderr.decode()
        result: dict[str, Any] = json.loads(stdout)
        assert result["version"] == "3.3.4"
        assert result["revision"] == "657352ea22c0738fe1c3a26005c2365032d02746"
        assert result["startCommandCalls"] == 0
        return result
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        for task in (output, errors):
            if not task.done():
                task.cancel()
        await asyncio.gather(output, errors, return_exceptions=True)


@asynccontextmanager
async def response_fixture(mode: str) -> AsyncIterator[str]:
    posts = 0

    async def models(_request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "codex"}]})

    async def chat(_request: web.Request) -> web.Response:
        nonlocal posts
        posts += 1
        if mode in {"401", "429", "503"}:
            return web.json_response({"error": {"message": "fixture error"}}, status=int(mode))
        if mode == "recover" and posts == 1:
            return web.json_response({"error": {"message": "fixture unavailable"}}, status=503)
        if mode == "malformed":
            return web.Response(text="{", content_type="application/json")
        content = "" if mode == "empty" else "formatted fixture"
        return web.json_response({"choices": [{"message": {"content": content}}]})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/")


@pytest.mark.asyncio
async def test_unhush_real_formatter_uses_strict_bridge(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path, responses=[completed_text_response("clean text")]
    ) as running:
        origin = str(running.client.base_url).removesuffix("/v1/")
        result = await run_client(
            tmp_path, origin, actions=[{"operation": "models"}, {"operation": "format"}]
        )
    assert result["results"] == [
        {"ok": True, "models": ["codex"]},
        {"ok": True, "content": "clean text"},
    ]
    assert result["blocked"] == 0
    body = result["requests"][-1]["body"]
    assert body["max_tokens"] == 256
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["messages"][1]["content"].startswith("<transcript>\n")
    assert result["deadlines"] == [5000, 90000]
    assert len(running.upstream.calls) == 1
    assert_server_policy(running.upstream.calls[0], stream=False)
    assert "max_tokens" not in running.upstream.calls[0]


@pytest.mark.asyncio
async def test_unhush_disabled_warmup_reports_skipped_without_model_request(tmp_path: Path) -> None:
    async with response_fixture("normal") as url:
        result = await run_client(
            tmp_path,
            url,
            settings={"unhush_llm_custom_warmup": "false"},
            actions=[{"operation": "warmup"}],
        )
    assert result["results"] == [{"ok": True, "status": "skipped"}]
    assert result["blocked"] == 0
    assert all(request["method"] == "GET" for request in result["requests"])
    assert 90000 not in result["deadlines"]


@pytest.mark.asyncio
async def test_unhush_actual_warmup_is_user_only_and_waits_for_content(tmp_path: Path) -> None:
    async with contract_server(tmp_path, responses=[completed_text_response("warm")]) as running:
        result = await run_client(
            tmp_path,
            str(running.client.base_url).removesuffix("/v1/"),
            actions=[{"operation": "warmup"}],
        )
    assert result["results"] == [{"ok": True, "status": "ready"}]
    assert result["requests"][-1]["body"] == {
        "model": "codex",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert len(running.upstream.calls) == 1
    assert "max_tokens" not in running.upstream.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["401", "429", "503", "empty", "malformed"])
async def test_unhush_formatter_rejects_error_or_empty_response_without_retry(
    tmp_path: Path, mode: str
) -> None:
    async with response_fixture(mode) as url:
        result = await run_client(tmp_path, url)
    assert result["results"][0]["ok"] is False
    assert len(result["requests"]) == 1
    assert result["deadlines"] == [90000]


@pytest.mark.asyncio
async def test_unhush_warmup_rejects_200_with_empty_content(tmp_path: Path) -> None:
    async with response_fixture("empty") as url:
        result = await run_client(tmp_path, url, actions=[{"operation": "warmup"}])
    assert result["results"] == [{"ok": False, "status": "failed"}]


@pytest.mark.asyncio
async def test_unhush_failed_warmup_recovers_after_injected_cooldown(tmp_path: Path) -> None:
    async with response_fixture("recover") as url:
        result = await run_client(
            tmp_path,
            url,
            actions=[
                {"operation": "warmup"},
                {"operation": "advanceClock", "milliseconds": 16000},
                {"operation": "warmup"},
            ],
        )
    assert result["results"][0] == {"ok": False, "status": "failed"}
    assert result["results"][-1] == {"ok": True, "status": "ready"}
    assert len([r for r in result["requests"] if r["method"] == "POST"]) == 2


@pytest.mark.asyncio
async def test_unhush_formatter_maps_injected_timeout_to_failure(tmp_path: Path) -> None:
    result = await run_client(tmp_path, "http://127.0.0.1:9", injectTimeout=True)
    assert result["results"][0]["ok"] is False
    assert "server unreachable" in result["results"][0]["error"]
    assert result["deadlines"] == [90000]
    assert len(result["requests"]) == 1


@pytest.mark.asyncio
async def test_unhush_contract_transport_refuses_nonloopback_egress(tmp_path: Path) -> None:
    result = await run_client(tmp_path, "http://203.0.113.1:9")
    assert result["results"][0]["ok"] is False
    assert result["blocked"] == 1
    assert result["requests"] == []


@pytest.mark.asyncio
async def test_unhush_repeated_formatter_calls_remain_distinct(tmp_path: Path) -> None:
    async with contract_server(
        tmp_path,
        responses=[
            completed_text_response("one"),
            completed_text_response("two"),
            completed_text_response("three"),
        ],
    ) as running:
        result = await run_client(
            tmp_path,
            str(running.client.base_url).removesuffix("/v1/"),
            actions=[{"operation": "format"}] * 3,
        )
    assert [item["content"] for item in result["results"]] == ["one", "two", "three"]
    assert len(running.upstream.calls) == 3
