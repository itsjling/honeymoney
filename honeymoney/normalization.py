"""Pure transaction normalization and duplicate detection helpers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def _normalized_row(
    source_row: dict[str, str],
    row_number: int | str,
    profile: dict[str, Any],
    config: dict[str, Any],
    columns: dict[str, str],
    source_file: str,
    source_page: str = "",
) -> dict[str, str]:
    transaction_date = _normalize_date(
        _value(source_row, columns.get("transaction_date")), profile
    )
    posting_date = _normalize_date(
        _value(source_row, columns.get("posting_date")), profile
    )
    canonical_date = transaction_date or posting_date
    description = _value(source_row, columns.get("description"))
    merchant = _value(source_row, columns.get("merchant")) or description
    original_currency = (
        _value(source_row, columns.get("original_currency"))
        or profile.get("account_currency", "")
    ).upper()
    invalid_amount_columns: list[str] = []
    original_amount = _signed_amount(source_row, columns, invalid_amount_columns)
    posted_currency = (
        _value(source_row, columns.get("posted_currency"))
        or original_currency
        or profile.get("account_currency", "")
    ).upper()
    posted_amount = _posted_amount(
        source_row, columns, original_amount, invalid_amount_columns
    )
    amount_hkd, amount_flags, amount_reason = _amount_hkd(
        posted_amount, posted_currency, config
    )
    statement_opening_balance = _optional_decimal_value(
        source_row, columns.get("statement_opening_balance")
    )
    statement_closing_balance = _optional_decimal_value(
        source_row, columns.get("statement_closing_balance")
    )

    flags = ["uncategorized"]
    if amount_flags:
        flags.extend(amount_flags)
    if invalid_amount_columns:
        flags.append("invalid_amount")
        amount_reason = _append_reason(
            amount_reason,
            f"Invalid amount in {', '.join(_unique(invalid_amount_columns))}",
        )

    return {
        "transaction_id": "",
        "source_id": "",
        "source_namespace_id": "",
        "source_revision": "",
        "source_record_id": "",
        "date": canonical_date,
        "transaction_date": transaction_date,
        "posting_date": posting_date,
        "account_id": _value(source_row, columns.get("account_id"))
        or str(profile.get("account_id", "")),
        "account": _value(source_row, columns.get("account"))
        or str(profile.get("account", "")),
        "account_type": str(
            profile.get("account_type")
            or _account_type_for_payment_method(str(profile.get("payment_method", "")))
        ),
        "institution": str(profile.get("institution", "")),
        "country": str(profile.get("country", "")),
        "original_amount": _format_decimal(original_amount),
        "original_currency": original_currency,
        "posted_amount": _format_decimal(posted_amount),
        "posted_currency": posted_currency,
        "amount_hkd": _format_decimal(amount_hkd) if amount_hkd is not None else "",
        "statement_opening_balance": statement_opening_balance,
        "statement_closing_balance": statement_closing_balance,
        "merchant": merchant,
        "original_description": description,
        "category": "Unknown",
        "flow_type": "unresolved",
        "flow_source": "deterministic",
        "transfer_group_id": "",
        "paired_transaction_id": "",
        "reconciliation_status": "not_applicable",
        "reconciliation_confidence": "",
        "owner": str(profile.get("owner", "Household")),
        "payment_method": str(profile.get("payment_method", "Unknown")),
        "confidence": "0.00",
        "needs_review": "true",
        "reason": amount_reason or "No categorization rules have been applied",
        "flags": ";".join(flags),
        "notes": "Imported from PDF" if source_page else "",
        "source_file": source_file,
        "source_page": source_page,
        "source_row": str(row_number),
    }


def _normalized_match_text(value: Any) -> str:
    return " ".join(str(value).strip().casefold().split())


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _default_profile() -> dict[str, Any]:
    return {
        "account_id": "",
        "account": "",
        "account_type": "unknown",
        "institution": "",
        "country": "",
        "account_currency": "",
        "owner": "Household",
        "payment_method": "Unknown",
    }


def _account_type_for_payment_method(payment_method: str) -> str:
    return {
        "Bank Account": "bank",
        "Credit Card": "credit_card",
        "Brokerage": "investment",
    }.get(payment_method, "unknown")


def _value(row: dict[str, str], column: str | None) -> str:
    if column is None or column == "":
        return ""
    return _clean_text(row.get(str(column)))


def _optional_decimal_value(row: dict[str, str], column: str | None) -> str:
    value = _value(row, column)
    if not value:
        return ""
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return ""
    return _format_decimal(parsed) if parsed.is_finite() else ""


def _clean_text(value: Any) -> str:
    text = str(value or "")
    cleaned = "".join(
        character
        for character in text
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    return cleaned.strip()


def _date_format_has_year(date_format: str) -> bool:
    index = 0
    while index < len(date_format):
        if date_format[index] != "%":
            index += 1
            continue
        if index + 1 >= len(date_format):
            return False
        directive = date_format[index + 1]
        if directive == "%":
            index += 2
            continue
        if directive in {"Y", "y"}:
            return True
        index += 2
    return False


def _parse_profile_date(
    value: str, date_format: str, *, fallback_year: int | None = None
) -> datetime:
    if _date_format_has_year(date_format):
        return datetime.strptime(value, date_format)
    if fallback_year is None:
        raise ValueError("A yearless date format requires a fallback year")
    return datetime.strptime(f"{value};{fallback_year}", f"{date_format};%Y")


def _normalize_date(value: str, profile: dict[str, Any]) -> str:
    if not value:
        return ""

    date_formats = profile.get("date_formats", ["%Y-%m-%d"])
    for date_format in date_formats:
        try:
            has_year = _date_format_has_year(date_format)
            statement_year = profile.get("statement_year") if not has_year else None
            parsed = _parse_profile_date(
                value,
                date_format,
                fallback_year=int(statement_year) if statement_year else 1900,
            ).date()
        except ValueError:
            continue
        return parsed.isoformat()
    return value


def _signed_amount(
    row: dict[str, str], columns: dict[str, str], invalid_columns: list[str]
) -> Decimal:
    amount_column = columns.get("amount")
    if amount_column:
        raw_amount = _value(row, amount_column)
        amount = _parse_decimal(raw_amount, invalid_columns, amount_column)
        return _apply_amount_sign(raw_amount, amount, row, columns)

    debit_column = columns.get("debit")
    credit_column = columns.get("credit")
    debit = _parse_decimal(_value(row, debit_column), invalid_columns, debit_column)
    credit = _parse_decimal(_value(row, credit_column), invalid_columns, credit_column)
    if debit != Decimal("0"):
        return -abs(debit)
    if credit != Decimal("0"):
        return abs(credit)
    return Decimal("0")


def _posted_amount(
    row: dict[str, str],
    columns: dict[str, str],
    fallback: Decimal,
    invalid_columns: list[str],
) -> Decimal:
    posted_column = columns.get("posted_amount")
    if posted_column:
        raw_amount = _value(row, posted_column)
        amount = _parse_decimal(raw_amount, invalid_columns, posted_column)
        return _apply_amount_sign(raw_amount, amount, row, columns)
    return fallback


def _apply_amount_sign(
    raw_amount: str, amount: Decimal, row: dict[str, str], columns: dict[str, str]
) -> Decimal:
    indicator = _normalized_match_text(_value(row, columns.get("credit_debit")))
    debit_values = {
        _normalized_match_text(value) for value in columns.get("debit_values", [])
    }
    credit_values = {
        _normalized_match_text(value) for value in columns.get("credit_values", [])
    }
    if indicator and indicator in debit_values:
        return -abs(amount)
    if indicator and indicator in credit_values:
        return abs(amount)
    if _amount_has_sign_suffix(raw_amount):
        return amount
    if columns.get("amount_default_sign") == "expense":
        return -abs(amount)
    if columns.get("amount_default_sign") == "income":
        return abs(amount)
    return amount


def _amount_hkd(
    amount: Decimal, currency: str, config: dict[str, Any]
) -> tuple[Decimal | None, list[str], str]:
    base_currency = str(config.get("base_currency", "HKD")).upper()
    if currency == base_currency:
        return amount, [], ""

    rate = config.get("exchange_rates", {}).get(currency)
    if rate is None:
        return None, ["missing_exchange_rate"], f"Missing exchange rate for {currency}"

    return amount * Decimal(str(rate)), [], ""


def _parse_decimal(
    value: str, invalid_columns: list[str] | None = None, column: str | None = None
) -> Decimal:
    if not value:
        return Decimal("0")
    cleaned = value.replace(",", "").strip()
    upper_cleaned = cleaned.upper()
    if upper_cleaned.endswith("CR"):
        return abs(_parse_decimal(cleaned[:-2], invalid_columns, column))
    if upper_cleaned.endswith("DR"):
        return -abs(_parse_decimal(cleaned[:-2], invalid_columns, column))
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation:
        if invalid_columns is not None and column:
            invalid_columns.append(str(column))
        return Decimal("0")
    if not parsed.is_finite():
        if invalid_columns is not None and column:
            invalid_columns.append(str(column))
        return Decimal("0")
    return parsed


def _amount_has_sign_suffix(value: str) -> bool:
    upper_value = value.replace(",", "").strip().upper()
    return upper_value.endswith("CR") or upper_value.endswith("DR")


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _annotate_duplicate_suspicions(
    transactions: list[dict[str, str]],
    retained_ledger_rows: tuple[dict[str, str], ...]
    | list[dict[str, str]]
    | None = None,
    *,
    operation_counts: dict[str, int] | None = None,
) -> None:
    """Flag only incoming rows that match an incoming or retained row."""
    comparison_rows = [*(retained_ledger_rows or ()), *transactions]
    current_row_ids = {id(transaction) for transaction in transactions}
    if operation_counts is not None:
        operation_counts["date_parses"] = 0
        operation_counts["window_checks"] = 0

    row_records: list[
        tuple[dict[str, str], date | None, tuple[str, ...], tuple[str, ...], bool]
    ] = []
    duplicate_keys: dict[tuple[str, ...], int] = {}
    for transaction in comparison_rows:
        key = _duplicate_key(transaction)
        duplicate_keys[key] = duplicate_keys.get(key, 0) + 1
        transaction_date = _parse_iso_date(transaction.get("date", ""))
        if operation_counts is not None:
            operation_counts["date_parses"] += 1
        row_records.append(
            (
                transaction,
                transaction_date,
                key,
                _duplicate_key_without_date(transaction),
                id(transaction) in current_row_ids,
            )
        )

    for transaction, _date, key, _near_key, is_current in row_records:
        if is_current and duplicate_keys[key] > 1:
            _mark_duplicate(transaction)

    near_date_groups: dict[
        tuple[str, ...], list[tuple[date, int, dict[str, str], bool]]
    ] = {}
    for index, (transaction, transaction_date, _key, near_key, is_current) in enumerate(
        row_records
    ):
        if transaction_date is not None:
            near_date_groups.setdefault(near_key, []).append(
                (transaction_date, index, transaction, is_current)
            )

    for group in near_date_groups.values():
        group.sort(key=lambda item: (item[0], item[1]))
        for index, (transaction_date, _order, transaction, is_current) in enumerate(
            group
        ):
            if not is_current:
                continue
            for neighbor_index in (index - 1, index + 1):
                if not 0 <= neighbor_index < len(group):
                    continue
                if operation_counts is not None:
                    operation_counts["window_checks"] += 1
                other_date = group[neighbor_index][0]
                if abs((transaction_date - other_date).days) <= 1:
                    _mark_duplicate(transaction)
                    break


def _duplicate_key(transaction: dict[str, str]) -> tuple[str, ...]:
    fields = [
        "date",
        "amount_hkd",
        "original_amount",
        "original_currency",
        "merchant",
        "original_description",
    ]
    return tuple(_normalized_match_text(transaction.get(field, "")) for field in fields)


def _duplicate_key_without_date(transaction: dict[str, str]) -> tuple[str, ...]:
    fields = [
        "amount_hkd",
        "original_amount",
        "original_currency",
        "merchant",
        "original_description",
    ]
    return tuple(_normalized_match_text(transaction.get(field, "")) for field in fields)


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _mark_duplicate(transaction: dict[str, str]) -> None:
    transaction["needs_review"] = "true"
    transaction["flags"] = _append_flag(transaction["flags"], "duplicate_suspected")
    transaction["reason"] = _append_reason(
        transaction["reason"], "Possible duplicate transaction"
    )


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing}; {reason}"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(item for item in existing.split(";") if item and item != flag)
