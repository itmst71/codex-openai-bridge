import hmac
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_openai_bridge.security import (
    TokenConfigurationError,
    bearer_is_authorized,
    load_bridge_token,
)


def _write_token(path: Path, token: str = "a" * 43) -> None:
    _write_raw_token(path, (token + "\n").encode("ascii"))


def _write_raw_token(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def test_accepts_only_exact_bearer_header() -> None:
    token = "a" * 43

    assert bearer_is_authorized(f"Bearer {token}", token) is True
    assert bearer_is_authorized(f"Bearer {'b' * 43}", token) is False


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "bearer " + "a" * 43,
        "Basic " + "a" * 43,
        "Bearer  " + "a" * 43,
        "Bearer " + "a" * 43 + " ",
        7,
    ],
)
def test_rejects_missing_or_malformed_bearer_header(authorization: object) -> None:
    assert bearer_is_authorized(authorization, "a" * 43) is False


def test_uses_constant_time_comparison_for_valid_token_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def compare_digest(left: str, right: str) -> bool:
        calls.append((left, right))
        return False

    monkeypatch.setattr(hmac, "compare_digest", compare_digest)

    assert bearer_is_authorized("Bearer " + "b" * 43, "a" * 43) is False
    assert calls == [("b" * 43, "a" * 43)]


def test_loads_secure_external_bridge_token(tmp_path: Path) -> None:
    token_file = tmp_path / "bridge-token"
    _write_token(token_file)

    assert load_bridge_token(token_file) == "a" * 43
    assert os.stat(token_file).st_mode & 0o777 == 0o600


def test_rejects_token_symlink_and_hardlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "bridge-token"
    _write_token(target)
    link.symlink_to(target)

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(link)

    link.unlink()
    os.link(target, link)
    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(link)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"a" * 42,
        b"a" * 44,
        b"a" * 43 + b"\n\n",
        b"a" * 42 + b"/\n",
        b"a" * 42 + b"=\n",
        b"a" * 42 + b"\xff\n",
    ],
)
def test_rejects_noncanonical_token_file_contents(tmp_path: Path, raw: bytes) -> None:
    token_file = tmp_path / "bridge-token"
    _write_raw_token(token_file, raw)

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(token_file)


def test_rejects_token_with_non_urlsafe_character(tmp_path: Path) -> None:
    token_file = tmp_path / "bridge-token"
    _write_token(token_file, "a" * 42 + " ")

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(token_file)


def test_rejects_token_with_wrong_length(tmp_path: Path) -> None:
    token_file = tmp_path / "bridge-token"
    _write_token(token_file, "a" * 42)

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(token_file)


def test_rejects_real_fifo_without_blocking_startup(tmp_path: Path) -> None:
    fifo = tmp_path / "bridge-token"
    os.mkfifo(fifo, mode=0o600)
    source = """
import sys
from pathlib import Path
from codex_openai_bridge.security import TokenConfigurationError, load_bridge_token
try:
    load_bridge_token(Path(sys.argv[1]))
except TokenConfigurationError:
    raise SystemExit(0)
raise SystemExit(1)
"""

    completed = subprocess.run(
        [sys.executable, "-c", source, str(fifo)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=0.5,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_rejects_non_regular_token_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "bridge-token"
    _write_token(token_file)
    real_metadata = os.stat(token_file)
    fields = list(real_metadata)
    fields[0] = 0o040000 | 0o600
    non_regular_metadata = os.stat_result(fields)
    monkeypatch.setattr(os, "fstat", lambda descriptor: non_regular_metadata)

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(token_file)


def test_rejects_token_file_not_owned_by_service_uid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "bridge-token"
    _write_token(token_file)
    real_metadata = os.stat(token_file)
    fields = list(real_metadata)
    fields[4] = os.geteuid() + 1
    foreign_metadata = os.stat_result(fields)
    monkeypatch.setattr(os, "fstat", lambda descriptor: foreign_metadata)

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(token_file)


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o604, 0o700])
def test_rejects_token_file_without_exact_0600_permissions(
    tmp_path: Path,
    mode: int,
) -> None:
    token_file = tmp_path / "bridge-token"
    _write_token(token_file)
    token_file.chmod(mode)

    with pytest.raises(TokenConfigurationError, match="unavailable"):
        load_bridge_token(token_file)
