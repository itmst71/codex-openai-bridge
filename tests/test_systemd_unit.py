from __future__ import annotations

import configparser
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy" / "systemd" / "codex-openai-bridge.service"
README_PATH = ROOT / "README.md"
PYPROJECT_PATH = ROOT / "pyproject.toml"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_systemd_unit.py"
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


def _contains_concrete_user_home(text: str) -> bool:
    return any(pattern.search(text) for pattern in CONCRETE_USER_HOME_PATTERNS)


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _unit() -> tuple[str, configparser.ConfigParser]:
    raw = UNIT_PATH.read_text(encoding="utf-8")
    parser = _CaseSensitiveConfigParser(interpolation=None, strict=True)
    parser.read_string(raw)
    return raw, parser


def test_console_script_and_hardened_user_unit_are_canonical() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"codex-openai-bridge": "codex_openai_bridge.cli:main"}

    raw, unit = _unit()
    assert set(unit) == {"DEFAULT", "Unit", "Service", "Install"}
    assert unit["Unit"]["Description"] == "Loopback OpenAI-compatible bridge for Codex OAuth"
    assert unit["Unit"]["Wants"] == "network-online.target"
    assert unit["Unit"]["After"] == "network-online.target"

    service = unit["Service"]
    assert service["Type"] == "simple"
    assert service["WorkingDirectory"] == "%h/src/codex-openai-bridge"
    assert service["ExecStart"] == "%h/src/codex-openai-bridge/.venv/bin/codex-openai-bridge serve"
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5s"
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "read-only"
    assert service["ReadWritePaths"] == "%h/.codex"
    assert service["RestrictAddressFamilies"] == "AF_UNIX AF_INET AF_INET6"
    assert service["UMask"] == "0077"
    assert service.get("Environment", "").split() == [
        "CODEX_BRIDGE_HOST=127.0.0.1",
        "PYTHONPATH=",
    ]
    assert unit["Install"]["WantedBy"] == "default.target"

    assert "EnvironmentFile=" not in raw
    assert re.search(r"CODEX_BRIDGE_CLIENT_TOKEN\s*=", raw) is None
    for forbidden in ("Authorization:", "Bearer ", "access_token", "chatgpt_account_id"):
        assert forbidden not in raw
    for forbidden in ("%h/.hermes", "CODEX_BRIDGE_HERMES_PYTHON", "OPENAI_API_KEY"):
        assert forbidden not in raw


def test_deployment_readme_covers_complete_operator_contract_without_secrets() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    required_headings = (
        "## Architecture",
        "## Supported and unsupported",
        "## Security assumptions",
        "## Configuration",
        "## Generate the bridge client token",
        "## Deploy the systemd user service",
        "## Honcho split configuration",
        "## curl example",
        "## OpenAI SDK example",
        "## Upgrade and rollback",
        "## Opt-in live tests",
    )
    for heading in required_headings:
        assert heading in readme

    for required in (
        "http://127.0.0.1:8646/v1",
        "CODEX_BRIDGE_CLIENT_TOKEN_FILE",
        "chmod 600",
        "systemd-analyze --user verify",
        "systemctl --user daemon-reload",
        "systemctl --user enable",
        "GET /healthz",
        "GET /readyz",
        "POST /v1/chat/completions",
        "POST /v1/responses",
        "GET /v1/models",
        "Embeddings are not supported",
        "max_retries=0",
        "EMBED_MESSAGES=false",
        "$HOME/src/codex-openai-bridge",
        "codex login",
        'cli_auth_credentials_store = "file"',
        "CODEX_BRIDGE_CODEX_PATH",
        "CODEX_BRIDGE_CODEX_HOME",
        "ReadWritePaths=%h/.codex",
    ):
        assert required in readme

    assert re.search(r"(?:sk-|Bearer )[A-Za-z0-9_-]{12,}", readme) is None


def test_concrete_user_home_detector_handles_source_boundaries() -> None:
    separator = "\\"
    escaped_separator = separator * 2
    linux_home = "/home/" + "alice"
    macos_home = "/Users/" + "Alice"
    windows_home = f"C:{separator}Users{separator}Alice"
    escaped_windows_home = f"C:{escaped_separator}Users{escaped_separator}Alice"
    lowercase_windows_home = f"c:{separator}users{separator}bob"
    escaped_lowercase_windows_home = f"c:{escaped_separator}users{escaped_separator}bob"
    concrete_paths = (
        linux_home + "/repo",
        macos_home + "/repo",
        windows_home + separator + "repo",
        escaped_windows_home + escaped_separator + "repo",
        lowercase_windows_home + separator + "repo",
        escaped_lowercase_windows_home + escaped_separator + "repo",
        linux_home + "\nNEXT=1",
        f'HOME="{linux_home}"\n',
        macos_home + "\nNEXT=1",
        f'HOME="{macos_home}"\n',
        f'WINDOWS_HOME="{windows_home}"\nNEXT=1',
        f'WINDOWS_HOME="{escaped_windows_home}"\nNEXT=1',
        f'windows_home="{lowercase_windows_home}"\nNEXT=1',
        f'windows_home="{escaped_lowercase_windows_home}"\nNEXT=1',
    )
    placeholder_name = "USERNAME"
    placeholders = (
        "/home/" + placeholder_name,
        "/home/" + placeholder_name + "/repo",
        "/Users/" + placeholder_name,
        "/Users/" + placeholder_name + "/repo",
        f"C:{separator}Users{separator}{placeholder_name}",
        f"C:{separator}Users{separator}{placeholder_name}{separator}repo",
        f"C:{escaped_separator}Users{escaped_separator}{placeholder_name}",
        (f"C:{escaped_separator}Users{escaped_separator}{placeholder_name}{escaped_separator}repo"),
    )

    assert all(_contains_concrete_user_home(path) for path in concrete_paths)
    assert not any(_contains_concrete_user_home(path) for path in placeholders)


def test_maintained_repository_text_has_no_concrete_user_home_directory() -> None:
    roots = (
        ROOT / ".github",
        ROOT / "deploy",
        ROOT / "scripts",
        ROOT / "src",
        ROOT / "tests",
    )
    files = [
        ROOT / ".gitignore",
        ROOT / "LICENSE",
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        *(
            path
            for root in roots
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ),
    ]

    offenders = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _contains_concrete_user_home(text):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_ci_systemd_verifier_resolves_portable_home_specifier(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "portable-home")
    environment.pop("PYTHONPATH", None)
    environment.pop("VIRTUAL_ENV", None)

    subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT)],
        cwd=ROOT,
        env=environment,
        check=True,
        timeout=30,
    )
