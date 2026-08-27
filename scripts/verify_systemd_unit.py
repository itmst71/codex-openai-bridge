"""Render and verify the user-service template under a synthetic arbitrary checkout."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_user_service.py"
UNIT_NAME = "codex-openai-bridge.service"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _directive(raw: str, name: str) -> str:
    prefix = f"{name}="
    values = [line[len(prefix) :] for line in raw.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise RuntimeError(f"generated unit must contain one {name}")
    return values[0]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-systemd-portable-") as temporary:
        root = Path(temporary)
        checkout = root / "arbitrary checkout"
        bridge_python = _executable(checkout / ".venv" / "bin" / "python")
        codex_path = _executable(root / "tool dir" / "codex")
        codex_home = root / "codex state"
        codex_home.mkdir(mode=0o700)
        destination = root / "user units" / UNIT_NAME
        environment = os.environ.copy()
        environment["HOME"] = str(root / "portable-home")
        environment.pop("PYTHONPATH", None)
        environment.pop("VIRTUAL_ENV", None)

        subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--checkout",
                str(checkout),
                "--codex-path",
                str(codex_path),
                "--codex-home",
                str(codex_home),
                "--destination",
                str(destination),
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )

        raw = destination.read_text(encoding="utf-8")
        if "WorkingDirectory=" in raw:
            raise RuntimeError("generated unit must not depend on a working directory")
        if shlex.split(_directive(raw, "ExecStart")) != [
            str(bridge_python),
            "-m",
            "codex_openai_bridge",
            "serve",
        ]:
            raise RuntimeError("generated ExecStart is not checkout-neutral")
        if shlex.split(_directive(raw, "ReadWritePaths")) != [str(codex_home)]:
            raise RuntimeError("generated Codex write authority is incorrect")
        metadata = destination.stat()
        if stat.S_IMODE(metadata.st_mode) != 0o644:
            raise RuntimeError("generated unit mode is incorrect")


if __name__ == "__main__":
    main()
