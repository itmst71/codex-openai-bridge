from pathlib import Path

from codex_openai_bridge import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_package_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_package_has_no_hermes_runtime_credential_dependency() -> None:
    source_root = ROOT / "src" / "codex_openai_bridge"
    assert not (source_root / "hermes_credential_helper.py").exists()
    maintained = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py"))
    for forbidden in ("hermes_cli", "CODEX_BRIDGE_HERMES_PYTHON", ".hermes"):
        assert forbidden not in maintained
