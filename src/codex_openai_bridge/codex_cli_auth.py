"""Strict file-mode credential reader for the official Codex CLI."""

from __future__ import annotations

import base64
import binascii
import json
import os
import selectors
import stat
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any

from codex_openai_bridge import __version__
from codex_openai_bridge.auth import Credential

CANONICAL_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_MAX_AUTH_BYTES = 64 * 1024
_MAX_TOKEN_BYTES = 64 * 1024
_MAX_ENCODED_PAYLOAD_BYTES = 16 * 1024
_MAX_DECODED_PAYLOAD_BYTES = 12 * 1024
_AUTH_CLAIM = "https://api.openai.com/auth"
_REFRESH_DEADLINE_SECONDS = 30.0
_MAX_APP_SERVER_BYTES = 256 * 1024
_MAX_APP_SERVER_MESSAGES = 256


class CodexCredentialError(RuntimeError):
    """Raised when official Codex CLI credentials cannot be used safely."""


def _fail() -> CodexCredentialError:
    return CodexCredentialError("credentials are unavailable")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _safe_absolute_path(path: object) -> bool:
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and ".." not in path.parts
        and not any(unicodedata.category(character) == "Cc" for character in str(path))
    )


def _decode_access_metadata(access_token: str) -> tuple[int, str]:
    if type(access_token) is not str or not access_token:
        raise ValueError
    if len(access_token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ValueError
    segments = access_token.split(".")
    if len(segments) != 3:
        raise ValueError
    encoded_payload = segments[1].encode("ascii")
    if not encoded_payload or len(encoded_payload) > _MAX_ENCODED_PAYLOAD_BYTES:
        raise ValueError
    padding = b"=" * (-len(encoded_payload) % 4)
    decoded = base64.b64decode(
        encoded_payload + padding,
        altchars=b"-_",
        validate=True,
    )
    if len(decoded) > _MAX_DECODED_PAYLOAD_BYTES:
        raise ValueError
    payload = json.loads(
        decoded.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_keys
    )
    if type(payload) is not dict:
        raise ValueError
    expires_at = payload.get("exp")
    auth_claim = payload.get(_AUTH_CLAIM)
    if type(expires_at) is not int or expires_at <= 0 or type(auth_claim) is not dict:
        raise ValueError
    account_id = auth_claim.get("chatgpt_account_id")
    if type(account_id) is not str or not account_id:
        raise ValueError
    return expires_at, account_id


def _read_bounded_auth(codex_home: Path) -> bytes:
    if not _safe_absolute_path(codex_home):
        raise ValueError
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_fd = -1
    auth_fd = -1
    try:
        directory_fd = os.open(
            codex_home,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec,
        )
        directory = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) & 0o022
        ):
            raise ValueError
        auth_fd = os.open(
            "auth.json",
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | cloexec,
            dir_fd=directory_fd,
        )
        before = os.fstat(auth_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_AUTH_BYTES
        ):
            raise ValueError
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(auth_fd, min(8192, _MAX_AUTH_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_AUTH_BYTES:
                raise ValueError
        after = os.fstat(auth_fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity or total != before.st_size:
            raise ValueError
        return b"".join(chunks)
    finally:
        if auth_fd >= 0:
            try:
                os.close(auth_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _parse_auth_document(raw: bytes) -> Credential:
    document = json.loads(
        raw.decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicate_keys
    )
    if type(document) is not dict or set(document) != {"OPENAI_API_KEY", "tokens", "last_refresh"}:
        raise ValueError
    if document["OPENAI_API_KEY"] is not None:
        raise ValueError
    last_refresh = document["last_refresh"]
    if type(last_refresh) is not str or not last_refresh or len(last_refresh.encode("utf-8")) > 256:
        raise ValueError
    tokens = document["tokens"]
    if type(tokens) is not dict or set(tokens) != {
        "id_token",
        "access_token",
        "refresh_token",
        "account_id",
    }:
        raise ValueError
    for key in ("id_token", "access_token", "refresh_token", "account_id"):
        value = tokens[key]
        if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise ValueError
    expires_at, jwt_account_id = _decode_access_metadata(tokens["access_token"])
    if jwt_account_id != tokens["account_id"]:
        raise ValueError
    return Credential(
        access_token=tokens["access_token"],
        base_url=CANONICAL_CODEX_BASE_URL,
        account_id=tokens["account_id"],
        expires_at=expires_at,
    )


def read_codex_file_credential(codex_home: Path) -> Credential:
    """Read one stable owner-controlled ChatGPT OAuth snapshot from Codex CLI storage."""
    try:
        return _parse_auth_document(_read_bounded_auth(codex_home))
    except (
        OSError,
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        pass
    raise _fail() from None


def _send_json(process: subprocess.Popen[bytes], document: dict[str, Any]) -> None:
    if process.stdin is None:
        raise ValueError
    encoded = json.dumps(document, separators=(",", ":")).encode() + b"\n"
    process.stdin.write(encoded)
    process.stdin.flush()


def _read_response(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    buffers: dict[str, bytearray],
    *,
    wanted_id: int,
    deadline: float,
    message_count: list[int],
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        for key, _events in selector.select(remaining):
            chunk = os.read(key.fd, 8192)
            if not chunk:
                selector.unregister(key.fd)
                continue
            name = key.data
            buffer = buffers[name]
            buffer.extend(chunk)
            if len(buffer) > _MAX_APP_SERVER_BYTES:
                raise ValueError
            if name != "stdout":
                continue
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                if not line or len(line) > _MAX_APP_SERVER_BYTES:
                    raise ValueError
                message_count[0] += 1
                if message_count[0] > _MAX_APP_SERVER_MESSAGES:
                    raise ValueError
                value = json.loads(
                    line.decode("utf-8", errors="strict"),
                    object_pairs_hook=_reject_duplicate_keys,
                )
                if type(value) is not dict:
                    raise ValueError
                rendered = json.dumps(value, separators=(",", ":"))
                for forbidden in ("access_token", "refresh_token", "id_token"):
                    if forbidden in rendered:
                        raise ValueError
                response_id = value.get("id")
                if response_id is None:
                    continue
                if type(response_id) is not int or response_id != wanted_id:
                    raise ValueError
                return value
        if process.poll() is not None and not selector.get_map():
            break
    raise ValueError


def _terminate_app_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _refresh_codex_login(codex_path: Path, codex_home: Path) -> None:
    if not _safe_absolute_path(codex_path) or not _safe_absolute_path(codex_home):
        raise ValueError
    executable = codex_path.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
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
            env={
                "CODEX_HOME": str(codex_home),
                "HOME": str(Path.home()),
                "PATH": os.defpath,
            },
        )
        if process.stdout is None or process.stderr is None:
            raise ValueError
        selector = selectors.DefaultSelector()
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ, name)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        message_count = [0]
        deadline = time.monotonic() + _REFRESH_DEADLINE_SECONDS
        _send_json(
            process,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex_openai_bridge",
                        "title": "codex-openai-bridge",
                        "version": __version__,
                    }
                },
            },
        )
        initialized = _read_response(
            process,
            selector,
            buffers,
            wanted_id=0,
            deadline=deadline,
            message_count=message_count,
        )
        if "error" in initialized or type(initialized.get("result")) is not dict:
            raise ValueError
        _send_json(process, {"method": "initialized", "params": {}})
        _send_json(
            process,
            {"method": "account/read", "id": 1, "params": {"refreshToken": True}},
        )
        response = _read_response(
            process,
            selector,
            buffers,
            wanted_id=1,
            deadline=deadline,
            message_count=message_count,
        )
        result = response.get("result")
        if "error" in response or type(result) is not dict:
            raise ValueError
        account = result.get("account")
        if (
            type(account) is not dict
            or account.get("type") != "chatgpt"
            or result.get("requiresOpenaiAuth") is not True
        ):
            raise ValueError
    finally:
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass
        if process is not None:
            _terminate_app_server(process)


def resolve_codex_cli_credential(
    codex_path: Path,
    codex_home: Path,
    *,
    force_refresh: bool = False,
) -> Credential:
    """Resolve one official Codex CLI ChatGPT credential, refreshing when required."""
    try:
        if type(force_refresh) is not bool:
            raise ValueError
        before = read_codex_file_credential(codex_home)
        if not force_refresh and before.expires_at > time.time() + 60:
            return before
        _refresh_codex_login(codex_path, codex_home)
        after = read_codex_file_credential(codex_home)
        if before.account_id is None or after.account_id != before.account_id:
            raise ValueError
        return after
    except (
        CodexCredentialError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        pass
    raise _fail() from None
