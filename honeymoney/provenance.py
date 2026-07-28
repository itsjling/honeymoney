"""Join canonical rows to their active source occurrences."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from honeymoney.identity import (
    IdentityError,
    logical_locator,
    record_fingerprint,
    source_namespace_id,
)
from honeymoney.identity_state import IdentityState
from honeymoney.overlap_contracts import OverlapManifest


class ProvenanceError(ValueError):
    """Report a value-free active-provenance failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ActiveProvenanceIndex:
    group_fingerprints: dict[str, str]
    active_slots: dict[str, set[str]]
    source_by_fingerprint: dict[str, list[dict[str, str]]]
    source_by_transaction_id: dict[str, list[dict[str, str]]]


def active_provenance_index(state: IdentityState) -> ActiveProvenanceIndex:
    """Build a checked index over active source evidence."""
    if state.canonical_migration_required or state.overlap_migration_required:
        raise ProvenanceError("migration_required")
    manifest = state.overlap_manifest
    source_rows = state.source_rows
    if manifest is None or source_rows is None:
        raise ProvenanceError("unavailable")
    return build_active_provenance_index(source_rows, manifest)


def build_active_provenance_index(
    source_rows: list[dict[str, str]],
    manifest: OverlapManifest,
) -> ActiveProvenanceIndex:
    """Build a checked index from an in-memory prospective generation."""
    group_fingerprints: dict[str, str] = {}
    active_slots: dict[str, set[str]] = {}
    for group in manifest["groups"]:
        group_id = group["overlap_group_id"]
        if group_id in group_fingerprints:
            raise ProvenanceError("inconsistent")
        group_fingerprints[group_id] = group["record_fingerprint"]
        active_slots[group_id] = {
            slot["transaction_id"]
            for slot in group["slots"]
            if slot["state"] == "active"
        }
    source_by_fingerprint: dict[str, list[dict[str, str]]] = {}
    source_by_transaction_id: dict[str, list[dict[str, str]]] = {}
    try:
        for row in source_rows:
            source_by_fingerprint.setdefault(record_fingerprint(row), []).append(row)
            source_by_transaction_id.setdefault(
                row.get("transaction_id", ""),
                [],
            ).append(row)
    except IdentityError as error:
        raise ProvenanceError("inconsistent") from error
    return ActiveProvenanceIndex(
        group_fingerprints,
        active_slots,
        source_by_fingerprint,
        source_by_transaction_id,
    )


def active_source_rows(
    row: Mapping[str, str],
    index: ActiveProvenanceIndex,
) -> list[dict[str, str]]:
    """Return the complete active source pool for one canonical row."""
    transaction_id = row.get("transaction_id", "")
    group_id = row.get("canonical_group_id", "")
    if group_id:
        fingerprint = index.group_fingerprints.get(group_id)
        if fingerprint is None or transaction_id not in index.active_slots.get(
            group_id, set()
        ):
            raise ProvenanceError("inconsistent")
        source_rows = index.source_by_fingerprint.get(fingerprint, [])
    else:
        source_rows = index.source_by_transaction_id.get(transaction_id, [])
        if len(source_rows) > 1:
            raise ProvenanceError("ambiguous")
    if not source_rows:
        raise ProvenanceError("unavailable")
    try:
        expected_count = int(row.get("source_occurrence_count", "") or len(source_rows))
    except ValueError as error:
        raise ProvenanceError("inconsistent") from error
    if expected_count != len(source_rows):
        raise ProvenanceError("inconsistent")
    return source_rows


def safe_source_location(
    value: str,
    expected_namespace_id: str,
    workspace_root: Path,
    source_root: Path | None,
) -> tuple[str, str]:
    """Return a checked workspace path and a bounded display name."""
    if not value:
        return "", ""
    candidate = Path(value)
    display = value.replace("\\", "/").rsplit("/", 1)[-1][:128]
    candidates = (
        [candidate.resolve()]
        if candidate.is_absolute()
        else [(workspace_root / candidate).resolve()]
    )
    if source_root is not None and not candidate.is_absolute():
        source_base = source_root if source_root.is_dir() else source_root.parent
        candidates.append((source_base / candidate).resolve())
    workspace_matches: set[Path] = set()
    for resolved in candidates:
        if not resolved.exists() or not expected_namespace_id:
            continue
        try:
            locator_kind, locator = logical_locator(resolved, workspace_root)
        except OSError:
            continue
        if source_namespace_id(locator_kind, locator) != expected_namespace_id:
            continue
        try:
            relative = resolved.relative_to(workspace_root)
        except ValueError:
            continue
        workspace_matches.add(relative)
    if len(workspace_matches) == 1:
        return str(next(iter(workspace_matches))), display
    return "", display
