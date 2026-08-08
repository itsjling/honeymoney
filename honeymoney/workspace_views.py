"""Pure planning and byte serialization for generated workspace views."""

from __future__ import annotations

import hashlib
import hmac
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from honeymoney.corrections import to_review_row
from honeymoney.csv_artifacts import csv_document
from honeymoney.periods import (
    UNDATED_PERIOD,
    PeriodSelection,
    ordered_view_rows,
    view_period_for_row,
)
from honeymoney.report import build_report_html
from honeymoney.schema import CATEGORIZED_COLUMNS, REVIEW_NEEDED_COLUMNS
from honeymoney.workspace_derivation import ViewReportInputs
from honeymoney.workspace_index import RegisteredView

VIEW_FILE_NAMES = ("transactions.csv", "review_needed.csv", "report.html")
_MONTH_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")
_PROOF = re.compile(r"[0-9a-f]{64}")
_PROOF_DOMAIN = b"honeymoney.view-unit.v1\0"


class WorkspaceViewError(ValueError):
    """A stable generated-view planning failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PlannedViewFile:
    """One complete-file publication target produced without filesystem access."""

    path: str
    content: bytes | None


@dataclass(frozen=True)
class ViewUnit:
    """The complete serializable output for one calendar-month or undated view."""

    period: str
    transactions_csv: bytes
    review_needed_csv: bytes
    report_html: bytes
    content_proof: str

    def files(self) -> tuple[PlannedViewFile, PlannedViewFile, PlannedViewFile]:
        """Return all files in this logical unit in the fixed publication order."""
        return (
            PlannedViewFile(
                view_relative_path(self.period, "transactions.csv"),
                self.transactions_csv,
            ),
            PlannedViewFile(
                view_relative_path(self.period, "review_needed.csv"),
                self.review_needed_csv,
            ),
            PlannedViewFile(
                view_relative_path(self.period, "report.html"), self.report_html
            ),
        )


@dataclass(frozen=True)
class WorkspaceViewPlan:
    """One pure diff between expected selected views and registered view proofs."""

    selected_periods: tuple[str, ...]
    units: tuple[ViewUnit, ...]
    writes: tuple[ViewUnit, ...]
    removals: tuple[str, ...]
    unchanged: tuple[str, ...]
    next_registered_views: tuple[RegisteredView, ...]

    def publication_files(self) -> tuple[PlannedViewFile, ...]:
        """Return write and removal targets ready for publication adaptation."""
        files: list[PlannedViewFile] = []
        for unit in self.writes:
            files.extend(unit.files())
        for period in self.removals:
            files.extend(
                PlannedViewFile(view_relative_path(period, name), None)
                for name in VIEW_FILE_NAMES
            )
        return tuple(files)


def plan_workspace_views(
    rows: Sequence[Mapping[str, str]],
    selection: PeriodSelection,
    registered_views: Sequence[RegisteredView],
    *,
    content_proof_key: bytes,
    installed_files: Mapping[str, bytes | None] | None = None,
    report_inputs: Mapping[str, ViewReportInputs] | None = None,
) -> WorkspaceViewPlan:
    """Build selected view units and their logical diff without filesystem writes."""
    registered = _registered_view_proofs(registered_views)
    grouped = _rows_by_period(rows)
    implied_periods = tuple(sorted(grouped))
    selected_periods = selection.selected_periods(implied_periods)
    units = tuple(
        build_view_unit(
            period,
            grouped.get(period, ()),
            content_proof_key=content_proof_key,
            report_inputs=(report_inputs or {}).get(period),
        )
        for period in selected_periods
    )
    writes = tuple(
        unit
        for unit in units
        if _unit_requires_write(unit, registered, installed_files)
    )
    unchanged = tuple(
        unit.period
        for unit in units
        if not _unit_requires_write(unit, registered, installed_files)
    )
    removals = (
        tuple(sorted(set(registered) - set(implied_periods)))
        if selection.kind == "all"
        else ()
    )
    next_registered = _next_registered_views(
        registered,
        units,
        cleanup=selection.kind == "all",
    )
    return WorkspaceViewPlan(
        selected_periods=selected_periods,
        units=units,
        writes=writes,
        removals=removals,
        unchanged=unchanged,
        next_registered_views=next_registered,
    )


def plan_automatic_view_refresh(
    previous_rows: Sequence[Mapping[str, str]],
    next_rows: Sequence[Mapping[str, str]],
    registered_views: Sequence[RegisteredView],
    *,
    content_proof_key: bytes,
    installed_files: Mapping[str, bytes | None],
    previous_report_inputs: Mapping[str, ViewReportInputs] | None = None,
    next_report_inputs: Mapping[str, ViewReportInputs] | None = None,
) -> WorkspaceViewPlan:
    """Plan an automatic refresh without repairing unrelated generated output.

    Every prior, next, and registered view is compared in memory.  A period whose
    expected bytes changed remains selected even when its next view is empty.
    This automatic path never removes a registered view.
    """
    registered = _registered_view_proofs(registered_views)
    previous_by_period = _rows_by_period(previous_rows)
    next_by_period = _rows_by_period(next_rows)
    considered = tuple(
        sorted(set(previous_by_period) | set(next_by_period) | set(registered))
    )
    units: list[ViewUnit] = []
    writes: list[ViewUnit] = []
    unchanged: list[str] = []
    for period in considered:
        previous_unit = build_view_unit(
            period,
            previous_by_period.get(period, ()),
            content_proof_key=content_proof_key,
            report_inputs=(previous_report_inputs or {}).get(period),
        )
        next_unit = build_view_unit(
            period,
            next_by_period.get(period, ()),
            content_proof_key=content_proof_key,
            report_inputs=(next_report_inputs or {}).get(period),
        )
        affected = (
            previous_unit.content_proof != next_unit.content_proof
            or period not in registered
        )
        if not affected:
            unchanged.append(period)
            continue
        units.append(next_unit)
        if (
            previous_unit.content_proof != next_unit.content_proof
            or not _unit_matches_installed(next_unit, installed_files)
        ):
            writes.append(next_unit)
        else:
            unchanged.append(period)
    next_registered = _next_registered_views(registered, units, cleanup=False)
    return WorkspaceViewPlan(
        selected_periods=tuple(unit.period for unit in units),
        units=tuple(units),
        writes=tuple(writes),
        removals=(),
        unchanged=tuple(unchanged),
        next_registered_views=next_registered,
    )


def build_view_unit(
    period: str,
    rows: Sequence[Mapping[str, str]],
    *,
    content_proof_key: bytes,
    report_inputs: ViewReportInputs | None = None,
) -> ViewUnit:
    """Serialize a complete generated view, including its deterministic proof."""
    _validate_period(period)
    ordered_rows = ordered_view_rows(rows)
    if any(view_period_for_row(row) != period for row in ordered_rows):
        raise WorkspaceViewError("view_period_mismatch")
    transactions_csv = csv_document(CATEGORIZED_COLUMNS, ordered_rows).encode()
    review_rows = [
        to_review_row(row)
        for row in ordered_rows
        if row.get("needs_review", "") == "true"
    ]
    review_needed_csv = csv_document(REVIEW_NEEDED_COLUMNS, review_rows).encode()
    report_html = build_report_html(
        ordered_rows,
        period,
        source_occurrence_count=(
            report_inputs.source_occurrence_count if report_inputs is not None else None
        ),
        balance_reconciliation=(
            report_inputs.balance_reconciliation if report_inputs is not None else None
        ),
    ).encode()
    files = {
        "transactions.csv": transactions_csv,
        "review_needed.csv": review_needed_csv,
        "report.html": report_html,
    }
    return ViewUnit(
        period=period,
        transactions_csv=transactions_csv,
        review_needed_csv=review_needed_csv,
        report_html=report_html,
        content_proof=view_content_proof(
            period,
            files,
            content_proof_key=content_proof_key,
        ),
    )


def view_content_proof(
    period: str,
    files: Mapping[str, bytes],
    *,
    content_proof_key: bytes,
) -> str:
    """Return the domain-separated HMAC proof for one complete view unit."""
    _validate_period(period)
    if not isinstance(content_proof_key, bytes) or len(content_proof_key) != 32:
        raise WorkspaceViewError("view_content_proof_key_invalid")
    if set(files) != set(VIEW_FILE_NAMES) or any(
        not isinstance(content, bytes) for content in files.values()
    ):
        raise WorkspaceViewError("view_content_invalid")
    framed = bytearray(_PROOF_DOMAIN)
    _frame_bytes(framed, period.encode("ascii"))
    for name in VIEW_FILE_NAMES:
        _frame_bytes(framed, name.encode("ascii"))
        _frame_bytes(framed, files[name])
    return hmac.new(content_proof_key, bytes(framed), hashlib.sha256).hexdigest()


def view_relative_path(period: str, name: str) -> str:
    """Return one checked workspace-relative generated-view file path."""
    _validate_period(period)
    if name not in VIEW_FILE_NAMES:
        raise WorkspaceViewError("view_file_name_invalid")
    return f"views/{period}/{name}"


def _rows_by_period(
    rows: Sequence[Mapping[str, str]],
) -> dict[str, tuple[Mapping[str, str], ...]]:
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for row in rows:
        grouped.setdefault(view_period_for_row(row), []).append(row)
    return {period: tuple(items) for period, items in grouped.items()}


def _registered_view_proofs(
    registered_views: Sequence[RegisteredView],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for registered in registered_views:
        period = registered["period"]
        proof = registered["content_proof"]
        _validate_period(period)
        if _PROOF.fullmatch(proof) is None or period in result:
            raise WorkspaceViewError("registered_view_invalid")
        result[period] = proof
    return result


def _next_registered_views(
    registered: Mapping[str, str],
    units: Sequence[ViewUnit],
    *,
    cleanup: bool,
) -> tuple[RegisteredView, ...]:
    next_proofs = {} if cleanup else dict(registered)
    next_proofs.update({unit.period: unit.content_proof for unit in units})
    return tuple(
        {"period": period, "content_proof": proof}
        for period, proof in sorted(next_proofs.items())
    )


def _unit_matches_installed(
    unit: ViewUnit,
    installed_files: Mapping[str, bytes | None],
) -> bool:
    return all(installed_files.get(file.path) == file.content for file in unit.files())


def _unit_requires_write(
    unit: ViewUnit,
    registered: Mapping[str, str],
    installed_files: Mapping[str, bytes | None] | None,
) -> bool:
    return registered.get(unit.period) != unit.content_proof or (
        installed_files is not None
        and not _unit_matches_installed(unit, installed_files)
    )


def _validate_period(period: str) -> None:
    if period == UNDATED_PERIOD:
        return
    if _MONTH_PERIOD.fullmatch(period) is None:
        raise WorkspaceViewError("view_period_invalid")
    try:
        date(int(period[:4]), int(period[-2:]), 1)
    except ValueError as error:
        raise WorkspaceViewError("view_period_invalid") from error


def _frame_bytes(target: bytearray, value: bytes) -> None:
    target.extend(struct.pack(">Q", len(value)))
    target.extend(value)
