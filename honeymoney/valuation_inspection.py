"""Read-only joins from missing canonical valuations to active source evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TypedDict

from honeymoney.identity import (
    IdentityError,
    logical_locator,
    record_fingerprint,
    source_namespace_id,
)
from honeymoney.identity_state import IdentityState

_SOURCE_DATA_FLAGS = {
    "invalid_amount",
    "statement_opening_balance_conflict",
    "statement_closing_balance_conflict",
}


class ValuationSourceEvidence(TypedDict):
    source_file: str
    source_display: str
    source_page: str
    source_row: str


class MissingValuation(TypedDict):
    transaction_id: str
    date: str
    original_amount: str
    original_currency: str
    posted_amount: str
    posted_currency: str
    flow_type: str
    valuation_status: str
    valuation_source: str
    source_occurrence_count: int
    source_data_flags: list[str]
    source_evidence: list[ValuationSourceEvidence]


class ValuationInspectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _ProvenanceIndex:
    group_fingerprints: dict[str, str]
    active_slots: dict[str, set[str]]
    source_by_fingerprint: dict[str, list[dict[str, str]]]
    source_by_transaction_id: dict[str, list[dict[str, str]]]


def inspect_missing_valuations(
    state: IdentityState,
    *,
    workspace_root: Path,
    source_root: Path | None = None,
    transaction_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[MissingValuation]:
    if state.canonical_migration_required or state.overlap_migration_required:
        raise ValuationInspectionError(
            "valuation_provenance_migration_required",
            "Canonical provenance must be migrated before valuation inspection.",
        )
    index = _provenance_index(state)
    rows = [
        row
        for row in state.rows
        if not row.get("amount_hkd", "").strip()
        and (transaction_id is None or row.get("transaction_id") == transaction_id)
        and (start is None or row.get("date", "") >= start)
        and (end is None or row.get("date", "") <= end)
    ]
    return [
        _missing_valuation(
            row,
            index,
            workspace_root.resolve(),
            source_root.resolve() if source_root is not None else None,
        )
        for row in rows
    ]


def _provenance_index(state: IdentityState) -> _ProvenanceIndex:
    manifest = state.overlap_manifest
    source_rows = state.source_rows
    if manifest is None or source_rows is None:
        raise ValuationInspectionError(
            "valuation_provenance_unavailable",
            "Active valuation provenance is unavailable.",
        )
    group_fingerprints: dict[str, str] = {}
    active_slots: dict[str, set[str]] = {}
    for group in manifest["groups"]:
        group_id = group["overlap_group_id"]
        if group_id in group_fingerprints:
            raise ValuationInspectionError(
                "valuation_provenance_inconsistent",
                "Canonical valuation provenance is inconsistent.",
            )
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
        raise ValuationInspectionError(
            "valuation_provenance_inconsistent",
            "Canonical valuation provenance is inconsistent.",
        ) from error
    return _ProvenanceIndex(
        group_fingerprints,
        active_slots,
        source_by_fingerprint,
        source_by_transaction_id,
    )


def _missing_valuation(
    row: Mapping[str, str],
    index: _ProvenanceIndex,
    workspace_root: Path,
    source_root: Path | None,
) -> MissingValuation:
    transaction_id = row.get("transaction_id", "")
    group_id = row.get("canonical_group_id", "")
    if group_id:
        fingerprint = index.group_fingerprints.get(group_id)
        if fingerprint is None or transaction_id not in index.active_slots.get(
            group_id, set()
        ):
            raise ValuationInspectionError(
                "valuation_provenance_inconsistent",
                "Canonical valuation provenance is inconsistent.",
            )
        source_rows = index.source_by_fingerprint.get(fingerprint, [])
    else:
        source_rows = index.source_by_transaction_id.get(transaction_id, [])
        if len(source_rows) > 1:
            raise ValuationInspectionError(
                "valuation_provenance_ambiguous",
                "Active valuation provenance is ambiguous.",
            )
    if not source_rows:
        raise ValuationInspectionError(
            "valuation_provenance_unavailable",
            "Active valuation provenance is unavailable.",
        )
    try:
        expected_count = int(row.get("source_occurrence_count", "") or len(source_rows))
    except ValueError as error:
        raise ValuationInspectionError(
            "valuation_provenance_inconsistent",
            "Canonical valuation provenance is inconsistent.",
        ) from error
    if expected_count != len(source_rows):
        raise ValuationInspectionError(
            "valuation_provenance_inconsistent",
            "Canonical valuation provenance is inconsistent.",
        )
    evidence = sorted(
        (
            *_source_location(
                source.get("source_file", ""),
                source.get("source_namespace_id", ""),
                workspace_root,
                source_root,
            ),
            source.get("source_page", ""),
            source.get("source_row", ""),
        )
        for source in source_rows
    )
    flags = sorted(
        set(filter(None, row.get("flags", "").split(";"))) & _SOURCE_DATA_FLAGS
    )
    return {
        "transaction_id": transaction_id,
        "date": row.get("date", ""),
        "original_amount": row.get("original_amount", ""),
        "original_currency": row.get("original_currency", ""),
        "posted_amount": row.get("posted_amount", ""),
        "posted_currency": row.get("posted_currency", ""),
        "flow_type": row.get("flow_type", ""),
        "valuation_status": row.get("valuation_status", "") or "missing",
        "valuation_source": row.get("valuation_source", "") or "missing",
        "source_occurrence_count": expected_count,
        "source_data_flags": flags,
        "source_evidence": [
            {
                "source_file": source_file,
                "source_display": source_display,
                "source_page": source_page,
                "source_row": source_row,
            }
            for source_file, source_display, source_page, source_row in evidence
        ],
    }


def _source_location(
    value: str,
    expected_namespace_id: str,
    workspace_root: Path,
    source_root: Path | None,
) -> tuple[str, str]:
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
