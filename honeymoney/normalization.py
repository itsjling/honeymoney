"""Pure transaction normalization and duplicate detection helpers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping

_REVIEW_REASON_CATEGORY = "category_decision"
_REVIEW_REASON_ACCOUNTING_FLOW = "accounting_flow"
_REVIEW_REASON_SOURCE_DATA = "source_data_issue"


def _normalized_row(
    source_row: dict[str, str],
    row_number: int | str,
    profile: Mapping[str, object],
    config: Mapping[str, object],
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
        or str(profile.get("account_currency", ""))
    ).upper()
    invalid_amount_columns: list[str] = []
    original_amount = _signed_amount(source_row, columns, invalid_amount_columns)
    explicit_posted_amount = _value(source_row, columns.get("posted_amount"))
    posted_currency = (
        _value(source_row, columns.get("posted_currency"))
        or (str(profile.get("account_currency", "")) if explicit_posted_amount else "")
        or original_currency
        or str(profile.get("account_currency", ""))
    ).upper()
    posted_amount = _posted_amount(
        source_row, columns, original_amount, invalid_amount_columns
    )
    statement_opening_balance = _optional_decimal_value(
        source_row, columns.get("statement_opening_balance")
    )
    statement_closing_balance = _optional_decimal_value(
        source_row, columns.get("statement_closing_balance")
    )

    flags = ["uncategorized"]
    amount_reason = ""
    if invalid_amount_columns:
        flags.append("invalid_amount")
        amount_reason = _append_reason(
            amount_reason,
            f"Invalid amount in {', '.join(_unique(invalid_amount_columns))}",
        )

    review_reasons = [_REVIEW_REASON_CATEGORY, _REVIEW_REASON_ACCOUNTING_FLOW]
    if invalid_amount_columns:
        review_reasons.append(_REVIEW_REASON_SOURCE_DATA)
    normalized = {
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
        "amount_hkd": "",
        "valuation_source": "",
        "valuation_status": "",
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
        "review_reasons": ";".join(review_reasons),
        "reason": amount_reason or "No categorization rules have been applied",
        "flags": ";".join(flags),
        "notes": "Imported from PDF" if source_page else "",
        "source_file": source_file,
        "source_page": source_page,
        "source_row": str(row_number),
    }
    return normalized


def _normalized_match_text(value: object) -> str:
    return " ".join(str(value).strip().casefold().split())


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _default_profile() -> dict[str, object]:
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


def _clean_text(value: object) -> str:
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


def _normalize_date(value: str, profile: Mapping[str, object]) -> str:
    if not value:
        return ""

    raw_date_formats = profile.get("date_formats", ["%Y-%m-%d"])
    date_formats = (
        [item for item in raw_date_formats if isinstance(item, str)]
        if isinstance(raw_date_formats, list)
        else ["%Y-%m-%d"]
    )
    if not date_formats:
        date_formats = ["%Y-%m-%d"]
    for date_format in date_formats:
        try:
            has_year = _date_format_has_year(date_format)
            statement_year = profile.get("statement_year") if not has_year else None
            parsed = _parse_profile_date(
                value,
                date_format,
                fallback_year=int(str(statement_year)) if statement_year else 1900,
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
    amount: Decimal, currency: str, config: Mapping[str, object]
) -> tuple[Decimal | None, list[str], str]:
    base_currency = str(config.get("base_currency", "HKD")).upper()
    if currency == base_currency:
        return amount, [], ""

    raw_rates = config.get("exchange_rates", {})
    rates = raw_rates if isinstance(raw_rates, Mapping) else {}
    rate = rates.get(currency)
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


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
