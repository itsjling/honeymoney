from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Iterable, Literal, Mapping, TypedDict, cast

from honeymoney.persistence import (
    generation_hashes,
    persist_generation,
    recover_generation,
    require_clean_generation,
)

MANAGED_FILES_NAME = ".honeymoney-managed-files.json"
MANAGED_FILES_SCHEMA_VERSION = 1

UpgradeResultName = Literal[
    "create",
    "update",
    "unchanged",
    "conflict",
    "preserved",
]


class ManagedFileEvidence(TypedDict):
    path: str
    origin: str
    sha256: str


class UpgradeResult(TypedDict):
    path: str
    kind: str
    result: UpgradeResultName
    reason: str


@dataclass(frozen=True)
class UpgradePlan:
    results: tuple[UpgradeResult, ...]
    documents: dict[Path, str]
    expected_hashes: dict[Path, str | None]

    @property
    def changed(self) -> bool:
        return bool(self.documents)

    @property
    def has_conflicts(self) -> bool:
        return any(item["result"] == "conflict" for item in self.results)


def managed_files_document(root: Path, paths: Iterable[Path]) -> str:
    evidence: list[ManagedFileEvidence] = []
    for path in sorted((path.resolve() for path in paths), key=str):
        relative_path = path.relative_to(root.resolve()).as_posix()
        evidence.append(
            {
                "path": relative_path,
                "origin": _managed_origin(relative_path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return _encode_manifest(evidence)


def recover_upgrade_generation(
    root: Path,
    desired_documents: Mapping[Path, str],
) -> None:
    root = root.resolve()
    manifest_path = root / MANAGED_FILES_NAME
    if manifest_path.is_symlink():
        raise ValueError("Managed-file evidence must not be a symbolic link")
    try:
        require_clean_generation(manifest_path)
        return
    except OSError:
        pass
    evidence = _load_manifest(manifest_path)
    evidence_paths = [(root / item["path"]).absolute() for item in evidence]
    recovery_paths = [
        *(path.absolute() for path in desired_documents),
        *evidence_paths,
    ]
    for path in recovery_paths:
        _managed_relative_path(root, path)
        unsafe_reason = _unsafe_target_reason(root, path)
        if unsafe_reason is not None:
            raise ValueError(
                f"Cannot recover managed profile {path.name}: {unsafe_reason}"
            )
    recover_generation(
        manifest_path,
        allowed_generation_paths=recovery_paths,
    )


def require_clean_upgrade_generation(root: Path) -> None:
    root = root.resolve()
    manifest_path = root / MANAGED_FILES_NAME
    if manifest_path.is_symlink():
        raise ValueError("Managed-file evidence must not be a symbolic link")
    require_clean_generation(manifest_path)


def build_upgrade_plan(
    root: Path,
    desired_documents: Mapping[Path, str],
    *,
    protected_directories: Iterable[Path],
    preserved_results: Iterable[UpgradeResult],
) -> UpgradePlan:
    root = root.resolve()
    manifest_path = root / MANAGED_FILES_NAME
    evidence = _load_manifest(manifest_path)
    evidence_by_path = {item["path"]: item for item in evidence}
    next_evidence = dict(evidence_by_path)
    protected = tuple(path.resolve() for path in protected_directories)
    documents: dict[Path, str] = {}
    expected_paths: set[Path] = {manifest_path}
    results: list[UpgradeResult] = []

    for raw_path, content in sorted(
        desired_documents.items(), key=lambda item: str(item[0])
    ):
        path = raw_path.absolute()
        relative_path = _managed_relative_path(root, path)
        origin = _managed_origin(relative_path)
        desired_hash = _content_hash(content)
        prior = evidence_by_path.get(relative_path)

        if _path_is_protected(path, protected):
            results.append(
                _result(
                    relative_path,
                    "bundled_profile",
                    "preserved",
                    "configured financial state is never changed",
                )
            )
            continue
        unsafe_reason = _unsafe_target_reason(root, path)
        if unsafe_reason is not None:
            results.append(
                _result(relative_path, "bundled_profile", "conflict", unsafe_reason)
            )
            continue
        if not path.exists():
            results.append(
                _result(
                    relative_path,
                    "bundled_profile",
                    "create",
                    "bundled profile is missing",
                )
            )
            documents[path] = content
            expected_paths.add(path)
            next_evidence[relative_path] = {
                "path": relative_path,
                "origin": origin,
                "sha256": desired_hash,
            }
            continue
        if not path.is_file():
            results.append(
                _result(
                    relative_path,
                    "bundled_profile",
                    "conflict",
                    "target is not a regular file",
                )
            )
            continue

        local_hash = _path_hash(path)
        if prior is None:
            result_name: UpgradeResultName = (
                "preserved" if local_hash == desired_hash else "conflict"
            )
            results.append(
                _result(
                    relative_path,
                    "bundled_profile",
                    result_name,
                    "managed origin is not proven",
                )
            )
            continue
        if prior["origin"] != origin or local_hash != prior["sha256"]:
            results.append(
                _result(
                    relative_path,
                    "bundled_profile",
                    "conflict",
                    "managed profile has local changes",
                )
            )
            continue
        if local_hash == desired_hash:
            results.append(
                _result(
                    relative_path,
                    "bundled_profile",
                    "unchanged",
                    "managed profile matches the installed bundle",
                )
            )
            next_evidence[relative_path] = {
                "path": relative_path,
                "origin": origin,
                "sha256": desired_hash,
            }
            continue

        results.append(
            _result(
                relative_path,
                "bundled_profile",
                "update",
                "managed profile is unchanged from its recorded version",
            )
        )
        documents[path] = content
        expected_paths.add(path)
        next_evidence[relative_path] = {
            "path": relative_path,
            "origin": origin,
            "sha256": desired_hash,
        }

    next_manifest = _encode_manifest(next_evidence.values())
    current_manifest = (
        manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else None
    )
    if next_evidence and current_manifest != next_manifest:
        documents[manifest_path] = next_manifest
        evidence_result: UpgradeResultName = (
            "update" if current_manifest is not None else "create"
        )
        results.append(
            _result(
                MANAGED_FILES_NAME,
                "managed_evidence",
                evidence_result,
                "record managed profile origins and digests",
            )
        )
    elif current_manifest is not None:
        results.append(
            _result(
                MANAGED_FILES_NAME,
                "managed_evidence",
                "unchanged",
                "managed-file evidence is current",
            )
        )

    results.extend(preserved_results)
    expected_hashes = generation_hashes(expected_paths) if documents else {}
    return UpgradePlan(
        results=tuple(results),
        documents=documents,
        expected_hashes=expected_hashes,
    )


def apply_upgrade_plan(root: Path, plan: UpgradePlan) -> None:
    if not plan.documents:
        return
    root = root.resolve()
    manifest_path = root / MANAGED_FILES_NAME
    if manifest_path.is_symlink():
        raise ValueError("Managed-file evidence must not be a symbolic link")
    for path in plan.documents:
        if path == manifest_path:
            continue
        _managed_relative_path(root, path)
        unsafe_reason = _unsafe_target_reason(root, path)
        if unsafe_reason is not None:
            raise ValueError(
                f"Cannot publish managed profile {path.name}: {unsafe_reason}"
            )
    persist_generation(
        manifest_path,
        plan.documents,
        expected_generation_hashes=plan.expected_hashes,
    )


def result_counts(results: Iterable[UpgradeResult]) -> dict[str, int]:
    counts = {
        "create": 0,
        "update": 0,
        "unchanged": 0,
        "conflict": 0,
        "preserved": 0,
    }
    for item in results:
        counts[item["result"]] += 1
    return counts


def _load_manifest(path: Path) -> list[ManagedFileEvidence]:
    if not path.exists():
        return []
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Managed-file evidence is not valid JSON") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "honeymoney_version",
        "files",
    }:
        raise ValueError("Managed-file evidence fields are invalid")
    if value.get("schema_version") != MANAGED_FILES_SCHEMA_VERSION:
        raise ValueError("Managed-file evidence version is not supported")
    manifest_version = value.get("honeymoney_version")
    if not isinstance(manifest_version, str):
        raise ValueError("Managed-file evidence has no HoneyMoney version")
    installed_version = version("honeymoney")
    if manifest_version != installed_version and _release_version(
        manifest_version
    ) > _release_version(installed_version):
        raise ValueError("Managed-file evidence belongs to a newer HoneyMoney version")
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise ValueError("Managed-file evidence files must be a list")

    files: list[ManagedFileEvidence] = []
    seen_paths: set[str] = set()
    for raw_item in raw_files:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "path",
            "origin",
            "sha256",
        }:
            raise ValueError("Managed-file evidence entry fields are invalid")
        relative_path = raw_item.get("path")
        origin = raw_item.get("origin")
        digest = raw_item.get("sha256")
        if (
            not isinstance(relative_path, str)
            or not _is_managed_relative_path(relative_path)
            or relative_path in seen_paths
        ):
            raise ValueError("Managed-file evidence path is invalid")
        if origin != _managed_origin(relative_path):
            raise ValueError("Managed-file evidence origin is invalid")
        if not _is_sha256(digest):
            raise ValueError("Managed-file evidence digest is invalid")
        seen_paths.add(relative_path)
        files.append(
            {
                "path": relative_path,
                "origin": cast(str, origin),
                "sha256": cast(str, digest),
            }
        )
    return files


def _encode_manifest(evidence: Iterable[ManagedFileEvidence]) -> str:
    return (
        json.dumps(
            {
                "schema_version": MANAGED_FILES_SCHEMA_VERSION,
                "honeymoney_version": version("honeymoney"),
                "files": sorted(evidence, key=lambda item: item["path"]),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _managed_relative_path(root: Path, path: Path) -> str:
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("Bundled profiles must stay inside the workspace") from error
    if not _is_managed_relative_path(relative_path):
        raise ValueError("Bundled profile path is invalid")
    return relative_path


def _is_managed_relative_path(value: str) -> bool:
    path = Path(value)
    return (
        not path.is_absolute()
        and len(path.parts) == 2
        and path.parts[0] == "profiles"
        and path.parts[1].endswith(".json")
        and path.parts[1] not in {"", ".", ".."}
        and path.as_posix() == value
    )


def _unsafe_target_reason(root: Path, path: Path) -> str | None:
    profiles_dir = root / "profiles"
    if profiles_dir.is_symlink():
        return "profiles directory is a symbolic link"
    if profiles_dir.exists() and not profiles_dir.is_dir():
        return "profiles path is not a directory"
    if path.is_symlink():
        return "target is a symbolic link"
    if not path.is_relative_to(profiles_dir):
        return "target is outside the managed profiles directory"
    return None


def _path_is_protected(path: Path, directories: tuple[Path, ...]) -> bool:
    return any(
        path == directory or path.is_relative_to(directory) for directory in directories
    )


def _managed_origin(relative_path: str) -> str:
    return f"honeymoney.bundle/{relative_path}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _path_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _release_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value)
    if match is None:
        raise ValueError("Managed-file evidence HoneyMoney version is invalid")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _result(
    path: str,
    kind: str,
    result: UpgradeResultName,
    reason: str,
) -> UpgradeResult:
    return {
        "path": path,
        "kind": kind,
        "result": result,
        "reason": reason,
    }
