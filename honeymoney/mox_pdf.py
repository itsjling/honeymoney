"""Mox statement layout parsing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping, TypeAlias

PdfWord: TypeAlias = Mapping[str, object]
PdfLine: TypeAlias = list[PdfWord]
PdfPageLines: TypeAlias = list[list[PdfLine]]
MoxSourceRow: TypeAlias = tuple[dict[str, str], int, int]

_DATE_TEXT = r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}"
_LABELED_PERIOD = re.compile(
    rf"^\s*Statement\s+period\s*:\s*(?P<start>{_DATE_TEXT})\s*[-–]\s*"
    rf"(?P<end>{_DATE_TEXT})\b",
    flags=re.IGNORECASE,
)
_UNLABELED_PERIOD = re.compile(
    rf"^\s*(?P<start>{_DATE_TEXT})\s+\S+\s+(?P<end>{_DATE_TEXT})\s*$",
    flags=re.IGNORECASE,
)
_BANK_ACCOUNT_HEADING = re.compile(
    r"^(?P<currency>HKD|USD)\s+Mox\s+Account\b",
    flags=re.IGNORECASE,
)
_ANY_BANK_ACCOUNT_HEADING = re.compile(
    r"^[A-Z]{3}\s+Mox\s+Account\b",
    flags=re.IGNORECASE,
)
_TIME_DEPOSIT_HEADING = re.compile(
    r"^Time\s+Deposit\b.*?\-\s*(?P<reference>\d{6,})\s*$",
    flags=re.IGNORECASE,
)
_PRINCIPAL_CURRENCY = re.compile(
    r"\bPrincipal\s+Amount\b.*?\b(?P<currency>[A-Z]{3})\b",
    flags=re.IGNORECASE,
)
_TABLE_CURRENCY = re.compile(
    r"\b(?:Corresponding\s+)?amount\s*\((?P<currency>[A-Z]{3})\)",
    flags=re.IGNORECASE,
)
_TRANSACTION_ROW = re.compile(
    r"^(?P<transaction_date>\d{1,2}\s+[A-Za-z]{3})\s+"
    r"(?P<posting_date>\d{1,2}\s+[A-Za-z]{3})\s+"
    r"(?P<description>.+?)\s+"
    r"(?:(?P<original_amount>[+\-]?\d[\d,]*\.\d{2})\s+"
    r"(?P<original_currency>[A-Z]{3})\s+)?"
    r"(?P<amount>[+\-]?\d[\d,]*\.\d{2})$",
)
_TRANSACTION_START = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{1,2}\s+[A-Za-z]{3}\s+")
_SUMMARY_AMOUNT = re.compile(
    r"^\s*(?P<balance>[+\-]?\d[\d,]*\.\d{2})\s+HKD\b",
    flags=re.IGNORECASE,
)
_STATEMENT_BALANCE_LABEL = re.compile(
    r"^\s*Statement\s+Balance(?:\s+[^\d].*)?$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class MoxStatementPeriod:
    start: date
    end: date


@dataclass(frozen=True)
class _MoxAccountSection:
    account_id: str
    account: str
    statement_section: str
    currency: str
    time_deposit: bool = False


def mox_statement_period(page_lines: PdfPageLines) -> MoxStatementPeriod:
    labeled = _period_matches(page_lines, _LABELED_PERIOD)
    candidates = labeled or _period_matches(page_lines[:1], _UNLABELED_PERIOD)
    if len(candidates) != 1:
        raise ValueError("Could not determine one Mox statement period")
    start, end = next(iter(candidates))
    if end < start or end - start > timedelta(days=62):
        raise ValueError("Mox statement period is invalid")
    return MoxStatementPeriod(start, end)


def resolve_mox_date(value: str, period: MoxStatementPeriod) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        pass
    else:
        if not period.start <= parsed <= period.end:
            raise ValueError("Mox posting date is outside its statement period")
        return parsed.isoformat()
    candidates = set()
    for year in range(period.start.year - 1, period.end.year + 2):
        try:
            candidate = datetime.strptime(f"{value} {year}", "%d %b %Y").date()
        except ValueError:
            continue
        if period.start <= candidate <= period.end:
            candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError("Could not resolve a Mox transaction date from its period")
    return next(iter(candidates)).isoformat()


def resolve_mox_source_dates(
    source_row: dict[str, str], period: MoxStatementPeriod
) -> dict[str, str]:
    resolved = dict(source_row)
    posting_value = resolved.get("posting_date", "")
    if not posting_value:
        raise ValueError("Mox transaction is missing its posting date")
    posting_date = date.fromisoformat(resolve_mox_date(posting_value, period))
    resolved["posting_date"] = posting_date.isoformat()
    transaction_value = resolved.get("transaction_date", "")
    if transaction_value:
        resolved["transaction_date"] = _resolve_transaction_date(
            transaction_value, posting_date
        ).isoformat()
    return resolved


def mox_time_deposit_account_id(reference: str) -> str:
    digest = hashlib.sha256(
        f"honeymoney:mox-time-deposit:v1:{reference}".encode()
    ).hexdigest()
    return f"mox_time_deposit_{digest}"


def mox_bank_source_rows(
    page_lines: PdfPageLines, period: MoxStatementPeriod
) -> list[MoxSourceRow]:
    rows: list[MoxSourceRow] = []
    row_groups: dict[str, list[dict[str, str]]] = {}
    balances: dict[str, dict[str, str]] = {}
    section: _MoxAccountSection | None = None
    pending_row = ""
    in_activity = False

    for page_number, lines in enumerate(page_lines, start=1):
        for line_number, line in enumerate(lines, start=1):
            text = _line_text(line)
            account_match = _BANK_ACCOUNT_HEADING.match(text)
            if account_match is not None:
                _reject_pending_row(pending_row)
                section = _bank_account_section(account_match.group("currency"))
                pending_row = ""
                in_activity = False
                continue
            if _ANY_BANK_ACCOUNT_HEADING.match(text) is not None:
                raise ValueError("Unsupported Mox bank account section")
            if text.casefold().startswith("time deposit"):
                _reject_pending_row(pending_row)
                deposit_match = _TIME_DEPOSIT_HEADING.match(text)
                if deposit_match is None:
                    raise ValueError("Unsupported Mox Time Deposit section")
                section = _time_deposit_section(deposit_match.group("reference"))
                pending_row = ""
                in_activity = False
                continue
            if section is None:
                continue
            if section.time_deposit:
                currency_match = _PRINCIPAL_CURRENCY.search(text)
                if currency_match is not None:
                    section = _MoxAccountSection(
                        section.account_id,
                        section.account,
                        section.statement_section,
                        currency_match.group("currency").upper(),
                        True,
                    )
                    continue

            header_text = _undouble_header_text(text)
            folded_text = header_text.casefold()
            transaction_line = _TRANSACTION_START.match(text) is not None
            if not transaction_line and (
                "settlement" in folded_text
                and ("transaction" in folded_text or "activity" in folded_text)
            ):
                _reject_pending_row(pending_row)
                header_currency = _TABLE_CURRENCY.search(header_text)
                if header_currency is not None:
                    section = _section_with_currency(
                        section, header_currency.group("currency")
                    )
                in_activity = True
                pending_row = ""
                continue
            if not in_activity:
                first_row = _TRANSACTION_ROW.match(text)
                if first_row is None or not first_row.group(
                    "description"
                ).casefold().startswith("opening balance"):
                    continue
                in_activity = True
            header_currency = (
                None if transaction_line else _TABLE_CURRENCY.search(header_text)
            )
            if header_currency is not None:
                section = _section_with_currency(
                    section, header_currency.group("currency")
                )
                continue

            if _TRANSACTION_START.match(text):
                _reject_pending_row(pending_row)
                pending_row = text
            elif pending_row:
                pending_row = f"{pending_row} {text}"
            else:
                continue
            match = _TRANSACTION_ROW.match(pending_row)
            if match is None:
                continue
            pending_row = ""
            if not section.currency:
                raise ValueError("Mox Time Deposit currency is unavailable")
            fields = match.groupdict()
            description = fields["description"].strip()
            folded_description = " ".join(description.casefold().split())
            if folded_description.startswith(("opening balance", "closing balance")):
                kind = (
                    "opening"
                    if folded_description.startswith("opening balance")
                    else "closing"
                )
                endpoints = balances.setdefault(section.account_id, {})
                existing = endpoints.get(kind)
                if existing is not None and existing != fields["amount"]:
                    raise ValueError("Mox statement balances conflict")
                endpoints[kind] = fields["amount"]
                if kind == "closing":
                    in_activity = False
                continue
            if Decimal(fields["amount"].replace(",", "")) == 0:
                continue

            source_row = resolve_mox_source_dates(
                {
                    "transaction_date": fields["transaction_date"],
                    "posting_date": fields["posting_date"],
                    "description": description,
                    "original_amount": fields["original_amount"] or fields["amount"],
                    "original_currency": fields["original_currency"]
                    or section.currency,
                    "amount": fields["amount"],
                    "account_id": section.account_id,
                    "account": section.account,
                    "statement_section": section.statement_section,
                    "currency": section.currency,
                    "statement_opening_balance": "",
                    "statement_closing_balance": "",
                },
                period,
            )
            rows.append((source_row, page_number, line_number))
            row_groups.setdefault(section.account_id, []).append(source_row)

    _reject_pending_row(pending_row)

    for account_id, account_rows in row_groups.items():
        endpoints = balances.get(account_id, {})
        if "opening" in endpoints:
            account_rows[0]["statement_opening_balance"] = endpoints["opening"]
        if "closing" in endpoints:
            account_rows[-1]["statement_closing_balance"] = endpoints["closing"]
    return rows


def mox_credit_closing_balance(page_lines: PdfPageLines) -> str:
    candidates: set[str] = set()
    for lines in page_lines:
        for index, line in enumerate(lines):
            text = _line_text(line)
            same_line = re.search(
                r"^\s*(?P<balance>[+\-]?\d[\d,]*\.\d{2})\s+HKD\s+"
                r"Statement\s+Balance(?:\s+[^\d].*)?$",
                text,
                flags=re.IGNORECASE,
            )
            if same_line is not None:
                candidates.add(same_line.group("balance"))
                continue
            if _STATEMENT_BALANCE_LABEL.match(text) is None or index == 0:
                continue
            prior = _SUMMARY_AMOUNT.match(_line_text(lines[index - 1]))
            if prior is not None:
                candidates.add(prior.group("balance"))
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _period_matches(
    page_lines: PdfPageLines, pattern: re.Pattern[str]
) -> set[tuple[date, date]]:
    candidates: set[tuple[date, date]] = set()
    for lines in page_lines:
        for line in lines:
            match = pattern.search(_line_text(line))
            if match is None:
                continue
            try:
                start = datetime.strptime(match.group("start"), "%d %b %Y").date()
                end = datetime.strptime(match.group("end"), "%d %b %Y").date()
            except ValueError:
                continue
            candidates.add((start, end))
    return candidates


def _line_text(line: PdfLine) -> str:
    return " ".join(str(word.get("text", "")) for word in line).strip()


def _undouble_header_text(text: str) -> str:
    tokens = text.split()
    collapsed: list[str] = []
    changed = False
    for token in tokens:
        if len(token) % 2 == 0 and all(
            token[index] == token[index + 1] for index in range(0, len(token), 2)
        ):
            collapsed.append(token[::2])
            changed = True
        else:
            collapsed.append(token)
    return " ".join(collapsed) if changed else text


def _resolve_transaction_date(value: str, posting_date: date) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        candidates = set()
        for year in range(posting_date.year - 1, posting_date.year + 2):
            try:
                candidate = datetime.strptime(f"{value} {year}", "%d %b %Y").date()
            except ValueError:
                continue
            if posting_date - timedelta(days=14) <= candidate <= posting_date:
                candidates.add(candidate)
        if len(candidates) != 1:
            raise ValueError(
                "Could not resolve a Mox transaction date from its posting date"
            )
        return next(iter(candidates))
    if not posting_date - timedelta(days=14) <= parsed <= posting_date:
        raise ValueError("Mox transaction date is outside the supported posting lag")
    return parsed


def _reject_pending_row(pending_row: str) -> None:
    if pending_row:
        raise ValueError("Mox transaction row is incomplete")


def _bank_account_section(currency: str) -> _MoxAccountSection:
    normalized = currency.upper()
    if normalized == "HKD":
        return _MoxAccountSection("mox_bank_main", "Mox Main", "HKD Mox Account", "HKD")
    return _MoxAccountSection(
        "mox_bank_usd", "Mox USD Account", "USD Mox Account", "USD"
    )


def _time_deposit_section(reference: str) -> _MoxAccountSection:
    account_id = mox_time_deposit_account_id(reference)
    return _MoxAccountSection(
        account_id,
        "Mox Time Deposit",
        f"Time Deposit {account_id.removeprefix('mox_time_deposit_')}",
        "",
        True,
    )


def _section_with_currency(
    section: _MoxAccountSection, currency: str
) -> _MoxAccountSection:
    normalized = currency.upper()
    if section.currency and section.currency != normalized:
        raise ValueError("Mox account section currencies conflict")
    return _MoxAccountSection(
        section.account_id,
        section.account,
        section.statement_section,
        normalized,
        section.time_deposit,
    )
