"""The optional real-consumer installer must not replace a different source snapshot."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_unhush_preparation_rejects_changed_cached_source_without_overwrite(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("requires Node for the optional consumer preparation guard")
    source = tmp_path / "upstream/package.json"
    source.parent.mkdir()
    source.write_text("do not replace\n")
    before = source.read_bytes(), source.stat().st_ino
    result = subprocess.run(
        [node, str(ROOT / "tests/consumer_contract/unhush_env/prepare.mjs"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "source integrity" in result.stderr
    assert (source.read_bytes(), source.stat().st_ino) == before
    assert not (tmp_path / "client.mjs").exists()
