#!/usr/bin/env python3
"""Render, verify, and atomically install the checkout's systemd user unit."""

from __future__ import annotations

import argparse
import ctypes
import os
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "deploy" / "systemd" / "codex-openai-bridge.service.in"
UNIT_NAME = "codex-openai-bridge.service"
_MAX_VERIFY_OUTPUT = 64 * 1024
_MAX_UNIT_BYTES = 64 * 1024
_VERIFY_TIMEOUT_SECONDS = 30.0
_TERMINATE_GRACE_SECONDS = 0.2
_READ_CHUNK_BYTES = 16 * 1024
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_PLACEHOLDERS = ("@BRIDGE_EXEC_START@", "@BRIDGE_ENVIRONMENT@", "@CODEX_HOME@")


class InstallError(ValueError):
    """Raised when a user-service unit cannot be rendered or installed safely."""


def _reject_control(value: str, *, name: str) -> None:
    if not value or any(unicodedata.category(character) == "Cc" for character in value):
        raise InstallError(f"{name} is invalid")


def _absolute_path(raw: str, *, name: str, must_exist: bool = True) -> Path:
    _reject_control(raw, name=name)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise InstallError(f"{name} must be an absolute path without parent traversal")
    if must_exist:
        try:
            return path.resolve(strict=True)
        except OSError:
            raise InstallError(f"{name} is unavailable") from None
    return path


def _validate_directory_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    sticky_root_directory = metadata.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in (0, os.geteuid())
        or (bool(mode & 0o022) and not sticky_root_directory)
    ):
        raise InstallError("destination parent is not an owner-controlled directory")


def _open_or_create_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise InstallError("destination parent is invalid")
    components = path.parts[1:]
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(Path("/") / components[0], flags)
        _validate_directory_descriptor(descriptor)
        for component in components[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            try:
                _validate_directory_descriptor(child)
            except BaseException:
                os.close(child)
                raise
            previous = descriptor
            descriptor = child
            os.close(previous)
        return descriptor
    except BaseException as error:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(error, (InstallError, OSError, UnicodeError)):
            raise InstallError("destination parent is unavailable") from None
        raise


def _directory_path_matches(path: Path, descriptor: int) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and current.st_dev == opened.st_dev
        and current.st_ino == opened.st_ino
    )


def _systemd_quote(value: str, *, name: str) -> str:
    _reject_control(value, name=name)
    if "%" in value or "$" in value:
        raise InstallError(f"{name} contains a systemd expansion character")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _validated_executable(path: Path, *, name: str) -> Path:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise InstallError(f"{name} must be an executable file")
    return path


def render_unit(*, checkout: Path, codex_path: Path, codex_home: Path) -> str:
    """Render one secret-free unit for exact machine-local absolute paths."""
    checkout = _absolute_path(str(checkout), name="checkout")
    if not checkout.is_dir():
        raise InstallError("checkout must be a directory")
    bridge_python = _validated_executable(
        checkout / ".venv" / "bin" / "python", name="checkout virtualenv Python"
    )
    codex_path = _validated_executable(
        _absolute_path(str(codex_path), name="Codex executable"), name="Codex executable"
    )
    codex_home = _absolute_path(str(codex_home), name="Codex home")
    if not codex_home.is_dir():
        raise InstallError("Codex home must be a directory")

    executable_argv = " ".join(
        (
            _systemd_quote(str(bridge_python), name="bridge Python path"),
            "-m",
            "codex_openai_bridge",
            "serve",
        )
    )
    environment = " ".join(
        _systemd_quote(value, name="service environment")
        for value in (
            "CODEX_BRIDGE_HOST=127.0.0.1",
            "PYTHONPATH=",
            f"CODEX_BRIDGE_CODEX_PATH={codex_path}",
            f"CODEX_BRIDGE_CODEX_HOME={codex_home}",
        )
    )
    rendered = TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "@BRIDGE_EXEC_START@": executable_argv,
        "@BRIDGE_ENVIRONMENT@": environment,
        "@CODEX_HOME@": _systemd_quote(str(codex_home), name="Codex home"),
    }
    for placeholder, value in replacements.items():
        if rendered.count(placeholder) != 1:
            raise InstallError(f"unit template has invalid placeholder {placeholder}")
        rendered = rendered.replace(placeholder, value)
    if any(placeholder in rendered for placeholder in _PLACEHOLDERS):
        raise InstallError("unit template contains an unresolved placeholder")
    return rendered


def _destination_identity(
    directory_descriptor: int, name: str, *, force: bool
) -> tuple[int, ...] | None:
    try:
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not force:
        raise InstallError("destination already exists; use --force to replace it")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise InstallError("existing destination is not an owner-controlled regular file")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _descriptor_identity(descriptor: int) -> tuple[int, ...]:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise InstallError("temporary unit is not an owner-controlled regular file")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _entry_matches(directory_descriptor: int, name: str, identity: tuple[int, ...]) -> bool:
    try:
        return _destination_identity(directory_descriptor, name, force=True) == identity
    except InstallError:
        return False


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    process_group = process.pid
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        if not _process_group_exists(process_group):
            break
        try:
            os.killpg(process_group, signal_number)
        except OSError:
            pass
        deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        while _process_group_exists(process_group) and time.monotonic() < deadline:
            try:
                process.wait(timeout=0.02)
            except (OSError, subprocess.TimeoutExpired):
                pass
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _run_verifier(argv: list[str], *, pass_fds: tuple[int, ...] = ()) -> tuple[bytes, bytes]:
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise InstallError("systemd unit verification failed")
        selector = selectors.DefaultSelector()
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ, name)
        deadline = time.monotonic() + _VERIFY_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstallError("systemd unit verification failed")
            for key, _events in selector.select(remaining):
                output = outputs[key.data]
                read_size = min(_READ_CHUNK_BYTES, _MAX_VERIFY_OUTPUT + 1 - len(output))
                chunk = os.read(key.fd, max(1, read_size))
                if chunk:
                    output.extend(chunk)
                    if len(output) > _MAX_VERIFY_OUTPUT:
                        raise InstallError("systemd unit verification failed")
                else:
                    selector.unregister(key.fd)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError("systemd unit verification failed")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise InstallError("systemd unit verification failed") from None
        if return_code != 0:
            raise InstallError("systemd unit verification failed")
        return bytes(outputs["stdout"]), bytes(outputs["stderr"])
    except OSError:
        raise InstallError("systemd unit verification failed") from None
    finally:
        if process is not None:
            _terminate_process_group(process)
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass


def _read_descriptor_bytes(descriptor: int) -> bytes:
    output = bytearray()
    offset = 0
    while len(output) <= _MAX_UNIT_BYTES:
        chunk = os.pread(
            descriptor,
            min(_READ_CHUNK_BYTES, _MAX_UNIT_BYTES + 1 - len(output)),
            offset,
        )
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        offset += len(chunk)
    raise InstallError("systemd unit verification failed")


def _verify_unit(descriptor: int) -> None:
    executable = shutil.which("systemd-analyze")
    if executable is None:
        raise InstallError("systemd-analyze is unavailable")
    before = _read_descriptor_bytes(descriptor)
    with tempfile.TemporaryDirectory(prefix="codex-unit-verify-") as temporary:
        path = Path(temporary) / UNIT_NAME
        path.write_bytes(before)
        path.chmod(0o600)
        _run_verifier([executable, "--user", "verify", str(path)])
    if _read_descriptor_bytes(descriptor) != before:
        raise InstallError("temporary unit changed during verification")


def _renameat2(
    directory_descriptor: int,
    source_name: str,
    destination_name: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise InstallError("atomic no-replace rename is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        directory_descriptor,
        os.fsencode(source_name),
        directory_descriptor,
        os.fsencode(destination_name),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _rename_noreplace(directory_descriptor: int, source_name: str, destination_name: str) -> None:
    _renameat2(
        directory_descriptor,
        source_name,
        destination_name,
        _RENAME_NOREPLACE,
    )


def _rename_exchange(directory_descriptor: int, source_name: str, destination_name: str) -> None:
    _renameat2(
        directory_descriptor,
        source_name,
        destination_name,
        _RENAME_EXCHANGE,
    )


def _fsync_directory(directory_descriptor: int) -> None:
    os.fsync(directory_descriptor)


def _raw_entry_identity(directory_descriptor: int, name: str) -> tuple[int, ...] | None:
    try:
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _opened_directory_path(directory_descriptor: int, lexical_path: Path) -> Path | None:
    opened = os.fstat(directory_descriptor)
    candidates: list[Path] = []
    try:
        link = os.readlink(f"/proc/self/fd/{directory_descriptor}")
    except OSError:
        pass
    else:
        if not link.endswith(" (deleted)"):
            candidates.append(Path(link))
    candidates.append(lexical_path)
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        try:
            metadata = os.stat(candidate, follow_symlinks=False)
        except OSError:
            continue
        if (
            stat.S_ISDIR(metadata.st_mode)
            and metadata.st_dev == opened.st_dev
            and metadata.st_ino == opened.st_ino
        ):
            return candidate
    return None


def _retained_paths(
    directory_descriptor: int,
    directory_path: Path,
    destination_name: str,
    temporary_name: str | None,
    *,
    candidate_identity: tuple[int, ...] | None = None,
    prior_identity: tuple[int, ...] | None = None,
) -> str:
    actual_parent = _opened_directory_path(directory_descriptor, directory_path)
    opened = os.fstat(directory_descriptor)
    interesting_names = {destination_name}
    if temporary_name is not None:
        interesting_names.add(temporary_name)
    entries: list[tuple[str, str]] = []
    candidate_found = False
    prior_found = False
    try:
        iterator = os.scandir(directory_descriptor)
    except OSError:
        iterator = None
    if iterator is not None:
        with iterator:
            for index, entry in enumerate(iterator):
                if index >= 4096:
                    break
                identity = _raw_entry_identity(directory_descriptor, entry.name)
                role: str | None = None
                if candidate_identity is not None and identity == candidate_identity:
                    role = "candidate"
                    candidate_found = True
                elif prior_identity is not None and identity == prior_identity:
                    role = "prior unit"
                    prior_found = True
                elif entry.name in interesting_names:
                    role = "unexpected entry"
                if role is None:
                    continue
                if actual_parent is not None:
                    locator = str(actual_parent / entry.name)
                else:
                    locator = (
                        f"opened-parent(dev={opened.st_dev},ino={opened.st_ino})/"
                        f"{entry.name} (absolute location unavailable)"
                    )
                entries.append((entry.name, f"{role}: {locator}"))
    for role, identity, found in (
        ("candidate", candidate_identity, candidate_found),
        ("prior unit", prior_identity, prior_found),
    ):
        if identity is None or found:
            continue
        entries.append(
            (
                f"~{role}",
                f"{role}: opened-parent(dev={opened.st_dev},ino={opened.st_ino})/"
                f"unknown-basename (retained dev={identity[0]},ino={identity[1]})",
            )
        )
    entries.sort(key=lambda item: item[0])
    return ", ".join(value for _name, value in entries) if entries else "none"


def install_unit(*, rendered: str, destination: Path, force: bool) -> Path | None:
    """Verify and atomically install; retain displaced entries instead of unlinking them."""
    destination = _absolute_path(str(destination), name="destination", must_exist=False)
    if destination.name != UNIT_NAME:
        raise InstallError(f"destination filename must be {UNIT_NAME}")

    parent_descriptor = _open_or_create_directory(destination.parent)
    temporary_name: str | None = None
    candidate_identity: tuple[int, ...] | None = None
    expected_destination: tuple[int, ...] | None = None
    descriptor = -1
    try:
        temporary_name = f".{destination.stem}.{secrets.token_hex(8)}.service"
        if not _directory_path_matches(destination.parent, parent_descriptor):
            raise InstallError("destination parent changed during installation")
        expected_destination = _destination_identity(
            parent_descriptor, destination.name, force=force
        )
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o644, dir_fd=parent_descriptor)
        _write_all(descriptor, rendered.encode("utf-8", errors="strict"))
        os.fsync(descriptor)
        candidate_identity = _descriptor_identity(descriptor)
        _verify_unit(descriptor)
        try:
            descriptor_unchanged = _descriptor_identity(descriptor) == candidate_identity
        except InstallError:
            descriptor_unchanged = False
        if not descriptor_unchanged or not _entry_matches(
            parent_descriptor, temporary_name, candidate_identity
        ):
            raise InstallError(
                f"temporary unit changed during verification; retained as {temporary_name}"
            )
        if not _directory_path_matches(destination.parent, parent_descriptor):
            raise InstallError(
                f"destination parent changed during verification; candidate retained as "
                f"{temporary_name}"
            )
        if (
            _destination_identity(parent_descriptor, destination.name, force=True)
            != expected_destination
        ):
            raise InstallError(
                f"destination changed during verification; candidate retained as {temporary_name}"
            )
        if not _entry_matches(parent_descriptor, temporary_name, candidate_identity):
            raise InstallError(
                f"temporary unit changed before commit; retained as {temporary_name}"
            )

        if expected_destination is None:
            _rename_noreplace(parent_descriptor, temporary_name, destination.name)
            try:
                if not _entry_matches(
                    parent_descriptor, destination.name, candidate_identity
                ) or not _directory_path_matches(destination.parent, parent_descriptor):
                    raise InstallError("destination or parent changed during commit")
                _fsync_directory(parent_descriptor)
                if not _entry_matches(
                    parent_descriptor, destination.name, candidate_identity
                ) or not _directory_path_matches(destination.parent, parent_descriptor):
                    raise InstallError("destination or parent changed during commit")
            except BaseException as error:
                try:
                    _fsync_directory(parent_descriptor)
                except BaseException:
                    pass
                actual = _retained_paths(
                    parent_descriptor,
                    destination.parent,
                    destination.name,
                    temporary_name,
                    candidate_identity=candidate_identity,
                )
                raise InstallError(f"initial install failed; retained entries: {actual}") from error
            return None
        else:
            exchanged = False
            try:
                _rename_exchange(parent_descriptor, temporary_name, destination.name)
                exchanged = True
                if (
                    not _entry_matches(parent_descriptor, temporary_name, expected_destination)
                    or not _entry_matches(parent_descriptor, destination.name, candidate_identity)
                    or not _directory_path_matches(destination.parent, parent_descriptor)
                ):
                    raise InstallError("destination or parent changed during commit")
                _fsync_directory(parent_descriptor)
                if (
                    not _entry_matches(parent_descriptor, temporary_name, expected_destination)
                    or not _entry_matches(parent_descriptor, destination.name, candidate_identity)
                    or not _directory_path_matches(destination.parent, parent_descriptor)
                ):
                    raise InstallError("destination or parent changed during commit")
            except BaseException as error:
                if exchanged:
                    destination_is_candidate = _entry_matches(
                        parent_descriptor, destination.name, candidate_identity
                    )
                    temporary_is_prior = _entry_matches(
                        parent_descriptor, temporary_name, expected_destination
                    )
                    if not destination_is_candidate or not temporary_is_prior:
                        retained = (
                            f"; prior unit retained as {temporary_name}"
                            if temporary_is_prior
                            else f"; unexpected entry retained as {temporary_name}"
                        )
                        raise InstallError(
                            "destination changed before atomic rollback" + retained
                        ) from error
                    try:
                        _rename_exchange(parent_descriptor, temporary_name, destination.name)
                    except BaseException as rollback_error:
                        actual = _retained_paths(
                            parent_descriptor,
                            destination.parent,
                            destination.name,
                            temporary_name,
                            candidate_identity=candidate_identity,
                            prior_identity=expected_destination,
                        )
                        raise InstallError(
                            f"atomic rollback failed; retained entries: {actual}"
                        ) from rollback_error
                    try:
                        _fsync_directory(parent_descriptor)
                    except BaseException:
                        pass
                    actual = _retained_paths(
                        parent_descriptor,
                        destination.parent,
                        destination.name,
                        temporary_name,
                        candidate_identity=candidate_identity,
                        prior_identity=expected_destination,
                    )
                    destination_is_prior = _entry_matches(
                        parent_descriptor, destination.name, expected_destination
                    )
                    temporary_is_candidate = _entry_matches(
                        parent_descriptor, temporary_name, candidate_identity
                    )
                    if destination_is_prior and temporary_is_candidate:
                        raise InstallError(
                            f"force install rolled back; retained entries: {actual}"
                        ) from error
                    retained_kind = (
                        "unexpected entry" if not temporary_is_candidate else "candidate"
                    )
                    raise InstallError(
                        f"force rollback encountered a race; {retained_kind} retained as "
                        f"{temporary_name}"
                    ) from error
                raise
        return destination.parent / temporary_name
    except BaseException as error:
        if isinstance(error, InstallError) and "retained entries:" in str(error):
            raise
        retained = _retained_paths(
            parent_descriptor,
            destination.parent,
            destination.name,
            temporary_name,
            candidate_identity=candidate_identity,
            prior_identity=expected_destination,
        )
        raise InstallError(f"{error}; retained entries: {retained}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, help="absolute project checkout path")
    parser.add_argument("--codex-path", required=True, help="absolute official Codex CLI path")
    parser.add_argument(
        "--codex-home",
        default=str(Path.home() / ".codex"),
        help="absolute Codex home (default: ~/.codex)",
    )
    parser.add_argument(
        "--destination",
        default=str(Path.home() / ".config" / "systemd" / "user" / UNIT_NAME),
        help="absolute generated user-unit path",
    )
    parser.add_argument("--force", action="store_true", help="replace an owner-controlled unit")
    return parser


def _fail(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(message)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        rendered = render_unit(
            checkout=Path(arguments.checkout),
            codex_path=Path(arguments.codex_path),
            codex_home=Path(arguments.codex_home),
        )
        destination = Path(arguments.destination)
        retained = install_unit(rendered=rendered, destination=destination, force=arguments.force)
    except (InstallError, OSError, UnicodeError) as error:
        _fail(parser, str(error))
    print(f"installed verified user unit: {destination}")
    if retained is not None:
        print(f"retained previous unit: {retained}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
