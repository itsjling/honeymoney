from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

# These public columns carry canonical non-text representations. Every other
# public CSV column is treated as text and neutralized at serialization time.
CANONICAL_CSV_COLUMNS = frozenset(
    {
        "identity_version",
        "identity_occurrence",
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
# Apostrophe plus the invisible Unicode tag for "honeymoney-csv-v1". The
# product-and-version-specific tag makes each encoded cell self-identifying
# without changing the document header or relying on ambiguous quote styles.
HONEYMONEY_CSV_ESCAPE_V1 = (
    "'"
    "\U000e0068\U000e006f\U000e006e\U000e0065\U000e0079"
    "\U000e006d\U000e006f\U000e006e\U000e0065\U000e0079"
    "\U000e002d\U000e0063\U000e0073\U000e0076\U000e002d"
    "\U000e0076\U000e0031\U000e007f"
)


@dataclass(frozen=True)
class CsvArtifact:
    rows: list[dict[str, str]]
    encoded_cells: frozenset[tuple[int, str]]


def csv_document(columns: list[str], rows: Iterable[Mapping[str, str]]) -> str:
    """Serialize a public CSV document with spreadsheet-safe text cells."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=columns,
        extrasaction="ignore",
    )
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


def read_csv_artifact(path: Path, columns: list[str]) -> CsvArtifact:
    """Read a public CSV and decode self-identifying spreadsheet-safe cells."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = []
        encoded_cells: set[tuple[int, str]] = set()
        for row_index, row in enumerate(csv.DictReader(handle)):
            for column in columns:
                if column not in CANONICAL_CSV_COLUMNS and (
                    row.get(column) or ""
                ).startswith(HONEYMONEY_CSV_ESCAPE_V1):
                    encoded_cells.add((row_index, column))
            rows.append(
                {
                    column: canonical_csv_cell(column, row.get(column) or "")
                    for column in columns
                }
            )
    return CsvArtifact(rows, frozenset(encoded_cells))


def spreadsheet_safe_cell(column: str, value: str) -> str:
    """Return a reversible spreadsheet display value for one public CSV cell."""
    if column in CANONICAL_CSV_COLUMNS:
        return value
    if value.startswith(HONEYMONEY_CSV_ESCAPE_V1) or _formula_triggering_text(value):
        return f"{HONEYMONEY_CSV_ESCAPE_V1}{value}"
    return value


def canonical_csv_cell(column: str, value: str) -> str:
    """Restore canonical text from a Honeymoney-authored public CSV cell."""
    if column in CANONICAL_CSV_COLUMNS or not value.startswith(
        HONEYMONEY_CSV_ESCAPE_V1
    ):
        return value
    return value[len(HONEYMONEY_CSV_ESCAPE_V1) :]


def _formula_triggering_text(value: str) -> bool:
    stripped = value.lstrip()
    leading_whitespace = value[: len(value) - len(stripped)]
    if any(marker in leading_whitespace for marker in _CONTROL_MARKERS):
        return True
    return stripped.startswith(_FORMULA_MARKERS)
