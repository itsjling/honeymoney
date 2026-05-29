from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

from honeymoney.schema import ALLOWED_CATEGORIES, ALLOWED_OWNERS


def apply_ollama_fallback(
    transactions: list[dict[str, str]], config: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    ollama_config = config.get("ollama", {})
    if not ollama_config.get("enabled", False):
        return {"status": "disabled"}, []

    unresolved = [
        transaction
        for transaction in transactions
        if transaction.get("category") == "Unknown" and transaction.get("needs_review") == "true"
    ]
    if not unresolved:
        return {"status": "skipped", "reason": "no unresolved transactions"}, []

    batch_size = _batch_size(ollama_config)
    applied = 0
    invalid = 0
    for batch in _chunks(unresolved, batch_size):
        try:
            response_body = _request_ollama(batch, ollama_config)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            warning = f"Ollama unavailable: {error}"
            for transaction in unresolved:
                if "ollama_categorized" in transaction.get("flags", ""):
                    continue
                transaction["flags"] = _append_flag(
                    transaction["flags"], "ollama_unavailable"
                )
                transaction["reason"] = _append_reason(
                    transaction["reason"], "Ollama unavailable"
                )
            return {"status": "unavailable", "error": str(error)}, [warning]

        batch_applied, batch_invalid = _apply_ollama_response(batch, response_body, config)
        applied += batch_applied
        invalid += batch_invalid

    status = "success" if applied and not invalid else "invalid_response"
    warnings = []
    if invalid:
        warnings = ["Ollama returned invalid categorizations"]
        applied_ids = {
            transaction["transaction_id"]
            for transaction in unresolved
            if "ollama_categorized" in transaction.get("flags", "")
        }
        for transaction in unresolved:
            if transaction["transaction_id"] in applied_ids:
                continue
            transaction["flags"] = _append_flag(
                transaction["flags"], "ollama_invalid_response"
            )
            transaction["reason"] = _append_reason(
                transaction["reason"], "Ollama returned invalid categorization"
            )
    return {"status": status, "applied_count": applied, "invalid_count": invalid}, warnings


def _batch_size(ollama_config: dict[str, Any]) -> int:
    try:
        batch_size = int(ollama_config.get("batch_size", 20))
    except (TypeError, ValueError):
        return 20
    return max(1, batch_size)


def _chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _request_ollama(
    transactions: list[dict[str, str]], ollama_config: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        "model": ollama_config.get("model", "qwen2.5:7b-instruct"),
        "stream": False,
        "prompt": json.dumps(
            {
                "task": "Categorize transactions as JSON only.",
                "transactions": [
                    _ollama_transaction_payload(row) for row in transactions
                ],
            }
        ),
    }

    request = urllib.request.Request(
        str(ollama_config.get("url", "http://localhost:11434/api/generate")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _ollama_transaction_payload(transaction: dict[str, str]) -> dict[str, str]:
    return {
        "id": transaction["transaction_id"],
        "date": transaction["date"],
        "merchant": transaction["merchant"],
        "description": transaction["original_description"],
        "original_amount": transaction["original_amount"],
        "original_currency": transaction["original_currency"],
        "posted_amount": transaction["posted_amount"],
        "posted_currency": transaction["posted_currency"],
        "amount_hkd": transaction["amount_hkd"],
        "institution": transaction["institution"],
        "payment_method": transaction["payment_method"],
    }


def _apply_ollama_response(
    unresolved: list[dict[str, str]],
    response_body: dict[str, Any],
    config: dict[str, Any],
) -> tuple[int, int]:
    raw_response = response_body.get("response", "")
    try:
        categorizations = json.loads(raw_response)
    except (TypeError, json.JSONDecodeError):
        return 0, len(unresolved)

    by_id = {transaction["transaction_id"]: transaction for transaction in unresolved}
    threshold = Decimal(str(config.get("review_confidence_threshold", 0.8)))
    applied = 0
    invalid = 0
    for categorization in categorizations:
        transaction = by_id.get(str(categorization.get("id", "")))
        category = str(categorization.get("category", ""))
        owner = str(categorization.get("owner", ""))
        try:
            confidence = Decimal(str(categorization.get("confidence", "")))
        except InvalidOperation:
            confidence = Decimal("-1")

        if (
            transaction is None
            or category not in ALLOWED_CATEGORIES
            or owner not in ALLOWED_OWNERS
            or confidence < Decimal("0")
            or confidence > Decimal("1")
        ):
            invalid += 1
            continue

        transaction["category"] = category
        transaction["owner"] = owner
        transaction["confidence"] = _format_decimal(confidence)
        transaction["reason"] = str(categorization.get("reason", "Ollama categorization"))
        transaction["flags"] = _remove_flag(transaction["flags"], "uncategorized")
        transaction["flags"] = _append_flag(transaction["flags"], "ollama_categorized")
        transaction["needs_review"] = (
            "false"
            if confidence >= threshold and category not in {"Unknown"}
            else "true"
        )
        applied += 1

    return applied, invalid


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(item for item in existing.split(";") if item and item != flag)


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing}; {reason}"


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
