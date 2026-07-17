from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1


def persist_generation(authoritative_path: Path, files: dict[Path, str]) -> None:
    """Durably publish files using the ledger replacement as the commit point."""
    authoritative_path = authoritative_path.resolve()
    normalized = {path.resolve(): content for path, content in files.items()}
    if authoritative_path not in normalized:
        raise ValueError("A persisted generation must include the authoritative ledger")

    recover_generation(authoritative_path)
    lock_path = _lock_path(authoritative_path)
    _acquire_lock(lock_path)
    generation = uuid.uuid4().hex
    state_path = _state_path(authoritative_path)
    entries = [
        _entry_for(target, content, generation)
        for target, content in normalized.items()
    ]
    entries.sort(key=lambda entry: entry["target"] == str(authoritative_path))
    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "generation": generation,
        "phase": "staging",
        "authoritative_path": str(authoritative_path),
        "entries": entries,
    }

    try:
        try:
            _write_state(state_path, state)
            for entry in entries:
                _stage_entry(entry, normalized[Path(entry["target"])])
            state["phase"] = "prepared"
            _write_state(state_path, state)
            for entry in entries:
                _replace_from_retained(entry, "staged")
            _fsync_directories(entries)
        except Exception as write_error:
            try:
                _restore_old_generation(state_path, state)
            except Exception as recovery_error:
                raise OSError(
                    "Output persistence failed and automatic recovery was incomplete; "
                    f"retained generation state: {state_path}"
                ) from recovery_error
            raise write_error

        _finish_generation(state_path, state)
    finally:
        _release_lock(lock_path)


def recover_generation(authoritative_path: Path) -> None:
    """Recover retained state according to the authoritative ledger generation."""
    authoritative_path = authoritative_path.resolve()
    state_path = _state_path(authoritative_path)
    lock_path = _lock_path(authoritative_path)
    if lock_path.exists() and _lock_owner_is_active(lock_path):
        raise OSError("Another output persistence operation is already in progress")
    state_temporary = _state_temporary_path(state_path)
    if not state_path.exists():
        if state_temporary.exists():
            state_temporary.unlink(missing_ok=True)
            _fsync_directory(state_path.parent)
        if lock_path.exists():
            _release_lock(lock_path)
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        _validate_state(state, authoritative_path)
        entries = state["entries"]
        authoritative = next(
            entry for entry in entries if entry["target"] == str(authoritative_path)
        )
        ledger_committed = (
            state.get("phase") == "prepared"
            and _path_hash(Path(authoritative["target"])) == authoritative["new_sha256"]
        )
        if ledger_committed:
            _complete_new_generation(state_path, state)
        else:
            _restore_old_generation(state_path, state)
        if lock_path.exists():
            _release_lock(lock_path)
    except Exception as error:
        raise OSError(
            "Unable to recover retained output generation; "
            f"state remains at {state_path}"
        ) from error


def _entry_for(target: Path, content: str, generation: str) -> dict[str, Any]:
    existed = target.exists()
    mode = stat.S_IMODE(target.stat().st_mode) if existed else _default_file_mode()
    old_hash = _path_hash(target) if existed else None
    stem = f".{target.name}.honeymoney-{generation}"
    return {
        "target": str(target),
        "staged": str(target.parent / f"{stem}.new"),
        "backup": str(target.parent / f"{stem}.old"),
        "install": str(target.parent / f"{stem}.install"),
        "existed": existed,
        "mode": mode,
        "old_sha256": old_hash,
        "new_sha256": _content_hash(content),
    }


def _stage_entry(entry: dict[str, Any], content: str) -> None:
    target = Path(entry["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_new_file(Path(entry["staged"]), content, entry["mode"])
    if entry["existed"]:
        _copy_file(Path(entry["target"]), Path(entry["backup"]), entry["mode"])


def _write_new_file(path: Path, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with (
            source.open("rb") as source_handle,
            os.fdopen(descriptor, "wb") as destination_handle,
        ):
            while chunk := source_handle.read(1024 * 1024):
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fchmod(destination_handle.fileno(), mode)
            os.fsync(destination_handle.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _complete_new_generation(state_path: Path, state: dict[str, Any]) -> None:
    entries = state["entries"]
    for entry in entries:
        target = Path(entry["target"])
        if _path_hash(target) == entry["new_sha256"]:
            continue
        staged = Path(entry["staged"])
        if not staged.exists() or _path_hash(staged) != entry["new_sha256"]:
            raise OSError("Retained generation is missing staged output")
        _replace_from_retained(entry, "staged")
    _fsync_directories(entries)
    _finish_generation(state_path, state)


def _restore_old_generation(state_path: Path, state: dict[str, Any]) -> None:
    entries = state["entries"]
    for entry in entries:
        target = Path(entry["target"])
        if entry["existed"]:
            if _path_hash(target) == entry["old_sha256"]:
                continue
            backup = Path(entry["backup"])
            if not backup.exists() or _path_hash(backup) != entry["old_sha256"]:
                raise OSError("Retained generation is missing prior output")
            _replace_from_retained(entry, "backup")
        else:
            target.unlink(missing_ok=True)
    _fsync_directories(entries)
    _finish_generation(state_path, state)


def _finish_generation(state_path: Path, state: dict[str, Any]) -> None:
    entries = state["entries"]
    for entry in entries:
        Path(entry["staged"]).unlink(missing_ok=True)
        Path(entry["backup"]).unlink(missing_ok=True)
        Path(entry["install"]).unlink(missing_ok=True)
    _fsync_directories(entries)
    state_path.unlink(missing_ok=True)
    _state_temporary_path(state_path).unlink(missing_ok=True)
    _fsync_directory(state_path.parent)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _state_temporary_path(path)
    temporary.unlink(missing_ok=True)
    content = json.dumps(state, indent=2, sort_keys=True) + "\n"
    _write_new_file(temporary, content, _default_file_mode())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _validate_state(state: dict[str, Any], authoritative_path: Path) -> None:
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError("Unsupported retained generation state")
    if state.get("authoritative_path") != str(authoritative_path):
        raise ValueError("Retained generation belongs to another ledger")
    entries = state.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Retained generation has no output entries")
    required = {
        "target",
        "staged",
        "backup",
        "install",
        "existed",
        "mode",
        "old_sha256",
        "new_sha256",
    }
    if any(not isinstance(entry, dict) or set(entry) != required for entry in entries):
        raise ValueError("Retained generation entries are invalid")
    if sum(entry["target"] == str(authoritative_path) for entry in entries) != 1:
        raise ValueError("Retained generation has no unique authoritative ledger")


def _fsync_directories(entries: list[dict[str, Any]]) -> None:
    directories = {
        Path(entry["target"]).parent
        for entry in entries
        if Path(entry["target"]).parent.exists()
    }
    for directory in sorted(directories, key=str):
        _fsync_directory(directory)


def _replace_from_retained(entry: dict[str, Any], source_field: str) -> None:
    source = Path(entry[source_field])
    install = Path(entry["install"])
    install.unlink(missing_ok=True)
    _copy_file(source, install, entry["mode"])
    os.replace(install, entry["target"])


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _state_path(authoritative_path: Path) -> Path:
    return (
        authoritative_path.parent / f".{authoritative_path.name}.honeymoney-state.json"
    )


def _lock_path(authoritative_path: Path) -> Path:
    return authoritative_path.parent / f".{authoritative_path.name}.honeymoney-lock"


def _state_temporary_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.name}.tmp")


def _path_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _default_file_mode() -> int:
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _acquire_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            _default_file_mode(),
        )
    except FileExistsError as error:
        raise OSError(
            "Another output persistence operation is already in progress"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _release_lock(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _lock_owner_is_active(path: Path) -> bool:
    try:
        owner = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if owner == os.getpid():
        return True
    try:
        os.kill(owner, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
