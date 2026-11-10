"""Bounded official-CLI catalog discovery through an isolated, secret-negative helper."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from codex_openai_bridge.auth import (
    _process_group_exists,
    _reject_duplicate_keys,
    _validate_runner_inputs,
)
from codex_openai_bridge.codex_cli_auth import (
    _safe_absolute_path,
    _send_json,
    _terminate_app_server,
    read_codex_file_credential,
)
from codex_openai_bridge.config import Settings
from codex_openai_bridge.model_catalog import (
    MAX_CATALOG_MODELS,
    CatalogModel,
    CatalogUnavailable,
    build_catalog,
)

_PROTOCOL_LIMIT = 64 * 1024
_RPC_LIMIT = 2 * 1024 * 1024
_STDERR_LIMIT = 256 * 1024
_MAX_MESSAGES = 256
_MAX_PAGES = 8


def _fail() -> CatalogUnavailable:
    return CatalogUnavailable("model catalog is unavailable")


def _account_tag(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8", errors="strict")).hexdigest()


def _collect_models(codex_path: Path, codex_home: Path, timeout: float) -> list[CatalogModel]:
    executable = codex_path.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise _fail()
    process = subprocess.Popen(
        [
            executable,
            "app-server",
            "--stdio",
            "--strict-config",
            "-c",
            'cli_auth_credentials_store="file"',
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        close_fds=True,
        start_new_session=False,
        env={"CODEX_HOME": str(codex_home), "HOME": str(Path.home()), "PATH": os.defpath},
    )
    selector = selectors.DefaultSelector()
    pending = bytearray()
    totals = {"stdout": 0, "stderr": 0}
    messages = 0
    deadline = time.monotonic() + timeout
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            if stream is None:
                raise _fail()
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ, name)

        def receive(wanted: int) -> dict[str, Any]:
            nonlocal messages
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    raw, _, rest = pending.partition(b"\n")
                    pending[:] = rest
                    messages += 1
                    if not raw or messages > _MAX_MESSAGES:
                        raise _fail()
                    value = json.loads(
                        raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
                    )
                    if type(value) is not dict:
                        raise _fail()
                    if "id" not in value:
                        continue
                    if type(value["id"]) is not int or value["id"] != wanted:
                        raise _fail()
                    result = value.get("result")
                    if "error" in value or type(result) is not dict:
                        raise _fail()
                    return result
                if not selector.get_map():
                    raise _fail()
                for key, _ in selector.select(max(0.0, deadline - time.monotonic())):
                    block = os.read(key.fd, 8192)
                    if not block:
                        selector.unregister(key.fd)
                        continue
                    name = key.data
                    totals[name] += len(block)
                    if totals[name] > (_RPC_LIMIT if name == "stdout" else _STDERR_LIMIT):
                        raise _fail()
                    if name == "stdout":
                        pending.extend(block)
            raise _fail()

        _send_json(
            process,
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex_openai_bridge", "version": "1"},
                },
            },
        )
        receive(0)
        _send_json(process, {"method": "initialized", "params": {}})
        models: list[CatalogModel] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for request_id in range(1, _MAX_PAGES + 1):
            params: dict[str, object] = {"includeHidden": False, "limit": 32}
            if cursor is not None:
                params["cursor"] = cursor
            _send_json(process, {"id": request_id, "method": "model/list", "params": params})
            result = receive(request_id)
            batch = result.get("data")
            if type(batch) is not list or "nextCursor" not in result:
                raise _fail()
            for row in batch:
                if (
                    type(row) is not dict
                    or type(row.get("model")) is not str
                    or type(row.get("hidden")) is not bool
                    or type(row.get("isDefault")) is not bool
                ):
                    raise _fail()
                models.append(CatalogModel(row["model"], row["isDefault"], row["hidden"]))
                if len(models) > MAX_CATALOG_MODELS:
                    raise _fail()
            following = result["nextCursor"]
            if following is None:
                build_catalog(models, overrides={}, refreshed_at=0.0)
                return models
            if (
                type(following) is not str
                or not following
                or len(following.encode("utf-8")) > 1024
                or following in seen_cursors
            ):
                raise _fail()
            seen_cursors.add(following)
            cursor = following
        raise _fail()
    finally:
        selector.close()
        _terminate_app_server(process)


def _helper_main() -> int:
    try:
        home = Path(os.environ["CODEX_BRIDGE_CODEX_HOME"])
        executable = Path(os.environ["CODEX_BRIDGE_CODEX_PATH"])
        if not _safe_absolute_path(home) or not _safe_absolute_path(executable):
            raise _fail()
        timeout = float(os.environ["CODEX_BRIDGE_HELPER_DEADLINE_SECONDS"])
        if not 0.1 <= timeout <= 60.0:
            raise _fail()
        before = read_codex_file_credential(home)
        models = _collect_models(executable, home, timeout)
        after = read_codex_file_credential(home)
        if not before.account_id or before.account_id != after.account_id:
            raise _fail()
        document = {
            "version": 1,
            "account": _account_tag(before.account_id),
            "models": [
                {"model": row.model, "is_default": row.is_default, "hidden": row.hidden}
                for row in models
            ],
        }
        encoded = json.dumps(document, separators=(",", ":")) + "\n"
        if len(encoded.encode()) > _PROTOCOL_LIMIT:
            raise _fail()
        sys.stdout.write(encoded)
        return 0
    except BaseException:
        sys.stderr.write("model catalog helper failed\n")
        return 1


def parse_catalog_output(stdout: bytes, *, account_id: str) -> tuple[CatalogModel, ...]:
    try:
        if (
            not stdout
            or len(stdout) > _PROTOCOL_LIMIT
            or type(account_id) is not str
            or not account_id
        ):
            raise _fail()
        value = json.loads(stdout.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        if (
            type(value) is not dict
            or set(value) != {"version", "account", "models"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["account"] != _account_tag(account_id)
            or type(value["models"]) is not list
        ):
            raise _fail()
        rows: list[CatalogModel] = []
        for row in value["models"]:
            if type(row) is not dict or set(row) != {"model", "is_default", "hidden"}:
                raise _fail()
            rows.append(CatalogModel(row["model"], row["is_default"], row["hidden"]))
        build_catalog(rows, overrides={}, refreshed_at=0.0)
        return tuple(rows)
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise _fail() from None


async def _read_output(stream: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while chunk := await stream.read(4096):
        result.extend(chunk)
        if len(result) > limit:
            raise _fail()
    return bytes(result)


async def _stop_group(process: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            async with asyncio.timeout(0.2):
                await process.wait()
        except TimeoutError:
            pass
    # Waiting for the helper leader alone does not settle its CLI descendants.
    deadline = time.monotonic() + 0.2
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


async def discover_models(settings: Settings, *, account_id: str) -> tuple[CatalogModel, ...]:
    """Run one bounded isolated CLI helper; cancellation tears down the entire process group."""
    process: asyncio.subprocess.Process | None = None
    tasks: list[asyncio.Task[Any]] = []
    try:
        _validate_runner_inputs(settings, False)
        async with asyncio.timeout(settings.helper_deadline_seconds):
            process = await asyncio.create_subprocess_exec(
                str(settings.credential_python_path),
                "-I",
                "-m",
                "codex_openai_bridge.codex_cli_models",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={
                    "CODEX_BRIDGE_CODEX_HOME": str(settings.codex_home),
                    "CODEX_BRIDGE_CODEX_PATH": str(settings.codex_path),
                    "CODEX_BRIDGE_HELPER_DEADLINE_SECONDS": str(settings.helper_deadline_seconds),
                    "HOME": str(Path.home()),
                },
            )
            if process.stdout is None or process.stderr is None:
                raise _fail()
            stdout_task = asyncio.create_task(_read_output(process.stdout, _PROTOCOL_LIMIT))
            stderr_task = asyncio.create_task(_read_output(process.stderr, 16 * 1024))
            wait_task = asyncio.create_task(process.wait())
            tasks = [stdout_task, stderr_task, wait_task]
            await asyncio.gather(*tasks)
            if wait_task.result() != 0:
                raise _fail()
            return parse_catalog_output(stdout_task.result(), account_id=account_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _fail() from None
    finally:
        interrupted = False
        if process is not None:
            cleanup = asyncio.create_task(_stop_group(process))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            if not cleanup.cancelled():
                cleanup.result()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if interrupted:
            raise asyncio.CancelledError


if __name__ == "__main__":
    raise SystemExit(_helper_main())
