"""Non-disclosing route receipts carried by existing replayed IDs, not server sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_PREFIX = "cobr_t1_"
_DIGEST = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_INNER = re.compile(r"[A-Za-z0-9_.-]+\Z", re.ASCII)
_MAX_BYTES = 768


class RouteReceiptError(ValueError):
    """A continuation cannot prove one available original route."""


def _invalid() -> RouteReceiptError:
    return RouteReceiptError("invalid continuation route")


def _mac(key: str, value: bytes) -> str:
    if type(key) is not str or not key:
        raise _invalid()
    try:
        digest = hmac.new(key.encode("utf-8"), value, hashlib.sha256).digest()
    except UnicodeError:
        raise _invalid() from None
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def route_fingerprint(key: str, model: str) -> str:
    if type(model) is not str or _MODEL.fullmatch(model) is None:
        raise _invalid()
    return _mac(key, b"codex-bridge-route-fingerprint-v1\x00" + model.encode("ascii"))


@dataclass(frozen=True, slots=True)
class RouteReceipt:
    binding_key: str = field(repr=False)
    public_model: str
    upstream_model: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.public_model) is not str or _ALIAS.fullmatch(self.public_model) is None:
            raise _invalid()
        route_fingerprint(self.binding_key, self.upstream_model)

    @property
    def fingerprint(self) -> str:
        return route_fingerprint(self.binding_key, self.upstream_model)


_CURRENT: ContextVar[RouteReceipt | None] = ContextVar("bridge_route_receipt", default=None)


@contextmanager
def receipt_scope(receipt: RouteReceipt | None) -> Iterator[None]:
    token = _CURRENT.set(receipt)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def activate_receipt(receipt: RouteReceipt | None) -> None:
    """Set only within the request-wide receipt_scope which owns reset."""
    _CURRENT.set(receipt)


def _tag(key: str, alias: str, purpose: str, fingerprint: str, inner: str) -> str:
    payload = json.dumps(
        ["codex-bridge-route-receipt-v1", alias, purpose, fingerprint, inner],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _mac(key, payload)


def _read(value: str, *, key: str, alias: str, purpose: str) -> tuple[str, str]:
    if (
        type(value) is not str
        or not value.isascii()
        or len(value) > _MAX_BYTES
        or not value.startswith(_PREFIX)
    ):
        raise _invalid()
    try:
        actual_purpose, fingerprint, tail = value[len(_PREFIX) :].split(".", 2)
        inner, tag = tail.rsplit(".", 1)
    except ValueError:
        raise _invalid() from None
    if (
        actual_purpose != purpose
        or purpose not in ("i", "r")
        or _DIGEST.fullmatch(fingerprint) is None
        or _DIGEST.fullmatch(tag) is None
        or _INNER.fullmatch(inner) is None
        or not inner.startswith("cobr_c1_" if purpose == "i" else "cobr_r3_")
        or not hmac.compare_digest(tag, _tag(key, alias, purpose, fingerprint, inner))
    ):
        raise _invalid()
    return fingerprint, inner


def wrap_route_token(value: str, *, public_model: str | None, purpose: str) -> str:
    receipt = _CURRENT.get()
    if receipt is None:
        return value
    if public_model != receipt.public_model:
        raise _invalid()
    fingerprint = receipt.fingerprint
    tag = _tag(receipt.binding_key, receipt.public_model, purpose, fingerprint, value)
    token = f"{_PREFIX}{purpose}.{fingerprint}.{value}.{tag}"
    _read(token, key=receipt.binding_key, alias=receipt.public_model, purpose=purpose)
    return token


def unwrap_route_token(value: str, *, public_model: str, purpose: str) -> str:
    receipt = _CURRENT.get()
    if receipt is None:
        if value.startswith(_PREFIX):
            raise _invalid()
        return value
    if public_model != receipt.public_model:
        raise _invalid()
    fingerprint, inner = _read(
        value, key=receipt.binding_key, alias=receipt.public_model, purpose=purpose
    )
    if not hmac.compare_digest(fingerprint, receipt.fingerprint):
        raise _invalid()
    return inner


def _replayed_ids(document: object, surface: str) -> Iterator[tuple[str, str]]:
    if type(document) is not dict:
        return
    rows = document.get("messages" if surface == "chat" else "input")
    if type(rows) is not list:
        return
    for row in rows:
        if type(row) is not dict:
            continue
        if surface == "chat":
            if row.get("role") == "tool" and type(row.get("tool_call_id")) is str:
                yield row["tool_call_id"], "i"
            if row.get("role") != "assistant":
                continue
            for field_name, purpose in (("tool_calls", "i"), ("reasoning_details", "r")):
                items = row.get(field_name)
                if type(items) is list:
                    for item in items:
                        if type(item) is dict and type(item.get("id")) is str:
                            yield item["id"], purpose
        else:
            for field_name in ("id", "call_id"):
                if type(row.get(field_name)) is str:
                    yield row[field_name], "i"


def select_receipt_model(
    document: object,
    *,
    surface: str,
    public_model: str,
    models: frozenset[str],
    binding_key: str,
) -> str | None:
    """Authenticate all existing receipt locations before selecting an effective route."""
    fingerprints: set[str] = set()
    for value, purpose in _replayed_ids(document, surface):
        if value.startswith(_PREFIX):
            fingerprint, _inner = _read(value, key=binding_key, alias=public_model, purpose=purpose)
            fingerprints.add(fingerprint)
    if not fingerprints:
        return None
    if len(fingerprints) != 1:
        raise _invalid()
    fingerprint = next(iter(fingerprints))
    matching = [model for model in models if route_fingerprint(binding_key, model) == fingerprint]
    if len(matching) != 1:
        raise _invalid()
    return matching[0]
