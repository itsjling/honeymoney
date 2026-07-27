"""Base-currency valuation with explicit source and completeness state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from honeymoney.contracts import Config

VALUATION_SOURCE_STATEMENT = "statement_posted"
VALUATION_SOURCE_MATCHED_EXCHANGE = "matched_exchange_leg"
VALUATION_SOURCE_DATED_RATE = "configured_dated_rate"
VALUATION_SOURCE_FIXED_RATE = "configured_fixed_rate"
VALUATION_SOURCE_MISSING = "missing"

VALUATION_STATUS_ACTUAL = "actual"
VALUATION_STATUS_ESTIMATED = "estimated"
VALUATION_STATUS_MISSING = "missing"

ALLOWED_VALUATION_SOURCES = frozenset(
    {
        VALUATION_SOURCE_STATEMENT,
        VALUATION_SOURCE_MATCHED_EXCHANGE,
        VALUATION_SOURCE_DATED_RATE,
        VALUATION_SOURCE_FIXED_RATE,
        VALUATION_SOURCE_MISSING,
    }
)
ALLOWED_VALUATION_STATUSES = frozenset(
    {
        VALUATION_STATUS_ACTUAL,
        VALUATION_STATUS_ESTIMATED,
        VALUATION_STATUS_MISSING,
    }
)


class ValuationSummary(TypedDict):
    sources: dict[str, int]
    statuses: dict[str, int]
    missing_count: int
    estimated_count: int


def value_transaction(
    row: dict[str, str],
    config: Config,
    *,
    preserve_matched: bool = True,
) -> None:
    """Set reporting value, source, and status from checked statement facts."""
    base_currency = str(config.get("base_currency", "HKD")).upper()
    posted_currency = row.get("posted_currency", "").strip().upper()
    amount = _decimal(row.get("posted_amount", ""))
    transaction_date = row.get("date", "")
    matched_amount = _decimal(row.get("amount_hkd", ""))

    if (
        preserve_matched
        and row.get("valuation_source") == VALUATION_SOURCE_MATCHED_EXCHANGE
        and matched_amount is not None
    ):
        row["amount_hkd"] = _format_decimal(matched_amount)
        row["valuation_status"] = VALUATION_STATUS_ACTUAL
        _set_missing_rate_flag(row, False)
        return

    if amount is None:
        legacy_amount = _decimal(row.get("amount_hkd", ""))
        if legacy_amount is not None:
            row["amount_hkd"] = _format_decimal(legacy_amount)
            row["valuation_source"] = (
                VALUATION_SOURCE_FIXED_RATE
                if posted_currency and posted_currency != base_currency
                else VALUATION_SOURCE_STATEMENT
            )
            row["valuation_status"] = (
                VALUATION_STATUS_ESTIMATED
                if posted_currency and posted_currency != base_currency
                else VALUATION_STATUS_ACTUAL
            )
            _set_missing_rate_flag(row, False)
            return
        _set_missing(row)
        return
    if posted_currency == base_currency:
        row["amount_hkd"] = _format_decimal(amount)
        row["valuation_source"] = VALUATION_SOURCE_STATEMENT
        row["valuation_status"] = VALUATION_STATUS_ACTUAL
        _set_missing_rate_flag(row, False)
        return

    dated_rate = _dated_rate(config, posted_currency, transaction_date)
    if dated_rate is not None:
        row["amount_hkd"] = _format_decimal(amount * dated_rate)
        row["valuation_source"] = VALUATION_SOURCE_DATED_RATE
        row["valuation_status"] = VALUATION_STATUS_ESTIMATED
        _set_missing_rate_flag(row, False)
        return

    fixed_rate = _fixed_rate(config, posted_currency)
    if fixed_rate is not None:
        row["amount_hkd"] = _format_decimal(amount * fixed_rate)
        row["valuation_source"] = VALUATION_SOURCE_FIXED_RATE
        row["valuation_status"] = VALUATION_STATUS_ESTIMATED
        _set_missing_rate_flag(row, False)
        return

    _set_missing(row)


def value_transactions(
    rows: Iterable[dict[str, str]],
    config: Config,
    *,
    preserve_matched: bool = True,
) -> None:
    for row in rows:
        value_transaction(row, config, preserve_matched=preserve_matched)


def set_matched_exchange_valuation(
    foreign_row: dict[str, str],
    base_amount: Decimal,
) -> None:
    foreign_row["amount_hkd"] = _format_decimal(base_amount)
    foreign_row["valuation_source"] = VALUATION_SOURCE_MATCHED_EXCHANGE
    foreign_row["valuation_status"] = VALUATION_STATUS_ACTUAL
    _set_missing_rate_flag(foreign_row, False)


def valuation_summary(rows: Iterable[Mapping[str, str]]) -> ValuationSummary:
    sources = {source: 0 for source in sorted(ALLOWED_VALUATION_SOURCES)}
    statuses = {status: 0 for status in sorted(ALLOWED_VALUATION_STATUSES)}
    for row in rows:
        source = str(row.get("valuation_source", "")) or VALUATION_SOURCE_MISSING
        status = str(row.get("valuation_status", "")) or VALUATION_STATUS_MISSING
        if source in sources:
            sources[source] += 1
        if status in statuses:
            statuses[status] += 1
    return {
        "sources": sources,
        "statuses": statuses,
        "missing_count": statuses[VALUATION_STATUS_MISSING],
        "estimated_count": statuses[VALUATION_STATUS_ESTIMATED],
    }


def validate_dated_rates(config: Config) -> None:
    raw = config.get("dated_exchange_rates")
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ValueError("Config field dated_exchange_rates must be a JSON object")
    for currency, entries in raw.items():
        if not isinstance(currency, str) or not currency.strip():
            raise ValueError(
                "Config field dated_exchange_rates keys must be non-empty strings"
            )
        if not isinstance(entries, Mapping):
            raise ValueError(
                f"Config field dated_exchange_rates.{currency} must be a JSON object"
            )
        for raw_date, rate in entries.items():
            try:
                date.fromisoformat(str(raw_date))
            except ValueError as error:
                raise ValueError(
                    f"Config field dated_exchange_rates.{currency} has an invalid date"
                ) from error
            parsed = _decimal(str(rate))
            if parsed is None or parsed <= 0:
                raise ValueError(
                    f"Config field dated_exchange_rates.{currency}.{raw_date} "
                    "must be a positive number"
                )


def configured_exchange_rate(
    config: Config, currency: str, transaction_date: str
) -> Decimal | None:
    """Return the exact-date rate, then the fixed fallback, for a currency."""
    return _dated_rate(config, currency, transaction_date) or _fixed_rate(
        config, currency
    )


def _dated_rate(config: Config, currency: str, transaction_date: str) -> Decimal | None:
    raw = config.get("dated_exchange_rates", {})
    if not isinstance(raw, Mapping):
        return None
    entries = raw.get(currency)
    if not isinstance(entries, Mapping):
        return None
    return _positive_decimal(entries.get(transaction_date))


def _fixed_rate(config: Config, currency: str) -> Decimal | None:
    raw = config.get("exchange_rates", {})
    if not isinstance(raw, Mapping):
        return None
    return _positive_decimal(raw.get(currency))


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(str(value)) if value is not None else None
    return parsed if parsed is not None and parsed > 0 else None


def _decimal(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _set_missing(row: dict[str, str]) -> None:
    row["amount_hkd"] = ""
    row["valuation_source"] = VALUATION_SOURCE_MISSING
    row["valuation_status"] = VALUATION_STATUS_MISSING
    _set_missing_rate_flag(row, True)


def _set_missing_rate_flag(row: dict[str, str], active: bool) -> None:
    flags = [item for item in row.get("flags", "").split(";") if item]
    if active and "missing_exchange_rate" not in flags:
        flags.append("missing_exchange_rate")
    if not active:
        flags = [item for item in flags if item != "missing_exchange_rate"]
    row["flags"] = ";".join(flags)


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
