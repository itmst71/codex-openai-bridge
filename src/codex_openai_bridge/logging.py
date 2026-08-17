"""Closed, secret-negative request observability."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import TextIO

LOGGER_NAME = "codex_openai_bridge.requests"
_LOGGER = logging.getLogger(LOGGER_NAME)
_NOISY_LOGGER_NAMES = ("aiohttp.access", "aiohttp.server", "httpx", "httpcore")
_HANDLER_NAME = "codex_openai_bridge.closed_events"
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_ENDPOINTS: Mapping[tuple[str, str], str] = {
    ("GET", "/healthz"): "health",
    ("GET", "/readyz"): "readiness",
    ("GET", "/v1/models"): "models",
    ("POST", "/v1/embeddings"): "embeddings",
    ("POST", "/v1/chat/completions"): "chat_completions",
    ("POST", "/v1/responses"): "responses",
}
_STATUS_CODES: Mapping[int, str] = {
    200: "ok",
    400: "invalid_request",
    401: "authentication_error",
    404: "not_found",
    413: "request_too_large",
    429: "rate_limited",
    499: "request_cancelled",
    500: "internal_error",
    502: "upstream_error",
    503: "unavailable",
    504: "upstream_timeout",
}
_MAX_DURATION_MS = 2**63 - 1


def endpoint_class(method: object, path: object) -> str:
    """Map request identity to a closed class without retaining raw paths or queries."""
    if type(method) is not str or type(path) is not str:
        return "other"
    return _ENDPOINTS.get((method, path), "other")


def configure_logging(*, stream: TextIO | None = None) -> None:
    """Enable only the bridge's closed event stream for CLI operation."""

    _LOGGER.disabled = False
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    if not any(handler.get_name() == _HANDLER_NAME for handler in _LOGGER.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.set_name(_HANDLER_NAME)
        _LOGGER.addHandler(handler)
    for name in _NOISY_LOGGER_NAMES:
        logging.getLogger(name).disabled = True


def generic_status_code(status: object) -> str:
    """Return a closed generic outcome code."""
    if type(status) is not int:
        return "internal_error"
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    if 200 <= status < 300:
        return "ok"
    if 400 <= status < 500:
        return "client_error"
    if 500 <= status < 600:
        return "server_error"
    return "invalid_status"


def emit_request_log(
    *,
    request_id: object,
    endpoint: object,
    status: object,
    duration_ms: object,
    request_bytes: object = None,
    response_bytes: object = None,
) -> None:
    """Emit one bounded JSON event containing only closed scalar metadata."""
    if (
        type(request_id) is not str
        or _REQUEST_ID.fullmatch(request_id) is None
        or type(endpoint) is not str
        or endpoint not in {*_ENDPOINTS.values(), "other"}
        or type(status) is not int
        or not 100 <= status <= 599
        or type(duration_ms) is not int
    ):
        return
    event: dict[str, str | int] = {
        "request_id": request_id,
        "endpoint": endpoint,
        "status": status,
        "duration_ms": min(max(duration_ms, 0), _MAX_DURATION_MS),
        "code": generic_status_code(status),
    }
    for name, value in (
        ("request_bytes", request_bytes),
        ("response_bytes", response_bytes),
    ):
        if type(value) is int and 0 <= value <= _MAX_DURATION_MS:
            event[name] = value
    try:
        _LOGGER.info(
            json.dumps(
                event,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except Exception:
        # Observability is best-effort and must not affect request handling.
        return
