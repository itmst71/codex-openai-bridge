"""Minimal isolated protocol adapter for Hermes Codex credentials."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

_ERROR_LINE = "credential helper failed"
_AUTH_CLAIM = "https://api.openai.com/auth"
_MAX_TOKEN_BYTES = 64 * 1024
_MAX_ENCODED_PAYLOAD_BYTES = 16 * 1024
_MAX_DECODED_PAYLOAD_BYTES = 12 * 1024


def _metadata_error() -> ValueError:
    return ValueError("credential metadata is unavailable")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _metadata_error()
        value[key] = item
    return value


def _extract_jwt_metadata(access_token: str) -> tuple[int | None, str | None]:
    if type(access_token) is not str or not access_token:
        raise _metadata_error()
    if len(access_token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise _metadata_error()
    segments = access_token.split(".")
    if len(segments) != 3:
        raise _metadata_error()
    encoded_payload = segments[1].encode("ascii")
    if not encoded_payload or len(encoded_payload) > _MAX_ENCODED_PAYLOAD_BYTES:
        raise _metadata_error()
    padding = b"=" * (-len(encoded_payload) % 4)
    decoded_payload = base64.b64decode(
        encoded_payload + padding,
        altchars=b"-_",
        validate=True,
    )
    if len(decoded_payload) > _MAX_DECODED_PAYLOAD_BYTES:
        raise _metadata_error()
    document = decoded_payload.decode("utf-8", errors="strict")
    payload: Any = json.loads(document, object_pairs_hook=_reject_duplicate_keys)
    if type(payload) is not dict:
        raise _metadata_error()

    expires_at = payload.get("exp")
    if expires_at is not None and (type(expires_at) is not int or expires_at <= 0):
        raise _metadata_error()

    account_id: Any = None
    auth_claim = payload.get(_AUTH_CLAIM)
    if auth_claim is not None:
        if type(auth_claim) is not dict:
            raise _metadata_error()
        account_id = auth_claim.get("chatgpt_account_id")
        if account_id is not None and (type(account_id) is not str or not account_id):
            raise _metadata_error()
    return expires_at, account_id


def extract_jwt_metadata(access_token: str) -> tuple[int | None, str | None]:
    """Read bounded, unverified metadata from a JWT payload."""
    try:
        return _extract_jwt_metadata(access_token)
    except (UnicodeError, binascii.Error, json.JSONDecodeError):
        pass
    raise _metadata_error()


def _parse_force_refresh(argv: Sequence[str]) -> bool:
    if list(argv) == []:
        return False
    if list(argv) == ["--force-refresh"]:
        return True
    raise ValueError


def _resolved_value(resolved: object, name: str) -> Any:
    if isinstance(resolved, Mapping):
        return resolved.get(name)
    return getattr(resolved, name)


def _build_protocol(resolved: object) -> dict[str, object]:
    access_token = _resolved_value(resolved, "api_key")
    base_url = _resolved_value(resolved, "base_url")
    if type(access_token) is not str or not access_token:
        raise ValueError
    if type(base_url) is not str or not base_url:
        raise ValueError
    expires_at, account_id = extract_jwt_metadata(access_token)
    if expires_at is None:
        raise ValueError
    return {
        "version": 1,
        "access_token": access_token,
        "base_url": base_url,
        "account_id": account_id,
        "expires_at": expires_at,
    }


@contextlib.contextmanager
def _hermes_source_import_path() -> Iterator[None]:
    source_root = str(Path(sys.prefix).resolve(strict=True).parent)
    helper_directory = str(Path(__file__).resolve(strict=True).parent)
    original_path = list(sys.path)
    sys.path[:] = [
        source_root,
        *(entry for entry in original_path if entry not in (source_root, helper_directory)),
    ]
    try:
        yield
    finally:
        sys.path[:] = original_path


def main(argv: Sequence[str] | None = None) -> int:
    """Resolve credentials and emit exactly one bounded protocol document."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        force_refresh = _parse_force_refresh(arguments)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                with _hermes_source_import_path():
                    from hermes_cli.auth import (  # type: ignore[import-not-found]
                        resolve_codex_runtime_credentials,
                    )

                    resolver: Callable[..., object] = resolve_codex_runtime_credentials
                    resolved = resolver(force_refresh=force_refresh)
        protocol = _build_protocol(resolved)
        sys.stdout.write(json.dumps(protocol, separators=(",", ":")) + "\n")
        return 0
    except BaseException:
        sys.stderr.write(_ERROR_LINE + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
