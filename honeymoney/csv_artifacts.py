from __future__ import annotations

import csv
import io
from typing import Iterable, Mapping

# These public columns carry canonical non-text representations. Every other
# public CSV column is treated as text and neutralized at serialization time.
CANONICAL_CSV_COLUMNS = frozenset(
    {
        "original_amount",
        "posted_amount",
        "amount_hkd",
        "statement_opening_balance",
        "statement_closing_balance",
        "reconciliation_confidence",
        "confidence",
        "needs_review",
        "source_page",
        "source_row",
    }
)

_FORMULA_MARKERS = ("=", "+", "-", "@")
_CONTROL_MARKERS = ("\t", "\r")
_ESCAPE_PREFIX = "'"


def csv_document(columns: list[str], rows: Iterable[Mapping[str, str]]) -> str:
    """Serialize a public CSV document with spreadsheet-safe text cells."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {
            column: spreadsheet_safe_cell(
                column,
                "" if row.get(column) is None else str(row.get(column)),
            )
            for column in columns
        }
        for row in rows
    )
    return buffer.getvalue()


def spreadsheet_safe_cell(column: str, value: str) -> str:
    """Return a reversible spreadsheet display value for one public CSV cell."""
    if column in CANONICAL_CSV_COLUMNS:
        return value
    if value.startswith(_ESCAPE_PREFIX) or _formula_triggering_text(value):
        return f"{_ESCAPE_PREFIX}{value}"
    return value


def canonical_csv_cell(column: str, value: str) -> str:
    """Restore canonical text from a Honeymoney-authored public CSV cell."""
    if column in CANONICAL_CSV_COLUMNS or not value.startswith(_ESCAPE_PREFIX):
        return value
    remainder = value[len(_ESCAPE_PREFIX) :]
    if remainder.startswith(_ESCAPE_PREFIX) or _formula_triggering_text(remainder):
        return remainder
    return value


def _formula_triggering_text(value: str) -> bool:
    stripped = value.lstrip()
    leading_whitespace = value[: len(value) - len(stripped)]
    if any(marker in leading_whitespace for marker in _CONTROL_MARKERS):
        return True
    return stripped.startswith(_FORMULA_MARKERS)
