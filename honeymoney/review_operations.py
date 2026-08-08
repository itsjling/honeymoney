"""Pure saved-review operations for stable view transactions."""

from __future__ import annotations

from honeymoney.reconciliation import transaction_direction
from honeymoney.review_state import (
    REVIEW_REASON_ACCOUNTING_FLOW,
    REVIEW_REASON_CATEGORY,
    REVIEW_REASON_CATEGORY_SUGGESTION,
    review_reason_tokens,
)

_DECISION_FIELDS = {
    "income": {"category": "Income", "flow_type": "income"},
    "refund": {"flow_type": "refund"},
    "internal-transfer": {
        "category": "Internal Transfer",
        "flow_type": "internal_transfer",
    },
    "credit-card-payment": {
        "category": "Credit Card Payment",
        "flow_type": "credit_card_payment",
    },
    "investment-transfer": {
        "category": "Investments",
        "flow_type": "investment_transfer",
    },
    "expense": {"flow_type": "expense"},
    "unresolved": {"flow_type": "unresolved"},
}


def accounting_decision_patch(
    transaction: dict[str, str], decision: str, reason: str
) -> dict[str, str]:
    """Build one correction patch without changing the derived row."""
    if decision not in _DECISION_FIELDS:
        raise ValueError(f"Unsupported review decision: {decision}")
    if decision == "income" and transaction_direction(transaction) != "inflow":
        raise ValueError("Income can be confirmed only for a normalized inflow")
    remaining = [
        item
        for item in review_reason_tokens(transaction.get("review_reasons", ""))
        if item != REVIEW_REASON_ACCOUNTING_FLOW
    ]
    patch = {"confidence": "1.00", "reason": reason}
    patch.update(_DECISION_FIELDS[decision])
    if "category" in patch:
        remaining = [
            item
            for item in remaining
            if item not in {REVIEW_REASON_CATEGORY, REVIEW_REASON_CATEGORY_SUGGESTION}
        ]
    if decision == "unresolved":
        remaining.append(REVIEW_REASON_ACCOUNTING_FLOW)
    patch["review_reasons"] = ";".join(dict.fromkeys(remaining))
    patch["needs_review"] = str(bool(remaining)).lower()
    return patch
