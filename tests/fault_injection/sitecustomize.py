"""Deterministic filesystem faults for subprocess-level CLI tests only."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_fault = os.environ.get("HONEYMONEY_TEST_FS_FAULT", "")
_triggered = False
_directory_fault_armed = False
_real_fsync = os.fsync
_real_open = os.open
_real_replace = os.replace
_descriptor_paths: dict[int, str] = {}


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
    return Path(destination).name == expected


def _replace(source: object, destination: object) -> None:
    global _directory_fault_armed, _triggered
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
        not _triggered
        and fault_mode == "file-fsync"
        and expected in Path(descriptor_path).name
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
    os.replace = _replace
    os.fsync = _fsync
