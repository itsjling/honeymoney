"""Read the supported Hang Seng ATM savings and enJoy card word layouts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Word:
    text: str
    x: float


Page = list[list[Word]]
SourceRow = tuple[dict[str, str], int, int]
_BankBalance = tuple[str, str, int, str]
_AMOUNT = re.compile(r"\$?([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})\s*(CR|DR|-)?", re.I)
_BANK_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})")
_BANK_HEADER = "Date Transaction Details Deposit Withdrawal Balance in HKD"
_BANK_SUMMARY = "Transaction Summary"
_CARD_HEADER = "TRANS DATE POST DATE NEW ACTIVITY AMOUNT"
_CARD_DATE = re.compile(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}")
_CARD_END = re.compile(
    r"\*+\s+(?:FINANCE CHARGE RATES|SUMMARY OF ACTIVITY)\s+\*+", re.I
)


def _cell(line: list[Word], left: float, right: float) -> str:
    return " ".join(word.text for word in line if left <= word.x < right).strip()


def _money(value: str, *, expense: bool = False, liability: bool = False) -> str:
    match = _AMOUNT.fullmatch(value)
    if match is None or (expense and match[1].startswith("-")):
        raise ValueError("Invalid Hang Seng amount")
    amount = Decimal(match[1].replace(",", ""))
    suffix = (match[2] or "").upper()
    if suffix == "-":
        amount = abs(amount) if expense else -abs(amount)
    elif suffix:
        amount = abs(amount) * (1 if suffix == "CR" else -1)
        if liability:
            amount = -amount
    elif expense:
        amount = -abs(amount)
    return format(amount, ".2f")


def _unique(values: list[str], label: str) -> str:
    if len(set(values)) != 1:
        raise ValueError(f"Missing or conflicting Hang Seng {label}")
    return values[0]


def _bank_statement_date(pages: list[Page]) -> date:
    values = []
    for page in pages:
        for line in page:
            match = re.fullmatch(
                r"Date\s*:\s*(\d{1,2} [A-Za-z]+ \d{4})", _cell(line, 400, 540), re.I
            )
            if match:
                try:
                    parsed = datetime.strptime(match[1], "%d %B %Y").date()
                except ValueError:
                    raise ValueError("Invalid Hang Seng statement date") from None
                values.append(parsed.isoformat())
    return date.fromisoformat(_unique(values, "statement date"))


def _bank_date(value: str, closing: date) -> str:
    match = _BANK_DATE.fullmatch(value)
    if match is None:
        raise ValueError("Invalid Hang Seng transaction date")
    try:
        month = datetime.strptime(match[2], "%b").month
        year = closing.year - (month > closing.month)
        parsed = date(year, month, int(match[1]))
    except ValueError:
        raise ValueError("Invalid Hang Seng transaction date") from None
    if parsed > closing:
        raise ValueError("Hang Seng transaction date exceeds statement date")
    return parsed.isoformat()


def _balances(rows: list[SourceRow], openings: list[str], closings: list[str]) -> None:
    opening = _unique(openings, "opening balance")
    closing = _unique(closings, "closing balance")
    for row, _, _ in rows:
        row["Opening"] = opening
        row["Closing"] = closing


def _bank_balances(
    rows: list[SourceRow], balances: list[_BankBalance], closing_date: date
) -> None:
    if not balances or balances[0][0] != "opening":
        raise ValueError("Missing or conflicting Hang Seng opening balance")
    if balances[-1][0] != "closing":
        raise ValueError("Missing or conflicting Hang Seng closing balance")
    for previous, current in zip(balances, balances[1:], strict=False):
        if previous[0] == current[0]:
            raise ValueError("Missing or conflicting Hang Seng bank balance sequence")
        if current[0] == "opening" and (
            current[1] != previous[1]
            or current[2] != previous[2] + 1
            or current[3] != previous[3]
        ):
            raise ValueError("Missing or conflicting Hang Seng balance carry")
    if balances[-1][3] != closing_date.isoformat():
        raise ValueError("Hang Seng closing balance date does not match statement date")
    for row, _, _ in rows:
        row["Opening"] = balances[0][1]
        row["Closing"] = balances[-1][1]


def _has_header(page: Page, header: str) -> bool:
    return any(header in _cell(line, 0, 1000) for line in page)


def _bank_line_fields(line: list[Word]) -> tuple[str, str, tuple[str, str, str]]:
    return (
        _cell(line, 70, 106),
        _cell(line, 106, 300),
        (
            _cell(line, 300, 385),
            _cell(line, 385, 475),
            _cell(line, 475, 540),
        ),
    )


def _bank_balance_description(description: str) -> bool:
    return description.replace(" ", "").casefold() in {
        "b/fbalance",
        "c/fbalance",
    }


def _bank_data_without_header(line: list[Word]) -> bool:
    raw_date, description, amounts = _bank_line_fields(line)
    return (
        _bank_balance_description(description)
        or bool(_BANK_DATE.fullmatch(raw_date) and (description or any(amounts)))
        or any(_AMOUNT.fullmatch(amount) for amount in amounts)
    )


def _bank_data_after_summary(line: list[Word]) -> bool:
    text = _cell(line, 0, 1000)
    raw_date, description, amounts = _bank_line_fields(line)
    return (
        _BANK_HEADER in text
        or _bank_balance_description(description)
        or bool(_BANK_DATE.fullmatch(raw_date) and (description or any(amounts)))
    )


def bank_rows(pages: list[Page]) -> list[SourceRow]:
    closing_date = _bank_statement_date(pages)
    rows: list[SourceRow] = []
    balances: list[_BankBalance] = []
    current_date = ""
    pending: SourceRow | None = None
    continuation = False
    summary_seen = False
    for page_number, page in enumerate(pages, 1):
        has_header = _has_header(page, _BANK_HEADER)
        if continuation and not has_header:
            raise ValueError("Hang Seng bank continuation page has no table header")
        in_table = False
        requires_opening = False
        for line_number, line in enumerate(page, 1):
            text = _cell(line, 0, 1000)
            if summary_seen:
                if _bank_data_after_summary(line):
                    raise ValueError("Hang Seng bank data after transaction summary")
                continue
            if text == _BANK_SUMMARY and not in_table:
                if not balances or balances[-1][0] != "closing":
                    raise ValueError("Hang Seng bank table has no closing balance")
                summary_seen = True
                continue
            if _BANK_HEADER in text:
                in_table = True
                requires_opening = not continuation or bool(
                    balances and balances[-1][0] == "closing"
                )
                continue
            if not in_table:
                if (
                    balances
                    and balances[-1][0] == "closing"
                    and _bank_data_without_header(line)
                ):
                    raise ValueError(
                        "Hang Seng bank data after closing balance has no header"
                    )
                continue
            raw_date, description, amounts = _bank_line_fields(line)
            deposit, withdrawal, balance = amounts
            if _bank_balance_description(description):
                if pending is not None:
                    rows.append(pending)
                    pending = None
                kind = (
                    "opening"
                    if description.replace(" ", "").casefold() == "b/fbalance"
                    else "closing"
                )
                if kind == "closing" and requires_opening:
                    raise ValueError("Missing or conflicting Hang Seng opening balance")
                try:
                    balance_date = _bank_date(raw_date, closing_date)
                except ValueError:
                    raise ValueError("Invalid Hang Seng balance date") from None
                balances.append((kind, _money(balance), page_number, balance_date))
                requires_opening = False
                if kind == "closing":
                    in_table = False
                continue
            if text in {_BANK_SUMMARY, "Important Notes"} and pending is None:
                raise ValueError("Hang Seng bank table has no closing balance")
            if raw_date:
                current_date = _bank_date(raw_date, closing_date)
            if deposit or withdrawal:
                if requires_opening:
                    raise ValueError("Missing or conflicting Hang Seng opening balance")
                if (
                    bool(deposit) == bool(withdrawal)
                    or not current_date
                    or not description
                ):
                    raise ValueError("Incomplete Hang Seng bank transaction")
                if pending is not None:
                    rows.append(pending)
                amount = (
                    _money(deposit) if deposit else _money(withdrawal, expense=True)
                )
                pending = (
                    {
                        "Date": current_date,
                        "Description": description,
                        "Amount": amount,
                    },
                    page_number,
                    line_number,
                )
            elif description:
                if pending is None or raw_date:
                    raise ValueError("Hang Seng bank description has no amount")
                pending[0]["Description"] += " " + description
            elif raw_date or balance:
                raise ValueError("Incomplete Hang Seng bank transaction")
        continuation = in_table
    if continuation:
        raise ValueError("Hang Seng bank table has no closing balance")
    if pending is not None:
        rows.append(pending)
    _bank_balances(rows, balances, closing_date)
    if not summary_seen:
        raise ValueError("Hang Seng bank statement has no transaction summary")
    return rows


def card_rows(pages: list[Page]) -> list[SourceRow]:
    rows: list[SourceRow] = []
    openings: list[str] = []
    closings: list[str] = []
    pending: SourceRow | None = None
    closing_pending = False
    continuation = False
    end_seen = False
    for page_number, page in enumerate(pages, 1):
        if continuation and not _has_header(page, _CARD_HEADER):
            raise ValueError("Hang Seng card continuation page has no table header")
        in_table = False
        for line_number, line in enumerate(page, 1):
            text = _cell(line, 0, 1000)
            if end_seen:
                transaction_date = _cell(line, 20, 85)
                posting_date = _cell(line, 85, 151)
                description = _cell(line, 151, 520)
                amount = _cell(line, 520, 600)
                if (
                    _CARD_HEADER in text
                    or _CARD_DATE.fullmatch(transaction_date) is not None
                    or _CARD_DATE.fullmatch(posting_date) is not None
                    or (description == "OPENING BALANCE" and bool(amount))
                ):
                    raise ValueError("Hang Seng card data after end marker")
                continue
            if closing_pending:
                closings.append(_money(_cell(line, 379, 483), liability=True))
                closing_pending = False
            if _cell(line, 379, 483) == "NEW BALANCE":
                closing_pending = True
            if _CARD_HEADER in text:
                in_table = True
                continue
            if not in_table:
                continue
            if _CARD_END.fullmatch(text):
                if pending is not None:
                    rows.append(pending)
                    pending = None
                in_table = False
                end_seen = True
                continue
            transaction_date = _cell(line, 20, 85)
            posting_date = _cell(line, 85, 151)
            description = _cell(line, 151, 520)
            amount = _cell(line, 520, 600)
            if description == "OPENING BALANCE":
                openings.append(_money(amount, liability=True))
                continue
            if transaction_date or posting_date or amount:
                if (
                    not transaction_date
                    or not posting_date
                    or not description
                    or not amount
                ):
                    raise ValueError("Incomplete Hang Seng card transaction")
                try:
                    transaction = datetime.strptime(transaction_date, "%d %b %Y").date()
                    posting = datetime.strptime(posting_date, "%d %b %Y").date()
                except ValueError:
                    raise ValueError("Invalid Hang Seng card date") from None
                if posting < transaction:
                    raise ValueError("Hang Seng posting date precedes transaction date")
                if pending is not None:
                    rows.append(pending)
                pending = (
                    {
                        "Date": transaction.isoformat(),
                        "Posting": posting.isoformat(),
                        "Description": description,
                        "Amount": _money(amount, expense=True),
                    },
                    page_number,
                    line_number,
                )
            elif description:
                if pending is None:
                    raise ValueError("Hang Seng card description has no transaction")
                pending[0]["Description"] += " " + description
        continuation = in_table
    if continuation:
        raise ValueError("Hang Seng card table has no end marker")
    if pending is not None:
        rows.append(pending)
    _balances(rows, openings, closings)
    return rows
