"""Whole-workspace financial derivation from ready import records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from honeymoney.account_bindings import canonical_bound_owners, enforce_bound_owners
from honeymoney.categorization_memory import (
    apply_local_categorization_memory,
    build_local_categorization_memory,
)
from honeymoney.classification_policy import apply_structural_classification
from honeymoney.contracts import BalanceReconciliation, Config
from honeymoney.corrections import apply_corrections
from honeymoney.duplicates import refresh_duplicate_candidates
from honeymoney.ollama import apply_ollama_fallback
from honeymoney.overlap import (
    CanonicalizationResult,
    apply_history_ambiguity,
    canonicalize_overlaps,
    enforce_overlap_review,
    project_corrections,
)
from honeymoney.overlap_contracts import OverlapDiagnostic, OverlapManifest
from honeymoney.reconciliation import (
    complete_statement_rows,
    reconcile_ledger,
    statement_balance_reconciliation,
)
from honeymoney.review_state import synchronize_review_states
from honeymoney.rules import apply_rules
from honeymoney.source_data_review import repair_source_data_review_state
from honeymoney.valuation import value_transactions


@dataclass(frozen=True)
class WorkspaceDerivation:
    source_rows: list[dict[str, str]]
    rows: list[dict[str, str]]
    canonicalization: CanonicalizationResult
    overlap_manifest: OverlapManifest
    overlap: OverlapDiagnostic
    reconciliation: dict[str, object]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ViewReportInputs:
    """Complete contributing-source facts for one view report."""

    source_occurrence_count: int
    balance_reconciliation: BalanceReconciliation


def view_report_inputs(
    derivation: WorkspaceDerivation,
    represented_rows: Sequence[Mapping[str, str]],
) -> ViewReportInputs:
    """Select complete current statements that support represented view rows."""
    represented_ids = {
        str(row.get("transaction_id", ""))
        for row in represented_rows
        if row.get("transaction_id")
    }
    occurrence_ids: set[str] = set()
    for group in derivation.overlap["groups"]:
        if represented_ids.intersection(group["canonical_transaction_ids"]):
            occurrence_ids.update(
                str(identifier)
                for pool in group["source_occurrence_pools"]
                for identifier in pool
            )
    represented_source_rows = [
        row
        for row in derivation.source_rows
        if row.get("transaction_id", "") in occurrence_ids
    ]
    complete_rows = complete_statement_rows(
        derivation.source_rows,
        represented_source_rows,
    )
    return ViewReportInputs(
        source_occurrence_count=len(complete_rows),
        balance_reconciliation=statement_balance_reconciliation(complete_rows),
    )


def derive_workspace_rows(
    source_rows: Sequence[Mapping[str, str]],
    overlap_manifest: Mapping[str, object],
    config: Config,
    *,
    rules: list[dict[str, object]],
    corrections: Mapping[str, Mapping[str, str]],
    profile_mappings: Mapping[str, object] | None = None,
    allow_model: bool = True,
) -> WorkspaceDerivation:
    """Derive every view transaction before any period partitioning."""
    statement_rows = [dict(row) for row in source_rows]
    overlap_result = canonicalize_overlaps(statement_rows, [], overlap_manifest)
    rows = overlap_result.rows
    projection = project_corrections(overlap_result, corrections)
    canonical_corrections = projection.corrections

    memory_rows: list[Mapping[str, str]] = [row for row in rows]
    memory = build_local_categorization_memory(
        memory_rows, canonical_corrections, config
    )
    apply_rules(rows, rules, config)
    apply_local_categorization_memory(rows, memory, config)
    apply_structural_classification(rows, config)
    apply_corrections(rows, canonical_corrections)
    apply_history_ambiguity(rows, projection.ambiguous_transaction_ids)
    model_warnings: list[str] = []
    if allow_model:
        _model_report, model_warnings = apply_ollama_fallback(
            rows,
            config,
            corrections=canonical_corrections,
        )
    value_transactions(statement_rows, config, preserve_matched=False)
    value_transactions(rows, config, preserve_matched=False)

    mappings = profile_mappings or {}
    if mappings:
        owner_updates = canonical_bound_owners(
            statement_rows,
            overlap_result.diagnostic["groups"],
            mappings,
        )
        enforce_bound_owners(rows, owner_updates)

    enforce_overlap_review(rows, overlap_result)
    final_review_ids = {
        identifier
        for identifier, patch in canonical_corrections.items()
        if str(patch.get("needs_review", "")).casefold() == "false"
    }
    refresh_duplicate_candidates(rows, final_review_ids=final_review_ids)
    repair_source_data_review_state(rows, statement_rows, overlap_result.manifest)
    enforce_overlap_review(rows, overlap_result)
    synchronize_review_states(rows, legacy=False)
    reconciliation = reconcile_ledger(rows, config, statement_rows=statement_rows)
    return WorkspaceDerivation(
        source_rows=statement_rows,
        rows=rows,
        canonicalization=overlap_result,
        overlap_manifest=overlap_result.manifest,
        overlap=overlap_result.diagnostic,
        reconciliation=dict(reconciliation),
        warnings=tuple(model_warnings),
    )
