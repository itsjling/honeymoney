"""Derive source-data review state from active source evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, TypedDict

from honeymoney.identity_state import IdentityState
from honeymoney.overlap_contracts import OverlapManifest
from honeymoney.provenance import (
    ActiveProvenanceIndex,
    ProvenanceError,
    active_provenance_index,
    active_source_rows,
    build_active_provenance_index,
    safe_source_location,
)
from honeymoney.review_state import (
    REVIEW_REASON_SOURCE_DATA,
    SOURCE_DATA_FLAGS,
    review_reason_tokens,
    set_review_reason,
)

_FLAG_FIELDS = {
    "invalid_amount": "amount",
    "source_provenance_ambiguous": "provenance",
    "source_provenance_inconsistent": "provenance",
    "statement_opening_balance_conflict": "statement_opening_balance",
    "statement_closing_balance_conflict": "statement_closing_balance",
}

_FLAG_TYPES = {
    "invalid_amount": "parser_conflict",
    "source_provenance_ambiguous": "provenance_conflict",
    "source_provenance_inconsistent": "provenance_conflict",
    "statement_opening_balance_conflict": "balance_conflict",
    "statement_closing_balance_conflict": "balance_conflict",
}

_PROVENANCE_CONFLICT_FLAGS = {
    "ambiguous": "source_provenance_ambiguous",
    "inconsistent": "source_provenance_inconsistent",
}

_BALANCE_PAGE_PREFIXES = (
    "statement_opening_balance_conflict_page_",
    "statement_closing_balance_conflict_page_",
)


class SourceDataEvidence(TypedDict):
    source_file: str
    source_display: str
    source_page: str
    statement_section: str
    field: str
    flag: str
    evidence_type: str
    evidence_status: str


class SourceDataInspection(TypedDict):
    transaction_id: str
    valuation_status: str
    valuation_source: str
    review_reason_active: bool
    correction_review_reason_active: bool
    evidence_status: str
    source_occurrence_count: int
    source_data_flags: list[str]
    active_evidence_flags: list[str]
    evidence: list[SourceDataEvidence]


class SourceDataReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def repair_source_data_review_state(
    ledger_rows: list[dict[str, str]],
    source_rows: list[dict[str, str]],
    overlap_manifest: OverlapManifest,
    *,
    transaction_ids: set[str] | None = None,
) -> set[str]:
    """Replace canonical source-data flags with current active support."""
    try:
        index = build_active_provenance_index(source_rows, overlap_manifest)
        changed: set[str] = set()
        for row in ledger_rows:
            if (
                transaction_ids is not None
                and row.get("transaction_id", "") not in transaction_ids
            ):
                continue
            before = (
                row.get("flags", ""),
                row.get("review_reasons", ""),
                row.get("needs_review", ""),
            )
            sources, join_provenance_flag = _active_sources(row, index)
            active_tokens = _active_source_data_tokens(sources)
            active_tokens.update(_active_row_provenance_flags(row))
            if join_provenance_flag:
                active_tokens.add(join_provenance_flag)
            evidence_flags = active_tokens & SOURCE_DATA_FLAGS
            existing_flags = [
                flag
                for flag in _tokens(row.get("flags", ""))
                if not _is_source_data_token(flag)
            ]
            row["flags"] = ";".join([*existing_flags, *sorted(active_tokens)])
            set_review_reason(
                row,
                REVIEW_REASON_SOURCE_DATA,
                bool(evidence_flags),
            )
            after = (
                row.get("flags", ""),
                row.get("review_reasons", ""),
                row.get("needs_review", ""),
            )
            if after != before:
                changed.add(row.get("transaction_id", ""))
        return changed
    except ProvenanceError as error:
        raise _source_data_provenance_error(error) from error


def inspect_source_data_review(
    state: IdentityState,
    transaction_id: str,
    *,
    workspace_root: Path,
    source_root: Path | None = None,
    correction_review_reason_active: bool = False,
) -> SourceDataInspection:
    """Return value-free source evidence for one canonical row."""
    row = next(
        (
            candidate
            for candidate in state.rows
            if candidate.get("transaction_id") == transaction_id
        ),
        None,
    )
    if row is None:
        raise SourceDataReviewError(
            "source_data_transaction_unknown",
            "The transaction ID is not in the current ledger.",
        )
    try:
        index = active_provenance_index(state)
        sources, join_provenance_flag = _active_sources(row, index)
    except ProvenanceError as error:
        raise _source_data_provenance_error(error) from error
    canonical_flags = sorted(set(_tokens(row.get("flags", ""))) & SOURCE_DATA_FLAGS)
    provenance_flags = _active_row_provenance_flags(row)
    if join_provenance_flag:
        provenance_flags.add(join_provenance_flag)
    active_flags = sorted(_active_evidence_flags(sources) | provenance_flags)
    reason_active = REVIEW_REASON_SOURCE_DATA in review_reason_tokens(
        row.get("review_reasons", "")
    )
    if active_flags:
        evidence_status = "active"
    elif canonical_flags or reason_active or correction_review_reason_active:
        evidence_status = "stale"
    else:
        evidence_status = "clear"
    resolved_workspace = workspace_root.resolve()
    resolved_source = source_root.resolve() if source_root is not None else None
    evidence: list[SourceDataEvidence] = []
    if provenance_flags:
        provenance_sources = sources or [{}]
        for source in provenance_sources:
            source_file, source_display = safe_source_location(
                source.get("source_file", ""),
                source.get("source_namespace_id", ""),
                resolved_workspace,
                resolved_source,
            )
            for provenance_flag in sorted(provenance_flags):
                evidence.append(
                    {
                        "source_file": source_file,
                        "source_display": source_display,
                        "source_page": source.get("source_page", "")[:32],
                        "statement_section": source.get("statement_section", "")[:128],
                        "field": _FLAG_FIELDS[provenance_flag],
                        "flag": provenance_flag,
                        "evidence_type": _FLAG_TYPES[provenance_flag],
                        "evidence_status": "active",
                    }
                )
    for source in sources:
        source_file, source_display = safe_source_location(
            source.get("source_file", ""),
            source.get("source_namespace_id", ""),
            resolved_workspace,
            resolved_source,
        )
        flags = sorted(set(_tokens(source.get("flags", ""))) & SOURCE_DATA_FLAGS)
        if not flags:
            if provenance_flags:
                continue
            evidence.append(
                {
                    "source_file": source_file,
                    "source_display": source_display,
                    "source_page": source.get("source_page", "")[:32],
                    "statement_section": source.get("statement_section", "")[:128],
                    "field": "",
                    "flag": "",
                    "evidence_type": "",
                    "evidence_status": "no_support",
                }
            )
            continue
        for flag in flags:
            for source_page in _evidence_pages(source, flag):
                evidence.append(
                    {
                        "source_file": source_file,
                        "source_display": source_display,
                        "source_page": source_page,
                        "statement_section": source.get("statement_section", "")[:128],
                        "field": _FLAG_FIELDS[flag],
                        "flag": flag,
                        "evidence_type": _FLAG_TYPES[flag],
                        "evidence_status": "active",
                    }
                )
    evidence.sort(
        key=lambda item: (
            item["source_file"],
            item["source_display"],
            item["source_page"],
            item["statement_section"],
            item["flag"],
        )
    )
    return {
        "transaction_id": transaction_id,
        "valuation_status": row.get("valuation_status", "") or "missing",
        "valuation_source": row.get("valuation_source", "") or "missing",
        "review_reason_active": reason_active,
        "correction_review_reason_active": correction_review_reason_active,
        "evidence_status": evidence_status,
        "source_occurrence_count": len(sources),
        "source_data_flags": canonical_flags,
        "active_evidence_flags": active_flags,
        "evidence": evidence,
    }


def source_data_review_active(
    correction: Mapping[str, str] | None,
) -> bool:
    """Return whether a correction still owns the source-data reason."""
    return bool(
        correction
        and REVIEW_REASON_SOURCE_DATA
        in review_reason_tokens(correction.get("review_reasons", ""))
    )


def _active_evidence_flags(
    rows: list[dict[str, str]],
) -> set[str]:
    return {
        flag
        for row in rows
        for flag in _tokens(row.get("flags", ""))
        if flag in SOURCE_DATA_FLAGS
    }


def _active_source_data_tokens(
    rows: list[dict[str, str]],
) -> set[str]:
    return {
        flag
        for row in rows
        for flag in _tokens(row.get("flags", ""))
        if _is_source_data_token(flag)
    }


def _active_row_provenance_flags(row: Mapping[str, str]) -> set[str]:
    flags = set(_tokens(row.get("flags", "")))
    active: set[str] = set()
    if (
        row.get("provenance_status", "") == "ambiguous_count_mismatch"
        and "overlap_count_ambiguous" in flags
    ):
        active.add("source_provenance_inconsistent")
    if "overlap_history_ambiguous" in flags:
        active.add("source_provenance_ambiguous")
    return active


def _active_sources(
    row: Mapping[str, str],
    index: ActiveProvenanceIndex,
) -> tuple[list[dict[str, str]], str]:
    try:
        return active_source_rows(row, index), ""
    except ProvenanceError as error:
        provenance_flag = _PROVENANCE_CONFLICT_FLAGS.get(error.code)
        if provenance_flag is None:
            raise
        return _provenance_conflict_sources(row, index), provenance_flag


def _provenance_conflict_sources(
    row: Mapping[str, str],
    index: ActiveProvenanceIndex,
) -> list[dict[str, str]]:
    group_id = row.get("canonical_group_id", "")
    if group_id:
        fingerprint = index.group_fingerprints.get(group_id)
        return (
            list(index.source_by_fingerprint.get(fingerprint, []))
            if fingerprint is not None
            else []
        )
    return list(index.source_by_transaction_id.get(row.get("transaction_id", ""), []))


def _is_source_data_token(token: str) -> bool:
    if token in SOURCE_DATA_FLAGS:
        return True
    return any(
        token.startswith(prefix) and token.removeprefix(prefix).isdigit()
        for prefix in _BALANCE_PAGE_PREFIXES
    )


def _tokens(value: str) -> list[str]:
    return [token for token in value.split(";") if token]


def _evidence_pages(row: Mapping[str, str], flag: str) -> list[str]:
    page_prefix = f"{flag}_page_"
    pages = sorted(
        {
            token.removeprefix(page_prefix)[:32]
            for token in _tokens(row.get("flags", ""))
            if token.startswith(page_prefix)
            and token.removeprefix(page_prefix).isdigit()
        },
        key=int,
    )
    return pages or [row.get("source_page", "")[:32]]


def _source_data_provenance_error(
    error: ProvenanceError,
) -> SourceDataReviewError:
    messages = {
        "migration_required": (
            "Canonical provenance must be migrated before source-data inspection."
        ),
        "unavailable": "Active source-data provenance is unavailable.",
        "inconsistent": "Canonical source-data provenance is inconsistent.",
        "ambiguous": "Active source-data provenance is ambiguous.",
    }
    return SourceDataReviewError(
        f"source_data_provenance_{error.code}",
        messages[error.code],
    )
