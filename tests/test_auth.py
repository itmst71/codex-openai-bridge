"""Hermes credential helper boundary tests."""

import asyncio
import base64
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

import codex_openai_bridge.auth as auth_module
from codex_openai_bridge import hermes_credential_helper
from codex_openai_bridge.auth import (
    Credential,
    CredentialManager,
    CredentialUnavailable,
    parse_credential_output,
    run_credential_helper,
)
from codex_openai_bridge.config import Settings


def _settings(tmp_path: Path, helper_source: str) -> Settings:
    helper_path = tmp_path / "credential_helper.py"
    helper_path.write_text(helper_source)
    return replace(
        Settings.from_env(),
        hermes_python_path=Path(sys.executable),
        helper_path=helper_path,
        helper_deadline_seconds=2.0,
    )


SUCCESS_SOURCE = """
import json
print(json.dumps({
    "version": 1,
    "access_token": "token-value",
    "base_url": "https://example.invalid",
    "account_id": None,
    "expires_at": 4102444800,
}))
"""


def _dual_stream_source(stdout_count: int, stderr_count: int) -> str:
    return f"""
import fcntl
import json
import os

for descriptor in (1, 2):
    fcntl.fcntl(descriptor, fcntl.F_SETPIPE_SZ, 4096)
document = json.dumps({{
    "version": 1,
    "access_token": "t",
    "base_url": "u",
    "account_id": None,
    "expires_at": 4102444800,
}}, separators=(",", ":")).encode()
stdout = document + b" " * ({stdout_count} - len(document))
stderr = b"e" * {stderr_count}
positions = [0, 0]
while positions[0] < len(stdout) or positions[1] < len(stderr):
    if positions[0] < len(stdout):
        end = min(positions[0] + 2048, len(stdout))
        os.write(1, stdout[positions[0]:end])
        positions[0] = end
    if positions[1] < len(stderr):
        end = min(positions[1] + 2048, len(stderr))
        os.write(2, stderr[positions[1]:end])
        positions[1] = end
"""


def _pid_is_active(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return False
    return state != "Z"


def _safety_kill(pids: list[int]) -> None:
    for pid in pids:
        if _pid_is_active(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _test_to_thread[T](function: Callable[..., T], /, *args: object, **kwargs: object) -> T:
    await asyncio.sleep(0)
    return function(*args, **kwargs)


def test_credential_api_is_frozen_and_strict_success_is_parsed() -> None:
    raw = (
        b'{"version":1,"access_token":"token-value","base_url":"https://example.invalid",'
        b'"account_id":"acct-1","expires_at":4102444800}'
    )

    credential = parse_credential_output(raw)

    assert type(credential) is Credential
    assert credential == Credential(
        access_token="token-value",
        base_url="https://example.invalid",
        account_id="acct-1",
        expires_at=4_102_444_800,
    )
    with pytest.raises(FrozenInstanceError):
        credential.access_token = "changed"  # type: ignore[misc]
    assert issubclass(CredentialUnavailable, RuntimeError)


@pytest.mark.parametrize(
    ("force_refresh", "suffix"),
    [(False, []), (True, ["--force-refresh"])],
)
def test_runner_uses_exact_argv_and_isolated_pipes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    force_refresh: bool,
    suffix: list[str],
) -> None:
    settings = _settings(tmp_path, SUCCESS_SOURCE)
    real_popen = subprocess.Popen
    calls: list[tuple[list[str | os.PathLike[str]], dict[str, Any]]] = []

    def spy_popen(argv: list[str | os.PathLike[str]], **kwargs: Any) -> subprocess.Popen[bytes]:
        calls.append((argv, kwargs))
        return cast("subprocess.Popen[bytes]", real_popen(argv, **kwargs))

    monkeypatch.setattr(subprocess, "Popen", spy_popen)

    assert (
        run_credential_helper(settings, force_refresh=force_refresh).access_token == "token-value"
    )
    assert calls == [
        (
            [settings.hermes_python_path, settings.helper_path, *suffix],
            {
                "shell": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "close_fds": True,
                "start_new_session": True,
            },
        )
    ]


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"{} {}",
        b"[]",
        b'{"version":1,"version":1,"access_token":"t","base_url":"u",'
        b'"account_id":null,"expires_at":1}',
        b'{"version":1,"access_token":"t","base_url":"u","account_id":null}',
        b'{"version":1,"access_token":"t","base_url":"u","account_id":null,'
        b'"expires_at":1,"extra":true}',
        b'{"version":true,"access_token":"t","base_url":"u","account_id":null,"expires_at":1}',
        b'{"version":1,"access_token":"","base_url":"u","account_id":null,"expires_at":1}',
        b'{"version":1,"access_token":1,"base_url":"u","account_id":null,"expires_at":1}',
        b'{"version":1,"access_token":"t","base_url":"","account_id":null,"expires_at":1}',
        b'{"version":1,"access_token":"t","base_url":"u","account_id":"","expires_at":1}',
        b'{"version":1,"access_token":"t","base_url":"u","account_id":false,"expires_at":1}',
        b'{"version":1,"access_token":"t","base_url":"u","account_id":null,"expires_at":true}',
        b'{"version":1,"access_token":"t","base_url":"u","account_id":null,"expires_at":0}',
        '{"version":1,"access_token":"t","base_url":"u","account_id":null,"expires_at":1}'.encode(
            "utf-16"
        ),
    ],
)
def test_parser_rejects_malformed_duplicate_or_nonexact_schema_without_echo(raw: bytes) -> None:
    with pytest.raises(CredentialUnavailable) as raised:
        parse_credential_output(raw)

    rendered = f"{raised.value!s} {raised.value!r}"
    assert "token" not in rendered
    assert "account" not in rendered
    assert "not-json" not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_runner_rejects_nonzero_exit_without_echoing_child_data_or_path(tmp_path: Path) -> None:
    secret = "SENSITIVE_TOKEN_ACCOUNT_123"
    settings = _settings(
        tmp_path,
        f"import sys\nprint({secret!r})\nprint({secret!r}, file=sys.stderr)\nraise SystemExit(7)\n",
    )

    with pytest.raises(CredentialUnavailable) as raised:
        run_credential_helper(settings)

    rendered = f"{raised.value!s} {raised.value!r}"
    assert secret not in rendered
    assert str(settings.helper_path) not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "change,force_refresh",
    [
        ({"helper_path": Path("relative-helper.py")}, False),
        ({"helper_path": Path("/tmp/helper\n.py")}, False),
        ({"max_helper_stdout_bytes": True}, False),
        ({"max_helper_stdout_bytes": 64 * 1024 + 1}, False),
        ({"max_helper_stderr_bytes": 0}, False),
        ({"helper_deadline_seconds": float("inf")}, False),
        ({"helper_deadline_seconds": 0.09}, False),
        ({"helper_deadline_seconds": 60.1}, False),
        ({}, 1),
    ],
)
def test_invalid_runner_inputs_are_rejected_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: dict[str, object],
    force_refresh: object,
) -> None:
    settings = replace(_settings(tmp_path, SUCCESS_SOURCE), **change)  # type: ignore[arg-type]

    def forbidden_popen(*args: object, **kwargs: object) -> None:
        pytest.fail(f"spawned with {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)

    with pytest.raises(CredentialUnavailable):
        run_credential_helper(settings, force_refresh=force_refresh)  # type: ignore[arg-type]


def test_exact_stdout_and_stderr_limits_are_accepted_beyond_pipe_capacity(tmp_path: Path) -> None:
    limit = 64 * 1024
    settings = replace(
        _settings(tmp_path, _dual_stream_source(limit, limit)),
        max_helper_stdout_bytes=limit,
        max_helper_stderr_bytes=limit,
    )

    credential = run_credential_helper(settings)

    assert credential.access_token == "t"


@pytest.mark.parametrize("excess_stream", ["stdout", "stderr"])
def test_one_byte_over_either_output_limit_is_rejected_without_deadlock(
    tmp_path: Path,
    excess_stream: str,
) -> None:
    limit = 64 * 1024
    stdout_count = limit + (excess_stream == "stdout")
    stderr_count = limit + (excess_stream == "stderr")
    settings = replace(
        _settings(tmp_path, _dual_stream_source(stdout_count, stderr_count)),
        max_helper_stdout_bytes=limit,
        max_helper_stderr_bytes=limit,
    )

    with pytest.raises(CredentialUnavailable):
        run_credential_helper(settings)


def test_timeout_kills_and_reaps_process_group_with_sigterm_ignoring_child(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "pids"
    child_source = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    source = f"""
import os
import signal
import subprocess
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", {child_source!r}])
with open({str(pid_path)!r}, "w") as output:
    output.write(f"{{os.getpid()}} {{child.pid}}")
time.sleep(60)
"""
    settings = replace(
        _settings(tmp_path, source),
        helper_deadline_seconds=1.0,
    )
    pids: list[int] = []

    try:
        started = time.monotonic()
        with pytest.raises(CredentialUnavailable):
            run_credential_helper(settings)
        elapsed = time.monotonic() - started
        pids = [int(raw) for raw in pid_path.read_text().split()]

        assert elapsed < 2.0
        deadline = time.monotonic() + 1.0
        while any(_pid_is_active(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(_pid_is_active(pid) for pid in pids)
    finally:
        if pid_path.exists() and not pids:
            pids = [int(raw) for raw in pid_path.read_text().split()]
        _safety_kill(pids)


def test_descendant_held_pipes_cannot_extend_deadline(tmp_path: Path) -> None:
    pid_path = tmp_path / "descendant-pid"
    child_source = "import time;time.sleep(60)"
    source = f"""
import json
import subprocess
import sys

child = subprocess.Popen([sys.executable, "-c", {child_source!r}])
with open({str(pid_path)!r}, "w") as output:
    output.write(str(child.pid))
print(json.dumps({{
    "version": 1,
    "access_token": "t",
    "base_url": "u",
    "account_id": None,
    "expires_at": 4102444800,
}}), flush=True)
"""
    settings = replace(_settings(tmp_path, source), helper_deadline_seconds=1.0)
    child_pid: int | None = None

    try:
        started = time.monotonic()
        with pytest.raises(CredentialUnavailable):
            run_credential_helper(settings)
        elapsed = time.monotonic() - started
        child_pid = int(pid_path.read_text())

        assert elapsed < 2.0
        deadline = time.monotonic() + 1.0
        while _pid_is_active(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _pid_is_active(child_pid)
    finally:
        if child_pid is None and pid_path.exists():
            child_pid = int(pid_path.read_text())
        _safety_kill([] if child_pid is None else [child_pid])


def test_base_exception_cleanup_does_not_mask_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_selector = selectors.DefaultSelector
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    class InterruptingSelector:
        def __init__(self) -> None:
            self.delegate = real_selector()

        def register(self, *args: object, **kwargs: object) -> object:
            return self.delegate.register(*args, **kwargs)  # type: ignore[arg-type]

        def get_map(self) -> object:
            return self.delegate.get_map()

        def select(self, timeout: float | None = None) -> list[object]:
            del timeout
            raise KeyboardInterrupt

        def unregister(self, fileobj: object) -> object:
            return self.delegate.unregister(fileobj)  # type: ignore[arg-type]

        def close(self) -> None:
            self.delegate.close()
            raise RuntimeError("cleanup failed")

    def spy_popen(argv: list[str | os.PathLike[str]], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", real_popen(argv, **kwargs))
        processes.append(process)
        return process

    monkeypatch.setattr(selectors, "DefaultSelector", InterruptingSelector)
    monkeypatch.setattr(subprocess, "Popen", spy_popen)

    try:
        with pytest.raises(KeyboardInterrupt):
            run_credential_helper(_settings(tmp_path, "import time; time.sleep(60)"))
        assert len(processes) == 1
        assert processes[0].poll() is not None
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)


def test_process_group_cleanup_failure_does_not_mask_primary_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def spy_popen(argv: list[str | os.PathLike[str]], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = cast("subprocess.Popen[bytes]", real_popen(argv, **kwargs))
        processes.append(process)
        return process

    def interrupting_read(*args: object, **kwargs: object) -> tuple[bytes, bytes]:
        del args, kwargs
        raise KeyboardInterrupt

    def broken_cleanup(process: subprocess.Popen[bytes]) -> None:
        del process
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    monkeypatch.setattr(auth_module, "_read_bounded_output", interrupting_read)
    monkeypatch.setattr(auth_module, "_terminate_process_group", broken_cleanup)

    try:
        with pytest.raises(KeyboardInterrupt):
            run_credential_helper(_settings(tmp_path, "import time; time.sleep(60)"))
        assert len(processes) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)


def test_helper_deadline_includes_process_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_popen = subprocess.Popen

    def delayed_popen(argv: list[str | os.PathLike[str]], **kwargs: Any) -> subprocess.Popen[bytes]:
        time.sleep(0.15)
        return cast("subprocess.Popen[bytes]", real_popen(argv, **kwargs))

    monkeypatch.setattr(subprocess, "Popen", delayed_popen)
    settings = replace(
        _settings(tmp_path, SUCCESS_SOURCE),
        helper_deadline_seconds=0.1,
    )

    with pytest.raises(CredentialUnavailable):
        run_credential_helper(settings)


@pytest.mark.asyncio
async def test_manager_caches_fresh_credentials_and_force_refresh_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(asyncio, "to_thread", _test_to_thread)

    def fake_runner(settings: Settings, *, force_refresh: bool = False) -> Credential:
        del settings
        calls.append(force_refresh)
        return Credential("t", "u", None, int(time.time()) + 3600)

    monkeypatch.setattr(auth_module, "run_credential_helper", fake_runner)
    manager = CredentialManager(_settings(tmp_path, SUCCESS_SOURCE))

    first = await manager.get_credentials()
    second = await manager.get_credentials()
    forced = await manager.get_credentials(force_refresh=True)

    assert first is second
    assert forced.access_token == "t"
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_manager_treats_credentials_inside_conservative_skew_as_expired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    monkeypatch.setattr(asyncio, "to_thread", _test_to_thread)

    def fake_runner(settings: Settings, *, force_refresh: bool = False) -> Credential:
        nonlocal calls
        del settings, force_refresh
        calls += 1
        return Credential(f"t-{calls}", "u", None, int(time.time()) + 30)

    monkeypatch.setattr(auth_module, "run_credential_helper", fake_runner)
    manager = CredentialManager(_settings(tmp_path, SUCCESS_SOURCE))

    assert (await manager.get_credentials()).access_token == "t-1"
    assert (await manager.get_credentials()).access_token == "t-2"


@pytest.mark.asyncio
async def test_manager_single_flight_shares_one_helper_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    monkeypatch.setattr(asyncio, "to_thread", _test_to_thread)

    def fake_runner(settings: Settings, *, force_refresh: bool = False) -> Credential:
        nonlocal calls
        del settings, force_refresh
        calls += 1
        time.sleep(0.05)
        return Credential("shared", "u", None, int(time.time()) + 3600)

    monkeypatch.setattr(auth_module, "run_credential_helper", fake_runner)
    manager = CredentialManager(_settings(tmp_path, SUCCESS_SOURCE))

    results = await asyncio.gather(*(manager.get_credentials() for _ in range(20)))

    assert calls == 1
    assert all(result is results[0] for result in results)


@pytest.mark.asyncio
async def test_manager_failure_does_not_poison_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0
    monkeypatch.setattr(asyncio, "to_thread", _test_to_thread)

    def fake_runner(settings: Settings, *, force_refresh: bool = False) -> Credential:
        nonlocal calls
        del settings, force_refresh
        calls += 1
        if calls == 1:
            raise CredentialUnavailable("credentials are unavailable")
        return Credential("retry", "u", None, int(time.time()) + 3600)

    monkeypatch.setattr(auth_module, "run_credential_helper", fake_runner)
    manager = CredentialManager(_settings(tmp_path, SUCCESS_SOURCE))

    with pytest.raises(CredentialUnavailable):
        await manager.get_credentials()
    assert (await manager.get_credentials()).access_token == "retry"
    assert calls == 2


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(
        b"="
    )
    return f"header.{encoded.decode()}.signature"


def _install_fake_hermes(
    monkeypatch: pytest.MonkeyPatch,
    resolver: Callable[..., object],
) -> None:
    package = ModuleType("hermes_cli")
    package.__path__ = []
    auth = ModuleType("hermes_cli.auth")
    auth.resolve_codex_runtime_credentials = resolver  # type: ignore[attr-defined]
    package.auth = auth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)


def test_helper_loads_hermes_source_from_its_venv_prefix_without_ambient_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    hermes_root = tmp_path / "hermes-agent"
    venv = hermes_root / "venv"
    package = hermes_root / "hermes_cli"
    helper_package = tmp_path / "bridge-package"
    venv.mkdir(parents=True)
    package.mkdir()
    helper_package.mkdir()
    (helper_package / "hermes_credential_helper.py").write_text("", encoding="utf-8")
    (helper_package / "collision_probe.py").write_text(
        "ORIGIN = 'bridge sibling'\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    token = _jwt(
        {
            "exp": 4_102_444_800,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-live-shape"},
        }
    )
    (package / "auth.py").write_text(
        "try:\n"
        "    import collision_probe\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
        "else:\n"
        "    raise RuntimeError('helper sibling import path leaked')\n"
        "def resolve_codex_runtime_credentials(*, force_refresh):\n"
        f"    return {{'api_key': {token!r}, 'base_url': 'https://example.invalid'}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(
        hermes_credential_helper,
        "__file__",
        str(helper_package / "hermes_credential_helper.py"),
    )
    monkeypatch.syspath_prepend(str(helper_package))
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli.auth", raising=False)
    original_path = list(sys.path)

    try:
        assert hermes_credential_helper.main([]) == 0
        captured = capsys.readouterr()
    finally:
        sys.modules.pop("hermes_cli.auth", None)
        sys.modules.pop("hermes_cli", None)

    assert captured.err == ""
    assert json.loads(captured.out) == {
        "version": 1,
        "access_token": token,
        "base_url": "https://example.invalid",
        "account_id": "acct-live-shape",
        "expires_at": 4_102_444_800,
    }
    assert sys.path == original_path


@pytest.mark.parametrize("argv,expected_force", [([], False), (["--force-refresh"], True)])
def test_helper_emits_only_versioned_protocol_and_calls_resolver_with_exact_bool(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected_force: bool,
) -> None:
    calls: list[bool] = []
    token = _jwt(
        {
            "exp": 4_102_444_800,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
        }
    )

    def resolver(*, force_refresh: bool) -> object:
        calls.append(force_refresh)
        print("resolver diagnostic")
        print("resolver stderr diagnostic", file=sys.stderr)
        return {"api_key": token, "base_url": "https://example.invalid"}

    _install_fake_hermes(monkeypatch, resolver)

    assert hermes_credential_helper.main(argv) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "version": 1,
        "access_token": token,
        "base_url": "https://example.invalid",
        "account_id": "acct-1",
        "expires_at": 4_102_444_800,
    }
    assert calls == [expected_force]


def test_helper_jwt_extraction_supports_absent_optional_claims() -> None:
    assert hermes_credential_helper.extract_jwt_metadata(_jwt({})) == (None, None)
    assert hermes_credential_helper.extract_jwt_metadata(_jwt({"exp": 123})) == (123, None)


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "header.%%%%.signature",
        "header." + "a" * (20 * 1024) + ".signature",
        _jwt({"exp": True}),
        _jwt({"exp": 0}),
        _jwt({"https://api.openai.com/auth": []}),
        _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": ""}}),
    ],
)
def test_helper_jwt_extraction_rejects_malformed_or_unbounded_payload(token: str) -> None:
    with pytest.raises(ValueError, match="credential metadata is unavailable"):
        hermes_credential_helper.extract_jwt_metadata(token)


@pytest.mark.parametrize("argv", [["--unknown"], ["--force-refresh", "--force-refresh"], ["x"]])
def test_helper_rejects_all_other_arguments_with_fixed_error(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    assert hermes_credential_helper.main(argv) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "credential helper failed\n"


def test_helper_failure_never_emits_resolver_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "SENSITIVE_HELPER_TOKEN_456"

    def resolver(*, force_refresh: bool) -> object:
        del force_refresh
        print(secret)
        print(secret, file=sys.stderr)
        raise RuntimeError(secret)

    _install_fake_hermes(monkeypatch, resolver)

    assert hermes_credential_helper.main([]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "credential helper failed\n"
    assert secret not in captured.err
