"""Owner-controlled public-alias to upstream-model routing configuration."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

_MODEL_CONFIG_MAX_BYTES = 64 * 1024
_MODEL_CONFIG_MAX_ALIASES = 16


class ModelConfigurationError(ValueError):
    """Raised when the model routing authority is unavailable or invalid."""


def _open_directory_without_symlinks(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            writable_by_others = bool(stat.S_IMODE(metadata.st_mode) & 0o022)
            sticky_directory = bool(metadata.st_mode & stat.S_ISVTX)
            if writable_by_others and not sticky_directory:
                raise ValueError
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def model_map_entry_exists(path: Path) -> bool:
    """Return false only when the final entry is absent under a verified parent."""
    if not isinstance(path, Path) or not path.is_absolute() or not path.name:
        raise ModelConfigurationError("model configuration is unavailable")
    try:
        parent = _open_directory_without_symlinks(path.parent)
        try:
            try:
                os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
        finally:
            os.close(parent)
        return True
    except (OSError, ValueError):
        raise ModelConfigurationError("model configuration is unavailable") from None


def load_model_map(path: Path, *, identifier_pattern: re.Pattern[str]) -> Mapping[str, str]:
    """Load one bounded immutable model map without following path symlinks."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ModelConfigurationError("model configuration is unavailable")
    try:
        parent = _open_directory_without_symlinks(path.parent)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o022
                or before.st_nlink != 1
                or before.st_size > _MODEL_CONFIG_MAX_BYTES
            ):
                raise ValueError
            chunks: list[bytes] = []
            bytes_read = 0
            while bytes_read <= _MODEL_CONFIG_MAX_BYTES:
                chunk = os.read(descriptor, _MODEL_CONFIG_MAX_BYTES + 1 - bytes_read)
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            len(raw) > _MODEL_CONFIG_MAX_BYTES
            or len(raw) != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise ValueError
        document = tomllib.loads(raw.decode("utf-8", errors="strict"))
        if set(document) != {"version", "models"} or type(document["version"]) is not int:
            raise ValueError
        if document["version"] != 1 or type(document["models"]) is not dict:
            raise ValueError
        models = document["models"]
        if not 1 <= len(models) <= _MODEL_CONFIG_MAX_ALIASES or "codex" not in models:
            raise ValueError
        alias_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z", re.ASCII)
        fullmatch = identifier_pattern.fullmatch
        parsed: dict[str, str] = {}
        for alias, model in models.items():
            if (
                type(alias) is not str
                or type(model) is not str
                or alias_pattern.fullmatch(alias) is None
                or fullmatch(model) is None
            ):
                raise ValueError
            parsed[alias] = model
        ordered = {"codex": parsed["codex"]}
        ordered.update((alias, parsed[alias]) for alias in sorted(parsed) if alias != "codex")
        return MappingProxyType(ordered)
    except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError, AttributeError):
        raise ModelConfigurationError("model configuration is unavailable") from None
