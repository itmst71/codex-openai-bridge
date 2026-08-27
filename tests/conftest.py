"""Shared pytest configuration for codex-openai-bridge."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_operator_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep host operator configuration outside every ordinary test."""
    home = tmp_path / "ambient-home"
    config_dir = home / ".config" / "codex-openai-bridge"
    config_dir.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    (home / ".config").chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
