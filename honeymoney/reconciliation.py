from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from honeymoney.classification_policy import trusted_accounting_provenance
from honeymoney.identity import ambiguous_legacy_transaction_ids
from honeymoney.schema import ALLOWED_ACCOUNT_TYPES, ALLOWED_FLOW_TYPES

TRANSFER_FLOW_TYPES = {
    "internal_transfer",
    "credit_card_payment",
    "investment_transfer",
}
EXTERNAL_FLOW_TYPES = {"income", "expense", "refund"}
AMBIGUITY_FLAG = "reconciliation_ambiguous"
AMBIGUITY_PRIOR_REVIEW_FLAG = "reconciliation_ambiguous_prior_review"
AMBIGUITY_REASON = "Ambiguous transfer candidates"


def reconcile_ledger(
    rows: list[dict[str, str]], config: dict[str, Any]
) -> dict[str, Any]:
    """Derive cash-flow treatment and pair unique owned-account transfers."""
    window = reconciliation_date_window(config)
    ambiguous_legacy_ids = ambiguous_legacy_transaction_ids(rows)
    protected = {
        id(row)
        for row in rows
        if row.get("transaction_id", "") in ambiguous_legacy_ids
        and not any(
            row.get(field, "")
            for field in (
                "source_id",
                "source_namespace_id",
                "source_revision",
                "source_record_id",
            )
        )
    }
    by_id = {
        row.get("transaction_id", ""): row
        for row in rows
        if id(row) not in protected and row.get("transaction_id")
    }
    for row in rows:
        if id(row) in protected:
            continue
        _reset_reconciliation(row)
        _derive_flow_type(row)

    candidates: list[tuple[int, str, str, str]] = []
    eligible = [row for row in rows if id(row) not in protected and _eligible(row)]
    for index, left in enumerate(eligible):
        for right in eligible[index + 1 :]:
            candidate = _candidate(left, right, window)
            if candidate is not None:
                candidates.append(candidate)

    choices: dict[str, list[tuple[int, str, str]]] = {}
    for distance, left_id, right_id, flow_type in candidates:
        choices.setdefault(left_id, []).append((distance, right_id, flow_type))
        choices.setdefault(right_id, []).append((distance, left_id, flow_type))

    best: dict[str, tuple[str, str]] = {}
    for transaction_id, options in choices.items():
        minimum = min(option[0] for option in options)
        nearest = [option for option in options if option[0] == minimum]
        if len(nearest) == 1:
            _, other_id, flow_type = nearest[0]
            best[transaction_id] = (other_id, flow_type)

    paired: set[str] = set()
    paired_groups = 0
    for distance, left_id, right_id, flow_type in sorted(candidates):
        if left_id in paired or right_id in paired:
            continue
        if best.get(left_id) != (right_id, flow_type):
            continue
        if best.get(right_id) != (left_id, flow_type):
            continue
        _pair(by_id[left_id], by_id[right_id], flow_type, distance)
        paired.update({left_id, right_id})
        paired_groups += 1

    ambiguous = 0
    unmatched = 0
    for row in rows:
        if id(row) in protected:
            continue
        transaction_id = row.get("transaction_id", "")
        if transaction_id in paired:
            continue
        if transaction_id in choices:
            row["reconciliation_status"] = "ambiguous"
            row["reconciliation_confidence"] = "0.00"
            if row.get("needs_review") == "true":
                row["flags"] = _append_token(
                    row.get("flags", ""), AMBIGUITY_PRIOR_REVIEW_FLAG
                )
            row["needs_review"] = "true"
            row["flags"] = _append_token(row.get("flags", ""), AMBIGUITY_FLAG)
            row["reason"] = _append_reason(row.get("reason", ""), AMBIGUITY_REASON)
            if row.get("flow_source") not in {"rule", "correction"}:
                row["flow_type"] = "unresolved"
                row["flow_source"] = "reconciliation"
            ambiguous += 1
        elif row.get("flow_type") in TRANSFER_FLOW_TYPES:
            row["reconciliation_status"] = "unmatched"
            unmatched += 1

    return {
        "transaction_count": len(rows),
        "paired_groups": paired_groups,
        "paired_transactions": len(paired),
        "ambiguous_transactions": ambiguous,
        "unmatched_transactions": unmatched,
        "unresolved_transactions": sum(
            1 for row in rows if row.get("flow_type") == "unresolved"
        ),
        "balance_reconciliation": _balance_reconciliation(rows),
    }


def reconciliation_date_window(config: dict[str, Any]) -> int:
    value = config.get("reconciliation", {}).get("date_window_days", 3)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 31:
        raise ValueError(
            "Config field reconciliation.date_window_days must be an integer from 0 to 31"
        )
    return value


def transaction_direction(row: dict[str, str]) -> str | None:
    amount = _amount(row)
    if amount is None or amount == 0:
        return None
    return "inflow" if amount > 0 else "outflow"


def _reset_reconciliation(row: dict[str, str]) -> None:
    flags = row.get("flags", "")
    if AMBIGUITY_FLAG in _tokens(flags):
        prior_review = AMBIGUITY_PRIOR_REVIEW_FLAG in _tokens(flags)
        current_review = row.get("needs_review", "")
        row["flags"] = _remove_token(
            _remove_token(flags, AMBIGUITY_FLAG), AMBIGUITY_PRIOR_REVIEW_FLAG
        )
        row["reason"] = _remove_reason(row.get("reason", ""), AMBIGUITY_REASON)
        if current_review != "false":
            row["needs_review"] = "true" if prior_review else "false"
    row["transfer_group_id"] = ""
    row["paired_transaction_id"] = ""
    row["reconciliation_status"] = "not_applicable"
    row["reconciliation_confidence"] = ""
    if row.get("flow_source") == "reconciliation":
        row["flow_type"] = ""
        row["flow_source"] = ""


def _derive_flow_type(row: dict[str, str]) -> None:
    existing = row.get("flow_type", "")
    if existing in ALLOWED_FLOW_TYPES and row.get("flow_source") in {
        "rule",
        "correction",
        "structural",
    }:
        return

    category = row.get("category", "")
    amount = _amount(row)
    account_type = row.get("account_type", "unknown")
    if account_type not in ALLOWED_ACCOUNT_TYPES:
        account_type = "unknown"

    if category == "Income":
        flow_type = "income" if trusted_accounting_provenance(row) else "unresolved"
    elif category == "Credit Card Payment":
        flow_type = (
            "credit_card_payment"
            if trusted_accounting_provenance(row)
            else "unresolved"
        )
    elif category == "Internal Transfer":
        flow_type = (
            "internal_transfer" if trusted_accounting_provenance(row) else "unresolved"
        )
    elif category in {"Savings", "Investments"}:
        flow_type = (
            "investment_transfer"
            if trusted_accounting_provenance(row)
            else "unresolved"
        )
    elif amount is None or amount == 0:
        flow_type = "unresolved"
    elif category in {"", "Unknown", "Other"}:
        flow_type = "unresolved"
    elif amount > 0 and account_type == "credit_card":
        flow_type = "refund"
    elif amount < 0:
        flow_type = "expense"
    else:
        flow_type = "unresolved"
    row["flow_type"] = flow_type
    row["flow_source"] = "deterministic"


def _eligible(row: dict[str, str]) -> bool:
    explicit_flow = row.get("flow_source") in {"rule", "correction"}
    if explicit_flow and row.get("flow_type") not in TRANSFER_FLOW_TYPES:
        return False
    return bool(
        row.get("transaction_id")
        and row.get("account_id")
        and row.get("account_type") in {"bank", "credit_card", "investment"}
        and _amount(row) not in {None, Decimal("0")}
        and _row_date(row) is not None
    )


def _candidate(
    left: dict[str, str], right: dict[str, str], window: int
) -> tuple[int, str, str, str] | None:
    if left["account_id"] == right["account_id"]:
        return None
    if all(
        row.get("flow_source") == "deterministic"
        and row.get("flow_type") in EXTERNAL_FLOW_TYPES
        for row in (left, right)
    ):
        return None
    left_amount = _amount(left)
    right_amount = _amount(right)
    if left_amount is None or right_amount is None or left_amount != -right_amount:
        return None
    left_date = _row_date(left)
    right_date = _row_date(right)
    if left_date is None or right_date is None:
        return None
    distance = abs((left_date - right_date).days)
    if distance > window:
        return None

    outgoing, incoming = (left, right) if left_amount < 0 else (right, left)
    out_type = outgoing["account_type"]
    in_type = incoming["account_type"]
    if out_type == "bank" and in_type == "credit_card":
        flow_type = "credit_card_payment"
    elif {out_type, in_type} == {"bank"}:
        flow_type = "internal_transfer"
    elif {out_type, in_type} == {"bank", "investment"}:
        flow_type = "investment_transfer"
    else:
        return None
    if any(
        row.get("flow_source") in {"rule", "correction"}
        and row.get("flow_type") != flow_type
        for row in (left, right)
    ):
        return None
    return (
        distance,
        left["transaction_id"],
        right["transaction_id"],
        flow_type,
    )


def _pair(
    left: dict[str, str], right: dict[str, str], flow_type: str, distance: int
) -> None:
    transaction_ids = sorted([left["transaction_id"], right["transaction_id"]])
    digest = hashlib.sha256("|".join(transaction_ids).encode("utf-8")).hexdigest()[:16]
    group_id = f"xfer_{digest}"
    confidence = "1.00" if distance == 0 else "0.95"
    for row, other in ((left, right), (right, left)):
        if row.get("flow_source") not in {"rule", "correction"}:
            row["flow_type"] = flow_type
            row["flow_source"] = "reconciliation"
        row["transfer_group_id"] = group_id
        row["paired_transaction_id"] = other["transaction_id"]
        row["reconciliation_status"] = "paired"
        row["reconciliation_confidence"] = confidence


def _amount(row: dict[str, str]) -> Decimal | None:
    try:
        amount = Decimal(row.get("amount_hkd", ""))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _row_date(row: dict[str, str]) -> date | None:
    try:
        return date.fromisoformat(row.get("date", ""))
    except ValueError:
        return None


def _balance_reconciliation(
    rows: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        account_id = row.get("account_id", "")
        if not account_id:
            continue
        groups.setdefault((account_id, row.get("source_file", "")), []).append(row)

    accounts: dict[str, dict[str, Any]] = {}
    for (account_id, source_file), statement_rows in sorted(groups.items()):
        opening = _first_decimal(statement_rows, "statement_opening_balance")
        closing = _first_decimal(statement_rows, "statement_closing_balance")
        statement: dict[str, Any] = {
            "source_file": source_file,
            "status": "unavailable",
        }
        if opening is not None and closing is not None:
            amounts = [
                _amount_from_field(row, "posted_amount") for row in statement_rows
            ]
            if all(amount is not None for amount in amounts):
                calculated = opening + sum(
                    (amount for amount in amounts if amount is not None), Decimal("0")
                )
                difference = closing - calculated
                statement.update(
                    {
                        "status": "reconciled" if difference == 0 else "difference",
                        "opening_balance": _decimal_text(opening),
                        "closing_balance": _decimal_text(closing),
                        "calculated_closing_balance": _decimal_text(calculated),
                        "difference": _decimal_text(difference),
                    }
                )
        account = accounts.setdefault(
            account_id, {"status": "unavailable", "statements": []}
        )
        account["statements"].append(statement)

    for account in accounts.values():
        statuses = {statement["status"] for statement in account["statements"]}
        if "difference" in statuses:
            account["status"] = "difference"
        elif statuses == {"reconciled"}:
            account["status"] = "reconciled"
    return accounts


def _first_decimal(rows: list[dict[str, str]], field: str) -> Decimal | None:
    for row in rows:
        value = _amount_from_field(row, field)
        if value is not None:
            return value
    return None


def _amount_from_field(row: dict[str, str], field: str) -> Decimal | None:
    try:
        amount = Decimal(row.get(field, ""))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _append_token(existing: str, token: str) -> str:
    tokens = _tokens(existing)
    if token not in tokens:
        tokens.append(token)
    return ";".join(tokens)


def _remove_token(existing: str, token: str) -> str:
    return ";".join(item for item in _tokens(existing) if item != token)


def _tokens(existing: str) -> list[str]:
    return [item for item in existing.split(";") if item]


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing}; {reason}"


def _remove_reason(existing: str, reason: str) -> str:
    return "; ".join(
        item.strip() for item in existing.split(";") if item.strip() != reason
    )
