"""Opt-in, correction-derived local categorization memory."""

from __future__ import annotations

import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Mapping

from honeymoney.classification_policy import category_policies
from honeymoney.identity import has_stable_v2_identity

LOCAL_MEMORY_FLAG = "local_memory_categorized"
LOCAL_MEMORY_CONFIDENCE = "0.90"
LOCAL_MEMORY_MIN_OBSERVATIONS = 2
IDENTITY_MIGRATION_AMBIGUITY_FLAG = "identity_migration_ambiguous"
_EXCLUDED_SIGNATURES = frozenset(
    {
        "ach",
        "apple",
        "card payment",
        "credit card payment",
        "fps",
        "payment",
        "transfer",
        "wire",
    }
)
_EXCLUDED_TOKENS = frozenset({"ach", "fps", "transfer", "wire"})
_MemoryKey = tuple[str, str, str, str]
_MemoryMatch = dict[str, str | int]


def build_local_categorization_memory(
    ledger_rows: list[Mapping[str, str]],
    corrections: Mapping[str, Mapping[str, str]],
    config: Mapping[str, object],
) -> dict[_MemoryKey, _MemoryMatch]:
    """Build eligible local evidence from a validated ledger and corrections."""
    if not local_categorization_memory_enabled(config):
        return {}

    policies = category_policies(dict(config))
    ledger_by_id = {
        str(row.get("transaction_id", "")): row
        for row in ledger_rows
        if has_stable_v2_identity(row)
        and IDENTITY_MIGRATION_AMBIGUITY_FLAG not in _flags(row)
    }
    threshold = _review_threshold(config)
    observations: dict[_MemoryKey, dict[str, set[str]]] = {}
    for transaction_id, correction in sorted(corrections.items()):
        row = ledger_by_id.get(transaction_id)
        category = str(correction.get("category", "")).strip()
        if (
            row is None
            or not category
            or policies.get(category) is None
            or policies[category].kind != "spending"
        ):
            continue
        if str(correction.get("needs_review", "")).casefold() != "false":
            continue
        confidence = str(correction.get("confidence", "")).strip()
        if confidence and _decimal(confidence) < threshold:
            continue
        key = local_memory_key(row)
        if key is not None:
            observations.setdefault(key, {}).setdefault(category, set()).add(
                transaction_id
            )

    return {
        key: {"category": category, "observation_count": len(transaction_ids)}
        for key, categories in observations.items()
        if len(categories) == 1
        for category, transaction_ids in categories.items()
        if len(transaction_ids) >= LOCAL_MEMORY_MIN_OBSERVATIONS
    }


def apply_local_categorization_memory(
    transactions: list[dict[str, str]],
    memory: Mapping[_MemoryKey, _MemoryMatch],
    config: Mapping[str, object],
) -> None:
    """Apply a local match only to still-unknown, current-v2 rows."""
    if not local_categorization_memory_enabled(config) or not memory:
        return

    threshold = _review_threshold(config)
    confidence = Decimal(LOCAL_MEMORY_CONFIDENCE)
    policies = category_policies(dict(config))
    for transaction in transactions:
        if not has_stable_v2_identity(transaction) or transaction.get(
            "category", ""
        ) not in {"", "Unknown"}:
            continue
        key = local_memory_key(transaction)
        match = memory.get(key) if key is not None else None
        if match is None:
            continue
        category = str(match["category"])
        if policies.get(category) is None or policies[category].kind != "spending":
            continue
        transaction["category"] = category
        transaction["confidence"] = LOCAL_MEMORY_CONFIDENCE
        transaction["needs_review"] = str(confidence < threshold).lower()
        transaction["reason"] = (
            "Matched local categorization memory from "
            f"{int(match['observation_count'])} reviewed transactions"
        )
        transaction["flags"] = _append_flag(
            _remove_flag(transaction.get("flags", ""), "uncategorized"),
            LOCAL_MEMORY_FLAG,
        )


def local_categorization_memory_enabled(config: Mapping[str, object]) -> bool:
    """Return whether the explicit, opt-in setting is enabled."""
    settings = config.get("categorization_memory", {})
    return isinstance(settings, Mapping) and settings.get("enabled") is True


def local_memory_key(row: Mapping[str, str]) -> _MemoryKey | None:
    """Return the conservative scope and merchant signature for a row."""
    signature = _normalize_merchant(str(row.get("merchant", "")))
    if not signature or signature in _EXCLUDED_SIGNATURES:
        return None
    if set(signature.split()) & _EXCLUDED_TOKENS:
        return None
    return (
        str(row.get("account_id", "")),
        str(row.get("institution", "")),
        str(row.get("posted_currency", "")),
        signature,
    )


def _review_threshold(config: Mapping[str, object]) -> Decimal:
    return _decimal(str(config.get("review_confidence_threshold", 0.8)))


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid local categorization memory confidence") from error


def _normalize_merchant(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


def _flags(row: Mapping[str, str]) -> frozenset[str]:
    return frozenset(part for part in str(row.get("flags", "")).split(";") if part)


def _append_flag(existing: str, flag: str) -> str:
    values = [part for part in existing.split(";") if part]
    if flag not in values:
        values.append(flag)
    return ";".join(values)


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(part for part in existing.split(";") if part and part != flag)
