from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, cast

from honeymoney.contracts import Config
from honeymoney.csv_artifacts import csv_document, read_csv_artifact
from honeymoney.duplicates import (
    refresh_duplicate_candidates,
    release_duplicate_review_ownership,
)
from honeymoney.identity import IdentityError, ambiguous_legacy_transaction_ids
from honeymoney.identity_contracts import IdentityManifest
from honeymoney.identity_state import (
    IdentityState,
    identity_manifest_path,
    load_configured_identity_state,
    load_identity_state,
    validate_source_evidence_manifest_agreement,
    validated_manifest_document,
)
from honeymoney.manual_pairs import (
    MANUAL_PAIR_FIELD,
    manual_pair_marker,
    validate_manual_pair_id,
    with_manual_pair_marker,
    without_manual_pair_marker,
)
from honeymoney.overlap import (
    CanonicalizationResult,
    apply_history_ambiguity,
    canonicalize_overlaps,
    clear_history_ambiguity,
    enforce_overlap_review,
    overlap_manifest_document,
    overlap_manifest_path,
    project_corrections,
    source_occurrences_path,
    validate_overlap_agreement,
)
from honeymoney.overlap_contracts import OverlapManifest
from honeymoney.persistence import (
    GenerationConflictError,
    configured_generation_paths,
    generation_hashes,
    generation_member_paths,
    persist_generation,
)
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.review_state import (
    REVIEW_REASON_ACCOUNTING_FLOW,
    REVIEW_REASON_CATEGORY,
    REVIEW_REASON_CATEGORY_SUGGESTION,
    replace_review_reasons,
    review_reason_labels,
    review_reason_tokens,
    set_review_reason,
    synchronize_review_state,
    synchronize_review_states,
    validate_review_reason_value,
)
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
from honeymoney.source_data_review import repair_source_data_review_state

CORRECTION_FIELDS = [
    "category",
    "flow_type",
    "owner",
    "payment_method",
    "confidence",
    "reason",
    "notes",
    "needs_review",
    "review_reasons",
    MANUAL_PAIR_FIELD,
]
CORRECTION_COLUMNS = ["transaction_id", *CORRECTION_FIELDS]
EDITABLE_CORRECTION_FIELDS = [
    field for field in CORRECTION_FIELDS if field != MANUAL_PAIR_FIELD
]
_MANUAL_PAIR_SUPERSEDED_REASON = "Manual transfer pair superseded by review"


@dataclass(frozen=True)
class CorrectionOperationResult:
    applied_count: int
    remaining_review_count: int
    transaction_ids: list[str]
    ledger_rows: list[dict[str, str]]
    rules_added: int = 0


def load_corrections(config: Config) -> dict[str, dict[str, str]]:
    corrections_path = config.get("corrections")
    if not corrections_path or not Path(str(corrections_path)).exists():
        return {}

    artifact = read_csv_artifact(Path(str(corrections_path)), CORRECTION_COLUMNS)
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
            if meaningful.get("review_reasons"):
                meaningful["needs_review"] = str(
                    bool(review_reason_tokens(meaningful["review_reasons"]))
                ).lower()
            validate_correction(transaction_id, meaningful, config)
            corrections[transaction_id] = meaningful
    return corrections


def validate_correction(
    transaction_id: str, correction: dict[str, str], config: Config
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
    if correction.get("review_reasons"):
        try:
            validate_review_reason_value(correction["review_reasons"])
        except ValueError as error:
            raise ValueError(
                f"Unsupported review_reasons in correction {transaction_id}: "
                f"{correction['review_reasons']}"
            ) from error
    if correction.get(MANUAL_PAIR_FIELD):
        try:
            validate_manual_pair_id(correction[MANUAL_PAIR_FIELD])
        except ValueError as error:
            raise ValueError(
                f"Unsupported manual_pair_id in correction {transaction_id}"
            ) from error


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
            "review_reasons",
        ]:
            if field in correction:
                transaction[field] = correction[field]

        if "flow_type" in correction:
            transaction["flow_source"] = "correction"
        if MANUAL_PAIR_FIELD in correction:
            pair_id = correction[MANUAL_PAIR_FIELD]
            transaction["flags"] = (
                with_manual_pair_marker(transaction.get("flags", ""), pair_id)
                if pair_id
                else without_manual_pair_marker(transaction.get("flags", ""))
            )

        if "needs_review" in correction:
            release_duplicate_review_ownership(transaction)
            transaction["needs_review"] = correction["needs_review"].casefold()
        if "category" in correction and correction["category"] not in {"", "Unknown"}:
            transaction["flags"] = _remove_flag(
                transaction.get("flags", ""), "uncategorized"
            )
            set_review_reason(transaction, REVIEW_REASON_CATEGORY, False)
            set_review_reason(transaction, REVIEW_REASON_CATEGORY_SUGGESTION, False)
        clear_history_ambiguity([transaction], {transaction["transaction_id"]})
        transaction["flags"] = _append_flag(
            transaction.get("flags", ""), "manual_correction"
        )
        if "review_reasons" in correction:
            replace_review_reasons(
                transaction,
                review_reason_tokens(correction["review_reasons"]),
            )
        synchronize_review_state(transaction)


def prepare_corrections_document(
    config: Config,
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
        Path(str(corrections_value)),
        csv_document(CORRECTION_COLUMNS, rows),
        merged,
    )


def review_state_correction_updates(
    corrections: Mapping[str, Mapping[str, str]],
    ledger_rows: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Return the review-state fields that differ from the repaired ledger."""
    by_id = {row.get("transaction_id", ""): row for row in ledger_rows}
    updates: dict[str, dict[str, str]] = {}
    for transaction_id, correction in corrections.items():
        row = by_id.get(transaction_id)
        if row is None:
            continue
        needs_review = row.get("needs_review", "false")
        review_reasons = row.get("review_reasons", "")
        if (
            correction.get("needs_review", "") != needs_review
            or correction.get("review_reasons", "") != review_reasons
        ):
            updates[transaction_id] = {
                "needs_review": needs_review,
                "review_reasons": review_reasons,
            }
    return updates


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
    synchronize_review_states(ledger_rows, legacy=True)
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
    if source_occurrences is not None:
        evidence = source_occurrences
    elif explicit_v2_bootstrap:
        evidence = [dict(row) for row in ledger_rows]
    else:
        if state is None or state.source_rows is None:
            raise AssertionError("identity state did not provide source occurrences")
        evidence = state.source_rows
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
        if state is None or state.overlap_manifest is None:
            raise AssertionError("identity state did not provide an overlap manifest")
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
        manifest = cast(IdentityManifest, identity_manifest)
        manifest_content = validated_manifest_document(evidence, manifest)
    else:
        if state is None:
            raise AssertionError("identity state did not provide an identity manifest")
        manifest = state.manifest
        manifest_content = validated_manifest_document(evidence, manifest)
    if source_evidence is not None:
        retained_evidence = source_evidence
    elif state is not None and state.source_evidence_rows is not None:
        retained_evidence = state.source_evidence_rows
    else:
        retained_evidence = evidence
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
        record["transaction_id"]: (source, record)
        for source in manifest["sources"]
        for record in source["records"]
    }
    for row in retained_evidence:
        source, record = records_by_transaction_id[row["transaction_id"]]
        row["source_id"] = source["source_id"]
        row["source_namespace_id"] = source["source_namespace_id"]
        row["source_record_id"] = record["source_record_id"]
        if record["state"] == "retired":
            row["source_revision"] = record["allocation_origin"]["source_revision"]
        else:
            row["source_revision"] = source["source_revision"]
    validate_source_evidence_manifest_agreement(retained_evidence, manifest)
    if overlap_manifest_document_value is not None:
        from honeymoney.overlap import parse_overlap_manifest

        canonical_manifest = parse_overlap_manifest(overlap_manifest_document_value)
        overlap_content = overlap_manifest_document_value
    elif overlap_manifest is not None:
        overlap_content = overlap_manifest_document(overlap_manifest)
        canonical_manifest = cast(OverlapManifest, overlap_manifest)
    elif migrated_overlap is not None:
        canonical_manifest = migrated_overlap.manifest
        overlap_content = overlap_manifest_document(canonical_manifest)
    else:
        if state is None or state.overlap_manifest is None:
            raise AssertionError("identity state did not provide an overlap manifest")
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
    config: Config,
    categorized_path: Path,
    correction_patches: dict[str, dict[str, str]],
    *,
    remembered_rules: list[dict[str, object]] | None = None,
    ledger_precondition: Callable[[list[dict[str, str]]], None] | None = None,
) -> CorrectionOperationResult:
    """Validate, merge, reconcile, and recoverably persist a correction operation."""
    corrections_value = config.get("corrections")
    if not corrections_value:
        raise ValueError("Config must define a corrections CSV path")
    corrections_path = Path(str(corrections_value))
    generation_paths = generation_member_paths(
        categorized_path,
        configured_generation_paths(config),
    )
    expected_generation = generation_hashes(generation_paths)
    state = load_configured_identity_state(categorized_path, config)
    migration_required = (
        state.canonical_migration_required
        or state.overlap_migration_required
        or state.ledger_schema_migration_required
    )
    if generation_hashes(generation_paths) != expected_generation:
        raise GenerationConflictError(
            "The ledger generation changed while this operation was reading it"
        )
    ledger_rows = _normalize_ledger_rows(state)
    ambiguous_ids = ambiguous_legacy_transaction_ids(ledger_rows)
    if ambiguous_ids:
        raise IdentityError("identity_legacy_transaction_id_ambiguous")
    if ledger_precondition is not None:
        ledger_precondition([dict(row) for row in ledger_rows])
    ledger_by_id = {
        row["transaction_id"]: row for row in ledger_rows if row.get("transaction_id")
    }

    requested_transaction_ids = set(correction_patches)
    existing_corrections = load_corrections(config)
    pair_by_transaction_id = {
        transaction_id: (
            existing_corrections.get(transaction_id, {}).get(MANUAL_PAIR_FIELD)
            or manual_pair_marker(row)
        )
        for transaction_id, row in ledger_by_id.items()
    }
    superseded_pair_ids = {
        pair_by_transaction_id.get(transaction_id, "")
        for transaction_id, patch in correction_patches.items()
        if _supersedes_manual_pair(patch)
    }
    superseded_pair_ids.discard("")
    expanded_patches: dict[str, dict[str, str]] = {}
    for transaction_id, pair_id in pair_by_transaction_id.items():
        if pair_id in superseded_pair_ids:
            expanded_patches[transaction_id] = _manual_pair_reset_patch(
                ledger_by_id[transaction_id]
            )
    for transaction_id, patch in correction_patches.items():
        expanded_patches[transaction_id] = {
            **expanded_patches.get(transaction_id, {}),
            **patch,
        }

    normalized_patches: dict[str, dict[str, str]] = {}
    for transaction_id, patch in expanded_patches.items():
        if transaction_id not in ledger_by_id:
            raise ValueError(f"Unknown transaction_id: {transaction_id}")
        validate_correction(transaction_id, patch, config)
        if not patch:
            raise ValueError(
                f"Correction for {transaction_id} must set at least one correction field"
            )
        normalized_patches[transaction_id] = dict(patch)

    merged_corrections = {
        transaction_id: dict(correction)
        for transaction_id, correction in existing_corrections.items()
    }
    for correction in merged_corrections.values():
        if correction.get(MANUAL_PAIR_FIELD) in superseded_pair_ids:
            correction.pop(MANUAL_PAIR_FIELD, None)
    effective_batch: dict[str, dict[str, str]] = {}
    for transaction_id, correction_patch in normalized_patches.items():
        merged_correction = {
            **merged_corrections.get(transaction_id, {}),
            **correction_patch,
        }
        effective_correction = dict(merged_correction)
        if correction_patch.get(MANUAL_PAIR_FIELD) == "":
            merged_correction.pop(MANUAL_PAIR_FIELD, None)
            effective_correction[MANUAL_PAIR_FIELD] = ""
        if "needs_review" not in merged_correction:
            merged_correction["needs_review"] = ledger_by_id[transaction_id].get(
                "needs_review", "true"
            )
            effective_correction["needs_review"] = merged_correction["needs_review"]
        validate_correction(transaction_id, effective_correction, config)
        _validate_resolved_state(
            transaction_id,
            ledger_by_id[transaction_id],
            effective_correction,
        )
        effective_batch[transaction_id] = effective_correction
        merged_corrections[transaction_id] = merged_correction

    source_rows = state.source_rows
    operation_overlap_manifest = state.overlap_manifest
    if source_rows is None or operation_overlap_manifest is None:
        raise AssertionError("identity state did not provide canonical source state")
    migration_ambiguous_ids: tuple[str, ...] = ()
    operation_overlap_result: CanonicalizationResult | None = None
    if state.canonical_migration_required and not state.bootstrap_required:
        canonical = canonicalize_overlaps(source_rows, [], operation_overlap_manifest)
        ledger_rows = canonical.rows
        operation_overlap_manifest = canonical.manifest
        operation_overlap_result = canonical
        batch_projection = project_corrections(canonical, effective_batch)
        correction_projection = project_corrections(canonical, merged_corrections)
        effective_batch = batch_projection.corrections
        merged_corrections = correction_projection.corrections
        migration_ambiguous_ids = tuple(
            sorted(
                {
                    *batch_projection.ambiguous_transaction_ids,
                    *correction_projection.ambiguous_transaction_ids,
                }
            )
        )
    elif any(row.get("canonical_group_id") for row in ledger_rows):
        operation_overlap_result = canonicalize_overlaps(
            source_rows, ledger_rows, operation_overlap_manifest
        )

    baseline_ledger = [dict(row) for row in ledger_rows]
    reconcile_ledger(baseline_ledger, config, statement_rows=source_rows)
    corrected_ledger = [dict(row) for row in ledger_rows]
    apply_corrections(corrected_ledger, effective_batch)
    reconcile_ledger(corrected_ledger, config, statement_rows=source_rows)
    refresh_duplicate_candidates(
        corrected_ledger,
        final_review_ids=_final_review_ids(merged_corrections),
    )
    apply_history_ambiguity(corrected_ledger, migration_ambiguous_ids)
    enforce_overlap_review(corrected_ledger, operation_overlap_result)
    corrected_ids = set(effective_batch)
    for index, (original, baseline, corrected) in enumerate(
        zip(ledger_rows, baseline_ledger, corrected_ledger)
    ):
        if (
            corrected.get("transaction_id", "") not in corrected_ids
            and corrected == baseline
        ):
            corrected_ledger[index] = original
    repair_source_data_review_state(
        corrected_ledger,
        source_rows,
        operation_overlap_manifest,
        transaction_ids=None if migration_required else corrected_ids,
    )
    for transaction_id, patch in review_state_correction_updates(
        merged_corrections,
        corrected_ledger,
    ).items():
        merged_corrections[transaction_id].update(patch)
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
        source_occurrences=source_rows,
        source_evidence=state.source_evidence_rows,
        overlap_manifest=operation_overlap_manifest,
    )
    files[corrections_path] = csv_document(CORRECTION_COLUMNS, correction_rows)
    rules_added = 0
    if remembered_rules:
        rules_path_value = config.get("rules")
        if not rules_path_value:
            raise ValueError("Config must define a rules JSON path to remember a rule")
        rules_path = Path(str(rules_path_value))
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

    persist_generation(
        categorized_path,
        files,
        expected_generation_hashes=expected_generation,
    )
    return CorrectionOperationResult(
        applied_count=len(requested_transaction_ids),
        remaining_review_count=len(review_rows),
        transaction_ids=sorted(requested_transaction_ids),
        ledger_rows=corrected_ledger,
        rules_added=rules_added,
    )


def _supersedes_manual_pair(patch: Mapping[str, str]) -> bool:
    return ("flow_type" in patch and patch["flow_type"] != "internal_transfer") or (
        "category" in patch and patch["category"] != "Internal Transfer"
    )


def _manual_pair_reset_patch(row: Mapping[str, str]) -> dict[str, str]:
    reasons = [
        *review_reason_tokens(row.get("review_reasons", "")),
        REVIEW_REASON_ACCOUNTING_FLOW,
        REVIEW_REASON_CATEGORY,
    ]
    return {
        "category": "Unknown",
        "flow_type": "unresolved",
        "confidence": "0.00",
        "reason": _MANUAL_PAIR_SUPERSEDED_REASON,
        "needs_review": "true",
        "review_reasons": ";".join(dict.fromkeys(reasons)),
        MANUAL_PAIR_FIELD: "",
    }


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


def read_ledger(
    path: Path,
    *,
    config: Config | None = None,
) -> list[dict[str, str]]:
    state = (
        load_identity_state(path)
        if config is None
        else load_configured_identity_state(path, config)
    )
    return _normalize_ledger_rows(state)


def _normalize_ledger_rows(state: IdentityState) -> list[dict[str, str]]:
    rows = state.rows
    for row in rows:
        if not row["account_type"] and not row.get("canonical_group_id"):
            row["account_type"] = {
                "Bank Account": "bank",
                "Credit Card": "credit_card",
                "Brokerage": "investment",
            }.get(row.get("payment_method", ""), "unknown")
    synchronize_review_states(rows, legacy=True)
    return rows


def to_review_row(row: dict[str, str]) -> dict[str, str]:
    review_row = {column: row.get(column, "") for column in REVIEW_NEEDED_COLUMNS}
    review_row["review_reason_labels"] = "; ".join(
        review_reason_labels(row.get("review_reasons", ""))
    )
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


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(item for item in existing.split(";") if item and item != flag)
