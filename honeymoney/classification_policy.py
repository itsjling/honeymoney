"""Accounting-safe category policy shared by categorization and reconciliation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from honeymoney.schema import allowed_categories

PROTECTED_ACCOUNTING_CATEGORIES = frozenset(
    {"Income", "Credit Card Payment", "Internal Transfer", "Savings", "Investments"}
)
MANUAL_ONLY_CATEGORIES = frozenset({"Other", "Unknown"})

_BUILT_IN_DESCRIPTIONS = {
    "Rent/Mortgage": "Housing payments such as rent or mortgage.",
    "Utilities": "Household utility bills.",
    "Groceries": "Food and household grocery purchases.",
    "Dining": "Restaurants, cafes, and food delivery.",
    "Transport": "Transport fares, taxis, and ride hailing.",
    "Octopus": "Octopus card top-ups and related charges.",
    "Cash": "Cash withdrawals and cash spending.",
    "Shopping": "General retail purchases.",
    "Travel": "Travel, lodging, and transport bookings.",
    "Health": "Healthcare, pharmacies, and insurance care costs.",
    "Subscriptions": "Recurring digital and membership subscriptions.",
    "Entertainment": "Entertainment and recreation purchases.",
    "Insurance": "Insurance premium payments.",
    "Taxes": "Tax payments.",
    "Gifts": "Gifts and charitable donations.",
    "Household": "Household goods and services.",
}
_STRUCTURAL_MARKERS = {
    "cashback": re.compile(r"\b(?:cashback|cash[- ]rebate)\b", re.IGNORECASE),
    "interest": re.compile(r"\binterest\b", re.IGNORECASE),
    "atm": re.compile(r"\b(?:atm withdrawal|cash withdrawal)\b", re.IGNORECASE),
    "card_payment": re.compile(
        r"\b(?:credit card payment|card payment|card settlement)\b", re.IGNORECASE
    ),
}


@dataclass(frozen=True)
class CategoryPolicy:
    kind: str
    description: str


@dataclass(frozen=True)
class ModelSuggestion:
    outcome: str
    reason: str = ""


def validate_category_policies(config: dict[str, Any]) -> None:
    raw = config.get("category_policies")
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise ValueError("Config field category_policies must be a JSON object")
    categories = allowed_categories(config)
    for category, policy in raw.items():
        if category not in categories:
            raise ValueError(
                f"Config field category_policies has unknown category {category!r}"
            )
        if not isinstance(policy, dict):
            raise ValueError(
                f"Config field category_policies.{category} must be a JSON object"
            )
        kind = policy.get("kind")
        if kind not in {"spending", "accounting", "manual_only"}:
            raise ValueError(
                f"Config field category_policies.{category}.kind must be spending, accounting, or manual_only"
            )
        description = policy.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(
                f"Config field category_policies.{category}.description must be a non-empty string"
            )
        if category in PROTECTED_ACCOUNTING_CATEGORIES and kind == "spending":
            raise ValueError(
                f"Config field category_policies.{category} cannot relax a protected accounting category to spending"
            )


def category_policies(config: dict[str, Any]) -> dict[str, CategoryPolicy]:
    """Return every configured category's resolved classification policy."""
    validate_category_policies(config)
    configured = config.get("category_policies", {})
    policies: dict[str, CategoryPolicy] = {}
    for category in allowed_categories(config):
        if category in PROTECTED_ACCOUNTING_CATEGORIES:
            default = CategoryPolicy(
                "accounting", f"Protected accounting category: {category}."
            )
        elif category in MANUAL_ONLY_CATEGORIES:
            default = CategoryPolicy(
                "manual_only", f"Manual review category: {category}."
            )
        elif category in _BUILT_IN_DESCRIPTIONS:
            default = CategoryPolicy("spending", _BUILT_IN_DESCRIPTIONS[category])
        else:
            default = CategoryPolicy(
                "manual_only", f"Custom category requiring manual review: {category}."
            )
        override = configured.get(category)
        if isinstance(override, dict):
            default = CategoryPolicy(override["kind"], override["description"].strip())
        policies[category] = default
    return policies


def model_category_descriptions(config: dict[str, Any]) -> dict[str, str]:
    return {
        category: policy.description
        for category, policy in sorted(category_policies(config).items())
        if policy.kind == "spending"
    }


def model_boundary_guidance() -> list[str]:
    """Stable prompt guidance that keeps merchant labeling out of accounting."""
    return [
        "A credit card is a payment method, not a purchase purpose.",
        "Cashback or a cash rebate is not cash spending.",
        "Food delivery belongs to Dining, not Transport.",
        "Internet or broadband service belongs to Utilities, not Transport.",
    ]


def protected_category(category: str) -> bool:
    return category in PROTECTED_ACCOUNTING_CATEGORIES


def trusted_accounting_provenance(row: dict[str, str]) -> bool:
    """Whether a protected category may establish an accounting flow."""
    if not protected_category(row.get("category", "")):
        return True
    source = row.get("flow_source", "")
    if source in {"rule", "correction", "structural", "reconciliation"}:
        return True
    flags = set(filter(None, row.get("flags", "").split(";")))
    if "manual_correction" in flags or "structural_classification" in flags:
        return True
    if any(flag.startswith("matched_rule:") for flag in flags):
        return True
    # Old ledgers had no provenance columns. Preserve them unless tagged as model output.
    return "ollama_categorized" not in flags


def apply_structural_classification(
    transactions: list[dict[str, str]], config: dict[str, Any]
) -> int:
    """Classify unambiguous accounting rows before an untrusted model sees them."""
    del config  # Kept in the shared public interface for future policy settings.
    count = 0
    for row in transactions:
        if _structural_match(row):
            count += 1
    return count


def _structural_match(row: dict[str, str]) -> bool:
    if row.get("category") != "Unknown" or row.get("needs_review") != "true":
        return False
    flags = set(filter(None, row.get("flags", "").split(";")))
    if "duplicate_suspected" in flags or any(
        flag.startswith("matched_rule:") for flag in flags
    ):
        return False
    amount = _amount(row)
    if amount is None:
        return False
    text = " ".join(
        part
        for part in (row.get("merchant", ""), row.get("original_description", ""))
        if part
    )
    if amount > 0 and _STRUCTURAL_MARKERS["cashback"].search(text):
        _set_structural(row, "Other", "refund", "cashback rebate")
    elif amount > 0 and _STRUCTURAL_MARKERS["interest"].search(text):
        _set_structural(row, "Income", "income", "interest")
    elif amount < 0 and _STRUCTURAL_MARKERS["atm"].search(text):
        _set_structural(row, "Cash", "expense", "ATM withdrawal")
    elif (
        amount > 0
        and row.get("account_type") == "credit_card"
        and _STRUCTURAL_MARKERS["card_payment"].search(text)
    ):
        _set_structural(
            row, "Credit Card Payment", "credit_card_payment", "card payment"
        )
    else:
        return False
    return True


def _set_structural(
    row: dict[str, str], category: str, flow_type: str, reason: str
) -> None:
    row["category"] = category
    row["flow_type"] = flow_type
    row["flow_source"] = "structural"
    row["confidence"] = "1.00"
    row["needs_review"] = (
        "true"
        if category == "Other" or row.get("owner") in {"", "Unknown"}
        else "false"
    )
    row["flags"] = _append(row.get("flags", ""), "structural_classification")
    row["reason"] = _append_reason(
        row.get("reason", ""), f"Structural classification: {reason}"
    )


def evaluate_model_suggestion(
    row: dict[str, str], category: str, confidence: Decimal, config: dict[str, Any]
) -> ModelSuggestion:
    policy = category_policies(config).get(category)
    if policy is None:
        return ModelSuggestion("invalid")
    if policy.kind != "spending":
        return ModelSuggestion(
            "rejected", f"Ollama policy rejected category {category}"
        )
    if (
        _amount(row) is None
        or _amount(row) == 0
        or _amount(row) >= 0
        or row.get("owner") in {"", "Unknown"}
        or "duplicate_suspected" in set(filter(None, row.get("flags", "").split(";")))
        or confidence < _threshold(config)
    ):
        return ModelSuggestion("reviewable")
    return ModelSuggestion("accepted")


def _threshold(config: dict[str, Any]) -> Decimal:
    try:
        return Decimal(str(config.get("review_confidence_threshold", 0.8)))
    except InvalidOperation:
        return Decimal("0.8")


def _amount(row: dict[str, str]) -> Decimal | None:
    try:
        amount = Decimal(row.get("amount_hkd", ""))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _append(value: str, item: str) -> str:
    parts = [part for part in value.split(";") if part]
    if item not in parts:
        parts.append(item)
    return ";".join(parts)


def _append_reason(value: str, item: str) -> str:
    return value if item in value else f"{value}; {item}" if value else item
