"""Shared selection and placement rules for output periods."""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal, Mapping

UNDATED_PERIOD = "undated"
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class PeriodSelectionError(ValueError):
    """A stable period-selector validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PeriodSelection:
    """One resolved output-period selection."""

    kind: Literal["month", "range", "undated", "all"]
    periods: tuple[str, ...]
    start: date | None = None
    end: date | None = None

    def selected_periods(
        self, available_periods: Iterable[str] = ()
    ) -> tuple[str, ...]:
        """Return the selected output-period names in stable order."""
        if self.kind == "all":
            return tuple(sorted(set(available_periods)))
        return self.periods


def resolve_period_selection(
    positional: str | None = None,
    *,
    month: str | None = None,
    start: str | None = None,
    end: str | None = None,
    undated: bool = False,
    all_periods: bool = False,
    today: date | None = None,
) -> PeriodSelection:
    """Resolve one period selector, defaulting to the current calendar month."""
    has_range = start is not None or end is not None
    selectors = sum(
        (
            positional is not None,
            month is not None,
            undated,
            all_periods,
            has_range,
        )
    )
    if selectors > 1:
        raise PeriodSelectionError("period_selector_conflict")
    if all_periods:
        return PeriodSelection("all", ())
    if undated:
        return PeriodSelection("undated", (UNDATED_PERIOD,))
    if has_range:
        if start is None or end is None:
            raise PeriodSelectionError("period_range_incomplete")
        first = _selector_date(start)
        last = _selector_date(end)
        if first > last:
            raise PeriodSelectionError("period_range_invalid")
        periods: list[str] = []
        year, number = first.year, first.month
        while (year, number) <= (last.year, last.month):
            periods.append(f"{year:04d}-{number:02d}")
            year, number = (year + 1, 1) if number == 12 else (year, number + 1)
        return PeriodSelection("range", tuple(periods), first, last)
    current = today or date.today()
    selected = positional if positional is not None else month
    if selected is not None:
        numeric = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", selected.strip())
        if numeric is not None:
            return _month_selection(int(numeric.group(1)), int(numeric.group(2)))
        names = {
            name.casefold(): number
            for number, name in enumerate(calendar.month_name)
            if name
        }
        names.update(
            {
                name.casefold(): number
                for number, name in enumerate(calendar.month_abbr)
                if name
            }
        )
        selected_month = names.get(selected.strip().casefold())
        if selected_month is not None:
            return _month_selection(current.year, selected_month)
        raise PeriodSelectionError("period_month_invalid")
    return _month_selection(current.year, current.month)


def view_period_for_row(row: Mapping[str, str]) -> str:
    """Return the calendar-month or undated output period for one view row."""
    view_date = view_date_for_row(row)
    if view_date is None:
        return UNDATED_PERIOD
    return f"{view_date.year:04d}-{view_date.month:02d}"


def view_date_for_row(row: Mapping[str, str]) -> date | None:
    """Return the valid date that chooses a row's output period."""
    for field in ("posting_date", "transaction_date"):
        parsed = _parse_iso_date(row.get(field, ""))
        if parsed is not None:
            return parsed
    return None


def transaction_date_for_row(row: Mapping[str, str]) -> date | None:
    """Return a row's valid transaction date for deterministic ordering."""
    return _parse_iso_date(row.get("transaction_date", ""))


def ordered_view_rows(
    rows: Iterable[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Return copies in the fixed public output order."""
    return sorted((dict(row) for row in rows), key=view_row_sort_key)


def view_row_sort_key(
    row: Mapping[str, str],
) -> tuple[
    date,
    date,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    str,
]:
    """Return the published deterministic sort key for a view row."""
    return (
        view_date_for_row(row) or date.min,
        transaction_date_for_row(row) or date.min,
        row.get("account_id", ""),
        row.get("merchant", ""),
        row.get("original_description", ""),
        row.get("original_amount", ""),
        row.get("original_currency", ""),
        row.get("posted_amount", ""),
        row.get("posted_currency", ""),
        row.get("amount_hkd", ""),
        row.get("canonical_group_id", ""),
        _canonical_slot(row.get("canonical_slot", "")),
        row.get("transaction_id", ""),
    )


def _parse_iso_date(value: str) -> date | None:
    if _ISO_DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _selector_date(value: str) -> date:
    if _ISO_DATE.fullmatch(value) is None:
        raise PeriodSelectionError("period_date_invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise PeriodSelectionError("period_date_invalid") from error


def _canonical_slot(value: str) -> int:
    try:
        slot = int(value)
    except ValueError:
        return 0
    return slot if slot > 0 else 0


def _month_selection(year: int, month: int) -> PeriodSelection:
    try:
        first = date(year, month, 1)
    except ValueError as error:
        raise PeriodSelectionError("period_month_invalid") from error
    last = date(year, month, calendar.monthrange(year, month)[1])
    return PeriodSelection("month", (f"{year:04d}-{month:02d}",), first, last)
