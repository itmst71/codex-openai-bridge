"""Bounded official Codex CLI credential acquisition."""

from __future__ import annotations

import asyncio
import json
import math
import os
import selectors
import signal
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_openai_bridge.config import Settings

_READ_CHUNK_BYTES = 64 * 1024
_TERMINATE_GRACE_SECONDS = 0.2
_EXPIRY_SKEW_SECONDS = 60.0
_MAX_HELPER_OUTPUT_BYTES = 64 * 1024
_MIN_HELPER_DEADLINE_SECONDS = 0.1
_MAX_HELPER_DEADLINE_SECONDS = 60.0


class CredentialUnavailable(RuntimeError):
    """Raised when credentials cannot be acquired safely."""


@dataclass(frozen=True, slots=True)
class Credential:
    """Validated credentials returned by the isolated helper."""

    access_token: str
    base_url: str
    account_id: str | None
    expires_at: int


class _HelperFailure(Exception):
    """Internal output/process failure that never includes child data."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _valid_path(path: object) -> bool:
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and ".." not in path.parts
        and not any(unicodedata.category(character) == "Cc" for character in str(path))
    )


def _validate_runner_inputs(settings: Settings, force_refresh: bool) -> None:
    if type(settings) is not Settings or type(force_refresh) is not bool:
        raise CredentialUnavailable("credentials are unavailable")
    for path in (
        settings.credential_python_path,
        settings.codex_path,
        settings.codex_home,
    ):
        if not _valid_path(path):
            raise CredentialUnavailable("credentials are unavailable")
    for limit in (settings.max_helper_stdout_bytes, settings.max_helper_stderr_bytes):
        if type(limit) is not int or not 1 <= limit <= _MAX_HELPER_OUTPUT_BYTES:
            raise CredentialUnavailable("credentials are unavailable")
    deadline = settings.helper_deadline_seconds
    if (
        type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or not _MIN_HELPER_DEADLINE_SECONDS <= deadline <= _MAX_HELPER_DEADLINE_SECONDS
    ):
        raise CredentialUnavailable("credentials are unavailable")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded teardown which never masks the primary failure."""
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except (OSError, ValueError):
        pass

    grace_end = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while _process_group_exists(process_group) and time.monotonic() < grace_end:
        try:
            process.wait(timeout=min(0.02, max(0.001, grace_end - time.monotonic())))
        except (OSError, subprocess.TimeoutExpired):
            pass

    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (OSError, ValueError):
            pass
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _close_process_streams(
    process: subprocess.Popen[bytes], selector: selectors.BaseSelector | None
) -> None:
    if selector is not None:
        try:
            selector.close()
        except BaseException:
            pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except BaseException:
                pass


def _read_bounded_output(
    process: subprocess.Popen[bytes],
    *,
    stdout_limit: int,
    stderr_limit: int,
    deadline: float,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise _HelperFailure
    selector = selectors.DefaultSelector()
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, name)

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _HelperFailure
            for key, _events in selector.select(remaining):
                name = key.data
                output = outputs[name]
                limit = limits[name]
                read_size = min(_READ_CHUNK_BYTES, limit + 1 - len(output))
                chunk = os.read(key.fd, max(1, read_size))
                if chunk:
                    output.extend(chunk)
                    if len(output) > limit:
                        raise _HelperFailure
                else:
                    selector.unregister(key.fd)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _HelperFailure
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _HelperFailure from exc
        if return_code != 0:
            raise _HelperFailure
        return bytes(outputs["stdout"]), bytes(outputs["stderr"])
    finally:
        try:
            selector.close()
        except BaseException:
            pass


def _parse_exact_credential(stdout: bytes) -> Credential:
    document = stdout.decode("utf-8", errors="strict")
    value: Any = json.loads(document, object_pairs_hook=_reject_duplicate_keys)
    if type(value) is not dict:
        raise ValueError
    if set(value) != {"version", "access_token", "base_url", "account_id", "expires_at"}:
        raise ValueError
    if value["version"] != 1 or type(value["version"]) is not int:
        raise ValueError
    access_token = value["access_token"]
    base_url = value["base_url"]
    account_id = value["account_id"]
    expires_at = value["expires_at"]
    if type(access_token) is not str or not access_token:
        raise ValueError
    if type(base_url) is not str or not base_url:
        raise ValueError
    if account_id is not None and (type(account_id) is not str or not account_id):
        raise ValueError
    if type(expires_at) is not int or expires_at <= 0:
        raise ValueError
    return Credential(
        access_token=access_token,
        base_url=base_url,
        account_id=account_id,
        expires_at=expires_at,
    )


def parse_credential_output(stdout: bytes) -> Credential:
    """Parse a successful helper response into an exact immutable value."""
    try:
        return _parse_exact_credential(stdout)
    except (KeyError, TypeError, ValueError):
        pass
    raise CredentialUnavailable("credentials are unavailable")


def run_credential_helper(settings: Settings, *, force_refresh: bool = False) -> Credential:
    """Run the isolated helper and return its validated credential response."""
    _validate_runner_inputs(settings, force_refresh)
    argv: list[str | os.PathLike[str]] = [
        settings.credential_python_path,
        "-I",
        "-m",
        "codex_openai_bridge.codex_cli_credential_helper",
    ]
    if force_refresh:
        argv.append("--force-refresh")
    deadline = time.monotonic() + settings.helper_deadline_seconds

    process: subprocess.Popen[bytes] | None = None
    failed = False
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
            env={
                "CODEX_BRIDGE_CODEX_HOME": str(settings.codex_home),
                "CODEX_BRIDGE_CODEX_PATH": str(settings.codex_path),
                "HOME": str(Path.home()),
            },
        )
        stdout, _stderr = _read_bounded_output(
            process,
            stdout_limit=settings.max_helper_stdout_bytes,
            stderr_limit=settings.max_helper_stderr_bytes,
            deadline=deadline,
        )
        return parse_credential_output(stdout)
    except BaseException as exc:
        if process is not None:
            try:
                _terminate_process_group(process)
            except BaseException:
                pass
        if isinstance(exc, CredentialUnavailable):
            raise
        if isinstance(exc, Exception):
            failed = True
        else:
            raise
    finally:
        if process is not None:
            _close_process_streams(process, None)
    if failed:
        raise CredentialUnavailable("credentials are unavailable")
    raise AssertionError("unreachable")


class CredentialManager:
    """Async single-flight cache around the blocking helper boundary."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._cached: Credential | None = None
        self._in_flight: asyncio.Task[Credential] | None = None
        self._in_flight_force_refresh = False

    def _cached_is_fresh(self) -> bool:
        return (
            self._cached is not None
            and self._cached.expires_at > time.time() + _EXPIRY_SKEW_SECONDS
        )

    async def _finish_in_flight(
        self,
        task: asyncio.Task[Credential],
        *,
        accept_result: bool = True,
    ) -> None:
        async with self._lock:
            if self._in_flight is not task or not task.done():
                return
            try:
                credential = task.result()
            except BaseException:
                pass
            else:
                if accept_result:
                    self._cached = credential
            self._in_flight = None
            self._in_flight_force_refresh = False

    async def get_credentials(self, *, force_refresh: bool = False) -> Credential:
        """Return cached credentials or share one bounded helper invocation."""
        if type(force_refresh) is not bool:
            raise CredentialUnavailable("credentials are unavailable")

        while True:
            if not force_refresh and self._cached_is_fresh():
                assert self._cached is not None
                return self._cached

            async with self._lock:
                if not force_refresh and self._cached_is_fresh():
                    assert self._cached is not None
                    return self._cached
                task = self._in_flight
                retry_with_force = False
                if task is None:
                    task = asyncio.create_task(
                        asyncio.to_thread(
                            run_credential_helper,
                            self._settings,
                            force_refresh=force_refresh,
                        )
                    )
                    self._in_flight = task
                    self._in_flight_force_refresh = force_refresh
                elif force_refresh and not self._in_flight_force_refresh:
                    retry_with_force = True

            try:
                credential = await asyncio.shield(task)
            except BaseException as exc:
                await self._finish_in_flight(task)
                if retry_with_force and isinstance(exc, Exception):
                    continue
                raise
            account_mismatch = (
                self._cached is not None
                and self._cached.account_id is not None
                and credential.account_id != self._cached.account_id
            )
            if account_mismatch:
                await self._finish_in_flight(task, accept_result=False)
                raise CredentialUnavailable("credentials are unavailable")
            await self._finish_in_flight(task)
            if retry_with_force:
                continue
            return credential

    async def get(self, *, force_refresh: bool = False) -> Credential:
        """Compatibility shorthand for callers that prefer ``get``."""
        return await self.get_credentials(force_refresh=force_refresh)
