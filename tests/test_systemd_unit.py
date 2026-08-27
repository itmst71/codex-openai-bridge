from __future__ import annotations

import configparser
import os
import re
import secrets
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

import scripts.install_user_service as installer_module

ROOT = Path(__file__).resolve().parents[1]
UNIT_TEMPLATE = ROOT / "deploy" / "systemd" / "codex-openai-bridge.service.in"
LEGACY_UNIT = ROOT / "deploy" / "systemd" / "codex-openai-bridge.service"
README_PATH = ROOT / "README.md"
README_JA_PATH = ROOT / "README.ja.md"
PYPROJECT_PATH = ROOT / "pyproject.toml"
INSTALL_SCRIPT = ROOT / "scripts" / "install_user_service.py"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_systemd_unit.py"
FIXED_CHECKOUT = re.compile(r"(?:%h|\$HOME)/src/codex-openai-bridge")
CONCRETE_USER_HOME_PATTERNS = (
    re.compile(
        r"/home/(?!USERNAME(?=/|[^A-Za-z0-9._-]|\Z))"
        r"[A-Za-z0-9._-]+(?=/|[^A-Za-z0-9._-]|\Z)"
    ),
    re.compile(
        r"/Users/(?!USERNAME(?=/|[^A-Za-z0-9._-]|\Z))"
        r"[A-Za-z0-9._-]+(?=/|[^A-Za-z0-9._-]|\Z)"
    ),
    re.compile(
        r"[A-Z]:\\+Users\\+"
        r"(?!USERNAME(?=\\+|[^A-Za-z0-9._-]|\Z))"
        r"[A-Za-z0-9._-]+(?=\\+|[^A-Za-z0-9._-]|\Z)",
        re.IGNORECASE,
    ),
)


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _contains_concrete_user_home(text: str) -> bool:
    return any(pattern.search(text) for pattern in CONCRETE_USER_HOME_PATTERNS)


def _unit(path: Path) -> tuple[str, configparser.ConfigParser]:
    raw = path.read_text(encoding="utf-8")
    parser = _CaseSensitiveConfigParser(interpolation=None, strict=True)
    parser.read_string(raw)
    return raw, parser


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_console_script_and_user_unit_template_are_checkout_neutral() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"codex-openai-bridge": "codex_openai_bridge.cli:main"}

    assert not LEGACY_UNIT.exists()
    raw, unit = _unit(UNIT_TEMPLATE)
    assert set(unit) == {"DEFAULT", "Unit", "Service", "Install"}
    assert unit["Unit"]["Description"] == "Loopback OpenAI-compatible bridge for Codex OAuth"
    assert unit["Unit"]["Wants"] == "network-online.target"
    assert unit["Unit"]["After"] == "network-online.target"

    service = unit["Service"]
    assert service["Type"] == "simple"
    assert "WorkingDirectory" not in service
    assert service["ExecStart"] == "@BRIDGE_EXEC_START@"
    assert service["Environment"] == "@BRIDGE_ENVIRONMENT@"
    assert service["ReadWritePaths"] == "@CODEX_HOME@"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5s"
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "read-only"
    assert service["RestrictAddressFamilies"] == "AF_UNIX AF_INET AF_INET6"
    assert service["UMask"] == "0077"
    assert unit["Install"]["WantedBy"] == "default.target"

    assert FIXED_CHECKOUT.search(raw) is None
    assert "EnvironmentFile=" not in raw
    assert re.search(r"CODEX_BRIDGE_CLIENT_TOKEN\s*=", raw) is None
    for forbidden in ("Authorization:", "Bearer ", "access_token", "chatgpt_account_id"):
        assert forbidden not in raw


def test_installer_renders_arbitrary_checkout_and_never_starts_service(tmp_path: Path) -> None:
    checkout = tmp_path / "arbitrary checkout"
    bridge_python = _executable(checkout / ".venv" / "bin" / "python")
    codex_path = _executable(tmp_path / "tool dir" / "codex")
    codex_home = tmp_path / "codex state"
    codex_home.mkdir(mode=0o700)
    destination = tmp_path / "systemd user" / "codex-openai-bridge.service"
    command = [
        sys.executable,
        str(INSTALL_SCRIPT),
        "--checkout",
        str(checkout),
        "--codex-path",
        str(codex_path),
        "--codex-home",
        str(codex_home),
        "--destination",
        str(destination),
    ]

    subprocess.run(command, cwd=tmp_path, check=True, timeout=30)

    raw, unit = _unit(destination)
    service = unit["Service"]
    assert "WorkingDirectory" not in service
    assert shlex.split(service["ExecStart"]) == [
        str(bridge_python),
        "-m",
        "codex_openai_bridge",
        "serve",
    ]
    environment = shlex.split(service["Environment"])
    assert environment == [
        "CODEX_BRIDGE_HOST=127.0.0.1",
        "PYTHONPATH=",
        f"CODEX_BRIDGE_CODEX_PATH={codex_path}",
        f"CODEX_BRIDGE_CODEX_HOME={codex_home}",
    ]
    assert shlex.split(service["ReadWritePaths"]) == [str(codex_home)]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert "@" not in raw
    assert "systemctl" not in INSTALL_SCRIPT.read_text(encoding="utf-8")

    before = destination.read_bytes()
    refused = subprocess.run(command, cwd=tmp_path, capture_output=True, timeout=30, check=False)
    assert refused.returncode != 0
    assert destination.read_bytes() == before
    subprocess.run([*command, "--force"], cwd=tmp_path, check=True, timeout=30)
    assert destination.read_bytes() == before


@pytest.mark.parametrize("suffix", ["%specifier", "$variable", "\ncontrol"])
def test_installer_rejects_systemd_special_path_before_write(
    tmp_path: Path,
    suffix: str,
) -> None:
    checkout = tmp_path / f"checkout{suffix}"
    _executable(checkout / ".venv" / "bin" / "python")
    codex_path = _executable(tmp_path / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    destination = tmp_path / "user" / "codex-openai-bridge.service"

    result = subprocess.run(
        [
            sys.executable,
            str(INSTALL_SCRIPT),
            "--checkout",
            str(checkout),
            "--codex-path",
            str(codex_path),
            "--codex-home",
            str(codex_home),
            "--destination",
            str(destination),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert not destination.exists()


def test_installer_force_rejects_symlink_destination(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _executable(checkout / ".venv" / "bin" / "python")
    codex_path = _executable(tmp_path / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    target = tmp_path / "unrelated.service"
    target.write_text("unrelated\n", encoding="utf-8")
    destination = tmp_path / "user" / "codex-openai-bridge.service"
    destination.parent.mkdir(mode=0o700)
    destination.symlink_to(target)

    result = subprocess.run(
        [
            sys.executable,
            str(INSTALL_SCRIPT),
            "--checkout",
            str(checkout),
            "--codex-path",
            str(codex_path),
            "--codex-home",
            str(codex_home),
            "--destination",
            str(destination),
            "--force",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert destination.is_symlink()
    assert target.read_text(encoding="utf-8") == "unrelated\n"


def test_installer_verification_failure_preserves_existing_unit(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    _executable(checkout / ".venv" / "bin" / "python")
    codex_path = _executable(tmp_path / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    destination = tmp_path / "user" / "codex-openai-bridge.service"
    destination.parent.mkdir(mode=0o700)
    destination.write_text("existing-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    fake_bin = tmp_path / "fake-bin"
    _executable(fake_bin / "systemd-analyze")
    (fake_bin / "systemd-analyze").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(fake_bin)

    result = subprocess.run(
        [
            sys.executable,
            str(INSTALL_SCRIPT),
            "--checkout",
            str(checkout),
            "--codex-path",
            str(codex_path),
            "--codex-home",
            str(codex_home),
            "--destination",
            str(destination),
            "--force",
        ],
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert destination.read_text(encoding="utf-8") == "existing-unit\n"
    retained = list(destination.parent.glob(".*.service"))
    assert len(retained) == 1
    assert "ExecStart=" in retained[0].read_text(encoding="utf-8")
    assert str(retained[0]) in result.stderr.decode("utf-8")


def test_verifier_kills_process_before_output_can_exceed_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script = _executable(tmp_path / "systemd-analyze")
    script.write_text(
        "#!/bin/sh\nprintf '%*s' 70000 '' | tr ' ' x\nsleep 10\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(shutil, "which", lambda _name: str(script))
    unit = tmp_path / "candidate.service"
    unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    started = time.monotonic()
    descriptor = os.open(unit, os.O_RDONLY | os.O_CLOEXEC)
    try:
        with pytest.raises(installer_module.InstallError, match="verification failed"):
            installer_module._verify_unit(descriptor)
    finally:
        os.close(descriptor)
    assert time.monotonic() - started < 2.0


def test_verifier_cleans_child_after_parent_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "child.pid"
    script = _executable(tmp_path / "systemd-analyze")
    script.write_text(
        f"#!/bin/sh\nsleep 30 &\nprintf '%s' $! > {shlex.quote(str(child_pid))}\nexit 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(shutil, "which", lambda _name: str(script))
    monkeypatch.setattr(installer_module, "_VERIFY_TIMEOUT_SECONDS", 0.2)
    unit = tmp_path / "candidate.service"
    unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    descriptor = os.open(unit, os.O_RDONLY | os.O_CLOEXEC)
    try:
        with pytest.raises(installer_module.InstallError, match="verification failed"):
            installer_module._verify_unit(descriptor)
    finally:
        os.close(descriptor)

    pid = int(child_pid.read_text(encoding="ascii"))
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("verifier child process survived cleanup")


def test_verifier_cleanup_survives_selector_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    leader_pid = tmp_path / "leader.pid"
    script = _executable(tmp_path / "systemd-analyze")
    script.write_text(
        f"#!/bin/sh\nprintf '%s' $$ > {shlex.quote(str(leader_pid))}\nsleep 30\n",
        encoding="utf-8",
    )
    real_selector = selectors.DefaultSelector

    class FailingCloseSelector:
        def __init__(self) -> None:
            self._delegate = real_selector()

        def register(self, *args: Any, **kwargs: Any) -> Any:
            return self._delegate.register(*args, **kwargs)

        def unregister(self, *args: Any, **kwargs: Any) -> Any:
            return self._delegate.unregister(*args, **kwargs)

        def select(self, *args: Any, **kwargs: Any) -> Any:
            return self._delegate.select(*args, **kwargs)

        def get_map(self) -> Any:
            return self._delegate.get_map()

        def close(self) -> None:
            self._delegate.close()
            raise OSError("injected selector close failure")

    monkeypatch.setattr(shutil, "which", lambda _name: str(script))
    monkeypatch.setattr(selectors, "DefaultSelector", FailingCloseSelector)
    monkeypatch.setattr(installer_module, "_VERIFY_TIMEOUT_SECONDS", 0.1)
    unit = tmp_path / "candidate.service"
    unit.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
    descriptor = os.open(unit, os.O_RDONLY | os.O_CLOEXEC)
    try:
        with pytest.raises(installer_module.InstallError, match="verification failed"):
            installer_module._verify_unit(descriptor)
    finally:
        os.close(descriptor)

    pid = int(leader_pid.read_text(encoding="ascii"))
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        os.kill(pid, 9)
        pytest.fail("verifier leader survived selector close failure")


def test_force_replacement_rejects_destination_identity_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    original = destination.stat()
    replacement = tmp_path / "replacement.service"
    replacement.write_text("other-unit\n", encoding="utf-8")
    replacement.chmod(0o644)

    def swap_during_verify(_descriptor: int) -> None:
        os.replace(replacement, destination)

    monkeypatch.setattr(installer_module, "_verify_unit", swap_during_verify)

    with pytest.raises(installer_module.InstallError, match="changed during verification"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=True,
        )

    assert destination.read_text(encoding="utf-8") == "other-unit\n"
    assert (original.st_dev, original.st_ino) != (
        destination.stat().st_dev,
        destination.stat().st_ino,
    )
    retained = list(tmp_path.glob(".*.service"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")


def test_force_replacement_uses_one_atomic_exchange(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    real_exchange = installer_module._rename_exchange
    exchanges: list[tuple[str, str]] = []

    def observe_exchange(
        directory_descriptor: int, source_name: str, destination_name: str
    ) -> None:
        assert stat.S_ISREG(
            os.stat(
                destination_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            ).st_mode
        )
        real_exchange(directory_descriptor, source_name, destination_name)
        assert stat.S_ISREG(
            os.stat(
                destination_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            ).st_mode
        )
        exchanges.append((source_name, destination_name))

    monkeypatch.setattr(installer_module, "_rename_exchange", observe_exchange)
    retained_path = installer_module.install_unit(
        rendered="[Service]\nExecStart=/bin/true\n",
        destination=destination,
        force=True,
    )

    assert len(exchanges) == 1
    assert retained_path is not None
    assert retained_path.read_text(encoding="utf-8") == "old-unit\n"
    assert destination.read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    retained = list(tmp_path.glob(".*.service"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "old-unit\n"


def test_force_exchange_race_restores_unexpected_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    racer = tmp_path / "racer.service"
    racer.write_text("unexpected-unit\n", encoding="utf-8")
    racer.chmod(0o644)
    displaced = tmp_path / "displaced.service"
    real_exchange = installer_module._rename_exchange
    calls = 0

    def race_then_exchange(
        directory_descriptor: int, source_name: str, destination_name: str
    ) -> None:
        nonlocal calls
        if calls == 0:
            os.rename(
                destination_name,
                displaced.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.rename(
                racer.name,
                destination_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        calls += 1
        real_exchange(directory_descriptor, source_name, destination_name)

    monkeypatch.setattr(installer_module, "_rename_exchange", race_then_exchange)

    with pytest.raises(installer_module.InstallError, match="unexpected entry retained"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=True,
        )

    assert calls == 1
    assert destination.read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    assert displaced.read_text(encoding="utf-8") == "old-unit\n"
    retained = list(tmp_path.glob(".*.service"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "unexpected-unit\n"


def test_force_exchange_fsync_failure_restores_original(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    calls = 0

    def fail_first_directory_fsync(_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")

    monkeypatch.setattr(installer_module, "_fsync_directory", fail_first_directory_fsync)

    with pytest.raises(installer_module.InstallError, match="candidate:"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=True,
        )

    assert calls >= 2
    assert destination.read_text(encoding="utf-8") == "old-unit\n"
    retained = list(tmp_path.glob(".*.service"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")


def test_force_exchange_failed_rollback_retains_prior_unit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    real_exchange = installer_module._rename_exchange
    exchanges = 0

    def fail_rollback_exchange(
        directory_descriptor: int, source_name: str, destination_name: str
    ) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges == 2:
            raise OSError("injected rollback failure")
        real_exchange(directory_descriptor, source_name, destination_name)

    monkeypatch.setattr(installer_module, "_rename_exchange", fail_rollback_exchange)
    monkeypatch.setattr(
        installer_module,
        "_fsync_directory",
        lambda _descriptor: (_ for _ in ()).throw(OSError("injected commit failure")),
    )

    with pytest.raises(installer_module.InstallError, match="retained entries"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=True,
        )

    retained = list(tmp_path.glob(".*.service"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "old-unit\n"
    assert destination.read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")


def test_initial_rollback_fsync_failure_reports_candidate_actual_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    calls = 0

    def always_fail_fsync(_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        raise OSError("persistent fsync failure")

    monkeypatch.setattr(installer_module, "_fsync_directory", always_fail_fsync)

    with pytest.raises(installer_module.InstallError) as captured:
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    retained = list(tmp_path.glob(".*.service"))
    assert calls >= 2
    assert destination.read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    assert retained == []
    retained_clause = str(captured.value).split("retained entries:", 1)[1]
    assert f"candidate: {destination}" in retained_clause


def test_force_rollback_fsync_failure_reports_actual_identities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    calls = 0

    def always_fail_fsync(_descriptor: int) -> None:
        nonlocal calls
        calls += 1
        raise OSError("persistent fsync failure")

    monkeypatch.setattr(installer_module, "_fsync_directory", always_fail_fsync)

    with pytest.raises(installer_module.InstallError) as captured:
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=True,
        )

    retained = list(tmp_path.glob(".*.service"))
    message = str(captured.value)
    assert calls >= 2
    assert destination.read_text(encoding="utf-8") == "old-unit\n"
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    assert f"prior unit: {destination}" in message
    assert f"candidate: {retained[0]}" in message


@pytest.mark.parametrize("force", [False, True])
def test_installer_never_commits_replaced_temporary_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    force: bool,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    if force:
        destination.write_text("old-unit\n", encoding="utf-8")
        destination.chmod(0o644)
    replacement = tmp_path / "replacement.service"
    replacement.write_text("unverified-unit\n", encoding="utf-8")
    replacement.chmod(0o644)

    def replace_temp_after_verify(_descriptor: int) -> None:
        temporary = next(tmp_path.glob(".codex-openai-bridge.*.service"))
        os.replace(replacement, temporary)

    monkeypatch.setattr(installer_module, "_verify_unit", replace_temp_after_verify)

    with pytest.raises(installer_module.InstallError, match=r"temporary.*changed"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=force,
        )

    if force:
        assert destination.read_text(encoding="utf-8") == "old-unit\n"
    else:
        assert not destination.exists()
    assert any(
        path.read_text(encoding="utf-8") == "unverified-unit\n"
        for path in tmp_path.glob(".*.service")
    )


def test_force_rollback_preserves_post_exchange_unexpected_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    destination.write_text("old-unit\n", encoding="utf-8")
    destination.chmod(0o644)
    unexpected = tmp_path / "unexpected.service"
    unexpected.write_text("unexpected-unit\n", encoding="utf-8")
    unexpected.chmod(0o644)

    def replace_destination_then_fail(_descriptor: int) -> None:
        os.replace(unexpected, destination)
        raise OSError("injected post-exchange failure")

    monkeypatch.setattr(installer_module, "_fsync_directory", replace_destination_then_fail)

    with pytest.raises(installer_module.InstallError, match="prior unit retained as"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=True,
        )

    assert destination.read_text(encoding="utf-8") == "unexpected-unit\n"
    assert any(
        path.read_text(encoding="utf-8") == "old-unit\n" for path in tmp_path.glob(".*.service")
    )


def test_initial_rollback_does_not_move_unexpected_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    unexpected = tmp_path / "unexpected.service"
    unexpected.write_text("unexpected-unit\n", encoding="utf-8")
    unexpected.chmod(0o644)
    displaced = tmp_path / "displaced-candidate.service"
    calls = 0

    def replace_destination_then_fail(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            os.rename(
                destination.name,
                displaced.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            os.rename(
                unexpected.name,
                destination.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            raise OSError("injected destination replacement")

    monkeypatch.setattr(installer_module, "_fsync_directory", replace_destination_then_fail)

    with pytest.raises(installer_module.InstallError) as captured:
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    message = str(captured.value)
    assert destination.read_text(encoding="utf-8") == "unexpected-unit\n"
    assert displaced.read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    assert str(destination) in message
    assert str(displaced) in message


def test_initial_failure_never_attempts_pathname_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    real_noreplace = installer_module._rename_noreplace
    calls = 0

    def fail_if_rollback_is_attempted(
        descriptor: int, source_name: str, destination_name: str
    ) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("initial install attempted pathname rollback")
        real_noreplace(descriptor, source_name, destination_name)

    monkeypatch.setattr(installer_module, "_rename_noreplace", fail_if_rollback_is_attempted)
    monkeypatch.setattr(
        installer_module,
        "_fsync_directory",
        lambda _descriptor: (_ for _ in ()).throw(OSError("injected commit failure")),
    )

    with pytest.raises(installer_module.InstallError, match="candidate"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    assert calls == 1
    assert destination.read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")


def test_retained_discovery_reports_unresolved_candidate_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "codex-openai-bridge.service"
    displaced = tmp_path / "candidate-after-many-entries.service"
    for index in range(300):
        (tmp_path / f"entry-{index:04d}").write_text("filler\n", encoding="utf-8")

    def displace_candidate_then_fail(descriptor: int) -> None:
        os.rename(
            destination.name,
            displaced.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
        raise OSError("injected displacement")

    monkeypatch.setattr(installer_module, "_fsync_directory", displace_candidate_then_fail)

    with pytest.raises(installer_module.InstallError) as captured:
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    message = str(captured.value)
    identity = displaced.stat()
    assert str(displaced) in message or f"ino={identity.st_ino}" in message


def test_lone_surrogate_destination_does_not_leak_directory_descriptors(
    tmp_path: Path,
) -> None:
    before = len(list(Path("/proc/self/fd").iterdir()))
    destination = tmp_path / "safe" / "\ud800" / "codex-openai-bridge.service"

    for _attempt in range(8):
        with pytest.raises(installer_module.InstallError):
            installer_module.install_unit(
                rendered="[Service]\nExecStart=/bin/true\n",
                destination=destination,
                force=False,
            )

    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_parent_swap_when_commit_fsync_begins_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "user"
    parent.mkdir(mode=0o700)
    moved_parent = tmp_path / "moved-user"
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o700)
    destination = parent / "codex-openai-bridge.service"
    real_fsync = installer_module._fsync_directory
    calls = 0

    def swap_parent_then_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            parent.rename(moved_parent)
            parent.symlink_to(attacker, target_is_directory=True)
        real_fsync(descriptor)

    monkeypatch.setattr(installer_module, "_fsync_directory", swap_parent_then_fsync)

    with pytest.raises(installer_module.InstallError, match="retained entries") as captured:
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    assert list(attacker.iterdir()) == []
    retained = list(moved_parent.iterdir())
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    message = str(captured.value)
    assert str(retained[0]) in message
    assert str(parent / retained[0].name) not in message


def test_temporary_name_failure_closes_parent_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "user" / "codex-openai-bridge.service"
    before = len(list(Path("/proc/self/fd").iterdir()))
    monkeypatch.setattr(
        secrets,
        "token_hex",
        lambda _length: (_ for _ in ()).throw(RuntimeError("injected token failure")),
    )

    with pytest.raises(installer_module.InstallError, match="retained entries: none"):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_installer_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    linked_parent = tmp_path / "user"
    linked_parent.symlink_to(attacker, target_is_directory=True)
    destination = linked_parent / "codex-openai-bridge.service"

    with pytest.raises(installer_module.InstallError):
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    assert list(attacker.iterdir()) == []


def test_installer_binds_cleanup_to_open_parent_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "user"
    parent.mkdir(mode=0o700)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    moved_parent = tmp_path / "moved-user"
    destination = parent / "codex-openai-bridge.service"

    def swap_parent_during_verify(_descriptor: int) -> None:
        parent.rename(moved_parent)
        parent.symlink_to(attacker, target_is_directory=True)

    monkeypatch.setattr(installer_module, "_verify_unit", swap_parent_during_verify)

    with pytest.raises(installer_module.InstallError, match="candidate retained") as captured:
        installer_module.install_unit(
            rendered="[Service]\nExecStart=/bin/true\n",
            destination=destination,
            force=False,
        )

    assert list(attacker.iterdir()) == []
    retained = list(moved_parent.iterdir())
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == ("[Service]\nExecStart=/bin/true\n")
    message = str(captured.value)
    assert str(retained[0]) in message
    assert str(parent / retained[0].name) not in message


def test_deployment_readmes_use_generated_unit_for_arbitrary_checkout() -> None:
    english = README_PATH.read_text(encoding="utf-8")
    japanese = README_JA_PATH.read_text(encoding="utf-8")
    for required in (
        "git clone https://github.com/itmst71/codex-openai-bridge.git",
        "uv run python scripts/install_user_service.py",
        '--checkout "$PWD"',
        '--codex-path "$(command -v codex)"',
        "does not start, stop, enable, or reload the service",
        "retains the previous unit under the",
        "validated concrete `--codex-home` rendered into `ReadWritePaths=`",
        "opened-parent(dev=...,ino=...)/<basename>",
        "uv run python scripts/verify_systemd_unit.py",
        "/path/to/codex-openai-bridge",
    ):
        assert required in english
    for required in (
        "git clone https://github.com/itmst71/codex-openai-bridge.git",
        "uv run python scripts/install_user_service.py",
        '--checkout "$PWD"',
        '--codex-path "$(command -v codex)"',
        "serviceのstart、stop、enable、reloadを実行しません",
        "旧unitを表示されたhidden filenameで保持",
        "具体的な`--codex-home`をrenderした`ReadWritePaths=`",
        "opened-parent(dev=...,ino=...)/<basename>",
        "uv run python scripts/verify_systemd_unit.py",
        "/path/to/codex-openai-bridge",
    ):
        assert required in japanese
    assert FIXED_CHECKOUT.search(english) is None
    assert FIXED_CHECKOUT.search(japanese) is None


def test_contribution_gates_verify_generated_unit_not_removed_sample() -> None:
    english = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    japanese = (ROOT / "CONTRIBUTING.ja.md").read_text(encoding="utf-8")
    for text in (english, japanese):
        assert "uv run python scripts/verify_systemd_unit.py" in text
        assert (
            "systemd-analyze --user verify deploy/systemd/codex-openai-bridge.service"
        ) not in text


def test_rollback_supports_revisions_before_the_installer_exists() -> None:
    english = README_PATH.read_text(encoding="utf-8")
    japanese = README_JA_PATH.read_text(encoding="utf-8")
    for required in (
        "Before upgrading, preserve the currently installed verified unit",
        "rollback-codex-openai-bridge.service",
        "If the target revision predates `install_user_service.py`",
        "restore the preserved unit",
    ):
        assert required in english
    for required in (
        "upgrade前に現在install済みの検証済みunitをcheckout外へ保存",
        "rollback-codex-openai-bridge.service",
        "対象revisionが`install_user_service.py`より古い場合",
        "保存済みunitを復元",
    ):
        assert required in japanese


def test_concrete_user_home_detector_handles_source_boundaries() -> None:
    separator = "\\"
    escaped_separator = separator * 2
    linux_home = "/home/" + "alice"
    macos_home = "/Users/" + "Alice"
    windows_home = f"C:{separator}Users{separator}Alice"
    escaped_windows_home = f"C:{escaped_separator}Users{escaped_separator}Alice"
    concrete_paths = (
        linux_home,
        linux_home + "/repo",
        linux_home + " ",
        linux_home + ",next",
        macos_home,
        macos_home + "/repo",
        f'HOME="{macos_home}"\n',
        windows_home,
        windows_home + separator + "repo",
        windows_home.lower() + separator + "repo",
        f'HOME="{windows_home}"\n',
        escaped_windows_home + escaped_separator + "repo",
        linux_home + "\nNEXT=1",
        f'HOME="{linux_home}"\n',
    )
    placeholder_name = "USERNAME"
    placeholders = (
        "/home/" + placeholder_name,
        "/home/" + placeholder_name + "/repo",
        "/Users/" + placeholder_name,
        "/Users/" + placeholder_name + "/repo",
        f'HOME="/Users/{placeholder_name}"\n',
        f"C:{separator}Users{separator}{placeholder_name}",
        f"C:{separator}Users{separator}{placeholder_name}{separator}repo",
        f"C:{escaped_separator}Users{escaped_separator}{placeholder_name}",
        f"C:{escaped_separator}Users{escaped_separator}{placeholder_name}{escaped_separator}repo",
    )

    assert all(_contains_concrete_user_home(path) for path in concrete_paths)
    assert not any(_contains_concrete_user_home(path) for path in placeholders)


def test_maintained_repository_text_has_no_host_or_fixed_checkout_residue() -> None:
    roots = (ROOT / ".github", ROOT / "deploy", ROOT / "scripts", ROOT / "src", ROOT / "tests")
    files = [
        ROOT / ".gitignore",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.ja.md",
        ROOT / "LICENSE",
        ROOT / "README.md",
        ROOT / "README.ja.md",
        ROOT / "SECURITY.md",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        *(path for root in roots for path in root.rglob("*") if path.is_file()),
    ]
    offenders: list[str] = []
    for path in files:
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _contains_concrete_user_home(text) or FIXED_CHECKOUT.search(text):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_ci_systemd_verifier_renders_arbitrary_checkout(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "portable-home")
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)

    subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT)],
        cwd=tmp_path,
        env=environment,
        check=True,
        timeout=30,
    )
