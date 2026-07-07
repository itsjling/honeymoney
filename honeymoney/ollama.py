from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, NamedTuple

from honeymoney.schema import allowed_categories, allowed_owners

_TICK_INTERVAL_SECONDS = 1.0


class OllamaProgress(NamedTuple):
    batch_number: int
    batch_count: int
    start_index: int
    end_index: int
    total: int
    elapsed_seconds: float


def apply_ollama_fallback(
    transactions: list[dict[str, str]],
    config: dict[str, Any],
    progress: Callable[[OllamaProgress], None] | None = None,
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
    chunks = _chunks(unresolved, batch_size)
    batch_count = len(chunks)
    applied = 0
    invalid = 0
    processed = 0
    details: list[str] = []
    for batch_number, batch in enumerate(chunks, start=1):
        start_index = processed + 1
        end_index = processed + len(batch)
        processed = end_index

        def tick(
            elapsed: float,
            _batch_number: int = batch_number,
            _start: int = start_index,
            _end: int = end_index,
        ) -> None:
            if progress is not None:
                progress(
                    OllamaProgress(
                        _batch_number, batch_count, _start, _end, len(unresolved), elapsed
                    )
                )

        tick(0.0)
        try:
            response_body = _request_ollama(
                batch, ollama_config, config, tick=tick if progress is not None else None
            )
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            error_text = _error_text(error)
            warning = f"Ollama unavailable: {error_text}"
            for transaction in unresolved:
                if "ollama_categorized" in transaction.get("flags", ""):
                    continue
                transaction["flags"] = _append_flag(
                    transaction["flags"], "ollama_unavailable"
                )
                transaction["reason"] = _append_reason(
                    transaction["reason"], "Ollama unavailable"
                )
            return {"status": "unavailable", "error": error_text}, [warning]

        batch_applied, batch_invalid, batch_details = _apply_ollama_response(
            batch, response_body, config
        )
        applied += batch_applied
        invalid += batch_invalid
        details.extend(batch_details)

    status = "success" if applied and not invalid else "invalid_response"
    warnings = []
    if invalid:
        warnings = ["Ollama returned invalid categorizations"]
        warnings.extend(details[:5])
        if len(details) > 5:
            warnings.append(f"...and {len(details) - 5} more invalid Ollama categorizations")
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
        batch_size = int(ollama_config.get("batch_size", 5))
    except (TypeError, ValueError):
        return 5
    return max(1, batch_size)


def _timeout_seconds(ollama_config: dict[str, Any]) -> float:
    try:
        timeout = float(ollama_config.get("timeout_seconds", 120))
    except (TypeError, ValueError):
        return 120.0
    return timeout if timeout > 0 else 120.0


def _error_text(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        message = ""
        try:
            body = json.loads(error.read().decode("utf-8", "replace"))
            message = str(body.get("error", ""))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        detail = f": {message}" if message else ""
        return f"HTTP {error.code} {error.reason}{detail}"
    return str(error)


def _chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _request_ollama(
    transactions: list[dict[str, str]],
    ollama_config: dict[str, Any],
    config: dict[str, Any],
    tick: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    categories = sorted(allowed_categories(config))
    owners = sorted(allowed_owners(config))
    payload = {
        "model": ollama_config.get("model", "qwen2.5:7b-instruct"),
        "stream": False,
        "think": bool(ollama_config.get("think", False)),
        "format": _response_format(categories, owners),
        "prompt": json.dumps(
            {
                "task": (
                    "Categorize each household transaction. Reply with a JSON object "
                    '{"categorizations": [...]} containing one item per transaction '
                    "with: id copied from the transaction, category from "
                    "allowed_categories, owner from allowed_owners, confidence "
                    "between 0 and 1, and a short reason."
                ),
                "allowed_categories": categories,
                "allowed_owners": owners,
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

    timeout = _timeout_seconds(ollama_config)
    if tick is None:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def worker() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result["body"] = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # re-raised on the caller's thread below
            error["exc"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    start = time.monotonic()
    thread.start()
    while thread.is_alive():
        thread.join(_TICK_INTERVAL_SECONDS)
        if thread.is_alive():
            tick(time.monotonic() - start)
    if "exc" in error:
        raise error["exc"]
    return result["body"]


def _response_format(categories: list[str], owners: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "categorizations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string", "enum": categories},
                        "owner": {"type": "string", "enum": owners},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "category", "owner", "confidence", "reason"],
                },
            }
        },
        "required": ["categorizations"],
    }


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
) -> tuple[int, int, list[str]]:
    raw_response = response_body.get("response", "")
    try:
        categorizations = json.loads(raw_response)
    except (TypeError, json.JSONDecodeError):
        detail = f"Ollama response was not JSON: {_snippet(raw_response)}"
        return 0, len(unresolved), [detail]
    if isinstance(categorizations, dict):
        categorizations = categorizations.get("categorizations")
    if not isinstance(categorizations, list):
        detail = f"Ollama response was not a JSON list: {_snippet(raw_response)}"
        return 0, len(unresolved), [detail]

    by_id = {transaction["transaction_id"]: transaction for transaction in unresolved}
    threshold = Decimal(str(config.get("review_confidence_threshold", 0.8)))
    categories = allowed_categories(config)
    owners = allowed_owners(config)
    applied = 0
    invalid = 0
    details: list[str] = []
    handled_ids: set[str] = set()
    seen_known_ids: set[str] = set()
    for categorization in categorizations:
        if not isinstance(categorization, dict):
            continue
        transaction_id = str(categorization.get("id", ""))
        transaction = by_id.get(transaction_id)
        if transaction is not None:
            seen_known_ids.add(transaction_id)
        category = str(categorization.get("category", ""))
        owner = str(categorization.get("owner", ""))
        reason = str(categorization.get("reason", ""))
        try:
            confidence = Decimal(str(categorization.get("confidence", "")))
        except InvalidOperation:
            confidence = Decimal("-1")

        problem = ""
        if transaction is None:
            problem = f"unknown transaction id {transaction_id or '(missing)'}"
        elif transaction_id in handled_ids:
            problem = "duplicate categorization for the same transaction"
        elif category not in categories:
            problem = f"category {category or '(missing)'!r} is not allowed"
        elif owner not in owners:
            problem = f"owner {owner or '(missing)'!r} is not allowed"
        elif not reason:
            problem = "missing reason"
        elif (
            not confidence.is_finite()
            or confidence < Decimal("0")
            or confidence > Decimal("1")
        ):
            problem = f"confidence {categorization.get('confidence')!r} is not between 0 and 1"
        if problem:
            invalid += 1
            subject = transaction.get("merchant", "") if transaction else transaction_id
            details.append(f"Ollama categorization rejected ({subject or 'unknown'}): {problem}")
            continue

        handled_ids.add(transaction_id)
        transaction["category"] = category
        transaction["owner"] = owner
        transaction["confidence"] = _format_decimal(confidence)
        transaction["reason"] = reason
        transaction["flags"] = _remove_flag(transaction["flags"], "uncategorized")
        transaction["flags"] = _append_flag(transaction["flags"], "ollama_categorized")
        transaction["needs_review"] = (
            "false"
            if confidence >= threshold and category not in {"Unknown"}
            else "true"
        )
        applied += 1

    unanswered = len(set(by_id) - handled_ids - seen_known_ids)
    if unanswered:
        invalid += unanswered
        details.append(
            f"Ollama returned no categorization for {unanswered} transaction(s)"
        )
    return applied, invalid, details


def _snippet(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return f"{text[:limit]}…"
    return text or "(empty)"


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
