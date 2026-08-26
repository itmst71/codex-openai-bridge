from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codex_openai_bridge.auth import Credential, CredentialUnavailable, run_credential_helper
from codex_openai_bridge.codex_cli_auth import (
    CANONICAL_CODEX_BASE_URL,
    CodexCredentialError,
    read_codex_file_credential,
    resolve_codex_cli_credential,
)
from codex_openai_bridge.config import Settings


def _jwt(account_id: str, *, exp: int = 4_102_444_800) -> str:
    payload = {
        "exp": exp,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(
        b"="
    )
    return f"header.{encoded.decode()}.signature"


def _document(account_id: str = "acct-synthetic") -> dict[str, object]:
    return {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt(account_id),
            "access_token": _jwt(account_id),
            "refresh_token": "refresh-synthetic",
            "account_id": account_id,
        },
        "last_refresh": "2026-08-26T00:00:00Z",
    }


def _write_auth(codex_home: Path, document: dict[str, object] | None = None) -> Path:
    codex_home.mkdir(mode=0o700)
    path = codex_home / "auth.json"
    path.write_text(json.dumps(_document() if document is None else document), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_reads_exact_owner_file_mode_chatgpt_credential(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    _write_auth(codex_home)

    credential = read_codex_file_credential(codex_home)

    assert credential.access_token == _jwt("acct-synthetic")
    assert credential.account_id == "acct-synthetic"
    assert credential.expires_at == 4_102_444_800
    assert credential.base_url == CANONICAL_CODEX_BASE_URL


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("OPENAI_API_KEY", "sk-synthetic"),
        lambda value: value.__setitem__("extra", True),
        lambda value: value["tokens"].__setitem__("extra", True),
        lambda value: value["tokens"].__setitem__("account_id", "acct-other"),
    ],
)
def test_rejects_api_key_extra_fields_and_account_mismatch(
    tmp_path: Path,
    mutation: object,
) -> None:
    codex_home = tmp_path / "codex-home"
    document = _document()
    mutation(document)  # type: ignore[operator]
    _write_auth(codex_home, document)

    with pytest.raises(CodexCredentialError, match="credentials are unavailable"):
        read_codex_file_credential(codex_home)


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode"])
def test_rejects_unsafe_auth_file_authority(tmp_path: Path, unsafe: str) -> None:
    codex_home = tmp_path / "codex-home"
    auth = _write_auth(codex_home)
    if unsafe == "symlink":
        target = tmp_path / "target.json"
        target.write_bytes(auth.read_bytes())
        target.chmod(0o600)
        auth.unlink()
        auth.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(auth, tmp_path / "alias.json")
    else:
        auth.chmod(0o640)

    with pytest.raises(CodexCredentialError, match="credentials are unavailable"):
        read_codex_file_credential(codex_home)


def test_rejects_duplicate_keys_and_oversized_file(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    auth = codex_home / "auth.json"
    auth.write_text('{"OPENAI_API_KEY":null,"OPENAI_API_KEY":null}', encoding="utf-8")
    auth.chmod(0o600)
    with pytest.raises(CodexCredentialError):
        read_codex_file_credential(codex_home)

    auth.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(CodexCredentialError):
        read_codex_file_credential(codex_home)


def test_fifo_auth_path_is_rejected_without_blocking(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    os.mkfifo(codex_home / "auth.json", mode=0o600)
    source = """
import sys
from pathlib import Path
from codex_openai_bridge.codex_cli_auth import CodexCredentialError, read_codex_file_credential
try:
    read_codex_file_credential(Path(sys.argv[1]))
except CodexCredentialError:
    raise SystemExit(0)
raise SystemExit(2)
"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", source, str(codex_home)],
        capture_output=True,
        timeout=1,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def _fake_codex(
    tmp_path: Path,
    *,
    refreshed_account: str = "acct-synthetic",
    account_type: str = "chatgpt",
    notification: dict[str, object] | None = None,
) -> Path:
    script = tmp_path / "codex"
    refreshed = _document(refreshed_account)
    refreshed_tokens = refreshed["tokens"]
    assert type(refreshed_tokens) is dict
    refreshed_tokens["access_token"] = _jwt(refreshed_account, exp=4_102_444_900)
    source = f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

assert sys.argv[1:] == [
    "app-server", "--stdio", "--strict-config", "-c", 'cli_auth_credentials_store="file"'
]
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        assert request["params"]["clientInfo"]["version"] == "0.1.0"
        if {notification is not None!r}:
            print({json.dumps(json.dumps(notification))}, flush=True)
        print(json.dumps({{"id": 0, "result": {{"userAgent": "fake"}}}}), flush=True)
    elif request.get("method") == "account/read":
        if request["params"].get("refreshToken"):
            auth = Path(os.environ["CODEX_HOME"]) / "auth.json"
            auth.write_text({json.dumps(json.dumps(refreshed))}, encoding="utf-8")
            auth.chmod(0o600)
        print(json.dumps({{
            "id": request["id"],
            "result": {{
                "account": {{"type": {account_type!r}}},
                "requiresOpenaiAuth": True,
            }},
        }}), flush=True)
"""
    script.write_text(source, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_fresh_credential_does_not_spawn_codex(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    _write_auth(codex_home)

    credential = resolve_codex_cli_credential(tmp_path / "missing-codex", codex_home)

    assert credential.account_id == "acct-synthetic"


def test_force_refresh_uses_official_app_server_and_preserves_account(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    _write_auth(codex_home)
    codex = _fake_codex(tmp_path)

    credential = resolve_codex_cli_credential(codex, codex_home, force_refresh=True)

    assert credential.account_id == "acct-synthetic"
    assert credential.expires_at == 4_102_444_900


@pytest.mark.parametrize(
    ("account", "account_type"),
    [("acct-other", "chatgpt"), ("acct-synthetic", "apiKey")],
)
def test_refresh_rejects_account_swap_and_non_chatgpt_mode(
    tmp_path: Path,
    account: str,
    account_type: str,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_auth(codex_home)
    codex = _fake_codex(tmp_path, refreshed_account=account, account_type=account_type)

    with pytest.raises(CodexCredentialError, match="credentials are unavailable"):
        resolve_codex_cli_credential(codex, codex_home, force_refresh=True)


def test_refresh_rejects_token_bearing_notification(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    _write_auth(codex_home)
    codex = _fake_codex(
        tmp_path,
        notification={"method": "account/updated", "params": {"access_token": "synthetic"}},
    )

    with pytest.raises(CodexCredentialError, match="credentials are unavailable"):
        resolve_codex_cli_credential(codex, codex_home, force_refresh=True)


def test_outer_helper_timeout_reaps_nested_app_server_process(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    expired = _document()
    tokens = expired["tokens"]
    assert type(tokens) is dict
    tokens["access_token"] = _jwt("acct-synthetic", exp=1)
    _write_auth(codex_home, expired)
    pid_path = tmp_path / "app-server.pid"
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import os,signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    settings = replace(
        Settings.from_env(),
        codex_path=codex,
        codex_home=codex_home,
        helper_deadline_seconds=0.5,
    )
    pid: int | None = None

    try:
        with pytest.raises(CredentialUnavailable, match="credentials are unavailable"):
            run_credential_helper(settings)
        pid = int(pid_path.read_text())
        deadline = time.monotonic() + 1
        while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not Path(f"/proc/{pid}").exists()
    finally:
        if pid is None and pid_path.exists():
            pid = int(pid_path.read_text())
        if pid is not None and Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("argv,forced", [([], False), (["--force-refresh"], True)])
def test_helper_emits_exact_protocol_and_fixed_failure_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    argv: list[str],
    forced: bool,
) -> None:
    from codex_openai_bridge import codex_cli_credential_helper

    calls: list[tuple[Path, Path, bool]] = []
    codex = tmp_path / "codex"
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_BRIDGE_CODEX_PATH", str(codex))
    monkeypatch.setenv("CODEX_BRIDGE_CODEX_HOME", str(codex_home))

    def fake_resolve(path: Path, home: Path, *, force_refresh: bool) -> Credential:
        calls.append((path, home, force_refresh))
        return Credential(
            "synthetic-token", CANONICAL_CODEX_BASE_URL, "acct-synthetic", 4_102_444_800
        )

    monkeypatch.setattr(codex_cli_credential_helper, "resolve_codex_cli_credential", fake_resolve)

    assert codex_cli_credential_helper.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "version": 1,
        "access_token": "synthetic-token",
        "base_url": CANONICAL_CODEX_BASE_URL,
        "account_id": "acct-synthetic",
        "expires_at": 4_102_444_800,
    }
    assert calls == [(codex, codex_home, forced)]

    assert codex_cli_credential_helper.main(["--unknown"]) == 1
    failed = capsys.readouterr()
    assert failed.out == ""
    assert failed.err == "credential helper failed\n"
