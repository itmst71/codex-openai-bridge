from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("aider", reason="requires the isolated Aider consumer contract")

from tests.contract._support import (
    CLIENT_TOKEN,
    assert_server_policy,
    contract_server,
    strict_text_sse,
)

_OUTPUT_LIMIT = 128 * 1024
_PROCESS_TIMEOUT = 30.0


def _is_loopback_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        return socket.getaddrinfo(host, None)[0][4][0] in {"127.0.0.1", "::1"}
    except socket.gaierror:
        return False


def _install_egress_guard(control: Path) -> tuple[Path, Path, Path]:
    guard_dir = control / "python-guard"
    guard_dir.mkdir()
    marker = control / "non-loopback-egress-attempted"
    active_marker = control / "egress-guard-active"
    (guard_dir / "sitecustomize.py").write_text(
        """\
import ipaddress
import os
from pathlib import Path
import socket

_original_getaddrinfo = socket.getaddrinfo
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
Path(os.environ["CODEX_BRIDGE_EGRESS_GUARD_ACTIVE"]).write_text(
    "active\\n", encoding="ascii"
)


def _is_loopback(host):
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeError:
            return False
    if host == "localhost":
        return True
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _mark_and_reject():
    Path(os.environ["CODEX_BRIDGE_EGRESS_MARKER"]).write_text(
        "blocked\\n", encoding="ascii"
    )
    raise OSError("non-loopback network is disabled by the consumer contract")


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        _mark_and_reject()
    return _original_getaddrinfo(host, *args, **kwargs)


def _guarded_connect(sock, address):
    if isinstance(address, tuple) and not _is_loopback(address[0]):
        _mark_and_reject()
    return _original_connect(sock, address)


def _guarded_connect_ex(sock, address):
    if isinstance(address, tuple) and not _is_loopback(address[0]):
        _mark_and_reject()
    return _original_connect_ex(sock, address)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
""",
        encoding="utf-8",
    )
    return guard_dir, marker, active_marker


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    return result.stdout.strip()


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > _OUTPUT_LIMIT:
            raise AssertionError("Aider process output exceeded the contract limit")
        chunks.append(chunk)


async def _run_aider(
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


def _aider_environment(
    home: Path,
    guard_dir: Path,
    egress_marker: Path,
    active_marker: Path,
) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CODEX_BRIDGE_EGRESS_MARKER": str(egress_marker),
        "CODEX_BRIDGE_EGRESS_GUARD_ACTIVE": str(active_marker),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "NO_PROXY": "127.0.0.1,localhost",
        "OPENAI_API_KEY": CLIENT_TOKEN,
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(guard_dir),
        "PYTHONUNBUFFERED": "1",
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    return environment


@pytest.mark.asyncio
async def test_aider_contract_egress_guard_rejects_nonloopback_resolution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    control = tmp_path / "control"
    home.mkdir()
    control.mkdir()
    guard_dir, egress_marker, active_marker = _install_egress_guard(control)

    returncode, _stdout, stderr = await _run_aider(
        [
            sys.executable,
            "-c",
            "import socket; socket.getaddrinfo('nonloopback.invalid', 443)",
        ],
        cwd=tmp_path,
        env=_aider_environment(home, guard_dir, egress_marker, active_marker),
    )

    assert returncode != 0
    assert active_marker.read_text(encoding="ascii") == "active\n"
    assert egress_marker.read_text(encoding="ascii") == "blocked\n"
    assert b"non-loopback network is disabled by the consumer contract" in stderr


def _assert_framework_versions() -> None:
    assert version("aider-chat") == "0.86.2"
    assert version("litellm") == "1.81.10"
    assert version("openai") == "2.20.0"


@pytest.mark.asyncio
async def test_aider_cli_applies_streaming_whole_file_edit_without_other_side_effects(
    tmp_path: Path,
) -> None:
    _assert_framework_versions()
    repo = tmp_path / "repo"
    control = tmp_path / "control"
    home = control / "home"
    repo.mkdir()
    home.mkdir(parents=True)
    guard_dir, egress_marker, active_marker = _install_egress_guard(control)
    target = repo / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.name", "Aider Contract")
    _git(repo, "config", "user.email", "aider-contract@example.invalid")
    _git(repo, "add", "target.py")
    _git(repo, "commit", "--quiet", "-m", "contract baseline")
    baseline_head = _git(repo, "rev-parse", "HEAD")

    model_settings = control / "model-settings.yml"
    model_settings.write_text(
        """\
- name: openai/codex
  edit_format: whole
  weak_model_name: null
  use_repo_map: false
  use_temperature: false
  streaming: true
  cache_control: false
  caches_by_default: false
""",
        encoding="utf-8",
    )
    config = control / "aider.conf.yml"
    config.write_text("{}\n", encoding="utf-8")
    env_file = control / "empty.env"
    env_file.write_text("", encoding="utf-8")
    input_history = control / "input.history"
    chat_history = control / "chat.history.md"
    model_metadata = control / "model-metadata.json"
    model_metadata.write_text(
        """{
  "openai/codex": {
    "max_tokens": 200000,
    "max_input_tokens": 200000,
    "max_output_tokens": 100000,
    "input_cost_per_token": 0,
    "output_cost_per_token": 0,
    "litellm_provider": "openai",
    "mode": "chat"
  }
}
""",
        encoding="utf-8",
    )

    edit = "target.py\n```\nVALUE = 2\n```\n"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    async with contract_server(bridge_dir, streams=[strict_text_sse(edit)]) as running:
        argv = [
            sys.executable,
            "-m",
            "aider",
            "--model",
            "openai/codex",
            "--openai-api-base",
            str(running.client.base_url).rstrip("/"),
            "--model-settings-file",
            str(model_settings),
            "--model-metadata-file",
            str(model_metadata),
            "--config",
            str(config),
            "--env-file",
            str(env_file),
            "--input-history-file",
            str(input_history),
            "--chat-history-file",
            str(chat_history),
            "--edit-format",
            "whole",
            "--message",
            "Set VALUE to 2.",
            "--file",
            "target.py",
            "--timeout",
            "3",
            "--map-tokens",
            "0",
            "--stream",
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-gitignore",
            "--no-attribute-author",
            "--no-attribute-committer",
            "--no-attribute-co-authored-by",
            "--no-auto-lint",
            "--no-auto-test",
            "--no-watch-files",
            "--no-cache-prompts",
            "--no-analytics",
            "--no-check-update",
            "--no-show-release-notes",
            "--no-show-model-warnings",
            "--no-check-model-accepts-settings",
            "--no-pretty",
            "--no-fancy-input",
            "--no-notifications",
            "--no-detect-urls",
            "--no-suggest-shell-commands",
            "--no-gui",
            "--no-copy-paste",
            "--disable-playwright",
            "--encoding",
            "utf-8",
            "--line-endings",
            "lf",
        ]
        returncode, stdout, stderr = await _run_aider(
            argv,
            cwd=repo,
            env=_aider_environment(home, guard_dir, egress_marker, active_marker),
        )

    assert returncode == 0, (stdout.decode(errors="replace"), stderr.decode(errors="replace"))
    combined_output = stdout + stderr
    for canary in (
        CLIENT_TOKEN.encode(),
        b"synthetic-upstream-token",
        b"contract-account",
        b"chatgpt.com/backend-api/codex",
    ):
        assert canary not in combined_output

    assert target.read_bytes() == b"VALUE = 2\n"
    assert _git(repo, "rev-parse", "HEAD") == baseline_head
    assert _git(repo, "status", "--porcelain") == "M target.py"
    assert sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if ".git" not in path.relative_to(repo).parts
    ) == ["target.py"]
    assert not (home / ".aider" / "caches" / "model_prices_and_context_window.json").exists()
    assert not egress_marker.exists()
    assert active_marker.read_text(encoding="ascii") == "active\n"

    assert len(running.upstream.calls) == 1
    assert running.request_paths == ["/v1/chat/completions"]
    payload: dict[str, Any] = running.upstream.calls[0]
    assert_server_policy(payload, stream=True)
    assert running.upstream.byte_streams[0].close_calls == 1
    for field in (
        "temperature",
        "top_p",
        "stop",
        "seed",
        "logit_bias",
        "presence_penalty",
        "frequency_penalty",
    ):
        assert field not in payload
    assert _is_loopback_host(running.client.base_url.host)
