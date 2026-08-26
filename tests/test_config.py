import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from codex_openai_bridge.config import ConfigError, Settings

MIB = 1024 * 1024

INTEGER_LIMITS = [
    ("CODEX_BRIDGE_PORT", 1, 65_535),
    ("CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES", 1, 16 * MIB),
    ("CODEX_BRIDGE_MAX_JSON_DEPTH", 1, 64),
    ("CODEX_BRIDGE_MAX_JSON_NODES", 1, 100_000),
    ("CODEX_BRIDGE_MAX_MESSAGES", 1, 1_024),
    ("CODEX_BRIDGE_MAX_TOOLS", 1, 256),
    ("CODEX_BRIDGE_MAX_STRING_BYTES", 1, MIB),
    ("CODEX_BRIDGE_MAX_HELPER_STDOUT_BYTES", 1, 64 * 1024),
    ("CODEX_BRIDGE_MAX_HELPER_STDERR_BYTES", 1, 64 * 1024),
    ("CODEX_BRIDGE_MAX_UPSTREAM_BODY_BYTES", 1, 64 * MIB),
    ("CODEX_BRIDGE_MAX_SSE_EVENT_BYTES", 1, 4 * MIB),
    ("CODEX_BRIDGE_MAX_STREAM_BYTES", 1, 128 * MIB),
    ("CODEX_BRIDGE_MAX_IN_FLIGHT", 1, 10),
]

FLOAT_LIMITS = [
    ("CODEX_BRIDGE_HELPER_DEADLINE_SECONDS", 0.1, 60.0),
    ("CODEX_BRIDGE_QUEUE_WAIT_SECONDS", 0.1, 60.0),
    ("CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS", 0.1, 60.0),
    ("CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS", 0.1, 120.0),
    ("CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS", 0.1, 300.0),
    ("CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS", 0.1, 600.0),
]


def test_default_settings_match_bounded_runtime_contract() -> None:
    settings = Settings.from_env()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8646
    assert settings.public_model == "codex"
    assert settings.upstream_model == "gpt-5.6-terra"
    assert settings.max_request_body_bytes == MIB
    assert settings.max_json_depth == 32
    assert settings.max_json_nodes == 20_000
    assert settings.max_messages == 512
    assert settings.max_tools == 128
    assert settings.max_string_bytes == 256 * 1024
    assert settings.max_helper_stdout_bytes == 16 * 1024
    assert settings.max_helper_stderr_bytes == 16 * 1024
    assert settings.helper_deadline_seconds == 30.0
    assert settings.max_upstream_body_bytes == 16 * MIB
    assert settings.max_sse_event_bytes == MIB
    assert settings.max_stream_bytes == 32 * MIB
    assert settings.max_in_flight == 2
    assert settings.queue_wait_seconds == 10.0
    assert settings.connect_timeout_seconds == 10.0
    assert settings.response_header_timeout_seconds == 30.0
    assert settings.stream_idle_timeout_seconds == 60.0
    assert settings.total_request_deadline_seconds == 240.0


@pytest.mark.parametrize("host", ["127.0.0.1", "127.255.255.254", "::1"])
def test_accepts_ip_loopback_addresses(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_HOST", host)

    assert Settings.from_env().host == host


def test_public_model_alias_cannot_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_PUBLIC_MODEL", "other")

    assert Settings.from_env().public_model == "codex"


def test_accepts_canonical_upstream_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_UPSTREAM_MODEL", "gpt-5.6-codex")

    assert Settings.from_env().upstream_model == "gpt-5.6-codex"


def test_settings_are_immutable() -> None:
    settings = Settings.from_env()

    with pytest.raises(FrozenInstanceError):
        settings.port = 9000  # type: ignore[misc]


def test_default_paths_are_derived_from_current_user_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_BRIDGE_CODEX_PATH", raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_BRIDGE_CLIENT_TOKEN_FILE", raising=False)

    settings = Settings.from_env()

    assert settings.codex_path == home / ".local" / "bin" / "codex"
    assert settings.codex_home == home / ".codex"
    assert settings.client_token_file == home / ".config" / "codex-openai-bridge" / "client-token"
    assert settings.codex_path.is_absolute()
    assert settings.codex_home.is_absolute()
    assert settings.credential_python_path == Path(sys.executable)
    assert settings.credential_python_path.is_absolute()

    assert settings.client_token_file.is_absolute()
    assert not hasattr(settings, "hermes_python_path")


def test_accepts_absolute_path_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_path = tmp_path / "bin" / "codex"
    codex_home = tmp_path / "codex-home"
    token_file = tmp_path / "client-token"
    monkeypatch.setenv("CODEX_BRIDGE_CODEX_PATH", str(codex_path))
    monkeypatch.setenv("CODEX_BRIDGE_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CODEX_BRIDGE_CLIENT_TOKEN_FILE", str(token_file))

    settings = Settings.from_env()

    assert settings.codex_path == codex_path
    assert settings.codex_home == codex_home
    assert settings.client_token_file == token_file


@pytest.mark.parametrize(
    "name",
    [
        "CODEX_BRIDGE_CODEX_PATH",
        "CODEX_BRIDGE_CODEX_HOME",
        "CODEX_BRIDGE_CLIENT_TOKEN_FILE",
    ],
)
def test_rejects_relative_configured_paths(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv(name, "relative/path")

    with pytest.raises(ConfigError, match=f"{name} must be an absolute path"):
        Settings.from_env()


@pytest.mark.parametrize(
    "raw",
    ["/tmp/../helper.py", "/tmp/helper\n.py", "/tmp/helper\u0085.py"],
)
def test_rejects_unsafe_configured_paths(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_CODEX_PATH", raw)

    with pytest.raises(ConfigError, match="CODEX_BRIDGE_CODEX_PATH"):
        Settings.from_env()


@pytest.mark.parametrize(
    "obsolete",
    [
        "CODEX_BRIDGE_" + "HERMES_PYTHON",
        "CODEX_BRIDGE_" + "HELPER_PATH",
    ],
)
def test_rejects_obsolete_hermes_credential_configuration(
    monkeypatch: pytest.MonkeyPatch,
    obsolete: str,
) -> None:
    monkeypatch.setenv(obsolete, "/tmp/obsolete")

    with pytest.raises(ConfigError, match="unsupported"):
        Settings.from_env()


@pytest.mark.parametrize(
    "raw",
    ["", "gpt model", "https://example.invalid/model", "g" * 129],
)
def test_rejects_invalid_upstream_model(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_UPSTREAM_MODEL", raw)

    with pytest.raises(ConfigError, match="CODEX_BRIDGE_UPSTREAM_MODEL"):
        Settings.from_env()


def test_rejects_non_loopback_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_HOST", "0.0.0.0")

    with pytest.raises(ConfigError, match="loopback"):
        Settings.from_env()


@pytest.mark.parametrize("name,minimum,maximum", INTEGER_LIMITS)
@pytest.mark.parametrize("kind", ["below", "above", "noncanonical"])
def test_rejects_invalid_integer_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    minimum: int,
    maximum: int,
    kind: str,
) -> None:
    raw = {
        "below": str(minimum - 1),
        "above": str(maximum + 1),
        "noncanonical": f"0{minimum}",
    }[kind]
    monkeypatch.setenv(name, raw)

    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("name,minimum,maximum", FLOAT_LIMITS)
@pytest.mark.parametrize("raw", ["0", "1e1", "nan", "true"])
def test_rejects_invalid_float_resource_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    minimum: float,
    maximum: float,
    raw: str,
) -> None:
    del minimum, maximum
    monkeypatch.setenv(name, raw)

    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("name,minimum,maximum", FLOAT_LIMITS)
@pytest.mark.parametrize("kind", ["just_below", "just_above"])
def test_rejects_exact_decimal_values_outside_float_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    minimum: float,
    maximum: float,
    kind: str,
) -> None:
    assert minimum == 0.1
    raw = "0.099999999999999999999" if kind == "just_below" else f"{maximum:.1f}{'0' * 20}1"
    monkeypatch.setenv(name, raw)

    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


@pytest.mark.parametrize("name,minimum,maximum", INTEGER_LIMITS)
def test_accepts_integer_resource_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    minimum: int,
    maximum: int,
) -> None:
    for value in (minimum, maximum):
        monkeypatch.setenv(name, str(value))
        if name == "CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES":
            monkeypatch.setenv("CODEX_BRIDGE_MAX_STRING_BYTES", str(min(value, 256 * 1024)))
        if name == "CODEX_BRIDGE_MAX_STREAM_BYTES":
            monkeypatch.setenv("CODEX_BRIDGE_MAX_SSE_EVENT_BYTES", str(min(value, MIB)))
        Settings.from_env()


@pytest.mark.parametrize("name,minimum,maximum", FLOAT_LIMITS)
def test_accepts_float_resource_limit_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    minimum: float,
    maximum: float,
) -> None:
    for value in (minimum, maximum):
        monkeypatch.setenv(name, str(value))
        if name == "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS":
            for dependent in (
                "CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS",
                "CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS",
                "CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS",
            ):
                monkeypatch.setenv(dependent, str(min(value, 0.1)))
        elif name in {
            "CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS",
            "CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS",
            "CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS",
        }:
            monkeypatch.setenv(
                "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS",
                str(max(value, 240.0)),
            )
        Settings.from_env()


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES": "1",
            "CODEX_BRIDGE_MAX_STRING_BYTES": "2",
        },
        {
            "CODEX_BRIDGE_MAX_STREAM_BYTES": "1",
            "CODEX_BRIDGE_MAX_SSE_EVENT_BYTES": "2",
        },
        {
            "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS": "1",
            "CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS": "2",
        },
        {
            "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS": "1",
            "CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS": "2",
        },
        {
            "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS": "1",
            "CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS": "2",
        },
    ],
)
def test_rejects_impossible_cross_field_limits(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match="must not exceed"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["0", "11", "true", "1.5"])
def test_rejects_invalid_concurrency(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("CODEX_BRIDGE_MAX_IN_FLIGHT", raw)

    with pytest.raises(ConfigError, match="MAX_IN_FLIGHT"):
        Settings.from_env()
