from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from honeymoney.csv_artifacts import csv_document, read_csv_artifact
from honeymoney.duplicates import release_duplicate_review_ownership
from honeymoney.identity import IdentityError, ambiguous_legacy_transaction_ids
from honeymoney.identity_state import (
    IdentityState,
    identity_manifest_path,
    load_identity_state,
    validate_source_evidence_manifest_agreement,
    validated_manifest_document,
)
from honeymoney.overlap import (
    apply_history_ambiguity,
    canonicalize_overlaps,
    enforce_overlap_review,
    overlap_manifest_document,
    overlap_manifest_path,
    project_corrections,
    source_occurrences_path,
    validate_overlap_agreement,
)
from honeymoney.persistence import persist_generation
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.rules import validate_rules
from honeymoney.schema import (
    ALLOWED_FLOW_TYPES,
    CATEGORIZED_COLUMNS,
    REVIEW_NEEDED_COLUMNS,
    SOURCE_OCCURRENCE_COLUMNS,
    allowed_categories,
    allowed_owners,
    allowed_payment_methods,
)

CORRECTION_FIELDS = [
    "category",
    "flow_type",
    "owner",
    "payment_method",
    "confidence",
    "reason",
    "notes",
    "needs_review",
]
CORRECTION_COLUMNS = ["transaction_id", *CORRECTION_FIELDS]


@dataclass(frozen=True)
class CorrectionOperationResult:
    applied_count: int
    remaining_review_count: int
    transaction_ids: list[str]
    ledger_rows: list[dict[str, str]]
    rules_added: int = 0


def load_corrections(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    corrections_path = config.get("corrections")
    if not corrections_path or not Path(corrections_path).exists():
        return {}

    artifact = read_csv_artifact(Path(corrections_path), CORRECTION_COLUMNS)
    corrections: dict[str, dict[str, str]] = {}
    for row_index, row in enumerate(artifact.rows):
        transaction_id = row.get("transaction_id", "").strip()
        if not transaction_id:
            continue
        meaningful = {}
        for field in CORRECTION_FIELDS:
            if field == "notes":
                continue
            value = _correction_csv_value(
                field,
                row.get(field, ""),
                (row_index, field) in artifact.encoded_cells,
            )
            if value:
                meaningful[field] = value
        raw_notes = row.get("notes")
        if raw_notes is not None and raw_notes != "":
            meaningful["notes"] = _correction_csv_value(
                "notes",
                raw_notes,
                (row_index, "notes") in artifact.encoded_cells,
            )
        if meaningful:
            validate_correction(transaction_id, meaningful, config)
            corrections[transaction_id] = meaningful
    return corrections


def validate_correction(
    transaction_id: str, correction: dict[str, str], config: dict[str, Any]
) -> None:
    unknown = set(correction) - set(CORRECTION_FIELDS)
    if unknown:
        raise ValueError(
            f"Unsupported correction fields for {transaction_id}: "
            + ", ".join(sorted(unknown))
        )
    if correction.get("category") and correction["category"] not in allowed_categories(
        config
    ):
        raise ValueError(
            f"Unsupported category in correction {transaction_id}: "
            f"{correction['category']}"
        )
    if (
        correction.get("flow_type")
        and correction["flow_type"] not in ALLOWED_FLOW_TYPES
    ):
        raise ValueError(
            f"Unsupported flow_type in correction {transaction_id}: "
            f"{correction['flow_type']}"
        )
    if correction.get("owner") and correction["owner"] not in allowed_owners(config):
        raise ValueError(
            f"Unsupported owner in correction {transaction_id}: {correction['owner']}"
        )
    if correction.get("payment_method") and correction[
        "payment_method"
    ] not in allowed_payment_methods(config):
        raise ValueError(
            "Unsupported payment_method in correction "
            f"{transaction_id}: {correction['payment_method']}"
        )
    if correction.get("confidence"):
        try:
            confidence = Decimal(correction["confidence"])
        except InvalidOperation as error:
            raise ValueError(
                f"Unsupported confidence in correction {transaction_id}: "
                f"{correction['confidence']}"
            ) from error
        if (
            not confidence.is_finite()
            or confidence < Decimal("0")
            or confidence > Decimal("1")
        ):
            raise ValueError(
                f"Unsupported confidence in correction {transaction_id}: "
                f"{correction['confidence']}"
            )
    if correction.get("needs_review") and correction["needs_review"].casefold() not in {
        "true",
        "false",
    }:
        raise ValueError(
            f"Unsupported needs_review in correction {transaction_id}: "
            f"{correction['needs_review']}"
        )


def apply_corrections(
    transactions: list[dict[str, str]], corrections: dict[str, dict[str, str]]
) -> None:
    for transaction in transactions:
        correction = corrections.get(transaction["transaction_id"])
        if not correction:
            continue

        for field in [
            "category",
            "flow_type",
            "owner",
            "payment_method",
            "confidence",
            "reason",
            "notes",
        ]:
            if field in correction:
                transaction[field] = correction[field]

        if "flow_type" in correction:
            transaction["flow_source"] = "correction"

        if "needs_review" in correction:
            release_duplicate_review_ownership(transaction)
            transaction["needs_review"] = correction["needs_review"].casefold()
        transaction["flags"] = _append_flag(
            transaction.get("flags", ""), "manual_correction"
        )


def prepare_corrections_document(
    config: dict[str, Any],
    correction_patches: dict[str, dict[str, str]] | None = None,
    *,
    removed_transaction_ids: set[str] | None = None,
) -> tuple[Path, str, dict[str, dict[str, str]]]:
    """Build filtered and merged correction state without changing the live file."""
    corrections_value = config.get("corrections")
    if not corrections_value:
        raise ValueError("Config must define a corrections CSV path")
    merged = load_corrections(config)
    for transaction_id in removed_transaction_ids or set():
        merged.pop(transaction_id, None)
    for transaction_id, patch in (correction_patches or {}).items():
        validate_correction(transaction_id, patch, config)
        merged[transaction_id] = {**merged.get(transaction_id, {}), **patch}
    rows = [
        _correction_row(transaction_id, correction)
        for transaction_id, correction in sorted(merged.items())
    ]
    return (
        Path(corrections_value),
        csv_document(CORRECTION_COLUMNS, rows),
        merged,
    )


def ledger_output_documents(
    categorized_path: Path,
    ledger_rows: list[dict[str, str]],
    *,
    identity_manifest: Mapping[str, object] | None = None,
    identity_manifest_document: str | None = None,
    source_occurrences: list[dict[str, str]] | None = None,
    source_evidence: list[dict[str, str]] | None = None,
    overlap_manifest: Mapping[str, object] | None = None,
    overlap_manifest_document_value: str | None = None,
) -> dict[Path, str]:
    """Build public and hidden artifacts for one canonical generation."""
    if identity_manifest is not None and identity_manifest_document is not None:
        raise ValueError("Pass either an identity manifest or its document, not both")
    if overlap_manifest is not None and overlap_manifest_document_value is not None:
        raise ValueError("Pass either an overlap manifest or its document, not both")
    state: IdentityState | None = None
    if (
        source_occurrences is None
        or source_evidence is None
        or (overlap_manifest is None and overlap_manifest_document_value is None)
        or (identity_manifest is None and identity_manifest_document is None)
    ):
        state = load_identity_state(categorized_path)
    explicit_v2_bootstrap = (
        state is not None
        and not state.rows
        and source_occurrences is None
        and (identity_manifest is not None or identity_manifest_document is not None)
        and bool(ledger_rows)
    )
    evidence = (
        source_occurrences
        if source_occurrences is not None
        else (
            [dict(row) for row in ledger_rows]
            if explicit_v2_bootstrap
            else state.source_rows
        )
    )
    supplied_by_id = {row.get("transaction_id", ""): row for row in ledger_rows}
    evidence = [dict(row) for row in evidence]
    for row in evidence:
        supplied = supplied_by_id.get(row.get("transaction_id", ""))
        if (
            supplied is not None
            and not row.get("source_id")
            and not row.get("account_type")
        ):
            row["account_type"] = supplied.get("account_type", "")
    write_time_migration = (
        state is not None
        and state.canonical_migration_required
        and not state.bootstrap_required
        and source_occurrences is None
        and overlap_manifest is None
        and overlap_manifest_document_value is None
    )
    migrated_overlap = None
    if write_time_migration or explicit_v2_bootstrap:
        for row in evidence:
            supplied = supplied_by_id.get(row.get("transaction_id", ""))
            if supplied is None:
                continue
            for field in (
                "category",
                "flow_type",
                "flow_source",
                "transfer_group_id",
                "paired_transaction_id",
                "reconciliation_status",
                "reconciliation_confidence",
                "owner",
                "payment_method",
                "confidence",
                "needs_review",
                "reason",
                "flags",
                "notes",
            ):
                row[field] = supplied.get(field, "")
        migrated_overlap = canonicalize_overlaps(evidence, [], state.overlap_manifest)
        ledger_rows = migrated_overlap.rows
    if identity_manifest_document is not None:
        from honeymoney.identity import parse_manifest

        manifest = parse_manifest(identity_manifest_document)
        manifest_content = validated_manifest_document(evidence, manifest)
    elif identity_manifest is not None:
        manifest = identity_manifest
        manifest_content = validated_manifest_document(evidence, manifest)
    else:
        manifest = state.manifest
        manifest_content = validated_manifest_document(evidence, manifest)
    retained_evidence = (
        source_evidence
        if source_evidence is not None
        else (state.source_evidence_rows if state is not None else evidence)
    )
    by_transaction_id = {row["transaction_id"]: dict(row) for row in retained_evidence}
    by_transaction_id.update({row["transaction_id"]: dict(row) for row in evidence})
    manifest_ids = {
        record["transaction_id"]
        for source in manifest["sources"]
        for record in source["records"]
    }
    active_order = [row["transaction_id"] for row in evidence]
    active_ids = set(active_order)
    retained_evidence = [
        by_transaction_id[identifier]
        for identifier in active_order
        if identifier in manifest_ids
    ]
    retained_evidence.extend(
        row
        for identifier, row in sorted(by_transaction_id.items())
        if identifier in manifest_ids and identifier not in active_ids
    )
    records_by_transaction_id = {
        record["transaction_id"]: record
        for source in manifest["sources"]
        for record in source["records"]
    }
    for row in retained_evidence:
        record = records_by_transaction_id[row["transaction_id"]]
        if record["state"] == "retired":
            row["source_revision"] = record["allocation_origin"]["source_revision"]
    validate_source_evidence_manifest_agreement(retained_evidence, manifest)
    if overlap_manifest_document_value is not None:
        from honeymoney.overlap import parse_overlap_manifest

        canonical_manifest = parse_overlap_manifest(overlap_manifest_document_value)
        overlap_content = overlap_manifest_document_value
    elif overlap_manifest is not None:
        canonical_manifest = overlap_manifest
        overlap_content = overlap_manifest_document(overlap_manifest)
    elif migrated_overlap is not None:
        canonical_manifest = migrated_overlap.manifest
        overlap_content = overlap_manifest_document(canonical_manifest)
    else:
        canonical_manifest = state.overlap_manifest
        overlap_content = state.overlap_manifest_document
    validate_overlap_agreement(ledger_rows, evidence, canonical_manifest)
    review_rows = [
        to_review_row(row) for row in ledger_rows if row.get("needs_review") == "true"
    ]
    return {
        categorized_path: csv_document(CATEGORIZED_COLUMNS, ledger_rows),
        categorized_path.parent / "review_needed.csv": csv_document(
            REVIEW_NEEDED_COLUMNS, review_rows
        ),
        identity_manifest_path(categorized_path): manifest_content,
        source_occurrences_path(categorized_path): csv_document(
            SOURCE_OCCURRENCE_COLUMNS, retained_evidence
        ),
        overlap_manifest_path(categorized_path): overlap_content,
    }


def apply_correction_operation(
    config: dict[str, Any],
    categorized_path: Path,
    correction_patches: dict[str, dict[str, str]],
    *,
    remembered_rules: list[dict[str, Any]] | None = None,
) -> CorrectionOperationResult:
    """Validate, merge, reconcile, and recoverably persist a correction operation."""
    corrections_value = config.get("corrections")
    if not corrections_value:
        raise ValueError("Config must define a corrections CSV path")
    corrections_path = Path(corrections_value)
    state = load_identity_state(categorized_path)
    ledger_rows = _normalize_ledger_rows(state)
    ambiguous_ids = ambiguous_legacy_transaction_ids(ledger_rows)
    if ambiguous_ids:
        raise IdentityError("identity_legacy_transaction_id_ambiguous")
    ledger_by_id = {
        row["transaction_id"]: row for row in ledger_rows if row.get("transaction_id")
    }

    normalized_patches: dict[str, dict[str, str]] = {}
    for transaction_id, patch in correction_patches.items():
        if transaction_id not in ledger_by_id:
            raise ValueError(f"Unknown transaction_id: {transaction_id}")
        validate_correction(transaction_id, patch, config)
        if not patch:
            raise ValueError(
                f"Correction for {transaction_id} must set at least one correction field"
            )
        normalized_patches[transaction_id] = dict(patch)

    existing_corrections = load_corrections(config)
    merged_corrections = dict(existing_corrections)
    effective_batch: dict[str, dict[str, str]] = {}
    for transaction_id, correction_patch in normalized_patches.items():
        merged_correction = {
            **existing_corrections.get(transaction_id, {}),
            **correction_patch,
        }
        if "needs_review" not in merged_correction:
            merged_correction["needs_review"] = ledger_by_id[transaction_id].get(
                "needs_review", "true"
            )
        validate_correction(transaction_id, merged_correction, config)
        _validate_resolved_state(
            transaction_id,
            ledger_by_id[transaction_id],
            merged_correction,
        )
        effective_batch[transaction_id] = merged_correction
        merged_corrections[transaction_id] = merged_correction

    operation_overlap_manifest = state.overlap_manifest
    migration_ambiguous_ids: tuple[str, ...] = ()
    if state.canonical_migration_required and not state.bootstrap_required:
        canonical = canonicalize_overlaps(state.source_rows, [], state.overlap_manifest)
        ledger_rows = canonical.rows
        operation_overlap_manifest = canonical.manifest
        projection = project_corrections(canonical, effective_batch)
        effective_batch = projection.corrections
        migration_ambiguous_ids = projection.ambiguous_transaction_ids

    baseline_ledger = [dict(row) for row in ledger_rows]
    reconcile_ledger(baseline_ledger, config, statement_rows=state.source_rows)
    corrected_ledger = [dict(row) for row in ledger_rows]
    apply_corrections(corrected_ledger, effective_batch)
    reconcile_ledger(corrected_ledger, config, statement_rows=state.source_rows)
    refresh_duplicate_candidates(
        corrected_ledger,
        final_review_ids=_final_review_ids(merged_corrections),
    )
    apply_history_ambiguity(corrected_ledger, migration_ambiguous_ids)
    enforce_overlap_review(corrected_ledger)
    corrected_ids = set(effective_batch)
    for index, (original, baseline, corrected) in enumerate(
        zip(ledger_rows, baseline_ledger, corrected_ledger)
    ):
        if (
            corrected.get("transaction_id", "") not in corrected_ids
            and corrected == baseline
        ):
            corrected_ledger[index] = original
    review_rows = [
        to_review_row(row)
        for row in corrected_ledger
        if row.get("needs_review") == "true"
    ]
    correction_rows = [
        _correction_row(transaction_id, correction)
        for transaction_id, correction in sorted(merged_corrections.items())
    ]

    files = ledger_output_documents(
        categorized_path,
        corrected_ledger,
        identity_manifest_document=state.manifest_document,
        source_occurrences=state.source_rows,
        source_evidence=state.source_evidence_rows,
        overlap_manifest=operation_overlap_manifest,
    )
    files[corrections_path] = csv_document(CORRECTION_COLUMNS, correction_rows)
    rules_added = 0
    if remembered_rules:
        rules_path_value = config.get("rules")
        if not rules_path_value:
            raise ValueError("Config must define a rules JSON path to remember a rule")
        rules_path = Path(rules_path_value)
        if not rules_path.exists():
            raise ValueError(f"Rules file does not exist: {rules_path}")
        with rules_path.open(encoding="utf-8") as fh:
            rules_document = json.load(fh)
        existing_rules = rules_document.get("rules", [])
        if not isinstance(existing_rules, list):
            raise ValueError("Rules document field rules must be a list")
        by_id = {str(rule.get("id", "")): rule for rule in existing_rules}
        for rule in remembered_rules:
            rule_id = str(rule.get("id", ""))
            prior = by_id.get(rule_id)
            if prior is None:
                existing_rules.append(rule)
                by_id[rule_id] = rule
                rules_added += 1
            elif prior != rule:
                raise ValueError(
                    f"Remembered rule id conflicts with existing rule: {rule_id}"
                )
        validate_rules(existing_rules, config)
        rules_document["rules"] = existing_rules
        files[rules_path] = json.dumps(rules_document, indent=2, sort_keys=True) + "\n"

    persist_generation(categorized_path, files)
    return CorrectionOperationResult(
        applied_count=len(normalized_patches),
        remaining_review_count=len(review_rows),
        transaction_ids=sorted(normalized_patches),
        ledger_rows=corrected_ledger,
        rules_added=rules_added,
    )


def _validate_resolved_state(
    transaction_id: str,
    ledger_row: dict[str, str],
    correction: dict[str, str],
) -> None:
    needs_review = correction.get(
        "needs_review", ledger_row.get("needs_review", "true")
    ).casefold()
    category = correction.get("category", ledger_row.get("category", ""))
    flow_type = correction.get("flow_type", ledger_row.get("flow_type", ""))
    explicit_flow = "flow_type" in correction or ledger_row.get("flow_source", "") in {
        "rule",
        "correction",
    }
    if (
        needs_review == "false"
        and category in {"", "Unknown"}
        and (flow_type in {"", "unresolved"} or not explicit_flow)
    ):
        raise ValueError(
            f"Correction {transaction_id}: Unknown category cannot be marked resolved "
            "without an explicit accounting flow decision"
        )


def _final_review_ids(corrections: Mapping[str, Mapping[str, str]]) -> set[str]:
    return {
        transaction_id
        for transaction_id, correction in corrections.items()
        if correction.get("needs_review", "").casefold() == "false"
    }


def _correction_row(transaction_id: str, correction: dict[str, str]) -> dict[str, str]:
    row = {"transaction_id": transaction_id, **correction}
    if "notes" in correction and correction["notes"] == "":
        # CSV has no null type. A single whitespace character preserves the
        # distinction between an omitted cell and an explicit clear operation.
        row["notes"] = " "
    return row


def _correction_csv_value(field: str, value: str, encoded_cell: bool) -> str:
    if field == "notes" and value == " ":
        return ""
    if encoded_cell:
        return value
    return value.strip()


def read_ledger(path: Path) -> list[dict[str, str]]:
    return _normalize_ledger_rows(load_identity_state(path))


def _normalize_ledger_rows(state: IdentityState) -> list[dict[str, str]]:
    rows = state.rows
    for row in rows:
        if not row["account_type"]:
            row["account_type"] = {
                "Bank Account": "bank",
                "Credit Card": "credit_card",
                "Brokerage": "investment",
            }.get(row.get("payment_method", ""), "unknown")
    return rows


def to_review_row(row: dict[str, str]) -> dict[str, str]:
    review_row = {column: row.get(column, "") for column in REVIEW_NEEDED_COLUMNS}
    review_row["suggested_category"] = row.get("category", "")
    review_row["suggested_flow_type"] = row.get("flow_type", "")
    review_row["suggested_owner"] = row.get("owner", "")
    review_row["suggested_payment_method"] = row.get("payment_method", "")
    review_row["category"] = ""
    review_row["flow_type"] = ""
    review_row["owner"] = ""
    review_row["payment_method"] = ""
    return review_row


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)
