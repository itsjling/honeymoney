"""Build conservative exact rules from active human review evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from honeymoney.classification_policy import protected_category
from honeymoney.reconciliation import transaction_direction
from honeymoney.rules import (
    MANAGED_RULE_MARKER,
    canonical_rule_amount,
    normalize_exact_text,
)
from honeymoney.schema import ALLOWED_FLOW_TYPES

_GENERIC_VALUES = frozenset(
    {
        "unknown",
        "other",
        "none",
        "n a",
        "n/a",
        "na",
        "not applicable",
        "payment",
        "card payment",
        "credit card payment",
        "transfer",
        "transaction",
    }
)
_AMBIGUITY_FLAGS = frozenset(
    {
        "identity_migration_ambiguous",
        "overlap_count_ambiguous",
        "overlap_history_ambiguous",
    }
)


@dataclass(frozen=True)
class LearningPlan:
    rules: list[dict[str, object]]
    candidate_count: int
    broad_rule_count: int
    amount_rule_count: int
    historical_rows_covered: int
    conflict_count: int
    skipped_count: int
    projected_coverage: float

    def counts(self) -> dict[str, int | float]:
        return {
            "candidates": self.candidate_count,
            "broad_rules": self.broad_rule_count,
            "amount_specific_rules": self.amount_rule_count,
            "historical_rows_covered": self.historical_rows_covered,
            "conflicts": self.conflict_count,
            "skips": self.skipped_count,
            "projected_coverage": self.projected_coverage,
        }


@dataclass(frozen=True)
class _Evidence:
    category: str
    flow_type: str


def plan_learned_rules(
    rows: list[dict[str, str]],
    corrections: Mapping[str, Mapping[str, str]],
) -> LearningPlan:
    grouped: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
    evidence_by_id: dict[str, _Evidence] = {}
    skipped = 0
    for row in rows:
        signature = _broad_signature(row)
        if signature is None or _ambiguous(row):
            skipped += 1
            continue
        grouped.setdefault(signature, []).append(row)
        correction = corrections.get(row.get("transaction_id", ""))
        row_evidence = _review_evidence(correction)
        if row_evidence is None:
            skipped += 1
            continue
        evidence_by_id[row["transaction_id"]] = row_evidence

    candidates = len(evidence_by_id)
    learned: list[dict[str, object]] = []
    covered: set[str] = set()
    conflicts = 0
    broad_count = 0
    amount_count = 0
    for signature, signature_rows in sorted(grouped.items()):
        group_evidence = [
            evidence_by_id.get(row.get("transaction_id", "")) for row in signature_rows
        ]
        available = {item for item in group_evidence if item is not None}
        if len(available) == 1 and all(item is not None for item in group_evidence):
            outcome = next(iter(available))
            learned.append(_managed_rule("broad", signature, outcome))
            broad_count += 1
            covered.update(row["transaction_id"] for row in signature_rows)
            continue
        if len(available) < 2:
            continue
        conflicts += 1
        amount_groups: dict[
            tuple[str, str], list[tuple[dict[str, str], _Evidence | None]]
        ] = {}
        for row, row_evidence in zip(signature_rows, group_evidence):
            amount_key = _amount_signature(row)
            if amount_key is None:
                continue
            amount_groups.setdefault(amount_key, []).append((row, row_evidence))
        for amount_signature, items in sorted(amount_groups.items()):
            outcomes = {
                row_evidence for _, row_evidence in items if row_evidence is not None
            }
            if len(outcomes) != 1 or any(
                row_evidence is None for _, row_evidence in items
            ):
                if len(outcomes) > 1:
                    conflicts += 1
                continue
            outcome = next(iter(outcomes))
            learned.append(
                _managed_rule(
                    "amount",
                    signature,
                    outcome,
                    amount_signature=amount_signature,
                )
            )
            amount_count += 1
            covered.update(row["transaction_id"] for row, _ in items)

    learned.sort(key=lambda rule: str(rule["id"]))
    return LearningPlan(
        rules=learned,
        candidate_count=candidates,
        broad_rule_count=broad_count,
        amount_rule_count=amount_count,
        historical_rows_covered=len(covered),
        conflict_count=conflicts,
        skipped_count=skipped,
        projected_coverage=(len(covered) / candidates if candidates else 0.0),
    )


def _broad_signature(row: Mapping[str, str]) -> tuple[str, str, str, str] | None:
    institution = normalize_exact_text(row.get("institution", ""))
    account_id = normalize_exact_text(row.get("account_id", ""))
    description = normalize_exact_text(row.get("original_description", ""))
    direction = transaction_direction(dict(row))
    values = (institution, account_id, description)
    if direction is None or any(
        not value or value in _GENERIC_VALUES for value in values
    ):
        return None
    return institution, account_id, description, direction


def _amount_signature(row: Mapping[str, str]) -> tuple[str, str] | None:
    amount = canonical_rule_amount(row.get("posted_amount", ""))
    currency = row.get("posted_currency", "").strip().upper()
    if amount in {None, "0"} or not currency:
        return None
    return amount, currency


def _review_evidence(correction: Mapping[str, str] | None) -> _Evidence | None:
    if correction is None or correction.get("needs_review", "").casefold() != "false":
        return None
    category = correction.get("category", "").strip()
    if not category or category == "Unknown":
        return None
    flow_type = correction.get("flow_type", "").strip()
    if protected_category(category) and flow_type not in ALLOWED_FLOW_TYPES:
        return None
    if flow_type and flow_type not in ALLOWED_FLOW_TYPES:
        return None
    return _Evidence(category, flow_type)


def _ambiguous(row: Mapping[str, str]) -> bool:
    flags = set(filter(None, row.get("flags", "").split(";")))
    return (
        bool(flags & _AMBIGUITY_FLAGS)
        or row.get("provenance_status", "") == "ambiguous_count_mismatch"
    )


def _managed_rule(
    kind: str,
    signature: tuple[str, str, str, str],
    evidence: _Evidence,
    *,
    amount_signature: tuple[str, str] | None = None,
) -> dict[str, object]:
    institution, account_id, description, direction = signature
    identity = {
        "kind": kind,
        "institution": institution,
        "account_id": account_id,
        "description": description,
        "direction": direction,
        "category": evidence.category,
        "flow_type": evidence.flow_type,
        "amount": amount_signature,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    conditions: list[dict[str, object]] = [
        {
            "field": "normalized_institution",
            "match_type": "exact",
            "patterns": [institution],
        },
        {
            "field": "normalized_account_id",
            "match_type": "exact",
            "patterns": [account_id],
        },
        {
            "field": "normalized_original_description",
            "match_type": "exact",
            "patterns": [description],
        },
        {"field": "direction", "match_type": "exact", "patterns": [direction]},
    ]
    if amount_signature is not None:
        amount, currency = amount_signature
        conditions.extend(
            [
                {
                    "field": "posted_amount",
                    "match_type": "exact",
                    "patterns": [amount],
                },
                {
                    "field": "posted_currency",
                    "match_type": "exact",
                    "patterns": [currency],
                },
            ]
        )
    rule: dict[str, object] = {
        "id": f"learn-{kind}-{digest}",
        "enabled": True,
        "managed_by": MANAGED_RULE_MARKER,
        "priority": -999 if amount_signature is not None else -1000,
        "confidence": 1.0,
        "conditions": conditions,
        "category": evidence.category,
    }
    if evidence.flow_type:
        rule["flow_type"] = evidence.flow_type
    return rule
