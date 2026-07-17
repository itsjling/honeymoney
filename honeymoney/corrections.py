from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from honeymoney.csv_artifacts import csv_document, read_csv_artifact
from honeymoney.persistence import persist_generation, recover_generation
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.rules import validate_rules
from honeymoney.schema import (
    ALLOWED_FLOW_TYPES,
    CATEGORIZED_COLUMNS,
    REVIEW_NEEDED_COLUMNS,
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
    categorized_path: Path, ledger_rows: list[dict[str, str]]
) -> dict[Path, str]:
    review_rows = [
        to_review_row(row) for row in ledger_rows if row.get("needs_review") == "true"
    ]
    return {
        categorized_path: csv_document(CATEGORIZED_COLUMNS, ledger_rows),
        categorized_path.parent / "review_needed.csv": csv_document(
            REVIEW_NEEDED_COLUMNS, review_rows
        ),
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
    ledger_rows = read_ledger(categorized_path)
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

    baseline_ledger = [dict(row) for row in ledger_rows]
    reconcile_ledger(baseline_ledger, config)
    corrected_ledger = [dict(row) for row in ledger_rows]
    apply_corrections(corrected_ledger, effective_batch)
    reconcile_ledger(corrected_ledger, config)
    corrected_ids = set(normalized_patches)
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

    files = ledger_output_documents(categorized_path, corrected_ledger)
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
    recover_generation(path)
    if not path.exists():
        return []
    rows = read_csv_artifact(path, CATEGORIZED_COLUMNS).rows
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
