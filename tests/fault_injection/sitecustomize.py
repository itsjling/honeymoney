"""Deterministic filesystem faults for subprocess-level CLI tests only."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
from pathlib import Path, PurePosixPath

_fault = os.environ.get("HONEYMONEY_TEST_FS_FAULT", "")
_triggered = False
_match_count = 0
_directory_fault_armed = False
_real_fsync = os.fsync
_real_fdopen = os.fdopen
_real_open = os.open
_real_replace = os.replace
_descriptor_paths: dict[int, str] = {}
_STAGED_NAME = re.compile(r"[0-9]{8}\.(?:old|new|install)\Z")


def _parse_fault(value: str) -> tuple[str, str, str, int] | None:
    """Read a test-only action, operation, target, and matching ordinal."""
    parts = value.split(":")
    if len(parts) != 4:
        return None
    action, operation, target, raw_ordinal = parts
    if action not in {"fail", "stop"} or operation not in {
        "staged-write",
        "staged-file-fsync",
        "replacement",
        "directory-fsync",
        "index-commit",
        "lock-acquired",
    }:
        return None
    try:
        ordinal = int(raw_ordinal)
    except ValueError:
        return None
    if ordinal < 1:
        return None
    candidate = PurePosixPath(target)
    if (
        "\\" in target
        or candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return None
    return action, operation, candidate.as_posix(), ordinal


_structured_fault = _parse_fault(_fault)


def _open(
    path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
) -> int:
    if dir_fd is None:
        descriptor = _real_open(path, flags, mode)
    else:
        descriptor = _real_open(path, flags, mode, dir_fd=dir_fd)
    _descriptor_paths[descriptor] = os.fspath(path)
    return descriptor


def _matches_target(destination: object, expected: str) -> bool:
    path = Path(os.fspath(destination))
    logical = _staged_target(path)
    return logical == expected or path.as_posix().endswith(f"/{expected}")


def _staged_target(path: Path) -> str | None:
    """Map a retained staged file to its journaled workspace target."""
    if _STAGED_NAME.fullmatch(path.name) is None:
        return None
    try:
        number = int(path.name.split(".", maxsplit=1)[0])
        recovery = path.parent
        if (
            recovery.parent.name != "publication"
            or recovery.parent.parent.name != ".honeymoney"
        ):
            return None
        journal = recovery.parent.parent / "publication-journal.json"
        document = json.loads(journal.read_text(encoding="utf-8"))
        entries = document["entries"]
        target = entries[number]["target"]
    except IndexError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError:
        return None
    return target if isinstance(target, str) else None


def _trigger(operation: str, path: Path) -> None:
    global _match_count, _triggered
    if _triggered or _structured_fault is None:
        return
    action, expected_operation, expected_target, ordinal = _structured_fault
    if operation != expected_operation or not _matches_target(path, expected_target):
        return
    _match_count += 1
    if _match_count != ordinal:
        return
    _triggered = True
    if action == "fail":
        raise OSError(f"synthetic {operation} failure")
    ready = os.environ.get("HONEYMONEY_TEST_FS_FAULT_READY")
    if ready:
        descriptor = _real_open(ready, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, f"{operation}:{expected_target}\n".encode())
            _real_fsync(descriptor)
        finally:
            os.close(descriptor)
    os.kill(os.getpid(), signal.SIGSTOP)


class _FaultingWriter:
    """Trigger immediately before one retained staged-file write."""

    def __init__(self, handle: object, path: Path) -> None:
        self._handle = handle
        self._path = path

    def __enter__(self) -> _FaultingWriter:
        self._handle.__enter__()  # type: ignore[union-attr]
        return self

    def __exit__(self, *arguments: object) -> object:
        return self._handle.__exit__(*arguments)  # type: ignore[union-attr]

    def write(self, content: object) -> object:
        _trigger("staged-write", self._path)
        return self._handle.write(content)  # type: ignore[union-attr]

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)


def _fdopen(descriptor: int, *arguments: object, **keywords: object) -> object:
    handle = _real_fdopen(descriptor, *arguments, **keywords)
    path = Path(_descriptor_paths.get(descriptor, ""))
    mode = str(arguments[0]) if arguments else str(keywords.get("mode", "r"))
    if (
        _structured_fault is not None
        and _structured_fault[1] == "staged-write"
        and "w" in mode
        and _STAGED_NAME.fullmatch(path.name) is not None
    ):
        return _FaultingWriter(handle, path)
    return handle


def _replace(source: object, destination: object) -> None:
    global _directory_fault_armed, _triggered
    if _structured_fault is not None:
        target = Path(os.fspath(destination))
        _trigger("replacement", target)
        _real_replace(source, destination)
        _trigger("index-commit", target)
        return
    mode, _, expected = _fault.partition(":")
    if not _triggered and _matches_target(destination, expected):
        if mode == "replace-before":
            _triggered = True
            raise OSError("synthetic replacement failure")
        if mode == "replace-after":
            _triggered = True
            _real_replace(source, destination)
            os._exit(75)
    _real_replace(source, destination)
    if mode == "directory-fsync-after" and _matches_target(destination, expected):
        _directory_fault_armed = True


def _fsync(descriptor: int) -> None:
    global _triggered
    descriptor_mode = os.fstat(descriptor).st_mode
    fault_mode, _, expected = _fault.partition(":")
    descriptor_path = _descriptor_paths.get(descriptor, "")
    for link_root in ("/dev/fd", "/proc/self/fd"):
        try:
            descriptor_path = os.readlink(f"{link_root}/{descriptor}")
            break
        except OSError:
            continue
    if (
        _structured_fault is not None
        and stat.S_ISREG(descriptor_mode)
        and _STAGED_NAME.fullmatch(Path(descriptor_path).name) is not None
    ):
        _trigger("staged-file-fsync", Path(descriptor_path))
    if (
        _structured_fault is not None
        and stat.S_ISREG(descriptor_mode)
        and Path(descriptor_path).name == "workspace.lock"
    ):
        _trigger("lock-acquired", Path(descriptor_path))
    if _structured_fault is not None and stat.S_ISDIR(descriptor_mode):
        _trigger("directory-fsync", Path(descriptor_path))
    if (
        not _triggered
        and fault_mode == "file-fsync"
        and expected in Path(descriptor_path).name
        and Path(descriptor_path).name.endswith(".new")
        and stat.S_ISREG(descriptor_mode)
    ):
        _triggered = True
        raise OSError("synthetic staged-file synchronization failure")
    if not _triggered and _fault == "directory-fsync" and stat.S_ISDIR(descriptor_mode):
        _triggered = True
        raise OSError("synthetic directory synchronization failure")
    if not _triggered and _directory_fault_armed and stat.S_ISDIR(descriptor_mode):
        _triggered = True
        raise OSError("synthetic post-replacement directory synchronization failure")
    _real_fsync(descriptor)


if _fault:
    os.open = _open
    os.fdopen = _fdopen
    os.replace = _replace
    os.fsync = _fsync

if os.environ.get("HONEYMONEY_TEST_OFFLINE") == "1":
    import ssl

    from scripts.offline_network_guard import install

    del ssl
    install()
