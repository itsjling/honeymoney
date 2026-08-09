"""Pure period-aware reads over fully derived workspace rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from honeymoney.periods import (
    PeriodSelection,
    ordered_view_rows,
    view_date_for_row,
    view_period_for_row,
)
from honeymoney.report import build_report_html
from honeymoney.valuation import VALUATION_STATUS_MISSING
from honeymoney.workspace_derivation import ViewReportInputs


@dataclass(frozen=True)
class WorkspaceQuery:
    """One selected read-only view of derived workspace transactions."""

    periods: tuple[str, ...]
    rows: tuple[dict[str, str], ...]
    pending_rows: tuple[dict[str, str], ...]
    missing_valuation_rows: tuple[dict[str, str], ...]
    report_label: str
    report_html: bytes

    @property
    def view_transaction_count(self) -> int:
        """Return the number of selected derived view transactions."""
        return len(self.rows)

    @property
    def pending_count(self) -> int:
        """Return the number of selected rows that need review."""
        return len(self.pending_rows)

    @property
    def missing_valuation_count(self) -> int:
        """Return the number of selected rows without a base-currency value."""
        return len(self.missing_valuation_rows)


def query_workspace_rows(
    rows: Sequence[Mapping[str, str]],
    selection: PeriodSelection,
    *,
    report_inputs: ViewReportInputs | None = None,
) -> WorkspaceQuery:
    """Return selected rows, query subsets, and report bytes without I/O.

    Selection uses the shared output-period placement rule: valid posting date,
    then valid transaction date, then the undated view.  Returned rows are
    copied and ordered with the published generated-view order.
    """
    available_periods = {view_period_for_row(row) for row in rows}
    periods = selection.selected_periods(available_periods)
    selected_periods = set(periods)
    selected_rows = tuple(
        ordered_view_rows(
            row for row in rows if _selected(row, selection, selected_periods)
        )
    )
    pending_rows = tuple(
        row for row in selected_rows if row.get("needs_review", "") == "true"
    )
    missing_valuation_rows = tuple(
        row for row in selected_rows if _has_missing_valuation(row)
    )
    report_label = _report_label(selection)
    return WorkspaceQuery(
        periods=periods,
        rows=selected_rows,
        pending_rows=pending_rows,
        missing_valuation_rows=missing_valuation_rows,
        report_label=report_label,
        report_html=build_report_html(
            list(selected_rows),
            report_label,
            source_occurrence_count=(
                report_inputs.source_occurrence_count
                if report_inputs is not None
                else None
            ),
            balance_reconciliation=(
                report_inputs.balance_reconciliation
                if report_inputs is not None
                else None
            ),
        ).encode("utf-8"),
    )


def _selected(
    row: Mapping[str, str],
    selection: PeriodSelection,
    selected_periods: set[str],
) -> bool:
    if view_period_for_row(row) not in selected_periods:
        return False
    if selection.kind != "range":
        return True
    view_date = view_date_for_row(row)
    if view_date is None or selection.start is None or selection.end is None:
        return False
    return selection.start <= view_date <= selection.end


def _has_missing_valuation(row: Mapping[str, str]) -> bool:
    """Match the valuation summary's missing-status fallback for derived rows."""
    status = row.get("valuation_status", "")
    if status:
        return status == VALUATION_STATUS_MISSING
    return _finite_decimal(row.get("amount_hkd", "")) is None


def _finite_decimal(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except InvalidOperation, ValueError:
        return None
    return parsed if parsed.is_finite() else None


def _report_label(selection: PeriodSelection) -> str:
    if selection.kind == "month":
        return selection.periods[0]
    if selection.kind == "range":
        if selection.start is None or selection.end is None:
            raise ValueError("period_range_invalid")
        return f"{selection.start.isoformat()} to {selection.end.isoformat()}"
    if selection.kind == "undated":
        return "undated"
    return "All periods"
