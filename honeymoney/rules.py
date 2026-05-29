from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from honeymoney.schema import (
    allowed_categories,
    allowed_owners,
    allowed_payment_methods,
)


def load_rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    rules_path = config.get("rules")
    if not rules_path:
        return []
    with Path(rules_path).open(encoding="utf-8") as fh:
        data = json.load(fh)
    rules = data.get("rules", [])
    validate_rules(rules, config)
    return rules


def validate_rules(rules: list[dict[str, Any]], config: dict[str, Any] | None = None) -> None:
    seen_ids: set[str] = set()
    categories = allowed_categories(config)
    owners = allowed_owners(config)
    payment_methods = allowed_payment_methods(config)
    allowed_fields = {
        "merchant",
        "description",
        "original_description",
        "institution",
        "account",
        "account_id",
        "payment_method",
        "country",
        "currency",
        "original_currency",
        "posted_currency",
    }
    for rule in rules:
        rule_id = str(rule.get("id", ""))
        if not rule_id:
            raise ValueError("Rule is missing id")
        if rule_id in seen_ids:
            raise ValueError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)

        if not rule.get("enabled", True):
            continue

        if rule.get("category") and rule["category"] not in categories:
            raise ValueError(f"Unsupported category in rule {rule_id}: {rule['category']}")
        if rule.get("owner") and rule["owner"] not in owners:
            raise ValueError(f"Unsupported owner in rule {rule_id}: {rule['owner']}")
        if (
            rule.get("payment_method")
            and rule["payment_method"] not in payment_methods
        ):
            raise ValueError(
                f"Unsupported payment_method in rule {rule_id}: {rule['payment_method']}"
            )
        if "confidence" in rule:
            try:
                confidence = Decimal(str(rule["confidence"]))
            except Exception as error:
                raise ValueError(
                    f"Unsupported confidence in rule {rule_id}: {rule['confidence']}"
                ) from error
            if not confidence.is_finite() or confidence < Decimal("0") or confidence > Decimal("1"):
                raise ValueError(
                    f"Unsupported confidence in rule {rule_id}: {rule['confidence']}"
                )
        if rule.get("match_type", "keyword") not in {"exact", "keyword", "regex"}:
            raise ValueError(f"Unsupported match_type in rule {rule_id}")
        if rule.get("field_logic", "any") not in {"any", "all"}:
            raise ValueError(f"Unsupported field_logic in rule {rule_id}")
        unknown_fields = set(rule.get("fields", [])) - allowed_fields
        if unknown_fields:
            raise ValueError(
                f"Unsupported fields in rule {rule_id}: {', '.join(sorted(unknown_fields))}"
            )
        if rule.get("match_type") == "regex":
            for pattern in rule.get("patterns", []):
                try:
                    re.compile(str(pattern))
                except re.error as error:
                    raise ValueError(
                        f"Invalid regex in rule {rule_id}: {pattern}"
                    ) from error


def apply_rules(
    transactions: list[dict[str, str]],
    rules: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    threshold = Decimal(str(config.get("review_confidence_threshold", 0.8)))
    ordered_rules = sorted(
        enumerate(rules),
        key=lambda indexed: (Decimal(str(indexed[1].get("priority", 0))), -indexed[0]),
        reverse=True,
    )

    for transaction in transactions:
        for _, rule in ordered_rules:
            if not rule.get("enabled", True):
                continue
            if not _rule_matches(transaction, rule):
                continue

            confidence = Decimal(str(rule.get("confidence", 0.9)))
            transaction["category"] = str(rule.get("category", transaction["category"]))
            transaction["owner"] = str(rule.get("owner", transaction["owner"]))
            if "payment_method" in rule:
                transaction["payment_method"] = str(rule["payment_method"])
            transaction["confidence"] = _format_decimal(confidence)
            transaction["needs_review"] = "false" if confidence >= threshold else "true"
            transaction["reason"] = f"Matched rule {rule.get('id', '')}".strip()
            transaction["flags"] = _remove_flag(transaction["flags"], "uncategorized")
            transaction["flags"] = _append_flag(
                transaction["flags"], f"matched_rule:{rule.get('id', '')}"
            )
            if rule.get("notes"):
                transaction["notes"] = _append_note(
                    transaction.get("notes", ""), str(rule["notes"])
                )
            break


def _rule_matches(transaction: dict[str, str], rule: dict[str, Any]) -> bool:
    fields = rule.get("fields", ["merchant", "original_description"])
    field_logic = rule.get("field_logic", "any")
    field_results = [
        _field_matches(
            transaction.get(field, ""),
            rule.get("patterns", []),
            rule.get("match_type", "keyword"),
            bool(rule.get("case_sensitive", False)),
        )
        for field in fields
    ]
    if field_logic == "all":
        return all(field_results)
    return any(field_results)


def _field_matches(
    value: str, patterns: list[str], match_type: str, case_sensitive: bool
) -> bool:
    haystack = value if case_sensitive else value.casefold()
    for pattern in patterns:
        needle = str(pattern) if case_sensitive else str(pattern).casefold()
        if match_type == "exact" and haystack == needle:
            return True
        if match_type == "keyword" and needle in haystack:
            return True
        if match_type == "regex":
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(str(pattern), value, flags=flags):
                return True
    return False


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(item for item in existing.split(";") if item and item != flag)


def _append_note(existing: str, note: str) -> str:
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}; {note}"


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
