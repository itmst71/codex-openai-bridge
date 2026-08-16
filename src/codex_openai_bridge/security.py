"""Local bridge authentication material loading."""

from __future__ import annotations

import hmac
import os
import re
import stat
from pathlib import Path

_TOKEN_BYTES = 43
_MAX_TOKEN_FILE_BYTES = _TOKEN_BYTES + 1
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class TokenConfigurationError(RuntimeError):
    """Raised when the bridge client token cannot be loaded safely."""


def bearer_is_authorized(authorization: object, expected_token: object) -> bool:
    """Validate one exact Bearer credential using constant-time comparison."""
    if type(authorization) is not str or type(expected_token) is not str:
        return False
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return False
    candidate = authorization[len(prefix) :]
    if _TOKEN_PATTERN.fullmatch(candidate) is None:
        return False
    return hmac.compare_digest(candidate, expected_token)


def load_bridge_token(path: Path) -> str:
    """Load the client-facing Bearer token without following symlinks."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError
            raw = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        token = raw.decode("ascii", errors="strict")
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError
        return token
    except (OSError, UnicodeError, ValueError):
        raise TokenConfigurationError("bridge token is unavailable") from None
