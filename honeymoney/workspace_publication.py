"""Recoverable, value-free workspace generation publication.

This module moves opaque bytes.  It does not parse or derive financial data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

from honeymoney.import_records import MAX_ATTEMPT_BYTES, attempt_document

JOURNAL_SCHEMA_VERSION = 2
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
INDEX_PATH = ".honeymoney/workspace-index.json"
JOURNAL_PATH = ".honeymoney/publication-journal.json"
LOCK_PATH = ".honeymoney/workspace.lock"
_GENERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_ATTEMPT_TARGET_RE = re.compile(
    r"\.honeymoney/import-records/(src_[0-9a-f]{64})/attempts/([0-9]{8})\.json\Z"
)


class PublicationError(OSError):
    """The retained publication cannot safely proceed."""


class WorkspaceBusyError(PublicationError):
    """A live or unverifiable process owns the workspace lock."""

    code = "workspace_busy"


@dataclass(frozen=True)
class PublicationTarget:
    """One workspace-relative file replacement or removal."""

    path: str
    content: bytes | None
    mode: int = PRIVATE_FILE_MODE


@dataclass(frozen=True)
class PublicationDirectory:
    """One workspace-relative directory creation or mode repair."""

    path: str
    mode: int = PRIVATE_DIRECTORY_MODE


@dataclass(frozen=True)
class AttemptReservation:
    """One immutable report finalized according to the index commit winner."""

    path: str
    success_content: bytes
    interrupted_content: bytes


@dataclass(frozen=True)
class PendingAttemptReservation:
    """One attempt reserved before its source work starts."""

    path: str
    interrupted_content: bytes


@dataclass(frozen=True)
class FixedAttemptReservation:
    """One known failure report that does not depend on the generation winner."""

    path: str
    failure_content: bytes


class _Entry(TypedDict):
    target: str
    entry_kind: Literal["file", "directory"]
    operation: Literal["write", "remove", "ensure"]
    old_exists: bool
    new_exists: bool
    old_mode: int | None
    new_mode: int | None
    old_path: str | None
    new_path: str | None
    old_sha256: str | None
    new_sha256: str | None


class _AttemptEntry(TypedDict):
    kind: Literal["reserved", "generation", "fixed_failure"]
    target: str
    mode: int
    success_sha256: str
    interrupted_sha256: str
    success_document: str
    interrupted_document: str


class _Journal(TypedDict):
    schema_version: int
    generation_id: str
    phase: Literal["reserved", "staging", "prepared", "committed"]
    commit_policy: Literal["index", "fixed"]
    index_target: str
    index_commit_sha256: str
    entries: list[_Entry]
    attempts: list[_AttemptEntry]


_JOURNAL_FIELDS = frozenset(
    {
        "schema_version",
        "generation_id",
        "phase",
        "commit_policy",
        "index_target",
        "index_commit_sha256",
        "entries",
        "attempts",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "kind",
        "target",
        "mode",
        "success_sha256",
        "interrupted_sha256",
        "success_document",
        "interrupted_document",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "target",
        "entry_kind",
        "operation",
        "old_exists",
        "new_exists",
        "old_mode",
        "new_mode",
        "old_path",
        "new_path",
        "old_sha256",
        "new_sha256",
    }
)


class WorkspaceLock:
    """An exclusive lock whose owner can be checked after a stopped process."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / LOCK_PATH
        self._owned = False

    def acquire(self) -> None:
        _ensure_directory(self.root / ".honeymoney", harden_existing=False)
        payload = (
            json.dumps(
                {"schema_version": 1, "pid": os.getpid()}, sort_keys=True
            ).encode()
            + b"\n"
        )
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                PRIVATE_FILE_MODE,
            )
        except FileExistsError as error:
            status = inspect_lock(self.root)
            message = (
                "workspace busy" if status == "live" else f"workspace lock is {status}"
            )
            raise WorkspaceBusyError(message) from error
        try:
            os.write(descriptor, payload)
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.path.parent)
        self._owned = True

    def release(self) -> None:
        if not self._owned:
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value != {"pid": os.getpid(), "schema_version": 1}:
                raise PublicationError("workspace lock ownership changed")
            self.path.unlink()
            _fsync_directory(self.path.parent)
            if not (self.path.parent / "workspace-index.json").exists():
                try:
                    self.path.parent.rmdir()
                    _fsync_directory(self.root)
                except OSError:
                    pass
        finally:
            self._owned = False

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def inspect_lock(root: Path) -> Literal["absent", "live", "stale", "unknown"]:
    path = root.resolve() / LOCK_PATH
    if not path.exists():
        return "absent"
    if path.is_symlink() or not path.is_file():
        return "unknown"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"schema_version", "pid"} or value["schema_version"] != 1:
            return "unknown"
        pid = value["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "unknown"
        os.kill(pid, 0)
        return "live"
    except ProcessLookupError:
        return "stale"
    except OSError, ValueError, TypeError, json.JSONDecodeError:
        return "unknown"


def reserve_publication(
    root: Path,
    generation_id: str,
    attempt_reports: list[PendingAttemptReservation],
) -> None:
    """Durably reserve attempt numbers before an accepted command starts work."""
    root = root.resolve()
    _require_owned_lock(root)
    if (root / JOURNAL_PATH).exists():
        raise PublicationError("publication recovery is required")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ValueError("invalid generation id")
    if not attempt_reports:
        raise ValueError("publication reservation requires an attempt")
    attempts = [_prepare_pending_attempt(root, item) for item in attempt_reports]
    attempt_paths = [item["target"] for item in attempts]
    if len(set(attempt_paths)) != len(attempt_paths):
        raise ValueError("duplicate attempt report target")
    index_digest = _path_digest(root / INDEX_PATH)
    if index_digest is None:
        raise PublicationError("workspace index is missing")
    recovery_root = root / ".honeymoney" / "publication" / generation_id
    _ensure_safe_target(root, recovery_root)
    entries, retained_entries, _ = _prepare_generation_entries(
        root,
        recovery_root,
        [],
        None,
        None,
        attempt_paths,
    )
    journal: _Journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "generation_id": generation_id,
        "phase": "reserved",
        "commit_policy": "index",
        "index_target": INDEX_PATH,
        "index_commit_sha256": index_digest,
        "entries": [],
        "attempts": attempts,
    }
    try:
        _write_journal(root / JOURNAL_PATH, journal)
    except Exception as error:
        raise PublicationError(
            f"publication failed; recovery retained at {JOURNAL_PATH}"
        ) from error


def _prepare_generation_entries(
    root: Path,
    recovery_root: Path,
    targets: list[PublicationTarget],
    workspace_index: bytes | None,
    directory_targets: list[PublicationDirectory] | None,
    attempt_targets: list[str],
) -> tuple[list[_Entry], list[tuple[_Entry, bytes | None]], _Entry | None]:
    all_targets = list(targets)
    if workspace_index is not None:
        all_targets.append(PublicationTarget(INDEX_PATH, workspace_index))
    file_paths = [_validate_relative_path(item.path) for item in all_targets]
    if len(set(file_paths)) != len(file_paths):
        raise ValueError("duplicate publication target")
    if workspace_index is not None and INDEX_PATH in file_paths[:-1]:
        raise ValueError("workspace index is supplied separately")
    if any(item.mode != PRIVATE_FILE_MODE for item in all_targets):
        raise ValueError("publication targets must use owner-only mode")

    directories = directory_targets or []
    directory_paths = [_validate_directory_path(item.path) for item in directories]
    if len(set(directory_paths)) != len(directory_paths):
        raise ValueError("duplicate publication directory")
    directories_by_path = dict(zip(directory_paths, directories, strict=True))
    if set(file_paths) & set(directory_paths):
        raise ValueError("publication target is both a file and a directory")
    if any(
        item.mode != PRIVATE_DIRECTORY_MODE for item in directories_by_path.values()
    ):
        raise ValueError("publication directories must use owner-only mode")

    for relative, item in zip(file_paths, all_targets, strict=True):
        if item.content is not None:
            _add_missing_parent_directories(
                root,
                relative,
                directories_by_path,
            )
    for relative in attempt_targets:
        _add_missing_parent_directories(root, relative, directories_by_path)

    ordered_directories = sorted(
        directories_by_path.items(),
        key=lambda pair: (len(PurePosixPath(pair[0]).parts), pair[0]),
    )
    entries: list[_Entry] = []
    retained: list[tuple[_Entry, bytes | None]] = []
    for relative, directory in ordered_directories:
        entry = _prepare_directory_entry(root, relative, directory)
        entries.append(entry)
        retained.append((entry, None))
    for number, (relative, target) in enumerate(
        zip(file_paths, all_targets, strict=True),
        start=len(entries),
    ):
        entry = _prepare_entry(root, recovery_root, number, relative, target)
        entries.append(entry)
        retained.append((entry, target.content))
    index_entry = next(
        (entry for entry in entries if entry["target"] == INDEX_PATH),
        None,
    )
    return entries, retained, index_entry


def _add_missing_parent_directories(
    root: Path,
    relative: str,
    directories: dict[str, PublicationDirectory],
) -> None:
    target = root / relative
    _ensure_safe_target(root, target)
    parent = target.parent
    while parent != root:
        try:
            directory_relative = parent.relative_to(root).as_posix()
        except ValueError as error:
            raise ValueError("publication directory escapes workspace") from error
        if directory_relative not in directories and not parent.exists():
            directories[directory_relative] = PublicationDirectory(directory_relative)
        elif parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise ValueError(f"target parent is not a directory: {directory_relative}")
        parent = parent.parent


def publish_generation(
    root: Path,
    generation_id: str,
    targets: list[PublicationTarget],
    workspace_index: bytes,
    *,
    attempt_reports: list[AttemptReservation | FixedAttemptReservation] | None = None,
    directory_targets: list[PublicationDirectory] | None = None,
) -> None:
    """Publish opaque bytes, retaining failures for doctor-only settlement.

    The caller must hold ``WorkspaceLock(root)`` for the whole accepted command.
    """
    root = root.resolve()
    _require_owned_lock(root)
    if (root / JOURNAL_PATH).exists():
        raise PublicationError("publication recovery is required")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ValueError("invalid generation id")
    _validate_index_generation(workspace_index, generation_id)

    reservations = attempt_reports or []
    attempts = [_prepare_attempt(root, item) for item in reservations]
    attempt_paths = [item["target"] for item in attempts]
    if len(set(attempt_paths)) != len(attempt_paths):
        raise ValueError("duplicate attempt report target")
    file_paths = [_validate_relative_path(item.path) for item in targets]
    if set(attempt_paths) & set(file_paths):
        raise ValueError("attempt report is not a rollback target")

    recovery_root = root / ".honeymoney" / "publication" / generation_id
    _ensure_safe_target(root, recovery_root)
    entries, retained_entries, index_entry = _prepare_generation_entries(
        root,
        recovery_root,
        targets,
        workspace_index,
        directory_targets,
        attempt_paths,
    )
    assert index_entry is not None
    journal: _Journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "generation_id": generation_id,
        "phase": "staging",
        "commit_policy": "index",
        "index_target": INDEX_PATH,
        "index_commit_sha256": cast(str, index_entry["new_sha256"]),
        "entries": entries,
        "attempts": attempts,
    }
    journal_path = root / JOURNAL_PATH
    try:
        _write_journal(journal_path, journal)
        _ensure_directory(recovery_root)
        for entry, content in retained_entries:
            _retain_entry(root, entry, content)
        _fsync_recovery_chain(root, recovery_root)
        journal["phase"] = "prepared"
        _write_journal(journal_path, journal)
        _install_generation_entries(root, entries, use_new=True)
        _finalize_attempts(root, attempts, committed=True)
        journal["phase"] = "committed"
        _write_journal(journal_path, journal)
        _cleanup(root, journal)
    except Exception as error:
        raise PublicationError(
            f"publication failed; recovery retained at {JOURNAL_PATH}"
        ) from error


def publish_reserved_generation(
    root: Path,
    generation_id: str,
    targets: list[PublicationTarget],
    workspace_index: bytes,
    *,
    attempt_reports: list[AttemptReservation | FixedAttemptReservation],
    directory_targets: list[PublicationDirectory] | None = None,
) -> None:
    """Enrich one reserved journal, then publish its checked generation."""
    root = root.resolve()
    _require_owned_lock(root)
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ValueError("invalid generation id")
    _validate_index_generation(workspace_index, generation_id)
    journal_path = root / JOURNAL_PATH
    reserved = _load_journal(root, journal_path)
    if reserved["phase"] != "reserved" or reserved["generation_id"] != generation_id:
        raise PublicationError("publication reservation disagrees")

    attempts = [_prepare_attempt(root, item) for item in attempt_reports]
    attempt_paths = [item["target"] for item in attempts]
    if len(set(attempt_paths)) != len(attempt_paths):
        raise ValueError("duplicate attempt report target")
    file_paths = [_validate_relative_path(item.path) for item in targets]
    if set(attempt_paths) & set(file_paths):
        raise ValueError("attempt report is not a rollback target")
    _validate_attempt_enrichment(reserved["attempts"], attempts)

    recovery_root = root / ".honeymoney" / "publication" / generation_id
    _ensure_safe_target(root, recovery_root)
    entries, retained_entries, index_entry = _prepare_generation_entries(
        root,
        recovery_root,
        targets,
        workspace_index,
        directory_targets,
        attempt_paths,
    )
    assert index_entry is not None
    enriched: _Journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "generation_id": generation_id,
        "phase": "staging",
        "commit_policy": "index",
        "index_target": INDEX_PATH,
        "index_commit_sha256": cast(str, index_entry["new_sha256"]),
        "entries": entries,
        "attempts": attempts,
    }
    try:
        _write_journal(journal_path, enriched)
        _ensure_directory(recovery_root)
        for entry, content in retained_entries:
            _retain_entry(root, entry, content)
        _fsync_recovery_chain(root, recovery_root)
        enriched["phase"] = "prepared"
        _write_journal(journal_path, enriched)
        _install_generation_entries(root, entries, use_new=True)
        _finalize_attempts(root, attempts, committed=True)
        enriched["phase"] = "committed"
        _write_journal(journal_path, enriched)
        _cleanup(root, enriched)
    except Exception as error:
        raise PublicationError(
            f"publication failed; recovery retained at {JOURNAL_PATH}"
        ) from error


def publish_reserved_failures(
    root: Path,
    generation_id: str,
    targets: list[PublicationTarget],
    *,
    attempt_reports: list[FixedAttemptReservation],
    directory_targets: list[PublicationDirectory] | None = None,
) -> None:
    """Settle reserved parse failures without changing workspace-index bytes."""
    root = root.resolve()
    _require_owned_lock(root)
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ValueError("invalid generation id")
    journal_path = root / JOURNAL_PATH
    reserved = _load_journal(root, journal_path)
    if (
        reserved["phase"] != "reserved"
        or reserved["generation_id"] != generation_id
        or reserved["commit_policy"] != "index"
    ):
        raise PublicationError("publication reservation disagrees")
    if _path_digest(root / INDEX_PATH) != reserved["index_commit_sha256"]:
        raise PublicationError("workspace index changed after reservation")

    relative_paths = [_validate_relative_path(item.path) for item in targets]
    if len(set(relative_paths)) != len(relative_paths):
        raise ValueError("duplicate publication target")
    if INDEX_PATH in relative_paths:
        raise ValueError("fixed failure publication rewrites the workspace index")
    attempts = [_prepare_attempt(root, item) for item in attempt_reports]
    if any(item["kind"] != "fixed_failure" for item in attempts):
        raise ValueError("reserved failure publication requires failure reports")
    attempt_paths = [item["target"] for item in attempts]
    if len(set(attempt_paths)) != len(attempt_paths):
        raise ValueError("duplicate attempt report target")
    if set(attempt_paths) & set(relative_paths):
        raise ValueError("attempt report is not a rollback target")
    _validate_attempt_enrichment(reserved["attempts"], attempts)

    recovery_root = root / ".honeymoney" / "publication" / generation_id
    _ensure_safe_target(root, recovery_root)
    entries, retained_entries, _ = _prepare_generation_entries(
        root,
        recovery_root,
        targets,
        None,
        directory_targets,
        attempt_paths,
    )
    enriched: _Journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "generation_id": generation_id,
        "phase": "staging",
        "commit_policy": "fixed",
        "index_target": INDEX_PATH,
        "index_commit_sha256": reserved["index_commit_sha256"],
        "entries": entries,
        "attempts": attempts,
    }
    try:
        _write_journal(journal_path, enriched)
        _ensure_directory(recovery_root)
        for entry, content in retained_entries:
            _retain_entry(root, entry, content)
        _fsync_recovery_chain(root, recovery_root)
        enriched["phase"] = "prepared"
        _write_journal(journal_path, enriched)
        _install_generation_entries(root, entries, use_new=True)
        _finalize_attempts(root, attempts, committed=True)
        enriched["phase"] = "committed"
        _write_journal(journal_path, enriched)
        _cleanup(root, enriched)
    except Exception as error:
        raise PublicationError(
            f"publication failed; recovery retained at {JOURNAL_PATH}"
        ) from error


def publish_failed_attempts(
    root: Path,
    generation_id: str,
    attempt_reports: list[FixedAttemptReservation],
) -> None:
    """Finalize known parse failures through a retained attempt-only journal."""
    root = root.resolve()
    _require_owned_lock(root)
    if (root / JOURNAL_PATH).exists():
        raise PublicationError("publication recovery is required")
    if not _GENERATION_RE.fullmatch(generation_id):
        raise ValueError("invalid generation id")
    if not attempt_reports:
        raise ValueError("attempt-only publication requires a report")
    attempts = [_prepare_attempt(root, item) for item in attempt_reports]
    attempt_paths = [item["target"] for item in attempts]
    if len(set(attempt_paths)) != len(attempt_paths):
        raise ValueError("duplicate attempt report target")
    index_digest = _path_digest(root / INDEX_PATH)
    if index_digest is None:
        raise PublicationError("workspace index is missing")
    recovery_root = root / ".honeymoney" / "publication" / generation_id
    _ensure_safe_target(root, recovery_root)
    entries, retained_entries, _ = _prepare_generation_entries(
        root,
        recovery_root,
        [],
        None,
        None,
        attempt_paths,
    )
    journal: _Journal = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "generation_id": generation_id,
        "phase": "staging",
        "commit_policy": "fixed",
        "index_target": INDEX_PATH,
        "index_commit_sha256": index_digest,
        "entries": entries,
        "attempts": attempts,
    }
    try:
        _write_journal(root / JOURNAL_PATH, journal)
        _ensure_directory(recovery_root)
        for entry, content in retained_entries:
            _retain_entry(root, entry, content)
        _fsync_recovery_chain(root, recovery_root)
        journal["phase"] = "prepared"
        _write_journal(root / JOURNAL_PATH, journal)
        _install_generation_entries(root, entries, use_new=True)
        _finalize_attempts(root, attempts, committed=True)
        journal["phase"] = "committed"
        _write_journal(root / JOURNAL_PATH, journal)
        _cleanup(root, journal)
    except Exception as error:
        raise PublicationError(
            f"publication failed; recovery retained at {JOURNAL_PATH}"
        ) from error


def inspect_retained_publication(
    root: Path,
) -> Literal["old", "new"] | None:
    """Validate retained state and report the generation proved to have won."""
    root = root.resolve()
    journal_path = root / JOURNAL_PATH
    if not journal_path.exists():
        return None
    journal = _load_journal(root, journal_path)
    _validate_current_targets(root, journal)
    return _publication_winner(root, journal)


def settle_retained_publication(root: Path) -> Literal["old", "new"]:
    """Doctor-only exact recovery; never reruns financial work."""
    root = root.resolve()
    status = inspect_lock(root)
    if status == "live":
        raise WorkspaceBusyError("workspace busy")
    if status == "unknown":
        raise WorkspaceBusyError("workspace lock is unknown")
    if status == "stale":
        (root / LOCK_PATH).unlink()
        _fsync_directory(root / ".honeymoney")
    with WorkspaceLock(root):
        journal_path = root / JOURNAL_PATH
        journal = _load_journal(root, journal_path)
        _validate_current_targets(root, journal)
        winner = _publication_winner(root, journal)
        _install_generation_entries(root, journal["entries"], use_new=winner == "new")
        _finalize_attempts(root, journal["attempts"], committed=winner == "new")
        _cleanup(root, journal)
        return winner


def _publication_winner(root: Path, journal: _Journal) -> Literal["old", "new"]:
    if journal["phase"] == "reserved":
        return "old"
    if journal["phase"] == "committed":
        _validate_committed_targets(root, journal)
        return "new"
    if journal["commit_policy"] == "fixed":
        return "new" if journal["phase"] == "prepared" else "old"
    index = root / journal["index_target"]
    return "new" if _path_digest(index) == journal["index_commit_sha256"] else "old"


def _install_generation_entries(
    root: Path,
    entries: list[_Entry],
    *,
    use_new: bool,
) -> None:
    index_entry = next(
        (entry for entry in entries if entry["target"] == INDEX_PATH), None
    )
    non_index = [entry for entry in entries if entry["target"] != INDEX_PATH]
    if use_new:
        directories = [
            entry for entry in non_index if entry["entry_kind"] == "directory"
        ]
        files = [entry for entry in non_index if entry["entry_kind"] == "file"]
        for entry in directories:
            _install_entry(root, entry, use_new=True)
        for entry in files:
            _install_entry(root, entry, use_new=True)
    else:
        files = [entry for entry in non_index if entry["entry_kind"] == "file"]
        directories = [
            entry for entry in non_index if entry["entry_kind"] == "directory"
        ]
        for entry in reversed(files):
            _install_entry(root, entry, use_new=False)
        for entry in reversed(directories):
            _install_entry(root, entry, use_new=False)
    if index_entry is not None:
        _install_entry(root, index_entry, use_new=use_new)


class _ValidatedAttempt(TypedDict):
    report: dict[str, object]
    document: str


def _prepare_pending_attempt(
    root: Path, reservation: PendingAttemptReservation
) -> _AttemptEntry:
    target, source_id, attempt_number = _validate_attempt_target(root, reservation.path)
    interrupted = _validate_attempt_content(
        reservation.interrupted_content,
        source_id=source_id,
        attempt_number=attempt_number,
        outcome="failure",
    )
    errors = interrupted["report"]["error_codes"]
    if not isinstance(errors, list) or "interrupted" not in errors:
        raise ValueError("interrupted attempt report lacks its error code")
    digest = _digest(reservation.interrupted_content)
    return {
        "kind": "reserved",
        "target": target,
        "mode": PRIVATE_FILE_MODE,
        "success_sha256": digest,
        "interrupted_sha256": digest,
        "success_document": interrupted["document"],
        "interrupted_document": interrupted["document"],
    }


def _prepare_attempt(
    root: Path, reservation: AttemptReservation | FixedAttemptReservation
) -> _AttemptEntry:
    target, source_id, attempt_number = _validate_attempt_target(root, reservation.path)
    if isinstance(reservation, FixedAttemptReservation):
        failure = _validate_attempt_content(
            reservation.failure_content,
            source_id=source_id,
            attempt_number=attempt_number,
            outcome="failure",
        )
        return {
            "kind": "fixed_failure",
            "target": target,
            "mode": PRIVATE_FILE_MODE,
            "success_sha256": _digest(reservation.failure_content),
            "interrupted_sha256": _digest(reservation.failure_content),
            "success_document": failure["document"],
            "interrupted_document": failure["document"],
        }
    success = _validate_attempt_content(
        reservation.success_content,
        source_id=source_id,
        attempt_number=attempt_number,
        outcome="success",
    )
    interrupted = _validate_attempt_content(
        reservation.interrupted_content,
        source_id=source_id,
        attempt_number=attempt_number,
        outcome="failure",
    )
    _validate_attempt_pair(success, interrupted)
    return {
        "kind": "generation",
        "target": target,
        "mode": PRIVATE_FILE_MODE,
        "success_sha256": _digest(reservation.success_content),
        "interrupted_sha256": _digest(reservation.interrupted_content),
        "success_document": success["document"],
        "interrupted_document": interrupted["document"],
    }


def _validate_attempt_target(root: Path, value: object) -> tuple[str, str, int]:
    target = _validate_relative_path(value)
    _ensure_safe_target(root, root / target)
    match = _ATTEMPT_TARGET_RE.fullmatch(target)
    if match is None or int(match.group(2)) < 1:
        raise ValueError("invalid attempt report target")
    if (root / target).exists():
        raise ValueError("attempt report target already exists")
    return target, match.group(1), int(match.group(2))


def _validate_attempt_content(
    content: bytes,
    *,
    source_id: str,
    attempt_number: int,
    outcome: Literal["success", "failure"],
) -> _ValidatedAttempt:
    if len(content) > MAX_ATTEMPT_BYTES:
        raise ValueError("attempt report exceeds its size limit")
    try:
        document = content.decode("utf-8")
        value: object = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("attempt report is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("attempt report is not an object")
    canonical = attempt_document(value)
    if canonical != document:
        raise ValueError("attempt report is not canonical")
    if (
        value.get("source_id") != source_id
        or value.get("attempt_number") != attempt_number
        or value.get("outcome") != outcome
    ):
        raise ValueError("attempt report target disagrees with its document")
    return {"report": cast(dict[str, object], value), "document": document}


def _validate_attempt_pair(
    success: _ValidatedAttempt, interrupted: _ValidatedAttempt
) -> None:
    errors = interrupted["report"]["error_codes"]
    if not isinstance(errors, list) or "interrupted" not in errors:
        raise ValueError("interrupted attempt report lacks its error code")
    for field in (
        "honeymoney_version",
        "source_id",
        "source_label",
        "attempt_number",
        "requested_action",
        "started_at",
        "source_revision",
        "parser_contract",
    ):
        if success["report"][field] != interrupted["report"][field]:
            raise ValueError("attempt report reservation disagrees")


def _validate_attempt_enrichment(
    reserved: list[_AttemptEntry], completed: list[_AttemptEntry]
) -> None:
    if [item["target"] for item in completed] != [item["target"] for item in reserved]:
        raise ValueError("completed attempts disagree with their reservation")
    fields = (
        "honeymoney_version",
        "source_id",
        "source_label",
        "attempt_number",
        "requested_action",
        "started_at",
        "source_revision",
        "parser_contract",
    )
    for pending, final in zip(reserved, completed, strict=True):
        pending_report = json.loads(pending["interrupted_document"])
        final_report = json.loads(final["interrupted_document"])
        if any(pending_report[field] != final_report[field] for field in fields):
            raise ValueError("completed attempt facts disagree with their reservation")


def _finalize_attempts(
    root: Path,
    attempts: list[_AttemptEntry],
    *,
    committed: bool,
) -> None:
    for entry in attempts:
        document = (
            entry["success_document"] if committed else entry["interrupted_document"]
        )
        expected_digest = (
            entry["success_sha256"] if committed else entry["interrupted_sha256"]
        )
        target = root / entry["target"]
        _ensure_safe_target(root, target)
        _ensure_managed_directory_tree(root, target.parent)
        existing_digest = _path_digest(target)
        if existing_digest is not None:
            if existing_digest != expected_digest:
                raise PublicationError(
                    f"attempt report is immutable: {entry['target']}"
                )
            os.chmod(target, entry["mode"])
            temporary = target.with_name(f".{target.name}.new")
            if temporary.exists():
                if _path_digest(temporary) != expected_digest:
                    raise PublicationError(
                        f"attempt staging bytes disagree: {entry['target']}"
                    )
                temporary.unlink()
                _fsync_directory(target.parent)
            continue
        temporary = target.with_name(f".{target.name}.new")
        if temporary.exists():
            if _path_digest(temporary) != expected_digest:
                raise PublicationError(
                    f"attempt staging bytes disagree: {entry['target']}"
                )
        else:
            _write_new(temporary, document.encode("utf-8"), entry["mode"])
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _path_digest(target) != expected_digest:
                raise PublicationError(
                    f"attempt report is immutable: {entry['target']}"
                )
        _fsync_directory(target.parent)
        temporary.unlink(missing_ok=True)
        _fsync_directory(target.parent)


def _prepare_entry(
    root: Path,
    recovery_root: Path,
    number: int,
    relative: str,
    item: PublicationTarget,
) -> _Entry:
    target = root / relative
    _ensure_safe_target(root, target)
    existed = target.exists()
    if existed and not target.is_file():
        raise ValueError(f"target is not a regular file: {relative}")
    old_mode = stat.S_IMODE(target.stat().st_mode) if existed else None
    retained_base = f"{number:08d}"
    old_path = f".honeymoney/publication/{recovery_root.name}/{retained_base}.old"
    new_path = f".honeymoney/publication/{recovery_root.name}/{retained_base}.new"
    return {
        "target": relative,
        "entry_kind": "file",
        "operation": "write" if item.content is not None else "remove",
        "old_exists": existed,
        "new_exists": item.content is not None,
        "old_mode": old_mode,
        "new_mode": item.mode if item.content is not None else None,
        "old_path": old_path if existed else None,
        "new_path": new_path if item.content is not None else None,
        "old_sha256": _path_digest(target) if existed else None,
        "new_sha256": _digest(item.content) if item.content is not None else None,
    }


def _prepare_directory_entry(
    root: Path,
    relative: str,
    item: PublicationDirectory,
) -> _Entry:
    target = root / relative
    _ensure_safe_target(root, target)
    existed = target.exists()
    if existed and not target.is_dir():
        raise ValueError(f"target is not a directory: {relative}")
    return {
        "target": relative,
        "entry_kind": "directory",
        "operation": "ensure",
        "old_exists": existed,
        "new_exists": True,
        "old_mode": stat.S_IMODE(target.stat().st_mode) if existed else None,
        "new_mode": item.mode,
        "old_path": None,
        "new_path": None,
        "old_sha256": None,
        "new_sha256": None,
    }


def _retain_entry(root: Path, entry: _Entry, content: bytes | None) -> None:
    if entry["entry_kind"] == "directory":
        return
    new_mode = entry["new_mode"]
    assert new_mode is not None or not entry["new_exists"]
    if entry["old_path"] is not None:
        old_mode = entry["old_mode"]
        assert old_mode is not None
        _copy_new(root / entry["target"], root / entry["old_path"], old_mode)
    if entry["new_path"] is not None:
        assert content is not None
        assert new_mode is not None
        _write_new(root / entry["new_path"], content, new_mode)


def _install_entry(root: Path, entry: _Entry, *, use_new: bool) -> None:
    if entry["entry_kind"] == "directory":
        _install_directory_entry(root, entry, use_new=use_new)
        return
    if use_new:
        expected_exists = entry["new_exists"]
        retained_value = entry["new_path"]
        expected_digest = entry["new_sha256"]
        expected_mode = entry["new_mode"]
    else:
        expected_exists = entry["old_exists"]
        retained_value = entry["old_path"]
        expected_digest = entry["old_sha256"]
        expected_mode = entry["old_mode"]
    target = root / entry["target"]
    _ensure_safe_target(root, target)
    if not expected_exists:
        if not target.exists():
            return
        target.unlink(missing_ok=True)
        _fsync_directory(target.parent)
        return
    assert expected_digest is not None
    assert expected_mode is not None
    if _path_digest(target) == expected_digest:
        if stat.S_IMODE(target.stat().st_mode) != expected_mode:
            os.chmod(target, expected_mode)
            _fsync_directory(target.parent)
        return
    assert retained_value is not None
    retained = root / retained_value
    install = retained.with_suffix(retained.suffix + ".install")
    install.unlink(missing_ok=True)
    _copy_new(retained, install, expected_mode)
    _ensure_managed_directory_tree(root, target.parent)
    os.replace(install, target)
    _fsync_directory(target.parent)


def _install_directory_entry(root: Path, entry: _Entry, *, use_new: bool) -> None:
    expected_exists = entry["new_exists"] if use_new else entry["old_exists"]
    expected_mode = entry["new_mode"] if use_new else entry["old_mode"]
    target = root / entry["target"]
    _ensure_safe_target(root, target)
    if not expected_exists:
        if not target.exists():
            return
        if target.is_symlink() or not target.is_dir():
            raise PublicationError(f"unsafe publication directory: {entry['target']}")
        target.rmdir()
        _fsync_directory(target.parent)
        return
    assert expected_mode is not None
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise PublicationError(f"unsafe publication directory: {entry['target']}")
        if stat.S_IMODE(target.stat().st_mode) != expected_mode:
            os.chmod(target, expected_mode)
            _fsync_directory(target.parent)
        return
    parent = target.parent
    if parent.is_symlink() or not parent.is_dir():
        raise PublicationError(
            f"publication directory parent is missing: {entry['target']}"
        )
    target.mkdir(mode=expected_mode)
    os.chmod(target, expected_mode)
    _fsync_directory(parent)


def _validate_current_targets(root: Path, journal: _Journal) -> None:
    if (
        journal["commit_policy"] == "fixed"
        and _path_digest(root / journal["index_target"])
        != journal["index_commit_sha256"]
    ):
        raise PublicationError("fixed publication index proof changed")
    if not journal["entries"]:
        if (
            _path_digest(root / journal["index_target"])
            != journal["index_commit_sha256"]
        ):
            raise PublicationError("attempt journal index proof changed")
        return
    for entry in journal["entries"]:
        if not _entry_matches_state(
            root, entry, use_new=False
        ) and not _entry_matches_state(root, entry, use_new=True):
            raise PublicationError(
                f"publication target matches neither retained state: {entry['target']}"
            )


def _validate_committed_targets(root: Path, journal: _Journal) -> None:
    """Require the index-last winner before recovery bytes may be absent."""
    if _path_digest(root / journal["index_target"]) != journal["index_commit_sha256"]:
        raise PublicationError("committed publication index proof changed")
    for entry in journal["entries"]:
        if not _entry_matches_state(root, entry, use_new=True):
            raise PublicationError("committed publication target changed")


def _entry_matches_state(root: Path, entry: _Entry, *, use_new: bool) -> bool:
    expected_exists = entry["new_exists"] if use_new else entry["old_exists"]
    expected_mode = entry["new_mode"] if use_new else entry["old_mode"]
    target = root / entry["target"]
    try:
        _ensure_safe_target(root, target)
    except ValueError as error:
        raise PublicationError("unsafe publication target") from error
    if not expected_exists:
        return not target.exists()
    if expected_mode is None or not target.exists() or target.is_symlink():
        return False
    try:
        mode = target.stat().st_mode
    except OSError:
        return False
    if stat.S_IMODE(mode) != expected_mode:
        return False
    if entry["entry_kind"] == "directory":
        return stat.S_ISDIR(mode)
    if not stat.S_ISREG(mode):
        return False
    expected_digest = entry["new_sha256"] if use_new else entry["old_sha256"]
    return expected_digest is not None and _path_digest(target) == expected_digest


def _cleanup(root: Path, journal: _Journal) -> None:
    for entry in journal["entries"]:
        for key in ("old_path", "new_path"):
            value = entry[key]
            if value is not None:
                retained = root / value
                retained.unlink(missing_ok=True)
                retained.with_suffix(retained.suffix + ".install").unlink(
                    missing_ok=True
                )
    recovery = root / ".honeymoney" / "publication" / journal["generation_id"]
    if recovery.exists():
        _fsync_directory(recovery)
        recovery.rmdir()
        _fsync_directory(recovery.parent)
        _fsync_directory(recovery.parent.parent)
    publication = recovery.parent
    try:
        publication.rmdir()
        _fsync_directory(publication.parent)
    except OSError:
        pass
    view_target_present = False
    for entry in journal["entries"]:
        parts = PurePosixPath(entry["target"]).parts
        if len(parts) == 3 and parts[0] == "views":
            view_target_present = True
            try:
                (root / "views" / parts[1]).rmdir()
                _fsync_directory(root / "views")
            except OSError:
                pass
    if view_target_present:
        try:
            (root / "views").rmdir()
            _fsync_directory(root)
        except OSError:
            pass
    (root / JOURNAL_PATH).unlink()
    _fsync_directory(root / ".honeymoney")


def _load_journal(root: Path, path: Path) -> _Journal:
    if path.is_symlink() or not path.is_file():
        raise PublicationError("publication journal path is unsafe")
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != _JOURNAL_FIELDS:
            raise ValueError("invalid journal fields")
        if value["schema_version"] != JOURNAL_SCHEMA_VERSION:
            raise ValueError("invalid journal schema")
        generation = value["generation_id"]
        if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
            raise ValueError("invalid generation id")
        if value["phase"] not in {"reserved", "staging", "prepared", "committed"}:
            raise ValueError("invalid journal phase")
        policy = value["commit_policy"]
        if policy not in {"index", "fixed"}:
            raise ValueError("invalid commit policy")
        if value["index_target"] != INDEX_PATH:
            raise ValueError("invalid index target")
        if not _is_digest(value["index_commit_sha256"]):
            raise ValueError("invalid commit proof")
        raw_entries = value["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("invalid entries")
        entries = [
            _validate_entry(
                root,
                generation,
                item,
                require_retained=value["phase"] == "prepared",
            )
            for item in raw_entries
        ]
        targets = [item["target"] for item in entries]
        if len(set(targets)) != len(targets):
            raise ValueError("invalid target order")
        if policy == "index" and value["phase"] != "reserved":
            if not entries:
                raise ValueError("index publication has no index entry")
            if targets[-1] != INDEX_PATH:
                raise ValueError("invalid target order")
            if entries[-1]["new_sha256"] != value["index_commit_sha256"]:
                raise ValueError("commit proof disagrees with index entry")
        elif INDEX_PATH in targets:
            raise ValueError("fixed publication rewrites the workspace index")
        raw_attempts = value["attempts"]
        if not isinstance(raw_attempts, list):
            raise ValueError("invalid attempt entries")
        attempts = [_validate_attempt_entry(root, item) for item in raw_attempts]
        if not entries and not attempts:
            raise ValueError("journal has no work")
        attempt_targets = [item["target"] for item in attempts]
        if len(set(attempt_targets)) != len(attempt_targets):
            raise ValueError("duplicate attempt report target")
        if set(attempt_targets) & set(targets):
            raise ValueError("attempt report is a rollback target")
        if value["phase"] == "reserved":
            if (
                policy != "index"
                or entries
                or any(item["kind"] != "reserved" for item in attempts)
            ):
                raise ValueError("invalid reserved publication")
        elif any(item["kind"] == "reserved" for item in attempts):
            raise ValueError("prepared publication has reserved attempts")
        result: _Journal = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "generation_id": generation,
            "phase": value["phase"],
            "commit_policy": value["commit_policy"],
            "index_target": INDEX_PATH,
            "index_commit_sha256": value["index_commit_sha256"],
            "entries": entries,
            "attempts": attempts,
        }
        return result
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise PublicationError("publication state is invalid") from error


def _validate_attempt_entry(root: Path, value: object) -> _AttemptEntry:
    if not isinstance(value, dict) or set(value) != _ATTEMPT_FIELDS:
        raise ValueError("invalid attempt entry fields")
    target = _validate_relative_path(value["target"])
    _ensure_safe_target(root, root / target)
    match = _ATTEMPT_TARGET_RE.fullmatch(target)
    if match is None or int(match.group(2)) < 1:
        raise ValueError("invalid attempt report target")
    if value["mode"] != PRIVATE_FILE_MODE:
        raise ValueError("invalid attempt report mode")
    kind = value["kind"]
    if kind not in {"reserved", "generation", "fixed_failure"}:
        raise ValueError("invalid attempt report kind")
    success_document = value["success_document"]
    interrupted_document = value["interrupted_document"]
    if not isinstance(success_document, str) or not isinstance(
        interrupted_document, str
    ):
        raise ValueError("invalid attempt report document")
    committed_outcome: Literal["success", "failure"] = (
        "success" if kind == "generation" else "failure"
    )
    success = _validate_attempt_content(
        success_document.encode("utf-8"),
        source_id=match.group(1),
        attempt_number=int(match.group(2)),
        outcome=committed_outcome,
    )
    interrupted = _validate_attempt_content(
        interrupted_document.encode("utf-8"),
        source_id=match.group(1),
        attempt_number=int(match.group(2)),
        outcome="failure",
    )
    if kind == "generation":
        _validate_attempt_pair(success, interrupted)
    elif success_document != interrupted_document:
        raise ValueError("fixed attempt report disagrees")
    if value["success_sha256"] != _digest(success_document.encode("utf-8")):
        raise ValueError("invalid success attempt digest")
    if value["interrupted_sha256"] != _digest(interrupted_document.encode("utf-8")):
        raise ValueError("invalid interrupted attempt digest")
    return cast(_AttemptEntry, value)


def _validate_entry(
    root: Path,
    generation: str,
    value: object,
    *,
    require_retained: bool,
) -> _Entry:
    if not isinstance(value, dict) or set(value) != _ENTRY_FIELDS:
        raise ValueError("invalid entry fields")
    target = _validate_relative_path(value["target"])
    _ensure_safe_target(root, root / target)
    entry_kind = value["entry_kind"]
    if entry_kind not in {"file", "directory"}:
        raise ValueError("invalid entry kind")
    operation = value["operation"]
    if operation not in {"write", "remove", "ensure"}:
        raise ValueError("invalid operation")
    old_exists = value["old_exists"]
    new_exists = value["new_exists"]
    if not isinstance(old_exists, bool) or not isinstance(new_exists, bool):
        raise ValueError("invalid existence state")
    old_mode = value["old_mode"]
    new_mode = value["new_mode"]
    if old_exists != _is_mode(old_mode):
        raise ValueError("invalid old mode")
    if new_exists != _is_mode(new_mode):
        raise ValueError("invalid new mode")
    if entry_kind == "file":
        if operation not in {"write", "remove"}:
            raise ValueError("invalid file operation")
        if new_exists != (operation == "write"):
            raise ValueError("operation disagrees with existence state")
        if new_exists and new_mode != PRIVATE_FILE_MODE:
            raise ValueError("invalid file mode")
    else:
        if operation != "ensure" or not new_exists:
            raise ValueError("invalid directory operation")
        if new_mode != PRIVATE_DIRECTORY_MODE:
            raise ValueError("invalid directory mode")
    prefix = f".honeymoney/publication/{generation}/"
    for path_key, digest_key, exists in (
        ("old_path", "old_sha256", old_exists),
        ("new_path", "new_sha256", new_exists),
    ):
        retained = value[path_key]
        digest = value[digest_key]
        if entry_kind == "directory":
            if retained is not None or digest is not None:
                raise ValueError("unexpected directory recovery state")
            continue
        if exists:
            if not isinstance(retained, str) or not retained.startswith(prefix):
                raise ValueError("invalid retained path")
            _validate_relative_path(retained)
            retained_path = root / retained
            _ensure_safe_target(root, retained_path)
            if not _is_digest(digest):
                raise ValueError("invalid retained digest")
            if retained_path.exists():
                if retained_path.is_symlink() or not retained_path.is_file():
                    raise ValueError("invalid retained bytes")
                if require_retained and _path_digest(retained_path) != digest:
                    raise ValueError("invalid retained digest")
            elif require_retained:
                raise ValueError("missing retained bytes")
        elif retained is not None or digest is not None:
            raise ValueError("unexpected retained state")
    return cast(_Entry, value)


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid workspace-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid workspace-relative path")
    return path.as_posix()


def _validate_directory_path(value: object) -> str:
    if value == ".":
        return "."
    return _validate_relative_path(value)


def _ensure_safe_target(root: Path, target: Path) -> None:
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("target escapes workspace") from error
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"symbolic links are not allowed: {current.relative_to(root)}"
            )


def _ensure_safe_parent(root: Path, path: Path) -> None:
    _ensure_safe_target(root, path)
    _ensure_directory(path.parent)


def _ensure_directory(path: Path, *, harden_existing: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if path.is_symlink() or not path.is_dir():
        raise PublicationError(f"unsafe directory: {path}")
    if harden_existing:
        os.chmod(path, PRIVATE_DIRECTORY_MODE)


def _ensure_managed_directory_tree(root: Path, path: Path) -> None:
    """Create and protect each managed directory below the workspace root."""
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise PublicationError("managed directory leaves workspace root") from error
    current = root
    for part in relative.parts:
        current = current / part
        _ensure_directory(current)


def _write_journal(path: Path, journal: _Journal) -> None:
    temporary = path.with_suffix(".json.new")
    temporary.unlink(missing_ok=True)
    _write_new(
        temporary,
        (json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        PRIVATE_FILE_MODE,
        harden_parent=False,
    )
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _write_new(
    path: Path,
    content: bytes,
    mode: int,
    *,
    harden_parent: bool = False,
) -> None:
    _ensure_directory(path.parent, harden_existing=harden_parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _copy_new(source: Path, destination: Path, mode: int) -> None:
    _ensure_directory(destination.parent)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_recovery_chain(root: Path, recovery_root: Path) -> None:
    """Persist staged recovery files and every directory that names them."""
    internal = root / ".honeymoney"
    publication = internal / "publication"
    if recovery_root.parent != publication:
        raise PublicationError("invalid recovery directory")
    for directory in (recovery_root, publication, internal):
        _ensure_safe_target(root, directory)
        if directory.is_symlink() or not directory.is_dir():
            raise PublicationError("unsafe recovery directory")
        _fsync_directory(directory)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _path_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise PublicationError(f"unsafe publication target: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _is_mode(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0o777
    )


def _validate_index_generation(content: bytes, generation_id: str) -> None:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("workspace index must be JSON") from error
    if not isinstance(value, dict) or value.get("generation_id") != generation_id:
        raise ValueError("workspace index must carry the new generation id")


def _require_owned_lock(root: Path) -> None:
    path = root / LOCK_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceBusyError("caller does not hold the workspace lock") from error
    if value != {"pid": os.getpid(), "schema_version": 1}:
        raise WorkspaceBusyError("caller does not hold the workspace lock")
