"""Stateless model-scoped public continuation identifiers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import cast

_PREFIX = "cobr_c1_"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z", re.ASCII)
_STATE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_TOKEN = re.compile(r"cobr_c1_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}\Z", re.ASCII)
_MAX_TOKEN_BYTES = 768
_STATE_PREFIX = "cobr_s1_"
_STATE_TOKEN = re.compile(r"cobr_s1_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}\Z", re.ASCII)


class ContinuationError(ValueError):
    """Raised when a public continuation identifier is invalid."""


def derive_route_binding_key(
    binding_key: str,
    *,
    public_model: str,
    upstream_model: str,
) -> str:
    """Derive an internal binding key pinned to one alias-to-model route."""
    if (
        type(binding_key) is not str
        or not binding_key
        or type(public_model) is not str
        or _IDENTIFIER.fullmatch(public_model) is None
        or type(upstream_model) is not str
        or _MODEL_IDENTIFIER.fullmatch(upstream_model) is None
    ):
        raise ContinuationError("invalid continuation")
    try:
        key = binding_key.encode("utf-8", errors="strict")
        route = json.dumps(
            {"a": public_model, "m": upstream_model, "v": 1},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeError):
        raise ContinuationError("invalid continuation") from None
    return _urlsafe_encode(hmac.new(key, route, hashlib.sha256).digest())


def _payload(
    *,
    raw_id: str,
    public_model: str,
    kind: str,
    state: str | None,
) -> bytes:
    if (
        type(raw_id) is not str
        or _IDENTIFIER.fullmatch(raw_id) is None
        or type(public_model) is not str
        or _IDENTIFIER.fullmatch(public_model) is None
        or type(kind) is not str
        or _KIND.fullmatch(kind) is None
        or (state is not None and (type(state) is not str or _STATE.fullmatch(state) is None))
    ):
        raise ContinuationError("invalid continuation")
    try:
        return json.dumps(
            {"a": public_model, "k": kind, "r": raw_id, "s": state, "v": 1},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeError):
        raise ContinuationError("invalid continuation") from None


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    if not value or len(value) % 4 == 1:
        raise ContinuationError("invalid continuation")
    try:
        decoded = base64.b64decode(
            value.encode("ascii") + b"=" * ((4 - len(value) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError):
        raise ContinuationError("invalid continuation") from None
    if _urlsafe_encode(decoded) != value:
        raise ContinuationError("invalid continuation")
    return decoded


def encode_continuation_id(
    *,
    raw_id: str,
    public_model: str,
    kind: str,
    binding_key: str,
    state: str | None = None,
) -> str:
    """Create one reversible HMAC-authenticated public continuation identifier."""
    if type(binding_key) is not str or not binding_key:
        raise ContinuationError("invalid continuation")
    try:
        key = binding_key.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ContinuationError("invalid continuation") from None
    payload = _payload(raw_id=raw_id, public_model=public_model, kind=kind, state=state)
    tag = hmac.new(key, payload, hashlib.sha256).digest()
    token = f"{_PREFIX}{_urlsafe_encode(payload)}.{_urlsafe_encode(tag)}"
    if len(token.encode("ascii")) > _MAX_TOKEN_BYTES:
        raise ContinuationError("invalid continuation")
    return token


def decode_continuation_id(
    value: object,
    *,
    public_model: str,
    kind: str,
    binding_key: str,
    state: str | None = None,
    allow_legacy: bool = False,
) -> str:
    """Verify one public continuation identifier and recover its upstream value."""
    if type(value) is not str or len(value.encode("utf-8", errors="strict")) > _MAX_TOKEN_BYTES:
        raise ContinuationError("invalid continuation")
    if not value.startswith(_PREFIX):
        if allow_legacy and _IDENTIFIER.fullmatch(value) is not None:
            return value
        raise ContinuationError("invalid continuation")
    if _TOKEN.fullmatch(value) is None or type(binding_key) is not str or not binding_key:
        raise ContinuationError("invalid continuation")
    encoded_payload, encoded_tag = value[len(_PREFIX) :].split(".", 1)
    payload = _urlsafe_decode(encoded_payload)
    tag = _urlsafe_decode(encoded_tag)
    if len(tag) != hashlib.sha256().digest_size:
        raise ContinuationError("invalid continuation")
    try:
        key = binding_key.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ContinuationError("invalid continuation") from None
    expected_tag = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise ContinuationError("invalid continuation")
    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ContinuationError("invalid continuation") from None
    if type(document) is not dict or set(document) != {"a", "k", "r", "s", "v"}:
        raise ContinuationError("invalid continuation")
    raw_id = cast(str, document["r"])
    canonical = _payload(
        raw_id=raw_id,
        public_model=document["a"],
        kind=document["k"],
        state=document["s"],
    )
    if (
        canonical != payload
        or document["v"] != 1
        or document["a"] != public_model
        or document["k"] != kind
        or document["s"] != state
    ):
        raise ContinuationError("invalid continuation")
    return raw_id


def _state_payload(
    *, value: str, public_model: str, kind: str, max_value_bytes: int, state: str
) -> bytes:
    if (
        type(value) is not str
        or type(max_value_bytes) is not int
        or max_value_bytes <= 0
        or len(value.encode("utf-8", errors="strict")) > max_value_bytes
        or type(public_model) is not str
        or _IDENTIFIER.fullmatch(public_model) is None
        or type(kind) is not str
        or _KIND.fullmatch(kind) is None
        or type(state) is not str
        or _STATE.fullmatch(state) is None
    ):
        raise ContinuationError("invalid continuation")
    try:
        return json.dumps(
            {"a": public_model, "k": kind, "s": state, "v": 1, "x": value},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeError):
        raise ContinuationError("invalid continuation") from None


def encode_continuation_state(
    value: str,
    *,
    public_model: str,
    kind: str,
    binding_key: str,
    max_value_bytes: int,
    state: str,
) -> str:
    """Create one alias-bound reversible HMAC envelope for opaque state."""
    if type(binding_key) is not str or not binding_key:
        raise ContinuationError("invalid continuation")
    try:
        key = binding_key.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ContinuationError("invalid continuation") from None
    payload = _state_payload(
        value=value,
        public_model=public_model,
        kind=kind,
        max_value_bytes=max_value_bytes,
        state=state,
    )
    tag = hmac.new(key, payload, hashlib.sha256).digest()
    token = f"{_STATE_PREFIX}{_urlsafe_encode(payload)}.{_urlsafe_encode(tag)}"
    if len(token.encode("ascii")) > max_value_bytes:
        raise ContinuationError("invalid continuation")
    return token


def decode_continuation_state(
    value: object,
    *,
    public_model: str,
    kind: str,
    binding_key: str,
    max_value_bytes: int,
    state: str,
) -> str:
    """Verify one opaque-state envelope and recover its upstream value."""
    if type(value) is not str or type(max_value_bytes) is not int or max_value_bytes <= 0:
        raise ContinuationError("invalid continuation")
    try:
        encoded_value = value.encode("ascii", errors="strict")
    except UnicodeError:
        raise ContinuationError("invalid continuation") from None
    if len(encoded_value) > max_value_bytes or _STATE_TOKEN.fullmatch(value) is None:
        raise ContinuationError("invalid continuation")
    encoded_payload, encoded_tag = value[len(_STATE_PREFIX) :].split(".", 1)
    payload = _urlsafe_decode(encoded_payload)
    tag = _urlsafe_decode(encoded_tag)
    if len(tag) != hashlib.sha256().digest_size or type(binding_key) is not str or not binding_key:
        raise ContinuationError("invalid continuation")
    try:
        key = binding_key.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ContinuationError("invalid continuation") from None
    if not hmac.compare_digest(tag, hmac.new(key, payload, hashlib.sha256).digest()):
        raise ContinuationError("invalid continuation")
    try:
        document = json.loads(payload.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ContinuationError("invalid continuation") from None
    if type(document) is not dict or set(document) != {"a", "k", "s", "v", "x"}:
        raise ContinuationError("invalid continuation")
    recovered = cast(str, document["x"])
    canonical = _state_payload(
        value=recovered,
        public_model=document["a"],
        kind=document["k"],
        max_value_bytes=max_value_bytes,
        state=document["s"],
    )
    if (
        canonical != payload
        or document["v"] != 1
        or document["a"] != public_model
        or document["k"] != kind
        or document["s"] != state
    ):
        raise ContinuationError("invalid continuation")
    return recovered
