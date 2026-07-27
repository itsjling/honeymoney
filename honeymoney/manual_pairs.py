from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Mapping

MANUAL_PAIR_FIELD = "manual_pair_id"
MANUAL_PAIR_FLAG_PREFIX = "manual_transfer_pair:"
MANUAL_PAIR_INVALID_FLAG = "manual_transfer_pair_invalid"
MANUAL_PAIR_INVALID_REASON = "Manual transfer pair no longer matches current facts"
_MANUAL_PAIR_PATTERN = re.compile(r"^mpair_[0-9a-f]{32}$")


class ManualPairError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def manual_pair_id(transaction_ids: list[str]) -> str:
    if len(transaction_ids) != 2 or len(set(transaction_ids)) != 2:
        raise ManualPairError(
            "manual_pair_requires_two",
            "A manual transfer pair requires two distinct current transaction IDs.",
        )
    digest = hashlib.sha256(
        "\0".join(sorted(transaction_ids)).encode("utf-8")
    ).hexdigest()[:32]
    return f"mpair_{digest}"


def validate_manual_pair_id(value: str) -> None:
    if not _MANUAL_PAIR_PATTERN.fullmatch(value):
        raise ValueError("manual_pair_id must use the mpair_ identifier format")


def manual_pair_marker(row: Mapping[str, str]) -> str:
    markers = [
        token.removeprefix(MANUAL_PAIR_FLAG_PREFIX)
        for token in _tokens(row.get("flags", ""))
        if token.startswith(MANUAL_PAIR_FLAG_PREFIX)
    ]
    if len(markers) != 1:
        return ""
    marker = markers[0]
    return marker if _MANUAL_PAIR_PATTERN.fullmatch(marker) else ""


def with_manual_pair_marker(flags: str, pair_id: str) -> str:
    validate_manual_pair_id(pair_id)
    retained = [
        token
        for token in _tokens(flags)
        if not token.startswith(MANUAL_PAIR_FLAG_PREFIX)
    ]
    retained.append(f"{MANUAL_PAIR_FLAG_PREFIX}{pair_id}")
    return ";".join(dict.fromkeys(retained))


def without_manual_pair_marker(flags: str) -> str:
    return ";".join(
        token
        for token in _tokens(flags)
        if not token.startswith(MANUAL_PAIR_FLAG_PREFIX)
    )


def validate_manual_pair_facts(
    left: Mapping[str, str],
    right: Mapping[str, str],
) -> None:
    left_id = left.get("transaction_id", "")
    right_id = right.get("transaction_id", "")
    if not left_id or not right_id or left_id == right_id:
        raise ManualPairError(
            "manual_pair_requires_two",
            "A manual transfer pair requires two distinct current transaction IDs.",
        )
    if not left.get("account_id") or left.get("account_id") != right.get("account_id"):
        raise ManualPairError(
            "manual_pair_account_mismatch",
            "Manual cash-movement pairs must belong to the same account.",
        )
    left_currency = left.get("posted_currency", "").strip().upper()
    right_currency = right.get("posted_currency", "").strip().upper()
    if not left_currency or left_currency != right_currency:
        raise ManualPairError(
            "manual_pair_currency_mismatch",
            "Manual cash-movement pairs must use the same posted currency.",
        )
    left_amount = _posted_amount(left)
    right_amount = _posted_amount(right)
    if left_amount is None or right_amount is None:
        raise ManualPairError(
            "manual_pair_amount_unavailable",
            "Manual cash-movement pairs require finite posted amounts.",
        )
    if left_amount == 0 or right_amount == 0 or (left_amount > 0) == (right_amount > 0):
        raise ManualPairError(
            "manual_pair_same_sign",
            "Manual cash-movement pairs must have opposite signs.",
        )
    if abs(left_amount) != abs(right_amount):
        raise ManualPairError(
            "manual_pair_amount_mismatch",
            "Manual cash-movement pairs must have equal absolute posted amounts.",
        )
    if not _owners_compatible(left.get("owner", ""), right.get("owner", "")):
        raise ManualPairError(
            "manual_pair_owner_mismatch",
            "Manual cash-movement pair ownership is not compatible.",
        )


def _posted_amount(row: Mapping[str, str]) -> Decimal | None:
    try:
        amount = Decimal(row.get("posted_amount", ""))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _owners_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    shared = {"", "Unknown", "Household"}
    return left in shared or right in shared


def _tokens(value: str) -> list[str]:
    return [token for token in value.split(";") if token]
