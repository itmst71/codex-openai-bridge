from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = ROOT / "deploy" / "systemd" / "codex-openai-bridge.service"
README_PATH = ROOT / "README.md"
PYPROJECT_PATH = ROOT / "pyproject.toml"


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
    assert service["WorkingDirectory"] == "/home/itmst/src/codex-openai-bridge"
    assert service["ExecStart"] == (
        "/home/itmst/src/codex-openai-bridge/.venv/bin/codex-openai-bridge serve"
    )
    assert service["Restart"] == "on-failure"
    assert service["RestartSec"] == "5s"
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "read-only"
    assert service["ReadWritePaths"] == "/home/itmst/.hermes"
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
    ):
        assert required in readme

    assert re.search(r"(?:sk-|Bearer )[A-Za-z0-9_-]{12,}", readme) is None
