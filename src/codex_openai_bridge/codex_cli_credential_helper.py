"""Isolated protocol adapter for official Codex CLI ChatGPT credentials."""

from __future__ import annotations

import json
import os
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from codex_openai_bridge.codex_cli_auth import resolve_codex_cli_credential

_ERROR_LINE = "credential helper failed"


def _path_from_env(name: str) -> Path:
    raw = os.environ.get(name)
    if not raw or any(unicodedata.category(character) == "Cc" for character in raw):
        raise ValueError
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError
    return path


def _parse_force_refresh(argv: Sequence[str]) -> bool:
    if list(argv) == []:
        return False
    if list(argv) == ["--force-refresh"]:
        return True
    raise ValueError


def main(argv: Sequence[str] | None = None) -> int:
    """Resolve credentials and emit exactly one bounded protocol document."""
    try:
        force_refresh = _parse_force_refresh(sys.argv[1:] if argv is None else argv)
        credential = resolve_codex_cli_credential(
            _path_from_env("CODEX_BRIDGE_CODEX_PATH"),
            _path_from_env("CODEX_BRIDGE_CODEX_HOME"),
            force_refresh=force_refresh,
        )
        document = {
            "version": 1,
            "access_token": credential.access_token,
            "base_url": credential.base_url,
            "account_id": credential.account_id,
            "expires_at": credential.expires_at,
        }
        sys.stdout.write(json.dumps(document, separators=(",", ":")) + "\n")
        return 0
    except BaseException:
        sys.stderr.write(_ERROR_LINE + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
