"""Fail-closed runtime configuration for the bridge."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from codex_openai_bridge.model_config import (
    ModelConfigurationError,
    load_model_map,
    model_map_entry_exists,
)

_MIB = 1024 * 1024
_DECIMAL_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)")
_DECIMAL_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_KNOWN_ENVIRONMENT = frozenset(
    {
        "CODEX_BRIDGE_CLIENT_TOKEN_FILE",
        "CODEX_BRIDGE_CONTINUATION_KEY_FILE",
        "CODEX_BRIDGE_CODEX_HOME",
        "CODEX_BRIDGE_CODEX_PATH",
        "CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS",
        "CODEX_BRIDGE_HELPER_DEADLINE_SECONDS",
        "CODEX_BRIDGE_HOST",
        "CODEX_BRIDGE_MAX_HELPER_STDERR_BYTES",
        "CODEX_BRIDGE_MAX_HELPER_STDOUT_BYTES",
        "CODEX_BRIDGE_MAX_IN_FLIGHT",
        "CODEX_BRIDGE_MAX_JSON_DEPTH",
        "CODEX_BRIDGE_MAX_JSON_NODES",
        "CODEX_BRIDGE_MAX_MESSAGES",
        "CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES",
        "CODEX_BRIDGE_MAX_SSE_EVENT_BYTES",
        "CODEX_BRIDGE_MAX_STREAM_BYTES",
        "CODEX_BRIDGE_MAX_STRING_BYTES",
        "CODEX_BRIDGE_MAX_TOOLS",
        "CODEX_BRIDGE_MAX_UPSTREAM_BODY_BYTES",
        "CODEX_BRIDGE_MODEL_CONFIG_FILE",
        "CODEX_BRIDGE_PORT",
        "CODEX_BRIDGE_PUBLIC_MODEL",
        "CODEX_BRIDGE_QUEUE_WAIT_SECONDS",
        "CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS",
        "CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS",
        "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS",
        "CODEX_BRIDGE_UPSTREAM_MODEL",
    }
)


class ConfigError(ValueError):
    """Raised when bridge configuration is invalid."""


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    if _DECIMAL_INTEGER.fullmatch(raw) is None:
        raise ConfigError(f"{name} must be a decimal integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    if _DECIMAL_NUMBER.fullmatch(raw) is None:
        raise ConfigError(f"{name} must be a decimal number")
    exact_value = Decimal(raw)
    if not Decimal(str(minimum)) <= exact_value <= Decimal(str(maximum)):
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g}")
    return float(exact_value)


def _configured_model() -> str:
    name = "CODEX_BRIDGE_UPSTREAM_MODEL"
    value = os.environ.get(name, "gpt-5.6-terra")
    if _MODEL_ID.fullmatch(value) is None:
        raise ConfigError(f"{name} must be a canonical model identifier")
    return value


def _configured_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, str(default))
    if any(unicodedata.category(character) == "Cc" for character in raw):
        raise ConfigError(f"{name} contains a control character")
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    if ".." in path.parts:
        raise ConfigError(f"{name} must not contain parent traversal")
    return path


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated bridge settings."""

    host: str
    port: int
    public_model: str
    upstream_model: str
    model_config_file: Path | None
    model_map: Mapping[str, str]
    codex_path: Path
    codex_home: Path
    credential_python_path: Path
    client_token_file: Path
    continuation_key_file: Path
    max_request_body_bytes: int
    max_json_depth: int
    max_json_nodes: int
    max_messages: int
    max_tools: int
    max_string_bytes: int
    max_helper_stdout_bytes: int
    max_helper_stderr_bytes: int
    helper_deadline_seconds: float
    max_upstream_body_bytes: int
    max_sse_event_bytes: int
    max_stream_bytes: int
    max_in_flight: int
    queue_wait_seconds: float
    connect_timeout_seconds: float
    response_header_timeout_seconds: float
    stream_idle_timeout_seconds: float
    total_request_deadline_seconds: float

    @property
    def public_models(self) -> tuple[str, ...]:
        """Return public aliases in stable discovery order."""
        return tuple(self.model_map)

    @property
    def model_map_active(self) -> bool:
        """Return whether operator model-map authority is active, even with one alias."""
        return self.model_config_file is not None

    def resolve_upstream_model(self, public_alias: str) -> str | None:
        """Resolve only a server-approved public alias."""
        if type(public_alias) is not str:
            return None
        return self.model_map.get(public_alias)

    def _validate_relationships(self) -> None:
        relationships = (
            (
                "CODEX_BRIDGE_MAX_STRING_BYTES",
                self.max_string_bytes,
                "CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES",
                self.max_request_body_bytes,
            ),
            (
                "CODEX_BRIDGE_MAX_SSE_EVENT_BYTES",
                self.max_sse_event_bytes,
                "CODEX_BRIDGE_MAX_STREAM_BYTES",
                self.max_stream_bytes,
            ),
            (
                "CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS",
                self.connect_timeout_seconds,
                "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS",
                self.total_request_deadline_seconds,
            ),
            (
                "CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS",
                self.response_header_timeout_seconds,
                "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS",
                self.total_request_deadline_seconds,
            ),
            (
                "CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS",
                self.stream_idle_timeout_seconds,
                "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS",
                self.total_request_deadline_seconds,
            ),
        )
        for smaller_name, smaller, larger_name, larger in relationships:
            if smaller > larger:
                raise ConfigError(f"{smaller_name} must not exceed {larger_name}")

    @classmethod
    def from_env(cls) -> Settings:
        for name in os.environ:
            if name.startswith("CODEX_BRIDGE_") and name not in _KNOWN_ENVIRONMENT:
                raise ConfigError(f"{name} is unsupported")
        host = os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ConfigError("bridge host must be a loopback IP address") from exc
        if not address.is_loopback:
            raise ConfigError("bridge host must be a loopback IP address")

        model_config_file: Path | None = None
        explicit_model_config = "CODEX_BRIDGE_MODEL_CONFIG_FILE" in os.environ
        default_model_config = Path.home() / ".config" / "codex-openai-bridge" / "models.toml"
        if explicit_model_config:
            candidate_model_config = _configured_path(
                "CODEX_BRIDGE_MODEL_CONFIG_FILE",
                Path(os.environ["CODEX_BRIDGE_MODEL_CONFIG_FILE"]),
            )
        else:
            candidate_model_config = default_model_config
        try:
            default_model_config_present = model_map_entry_exists(candidate_model_config)
        except ModelConfigurationError as exc:
            raise ConfigError("model configuration is unavailable") from exc
        if explicit_model_config or default_model_config_present:
            model_config_file = candidate_model_config
            if "CODEX_BRIDGE_UPSTREAM_MODEL" in os.environ:
                raise ConfigError(
                    "CODEX_BRIDGE_MODEL_CONFIG_FILE conflicts with CODEX_BRIDGE_UPSTREAM_MODEL"
                )
            try:
                model_map = load_model_map(model_config_file, identifier_pattern=_MODEL_ID)
            except ModelConfigurationError as exc:
                raise ConfigError("model configuration is unavailable") from exc
        else:
            model_map = MappingProxyType({"codex": _configured_model()})

        settings = cls(
            host=host,
            port=_bounded_int("CODEX_BRIDGE_PORT", 8646, minimum=1, maximum=65_535),
            public_model="codex",
            upstream_model=model_map["codex"],
            model_config_file=model_config_file,
            model_map=model_map,
            codex_path=_configured_path(
                "CODEX_BRIDGE_CODEX_PATH", Path.home() / ".local" / "bin" / "codex"
            ),
            codex_home=_configured_path("CODEX_BRIDGE_CODEX_HOME", Path.home() / ".codex"),
            credential_python_path=Path(sys.executable),
            client_token_file=_configured_path(
                "CODEX_BRIDGE_CLIENT_TOKEN_FILE",
                Path.home() / ".config" / "codex-openai-bridge" / "client-token",
            ),
            continuation_key_file=_configured_path(
                "CODEX_BRIDGE_CONTINUATION_KEY_FILE",
                Path.home() / ".config" / "codex-openai-bridge" / "continuation-key",
            ),
            max_request_body_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_REQUEST_BODY_BYTES",
                16 * _MIB,
                minimum=1,
                maximum=32 * _MIB,
            ),
            max_json_depth=_bounded_int("CODEX_BRIDGE_MAX_JSON_DEPTH", 32, minimum=1, maximum=64),
            max_json_nodes=_bounded_int(
                "CODEX_BRIDGE_MAX_JSON_NODES", 100_000, minimum=1, maximum=200_000
            ),
            max_messages=_bounded_int("CODEX_BRIDGE_MAX_MESSAGES", 1_024, minimum=1, maximum=2_048),
            max_tools=_bounded_int("CODEX_BRIDGE_MAX_TOOLS", 256, minimum=1, maximum=512),
            max_string_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_STRING_BYTES", _MIB, minimum=1, maximum=2 * _MIB
            ),
            max_helper_stdout_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_HELPER_STDOUT_BYTES",
                16 * 1024,
                minimum=1,
                maximum=64 * 1024,
            ),
            max_helper_stderr_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_HELPER_STDERR_BYTES",
                16 * 1024,
                minimum=1,
                maximum=64 * 1024,
            ),
            helper_deadline_seconds=_bounded_float(
                "CODEX_BRIDGE_HELPER_DEADLINE_SECONDS", 30.0, minimum=0.1, maximum=60.0
            ),
            max_upstream_body_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_UPSTREAM_BODY_BYTES",
                16 * _MIB,
                minimum=1,
                maximum=32 * _MIB,
            ),
            max_sse_event_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_SSE_EVENT_BYTES", _MIB, minimum=1, maximum=2 * _MIB
            ),
            max_stream_bytes=_bounded_int(
                "CODEX_BRIDGE_MAX_STREAM_BYTES", 32 * _MIB, minimum=1, maximum=64 * _MIB
            ),
            max_in_flight=_bounded_int("CODEX_BRIDGE_MAX_IN_FLIGHT", 2, minimum=1, maximum=10),
            queue_wait_seconds=_bounded_float(
                "CODEX_BRIDGE_QUEUE_WAIT_SECONDS", 10.0, minimum=0.1, maximum=60.0
            ),
            connect_timeout_seconds=_bounded_float(
                "CODEX_BRIDGE_CONNECT_TIMEOUT_SECONDS", 10.0, minimum=0.1, maximum=60.0
            ),
            response_header_timeout_seconds=_bounded_float(
                "CODEX_BRIDGE_RESPONSE_HEADER_TIMEOUT_SECONDS",
                30.0,
                minimum=0.1,
                maximum=120.0,
            ),
            stream_idle_timeout_seconds=_bounded_float(
                "CODEX_BRIDGE_STREAM_IDLE_TIMEOUT_SECONDS",
                60.0,
                minimum=0.1,
                maximum=300.0,
            ),
            total_request_deadline_seconds=_bounded_float(
                "CODEX_BRIDGE_TOTAL_REQUEST_DEADLINE_SECONDS",
                240.0,
                minimum=0.1,
                maximum=600.0,
            ),
        )
        settings._validate_relationships()
        return settings
