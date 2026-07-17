"""Deterministic filesystem faults for subprocess-level CLI tests only."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_fault = os.environ.get("HONEYMONEY_TEST_FS_FAULT", "")
_triggered = False
_directory_fault_armed = False
_real_fsync = os.fsync
_real_replace = os.replace


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
    mode = os.fstat(descriptor).st_mode
    if not _triggered and _fault == "file-fsync" and stat.S_ISREG(mode):
        _triggered = True
        raise OSError("synthetic staged-file synchronization failure")
    if not _triggered and _fault == "directory-fsync" and stat.S_ISDIR(mode):
        _triggered = True
        raise OSError("synthetic directory synchronization failure")
    if not _triggered and _directory_fault_armed and stat.S_ISDIR(mode):
        _triggered = True
        raise OSError("synthetic post-replacement directory synchronization failure")
    _real_fsync(descriptor)


if _fault:
    os.replace = _replace
    os.fsync = _fsync
