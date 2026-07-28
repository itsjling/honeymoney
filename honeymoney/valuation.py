"""Base-currency valuation with explicit source and completeness state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from honeymoney.contracts import Config
from honeymoney.rates import RateObservation, resolve_cached_rate

VALUATION_SOURCE_STATEMENT = "statement_posted"
VALUATION_SOURCE_MATCHED_EXCHANGE = "matched_exchange_leg"
VALUATION_SOURCE_DATED_RATE = "configured_dated_rate"
VALUATION_SOURCE_HKMA_RATE = "hkma_daily_reference_rate"
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
        VALUATION_SOURCE_HKMA_RATE,
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
    cash_flow_blocking_missing_count: int
    excluded_flow_missing_count: int
    unresolved_flow_missing_count: int
    zero_amount_missing_count: int
    other_flow_missing_count: int
    cash_flow_complete: bool
    cash_flow: CashFlowValuationSummary


class CashFlowTotals(TypedDict):
    income: str
    spending: str
    refunds: str
    net_cash_flow: str


class CashFlowValuationSummary(TypedDict):
    currency: str
    actual: CashFlowTotals
    estimated: CashFlowTotals
    combined_estimate: CashFlowTotals


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
        _clear_rate_metadata(row)
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
            _clear_rate_metadata(row)
            _set_missing_rate_flag(row, False)
            return
        _set_missing(row)
        return
    if posted_currency == base_currency:
        row["amount_hkd"] = _format_decimal(amount)
        row["valuation_source"] = VALUATION_SOURCE_STATEMENT
        row["valuation_status"] = VALUATION_STATUS_ACTUAL
        _clear_rate_metadata(row)
        _set_missing_rate_flag(row, False)
        return

    dated_rate = _dated_rate(config, posted_currency, transaction_date)
    if dated_rate is not None:
        row["amount_hkd"] = _format_decimal(amount * dated_rate)
        row["valuation_source"] = VALUATION_SOURCE_DATED_RATE
        row["valuation_status"] = VALUATION_STATUS_ESTIMATED
        row["valuation_rate_date"] = transaction_date
        row["valuation_provider"] = "Configured exact-date rate"
        _set_missing_rate_flag(row, False)
        return

    cached_rate = _cached_rate(config, posted_currency, transaction_date)
    if cached_rate is not None:
        row["amount_hkd"] = _format_decimal(
            amount * Decimal(str(cached_rate["raw_rate"]))
        )
        row["valuation_source"] = VALUATION_SOURCE_HKMA_RATE
        row["valuation_status"] = VALUATION_STATUS_ESTIMATED
        row["valuation_rate_date"] = str(cached_rate["observed_rate_date"])
        row["valuation_provider"] = str(cached_rate["provider"])
        _set_missing_rate_flag(row, False)
        return

    fixed_rate = _fixed_rate(config, posted_currency)
    if fixed_rate is not None:
        row["amount_hkd"] = _format_decimal(amount * fixed_rate)
        row["valuation_source"] = VALUATION_SOURCE_FIXED_RATE
        row["valuation_status"] = VALUATION_STATUS_ESTIMATED
        row["valuation_rate_date"] = ""
        row["valuation_provider"] = "Configured fixed rate"
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
    _clear_rate_metadata(foreign_row)
    _set_missing_rate_flag(foreign_row, False)


def valuation_summary(rows: Iterable[Mapping[str, str]]) -> ValuationSummary:
    sources = {source: 0 for source in sorted(ALLOWED_VALUATION_SOURCES)}
    statuses = {status: 0 for status in sorted(ALLOWED_VALUATION_STATUSES)}
    totals = {
        VALUATION_STATUS_ACTUAL: _empty_cash_flow_totals(),
        VALUATION_STATUS_ESTIMATED: _empty_cash_flow_totals(),
    }
    cash_flow_blocking_missing_count = 0
    excluded_flow_missing_count = 0
    unresolved_flow_missing_count = 0
    zero_amount_missing_count = 0
    other_flow_missing_count = 0
    for row in rows:
        source = str(row.get("valuation_source", "")) or VALUATION_SOURCE_MISSING
        amount = _decimal(str(row.get("amount_hkd", "")))
        status = str(row.get("valuation_status", ""))
        if not status:
            status = (
                VALUATION_STATUS_MISSING
                if amount is None
                else (
                    VALUATION_STATUS_ESTIMATED
                    if source
                    in {
                        VALUATION_SOURCE_DATED_RATE,
                        VALUATION_SOURCE_HKMA_RATE,
                        VALUATION_SOURCE_FIXED_RATE,
                    }
                    else VALUATION_STATUS_ACTUAL
                )
            )
        if source in sources:
            sources[source] += 1
        if status in statuses:
            statuses[status] += 1
        flow_type = str(row.get("flow_type", "")) or "unresolved"
        if status in totals and amount is not None:
            _add_cash_flow_amount(totals[status], flow_type, amount)
        if status != VALUATION_STATUS_MISSING:
            continue
        if flow_type in {
            "internal_transfer",
            "credit_card_payment",
            "investment_transfer",
        }:
            excluded_flow_missing_count += 1
        elif flow_type == "unresolved":
            unresolved_flow_missing_count += 1
        elif flow_type in {"income", "expense", "refund"}:
            posted_amount = _decimal(str(row.get("posted_amount", "")))
            if posted_amount == 0:
                zero_amount_missing_count += 1
            else:
                cash_flow_blocking_missing_count += 1
        else:
            other_flow_missing_count += 1
    actual = _serialized_cash_flow_totals(totals[VALUATION_STATUS_ACTUAL])
    estimated = _serialized_cash_flow_totals(totals[VALUATION_STATUS_ESTIMATED])
    combined = _serialized_cash_flow_totals(
        {
            field: (
                totals[VALUATION_STATUS_ACTUAL][field]
                + totals[VALUATION_STATUS_ESTIMATED][field]
            )
            for field in ("income", "spending", "refunds", "net_cash_flow")
        }
    )
    return {
        "sources": sources,
        "statuses": statuses,
        "missing_count": statuses[VALUATION_STATUS_MISSING],
        "estimated_count": statuses[VALUATION_STATUS_ESTIMATED],
        "cash_flow_blocking_missing_count": cash_flow_blocking_missing_count,
        "excluded_flow_missing_count": excluded_flow_missing_count,
        "unresolved_flow_missing_count": unresolved_flow_missing_count,
        "zero_amount_missing_count": zero_amount_missing_count,
        "other_flow_missing_count": other_flow_missing_count,
        "cash_flow_complete": cash_flow_blocking_missing_count == 0,
        "cash_flow": {
            "currency": "HKD",
            "actual": actual,
            "estimated": estimated,
            "combined_estimate": combined,
        },
    }


def _empty_cash_flow_totals() -> dict[str, Decimal]:
    return {
        "income": Decimal("0"),
        "spending": Decimal("0"),
        "refunds": Decimal("0"),
        "net_cash_flow": Decimal("0"),
    }


def _add_cash_flow_amount(
    totals: dict[str, Decimal],
    flow_type: str,
    amount: Decimal,
) -> None:
    if flow_type == "income":
        totals["income"] += amount
    elif flow_type == "expense":
        totals["spending"] += amount
    elif flow_type == "refund":
        totals["refunds"] += amount
    else:
        return
    totals["net_cash_flow"] += amount


def _serialized_cash_flow_totals(
    totals: Mapping[str, Decimal],
) -> CashFlowTotals:
    return {
        "income": _format_decimal(totals["income"]),
        "spending": _format_decimal(totals["spending"]),
        "refunds": _format_decimal(totals["refunds"]),
        "net_cash_flow": _format_decimal(totals["net_cash_flow"]),
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
    """Return the first configured or cached rate in valuation order."""
    dated_rate = _dated_rate(config, currency, transaction_date)
    if dated_rate is not None:
        return dated_rate
    cached_rate = _cached_rate(config, currency, transaction_date)
    if cached_rate is not None:
        return Decimal(str(cached_rate["raw_rate"]))
    return _fixed_rate(config, currency)


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


def _cached_rate(
    config: Config, currency: str, transaction_date: str
) -> RateObservation | None:
    raw = config.get("_rate_cache")
    if not isinstance(raw, Mapping):
        return None
    return resolve_cached_rate(raw, currency, transaction_date)


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
    _clear_rate_metadata(row)
    _set_missing_rate_flag(row, True)


def _clear_rate_metadata(row: dict[str, str]) -> None:
    row["valuation_rate_date"] = ""
    row["valuation_provider"] = ""


def _set_missing_rate_flag(row: dict[str, str], active: bool) -> None:
    flags = [item for item in row.get("flags", "").split(";") if item]
    if active and "missing_exchange_rate" not in flags:
        flags.append("missing_exchange_rate")
    if not active:
        flags = [item for item in flags if item != "missing_exchange_rate"]
    row["flags"] = ";".join(flags)


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
