from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from tests.contract._support import (
    CLIENT_TOKEN,
    assert_server_policy,
    completed_text_response,
    completed_tool_response,
    contract_server,
    strict_text_sse,
)

_CORE_ROOT_TEXT = os.environ.get("CONTINUE_CORE_ROOT")
_TSX_BIN_TEXT = os.environ.get("CONTINUE_TSX_BIN")
if not _CORE_ROOT_TEXT or not _TSX_BIN_TEXT:
    pytest.skip(
        "requires the isolated Continue core consumer contract environment",
        allow_module_level=True,
    )

_CORE_ROOT = Path(_CORE_ROOT_TEXT).resolve()
_TSX_BIN = Path(_TSX_BIN_TEXT).resolve()
_NODE_BIN = shutil.which("node")
_OUTPUT_LIMIT = 128 * 1024
_PROCESS_TIMEOUT = 30.0


def _install_egress_guard(control: Path) -> tuple[Path, Path, Path]:
    guard = control / "continue-egress-guard.cjs"
    marker = control / "non-loopback-egress-attempted"
    active_marker = control / "egress-guard-active"
    guard.write_text(
        """\
const dns = require("node:dns");
const fs = require("node:fs");
const net = require("node:net");

const marker = process.env.CONTINUE_EGRESS_MARKER;
function allowed(host) {
  return host === "127.0.0.1" || host === "::1" || host === "localhost";
}
function reject(host) {
  fs.writeFileSync(marker, "blocked\\n");
  const error = new Error("non-loopback network is disabled by the consumer contract");
  error.code = "ENETUNREACH";
  throw error;
}
function hostOf(args) {
  const first = args[0];
  if (Array.isArray(first)) return hostOf(first);
  if (typeof first === "object" && first !== null) {
    return first.host || first.hostname;
  }
  if (typeof first === "number") return args[1];
  if (typeof first === "string") return first;
}

const originalLookup = dns.lookup;
dns.lookup = function(host, ...args) {
  if (!allowed(host)) return reject(host);
  return originalLookup.call(this, host, ...args);
};
const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  const host = hostOf(args);
  if (host && !allowed(host)) return reject(host);
  return originalConnect.apply(this, args);
};
fs.writeFileSync(process.env.CONTINUE_EGRESS_GUARD_ACTIVE, "active\\n");
""",
        encoding="utf-8",
    )
    return guard, marker, active_marker


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > _OUTPUT_LIMIT:
            raise AssertionError("Continue process output exceeded the contract limit")
        chunks.append(chunk)


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_read_bounded(process.stdout))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr))
    try:
        returncode, stdout, stderr = await asyncio.wait_for(
            asyncio.gather(process.wait(), stdout_task, stderr_task),
            timeout=_PROCESS_TIMEOUT,
        )
        return returncode, stdout, stderr
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise


def _environment(
    *,
    home: Path,
    guard: Path,
    marker: Path,
    active_marker: Path,
    bridge_url: str | None = None,
) -> dict[str, str]:
    assert _NODE_BIN is not None
    environment = {
        "CONTINUE_CORE_ROOT": str(_CORE_ROOT),
        "CONTINUE_EGRESS_GUARD_ACTIVE": str(active_marker),
        "CONTINUE_EGRESS_MARKER": str(marker),
        "CONTINUE_GLOBAL_DIR": str(home / ".continue"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NODE_ENV": "test",
        "NODE_OPTIONS": f"--require {guard}",
        "NO_PROXY": "127.0.0.1,localhost",
        "PATH": os.environ["PATH"],
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    if bridge_url is not None:
        environment["CONTINUE_BRIDGE_URL"] = bridge_url
        environment["CONTINUE_BRIDGE_TOKEN"] = CLIENT_TOKEN
    return environment


def _assert_installation() -> None:
    assert _NODE_BIN is not None
    assert _CORE_ROOT.is_dir()
    package = json.loads((_CORE_ROOT / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == "@continuedev/core"
    assert package["version"] == "1.1.0"
    assert _TSX_BIN.is_file()
    assert _TSX_BIN.stat().st_mode & stat.S_IXUSR
    tsx_package = json.loads(
        (_CORE_ROOT.parent.parent / "tsx" / "package.json").read_text(encoding="utf-8")
    )
    assert tsx_package["version"] == "4.23.12"
    adapter_package = json.loads(
        (_CORE_ROOT.parent.parent / "@continuedev" / "openai-adapters" / "package.json").read_text(
            encoding="utf-8"
        )
    )
    assert adapter_package["version"] == "1.37.0"
    sqlite_package = json.loads(
        (_CORE_ROOT.parent.parent / "sqlite3" / "package.json").read_text(encoding="utf-8")
    )
    assert sqlite_package == {
        "name": "sqlite3",
        "version": "5.1.7",
        "private": True,
        "description": "No-native test stub for the Continue consumer contract",
        "main": "index.cjs",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe",
    [
        "require('node:dns').lookup('nonloopback.invalid', () => {})",
        "require('node:net').connect({host: '203.0.113.1', port: 9})",
    ],
)
async def test_continue_node_egress_guard_rejects_nonloopback_network(
    tmp_path: Path,
    probe: str,
) -> None:
    _assert_installation()
    home = tmp_path / "home"
    work = tmp_path / "work"
    control = tmp_path / "control"
    for directory in (home, work, control):
        directory.mkdir()
    guard, marker, active_marker = _install_egress_guard(control)

    returncode, _stdout, stderr = await _run_process(
        [
            _NODE_BIN or "node",
            "-e",
            probe,
        ],
        cwd=work,
        env=_environment(
            home=home,
            guard=guard,
            marker=marker,
            active_marker=active_marker,
        ),
    )

    assert returncode != 0
    assert active_marker.read_text(encoding="ascii") == "active\n"
    assert marker.read_text(encoding="utf-8") == "blocked\n"
    assert b"non-loopback network is disabled by the consumer contract" in stderr


@pytest.mark.asyncio
async def test_continue_core_chat_edit_and_tool_roundtrip(tmp_path: Path) -> None:
    _assert_installation()
    home = tmp_path / "home"
    work = tmp_path / "work"
    control = tmp_path / "control"
    bridge = tmp_path / "bridge"
    for directory in (home, work, control, bridge):
        directory.mkdir()
    guard, marker, active_marker = _install_egress_guard(control)
    probe = Path(__file__).with_name("continue_core_probe.mjs")

    async with contract_server(
        bridge,
        responses=[
            completed_tool_response(),
            completed_text_response("continue tool complete"),
        ],
        streams=[
            strict_text_sse("continue chat"),
            strict_text_sse("continue edit"),
        ],
    ) as running:
        returncode, stdout, stderr = await _run_process(
            [str(_TSX_BIN), str(probe)],
            cwd=work,
            env=_environment(
                home=home,
                guard=guard,
                marker=marker,
                active_marker=active_marker,
                bridge_url=str(running.client.base_url).rstrip("/"),
            ),
        )

    assert returncode == 0, (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
    output_lines = stdout.decode(encoding="utf-8", errors="strict").splitlines()
    contract_lines = [line for line in output_lines if line.startswith("CONTINUE_CONTRACT=")]
    assert len(contract_lines) == 1
    contract_line = contract_lines[0]
    result = json.loads(contract_line.removeprefix("CONTINUE_CONTRACT="))
    assert result == {
        "chatStream": True,
        "editStream": True,
        "toolRoundtrip": True,
        "packageVersion": "1.1.0",
    }
    combined_output = stdout + stderr
    for canary in (
        CLIENT_TOKEN.encode(),
        b"synthetic-upstream-token",
        b"contract-account",
        b"chatgpt.com/backend-api/codex",
    ):
        assert canary not in combined_output

    assert active_marker.read_text(encoding="ascii") == "active\n"
    assert not marker.exists()
    assert list(work.iterdir()) == []
    assert running.request_paths == ["/v1/chat/completions"] * 4
    assert len(running.upstream.calls) == 4
    assert [call["stream"] for call in running.upstream.calls] == [True, True, False, False]
    assert [stream.close_calls for stream in running.upstream.byte_streams] == [1, 1]

    for call in running.upstream.calls:
        assert_server_policy(call, stream=bool(call["stream"]))
        for field in (
            "temperature",
            "top_p",
            "stop",
            "seed",
            "logit_bias",
            "presence_penalty",
            "frequency_penalty",
        ):
            assert field not in call
    for call in running.upstream.calls[2:]:
        assert call["parallel_tool_calls"] is False
        assert call["tools"] == [
            {
                "type": "function",
                "name": "weather",
                "description": "Return bounded weather information for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ]
    assert running.upstream.calls[2]["tool_choice"] == {
        "type": "function",
        "name": "weather",
    }
    assert running.upstream.calls[3]["tool_choice"] == "none"
    assert running.upstream.calls[3]["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_weather_contract",
        "output": "bounded weather result",
    }
