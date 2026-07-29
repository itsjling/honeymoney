"""Read-only joins from missing canonical valuations to active source evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, TypedDict

from honeymoney.identity_state import IdentityState
from honeymoney.provenance import (
    ActiveProvenanceIndex,
    ProvenanceError,
    active_provenance_index,
    active_source_rows,
    safe_source_location,
)
from honeymoney.review_state import SOURCE_DATA_FLAGS


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


def inspect_missing_valuations(
    state: IdentityState,
    *,
    workspace_root: Path,
    source_root: Path | None = None,
    transaction_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[MissingValuation]:
    try:
        index = active_provenance_index(state)
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
    except ProvenanceError as error:
        raise _valuation_provenance_error(error) from error


def _missing_valuation(
    row: Mapping[str, str],
    index: ActiveProvenanceIndex,
    workspace_root: Path,
    source_root: Path | None,
) -> MissingValuation:
    transaction_id = row.get("transaction_id", "")
    source_rows = active_source_rows(row, index)
    expected_count = len(source_rows)
    evidence = sorted(
        (
            *safe_source_location(
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
        set(filter(None, row.get("flags", "").split(";"))) & SOURCE_DATA_FLAGS
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


def _valuation_provenance_error(error: ProvenanceError) -> ValuationInspectionError:
    messages = {
        "migration_required": (
            "Canonical provenance must be migrated before valuation inspection."
        ),
        "unavailable": "Active valuation provenance is unavailable.",
        "inconsistent": "Canonical valuation provenance is inconsistent.",
        "ambiguous": "Active valuation provenance is ambiguous.",
    }
    return ValuationInspectionError(
        f"valuation_provenance_{error.code}",
        messages[error.code],
    )
