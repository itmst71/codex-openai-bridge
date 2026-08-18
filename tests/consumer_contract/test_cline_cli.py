from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.contract._support import CLIENT_TOKEN, assert_server_policy, contract_server

_CLINE_PREFIX_TEXT = os.environ.get("CLINE_CONTRACT_PREFIX")
pytestmark = pytest.mark.skipif(
    not _CLINE_PREFIX_TEXT,
    reason="requires the isolated Cline CLI consumer contract",
)
_CLINE_PREFIX = Path(_CLINE_PREFIX_TEXT) if _CLINE_PREFIX_TEXT else Path("/missing-cline")
_CLINE_BIN = _CLINE_PREFIX / "node_modules" / ".bin" / "cline"
_OUTPUT_LIMIT = 256 * 1024
_PROCESS_TIMEOUT = 45.0


def _package_version(name: str) -> str:
    document = json.loads(
        (_CLINE_PREFIX / "node_modules" / name / "package.json").read_text(encoding="utf-8")
    )
    return str(document["version"])


def _assert_installation() -> None:
    assert platform.system() == "Linux"
    assert platform.machine() in {"x86_64", "amd64"}
    assert _CLINE_BIN.exists()
    assert _package_version("@cline/cli-linux-x64") == "3.0.55"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    return completed.stdout.strip()


async def _read_bounded(stream: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > _OUTPUT_LIMIT:
            raise AssertionError("Cline process output exceeded the contract limit")
        chunks.append(chunk)


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = _PROCESS_TIMEOUT,
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
            timeout=timeout,
        )
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return returncode, stdout, stderr
        os.killpg(process.pid, signal.SIGKILL)
        raise AssertionError("Cline left a process-group descendant after successful exit")
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


def _compile_connect_guard(control: Path) -> tuple[Path, Path, Path]:
    compiler = shutil.which("cc")
    assert compiler is not None, "C compiler is required for the Cline egress guard"
    source = control / "connect_guard.c"
    library = control / "connect_guard.so"
    active = control / "guard-active"
    blocked = control / "guard-blocked"
    source.write_text(
        r"""#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

static void mark(const char *name, const char *value) {
    const char *path = getenv(name);
    if (!path) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return;
    ssize_t written = write(fd, value, strlen(value));
    (void)written;
    (void)close(fd);
}

__attribute__((constructor)) static void activate(void) {
    mark("CLINE_GUARD_ACTIVE", "active\n");
}

static int allowed(const struct sockaddr *address) {
    if (!address) return 0;
    if (address->sa_family == AF_UNIX || address->sa_family == AF_NETLINK) return 1;
    if (address->sa_family == AF_INET) {
        const struct sockaddr_in *in = (const struct sockaddr_in *)address;
        return (ntohl(in->sin_addr.s_addr) >> 24) == 127;
    }
    if (address->sa_family == AF_INET6) {
        const struct sockaddr_in6 *in6 = (const struct sockaddr_in6 *)address;
        return IN6_IS_ADDR_LOOPBACK(&in6->sin6_addr);
    }
    return 0;
}

int connect(int fd, const struct sockaddr *address, socklen_t length) {
    static int (*real_connect)(int, const struct sockaddr *, socklen_t);
    if (!real_connect) real_connect = dlsym(RTLD_NEXT, "connect");
    if (!allowed(address)) {
        mark("CLINE_GUARD_BLOCKED", "blocked\n");
        errno = ENETUNREACH;
        return -1;
    }
    return real_connect(fd, address, length);
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            compiler,
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(library),
            str(source),
            "-ldl",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20.0,
    )
    return library, active, blocked


def _environment(
    *,
    home: Path,
    data: Path,
    hooks: Path,
    library: Path,
    active: Path,
    blocked: Path,
) -> dict[str, str]:
    return {
        "CLINE_COMMAND_PERMISSIONS": json.dumps(
            {"allow": [], "deny": ["*"], "allowRedirects": False},
            separators=(",", ":"),
        ),
        "CLINE_DATA_DIR": str(data),
        "CLINE_GUARD_ACTIVE": str(active),
        "CLINE_GUARD_BLOCKED": str(blocked),
        "CLINE_HOOKS_DIR": str(hooks),
        "CLINE_LOG_ENABLED": "0",
        "CLINE_SANDBOX": "1",
        "CLINE_SESSION_BACKEND_MODE": "local",
        "E2E_TEST": "true",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LD_PRELOAD": str(library),
        "NO_PROXY": "127.0.0.1,localhost",
        "PATH": os.environ["PATH"],
    }


def _function_call_sse(*, name: str, arguments: dict[str, Any], call_id: str, index: int) -> bytes:
    item_id = f"function_cline_{index}"
    response_id = f"resp_cline_{index}"
    argument_text = json.dumps(arguments, separators=(",", ":"))
    added = {
        "id": item_id,
        "type": "function_call",
        "status": "in_progress",
        "call_id": call_id,
        "name": name,
        "arguments": "",
    }
    done = {**added, "status": "completed", "arguments": argument_text}
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "response": {"id": response_id, "created_at": index, "status": "in_progress"},
        },
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "delta": argument_text,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": 0,
            "arguments": argument_text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": index,
                "status": "completed",
                "output": [done],
                "usage": {
                    "input_tokens": 3,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 4,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 7,
                },
            },
        },
    ]
    frames: list[bytes] = []
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence
        event_type = str(event["type"]).encode("ascii")
        data = json.dumps(event, separators=(",", ":")).encode("utf-8")
        frames.append(b"event: " + event_type + b"\ndata: " + data + b"\n\n")
    return b"".join(frames)


def _write_global_settings(data: Path) -> None:
    settings = data / "settings"
    settings.mkdir(parents=True)
    (settings / "global-settings.json").write_text(
        json.dumps(
            {
                "telemetryOptOut": True,
                "autoUpdateEnabled": False,
                "compactionEnabled": False,
                "toolAutoApprove": True,
                "disabledTools": [
                    "web_search",
                    "search_codebase",
                    "run_commands",
                    "fetch_web_content",
                    "skills",
                    "ask_question",
                    "spawn_agent",
                    "teams",
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _result_documents(stdout: bytes) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="strict").splitlines():
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict):
            documents.append(document)
    return documents


@pytest.mark.asyncio
async def test_cline_native_guard_rejects_direct_nonloopback_connect(tmp_path: Path) -> None:
    _assert_installation()
    control = tmp_path / "control"
    control.mkdir()
    library, active, blocked = _compile_connect_guard(control)
    env = {
        "CLINE_GUARD_ACTIVE": str(active),
        "CLINE_GUARD_BLOCKED": str(blocked),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LD_PRELOAD": str(library),
        "PATH": os.environ["PATH"],
    }
    returncode, _stdout, _stderr = await _run_process(
        [
            shutil.which("python3") or "python3",
            "-c",
            "import socket; socket.create_connection(('203.0.113.1', 9), timeout=1)",
        ],
        cwd=tmp_path,
        env=env,
        timeout=10.0,
    )
    assert returncode != 0
    assert active.read_text(encoding="ascii") == "active\n"
    assert blocked.read_text(encoding="ascii") == "blocked\n"


@pytest.mark.asyncio
async def test_cline_cli_reads_edits_and_submits_without_other_side_effects(tmp_path: Path) -> None:
    _assert_installation()
    repo = tmp_path / "repo"
    control = tmp_path / "control"
    home = control / "home"
    data = control / "data"
    hooks = control / "hooks"
    for directory in (repo, home, data, hooks):
        directory.mkdir(parents=True)
    library, active, blocked = _compile_connect_guard(control)
    env = _environment(
        home=home,
        data=data,
        hooks=hooks,
        library=library,
        active=active,
        blocked=blocked,
    )
    _write_global_settings(data)

    target = repo / "target.txt"
    target.write_text("VALUE=1\n", encoding="utf-8")
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.name", "Cline Contract")
    _git(repo, "config", "user.email", "cline-contract@example.invalid")
    _git(repo, "add", "target.txt")
    _git(repo, "commit", "--quiet", "-m", "contract baseline")
    baseline_head = _git(repo, "rev-parse", "HEAD")

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    streams = [
        _function_call_sse(
            name="read_files",
            arguments={"files": [{"path": str(target)}]},
            call_id="call_cline_read",
            index=1,
        ),
        _function_call_sse(
            name="editor",
            arguments={
                "path": str(target),
                "old_text": "VALUE=1\n",
                "new_text": "VALUE=2\n",
            },
            call_id="call_cline_edit",
            index=2,
        ),
        _function_call_sse(
            name="submit_and_exit",
            arguments={
                "summary": "Changed the requested value in the target file.",
                "verified": True,
            },
            call_id="call_cline_submit",
            index=3,
        ),
    ]
    async with contract_server(bridge_dir, streams=streams) as running:
        base_url = str(running.client.base_url).rstrip("/")
        auth_argv = [
            str(_CLINE_BIN),
            "auth",
            "--provider",
            "openai",
            "--apikey",
            CLIENT_TOKEN,
            "--modelid",
            "codex",
            "--baseurl",
            base_url,
            "--data-dir",
            str(data),
        ]
        auth_code, auth_stdout, auth_stderr = await _run_process(
            auth_argv,
            cwd=control,
            env=env,
            timeout=20.0,
        )
        assert auth_code == 0, (len(auth_stdout), len(auth_stderr))

        system_prompt = (
            "You are a bounded coding agent. "
            f"The workspace root is {repo}. Use absolute paths under that root. "
            "Use read_files, editor, and submit_and_exit only. Never run shell commands."
        )
        task = (
            f"Read {target}. Use the editor tool to change only VALUE=1 to VALUE=2. "
            "Then submit and exit."
        )
        argv = [
            str(_CLINE_BIN),
            "--yolo",
            "--json",
            "--thinking",
            "none",
            "--compaction",
            "off",
            "--retries",
            "0",
            "--timeout",
            "30",
            "--provider",
            "openai-compatible",
            "--model",
            "codex",
            "--cwd",
            str(repo),
            "--data-dir",
            str(data),
            "--hooks-dir",
            str(hooks),
            "--system",
            system_prompt,
            task,
        ]
        returncode, stdout, stderr = await _run_process(argv, cwd=repo, env=env)

    assert returncode == 0, (len(stdout), len(stderr))
    combined = auth_stdout + auth_stderr + stdout + stderr
    for canary in (
        CLIENT_TOKEN.encode(),
        b"synthetic-upstream-token",
        b"contract-account",
        b"chatgpt.com/backend-api/codex",
    ):
        assert canary not in combined

    documents = _result_documents(stdout)
    run_results = [document for document in documents if document.get("type") == "run_result"]
    assert len(run_results) == 1
    assert run_results[0]["finishReason"] == "completed"
    assert run_results[0]["iterations"] == 3
    tool_names = [
        document["event"]["toolName"]
        for document in documents
        if isinstance(document.get("event"), dict)
        and document["event"].get("type") == "content_start"
        and document["event"].get("contentType") == "tool"
    ]
    assert tool_names == ["read_files", "editor", "submit_and_exit"]

    assert target.read_bytes() == b"VALUE=2\n"
    assert _git(repo, "rev-parse", "HEAD") == baseline_head
    assert _git(repo, "status", "--porcelain") == "M target.txt"
    assert sorted(
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if ".git" not in path.relative_to(repo).parts
    ) == ["target.txt"]
    assert active.read_text(encoding="ascii") == "active\n"
    assert not blocked.exists()

    provider_file = data / "settings" / "providers.json"
    assert provider_file.stat().st_mode & 0o777 == 0o600
    provider_document = json.loads(provider_file.read_text(encoding="utf-8"))
    assert provider_document["lastUsedProvider"] == "openai-compatible"
    provider_settings = provider_document["providers"]["openai-compatible"]["settings"]
    assert set(provider_settings) == {"provider", "apiKey", "model", "baseUrl"}
    assert provider_settings["provider"] == "openai-compatible"
    assert provider_settings["apiKey"] == CLIENT_TOKEN
    assert provider_settings["model"] == "codex"
    assert provider_settings["baseUrl"] == base_url

    assert running.request_paths == ["/v1/chat/completions"] * 3
    assert len(running.upstream.calls) == 3
    assert [stream.close_calls for stream in running.upstream.byte_streams] == [1, 1, 1]
    for call in running.upstream.calls:
        assert_server_policy(call, stream=True)
        assert call.get("tool_choice") == "auto"
        assert [tool["name"] for tool in call["tools"]] == [
            "read_files",
            "editor",
            "submit_and_exit",
        ]
        for tool in call["tools"]:
            assert tool["description"].strip()
        for field in (
            "temperature",
            "top_p",
            "stop",
            "seed",
            "logit_bias",
            "presence_penalty",
            "frequency_penalty",
            "parallel_tool_calls",
        ):
            assert field not in call

    second_outputs = [
        item
        for item in running.upstream.calls[1]["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    third_outputs = [
        item
        for item in running.upstream.calls[2]["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert [item["call_id"] for item in second_outputs] == ["call_cline_read"]
    assert [item["call_id"] for item in third_outputs] == [
        "call_cline_read",
        "call_cline_edit",
    ]
    assert all(isinstance(item["output"], str) and item["output"] for item in third_outputs)
