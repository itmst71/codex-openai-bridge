"""Verify the deployed systemd unit in an isolated synthetic root."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy" / "systemd" / "codex-openai-bridge.service"


def _single_directive(raw: str, name: str) -> str:
    prefix = f"{name}="
    values = [line[len(prefix) :] for line in raw.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise RuntimeError(f"unit must contain exactly one nonempty {name} directive")
    return values[0]


def _rooted(root: Path, path: Path, *, name: str) -> Path:
    parts = path.parts
    if ".." in parts:
        raise RuntimeError(f"{name} must not contain parent traversal")
    if parts and parts[0] == "%h":
        if any("%" in part for part in parts[1:]):
            raise RuntimeError(f"{name} contains an unsupported systemd specifier")
        user_home = Path.home()
        if not user_home.is_absolute() or ".." in user_home.parts:
            raise RuntimeError("current user home must be an absolute path without traversal")
        path = user_home.joinpath(*parts[1:])
    elif not path.is_absolute() or any("%" in part for part in parts):
        raise RuntimeError(
            f"{name} must be absolute or start with %h and contain no other specifier"
        )
    return root / path.relative_to("/")


def main() -> None:
    raw = UNIT_PATH.read_text(encoding="utf-8")
    exec_argv = shlex.split(_single_directive(raw, "ExecStart"))
    if not exec_argv:
        raise RuntimeError("ExecStart must contain an executable")

    executable = Path(exec_argv[0])
    working_directory = Path(_single_directive(raw, "WorkingDirectory"))
    read_write_paths = [
        Path(value) for value in shlex.split(_single_directive(raw, "ReadWritePaths"))
    ]

    with tempfile.TemporaryDirectory(prefix="codex-systemd-root-") as temporary_root:
        synthetic_root = Path(temporary_root)
        unit_directory = synthetic_root / "etc" / "systemd" / "system"
        unit_directory.mkdir(parents=True)
        shutil.copy2(UNIT_PATH, unit_directory / UNIT_PATH.name)

        rooted_working_directory = _rooted(
            synthetic_root, working_directory, name="WorkingDirectory"
        )
        rooted_working_directory.mkdir(parents=True)
        for path in read_write_paths:
            _rooted(synthetic_root, path, name="ReadWritePaths").mkdir(parents=True)

        rooted_executable = _rooted(synthetic_root, executable, name="ExecStart executable")
        rooted_executable.parent.mkdir(parents=True, exist_ok=True)
        rooted_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        rooted_executable.chmod(0o755)

        subprocess.run(
            [
                "systemd-analyze",
                "--recursive-errors=no",
                f"--root={synthetic_root}",
                "verify",
                UNIT_PATH.name,
            ],
            check=True,
            timeout=30,
        )


if __name__ == "__main__":
    main()
