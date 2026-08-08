from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from honeymoney.import_records import (
    ATTEMPT_SCHEMA_VERSION,
    TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
    build_summary,
    initialize_record,
    read_transaction_snapshot,
    safe_source_label,
    write_attempt,
    write_summary,
    write_transaction_snapshot,
)
from honeymoney.overlap import empty_overlap_manifest
from honeymoney.periods import resolve_period_selection
from honeymoney.workspace_derivation import derive_workspace_rows
from honeymoney.workspace_views import VIEW_FILE_NAMES, plan_workspace_views

_SOURCE_COUNT = 1_000
_ROWS_PER_SOURCE = 200
_LARGE_RECORD_ROWS = 5_000
_VIEW_COUNT = 120
_SNAPSHOT_COLUMNS = ("source_record_id", "posting_date")
_CONFIG = {
    "base_currency": "HKD",
    "exchange_rates": {"HKD": 1.0},
    "review_confidence_threshold": 0.8,
    "reconciliation": {"date_window_days": 3},
    "categorization_memory": {"enabled": False},
    "ollama": {"enabled": False},
}


def _source_id(index: int) -> str:
    return f"src_{index:064x}"


def _source_record_id(index: int) -> str:
    return f"rec_{index:064x}"


def _snapshot_rows(start: int, count: int) -> list[dict[str, str]]:
    return [
        {
            "source_record_id": _source_record_id(start + offset),
            "posting_date": "2026-01-15",
        }
        for offset in range(count)
    ]


def _success_attempt(
    source_id: str,
    digest: str,
    transaction_count: int,
) -> dict[str, object]:
    return {
        "schema_version": ATTEMPT_SCHEMA_VERSION,
        "honeymoney_version": "0.2.0",
        "source_id": source_id,
        "source_label": safe_source_label(source_id, "csv"),
        "attempt_number": 1,
        "requested_action": "import",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "outcome": "success",
        "source_revision": "rev_" + "a" * 64,
        "parser_contract": "ext_" + "b" * 64,
        "counts": {"statement_transaction_count": transaction_count},
        "warnings": [],
        "warning_count": 0,
        "omitted_warning_count": 0,
        "error_codes": [],
        "error_count": 0,
        "omitted_error_count": 0,
        "transactions_schema_version": TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
        "transactions_digest": digest,
    }


def _derivation_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source_index in range(_SOURCE_COUNT):
        source_id = _source_id(source_index)
        source_token = f"{source_index:064x}"
        for event_index in range(_ROWS_PER_SOURCE):
            row_index = source_index * _ROWS_PER_SOURCE + event_index
            day = event_index % 28 + 1
            rows.append(
                {
                    "transaction_id": f"txn_{row_index:032x}",
                    "source_id": source_id,
                    "source_namespace_id": f"ns_{source_token}",
                    "source_revision": f"rev_{source_token}",
                    "source_record_id": _source_record_id(row_index),
                    "account_id": "checking",
                    "date": f"2026-01-{day:02d}",
                    "transaction_date": f"2026-01-{day:02d}",
                    "posting_date": f"2026-01-{day:02d}",
                    "original_amount": f"-{event_index + 1}",
                    "original_currency": "HKD",
                    "posted_amount": f"-{event_index + 1}",
                    "posted_currency": "HKD",
                    "merchant": f"Synthetic event {event_index:03d}",
                    "original_description": f"Synthetic event {event_index:03d}",
                }
            )
    return rows


def _periods() -> tuple[str, ...]:
    return tuple(
        f"{2016 + offset // 12:04d}-{offset % 12 + 1:02d}"
        for offset in range(_VIEW_COUNT)
    )


def _view_row(index: int, period: str) -> dict[str, str]:
    return {
        "transaction_id": f"txn_{index:032x}",
        "canonical_group_id": f"ovg_{index:064x}",
        "canonical_slot": "1",
        "posting_date": f"{period}-15",
        "transaction_date": f"{period}-15",
        "account_id": "checking",
        "merchant": "Synthetic merchant",
        "original_description": "Synthetic description",
        "original_amount": "-1",
        "original_currency": "HKD",
        "posted_amount": "-1",
        "posted_currency": "HKD",
        "amount_hkd": "-1",
        "category": "Unknown",
        "flow_type": "unresolved",
        "needs_review": "true",
        "review_reasons": "category_decision;accounting_flow",
    }


class MatureWorkspaceShapesTest(unittest.TestCase):
    def test_derives_200000_rows_across_1000_sources(self) -> None:
        source_rows = _derivation_rows()

        derivation = derive_workspace_rows(
            source_rows,
            empty_overlap_manifest("ovns_" + "c" * 64),
            _CONFIG,
            rules=[],
            corrections={},
            allow_model=False,
        )

        del source_rows
        self.assertEqual(len(derivation.source_rows), _SOURCE_COUNT * _ROWS_PER_SOURCE)
        self.assertEqual(
            len({row["source_id"] for row in derivation.source_rows}), _SOURCE_COUNT
        )
        self.assertEqual(
            set(Counter(row["source_id"] for row in derivation.source_rows).values()),
            {_ROWS_PER_SOURCE},
        )
        self.assertEqual(
            len({row["source_record_id"] for row in derivation.source_rows}),
            _SOURCE_COUNT * _ROWS_PER_SOURCE,
        )
        self.assertEqual(
            len({row["transaction_id"] for row in derivation.source_rows}),
            _SOURCE_COUNT * _ROWS_PER_SOURCE,
        )
        self.assertEqual(len(derivation.rows), _ROWS_PER_SOURCE)
        self.assertEqual(
            len({row["transaction_id"] for row in derivation.rows}), _ROWS_PER_SOURCE
        )
        self.assertTrue(
            all(
                row["source_occurrence_count"] == str(_SOURCE_COUNT)
                for row in derivation.rows
            )
        )
        self.assertEqual(len(derivation.overlap_manifest["groups"]), _ROWS_PER_SOURCE)

    def test_sample_import_record_packages_round_trip_average_shape(self) -> None:
        source_indexes = (0, _SOURCE_COUNT // 2, _SOURCE_COUNT - 1)
        with tempfile.TemporaryDirectory() as temporary:
            records_root = Path(temporary) / "import-records"
            for source_index in source_indexes:
                with self.subTest(source_index=source_index):
                    source_id = _source_id(source_index)
                    expected_rows = _snapshot_rows(
                        source_index * _ROWS_PER_SOURCE,
                        _ROWS_PER_SOURCE,
                    )
                    record = initialize_record(records_root, source_id)
                    snapshot, digest = write_transaction_snapshot(
                        record,
                        _SNAPSHOT_COLUMNS,
                        expected_rows,
                    )
                    write_attempt(
                        record,
                        _success_attempt(source_id, digest, _ROWS_PER_SOURCE),
                    )
                    write_summary(record, source_id)

                    self.assertEqual(
                        read_transaction_snapshot(snapshot, _SNAPSHOT_COLUMNS),
                        expected_rows,
                    )
                    summary = build_summary(record, source_id)
                    self.assertTrue(summary["ready"])
                    self.assertEqual(
                        summary["statement_transaction_count"], _ROWS_PER_SOURCE
                    )
                    self.assertEqual(summary["source_id"], source_id)

    def test_single_import_record_round_trips_5000_transactions(self) -> None:
        source_id = _source_id(_SOURCE_COUNT)
        expected_rows = _snapshot_rows(
            _SOURCE_COUNT * _ROWS_PER_SOURCE, _LARGE_RECORD_ROWS
        )
        with tempfile.TemporaryDirectory() as temporary:
            record = initialize_record(Path(temporary) / "import-records", source_id)
            snapshot, digest = write_transaction_snapshot(
                record,
                _SNAPSHOT_COLUMNS,
                expected_rows,
            )
            write_attempt(
                record, _success_attempt(source_id, digest, _LARGE_RECORD_ROWS)
            )
            write_summary(record, source_id)

            actual_rows = read_transaction_snapshot(snapshot, _SNAPSHOT_COLUMNS)
            summary = build_summary(record, source_id)

        self.assertEqual(actual_rows, expected_rows)
        self.assertEqual(
            actual_rows[0]["source_record_id"], expected_rows[0]["source_record_id"]
        )
        self.assertEqual(
            actual_rows[_LARGE_RECORD_ROWS // 2]["source_record_id"],
            expected_rows[_LARGE_RECORD_ROWS // 2]["source_record_id"],
        )
        self.assertEqual(
            actual_rows[-1]["source_record_id"], expected_rows[-1]["source_record_id"]
        )
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["statement_transaction_count"], _LARGE_RECORD_ROWS)

    def test_serializes_120_monthly_view_units_with_repeatable_bytes(self) -> None:
        periods = _periods()
        rows = [_view_row(index, period) for index, period in enumerate(periods)]
        selection = resolve_period_selection(all_periods=True)

        first = plan_workspace_views(
            rows,
            selection,
            (),
            content_proof_key=b"m" * 32,
        )
        second = plan_workspace_views(
            rows,
            selection,
            (),
            content_proof_key=b"m" * 32,
        )

        first_files = {
            unit.period: tuple(file.content for file in unit.files())
            for unit in first.units
        }
        second_files = {
            unit.period: tuple(file.content for file in unit.files())
            for unit in second.units
        }
        self.assertEqual(first.selected_periods, periods)
        self.assertEqual(tuple(unit.period for unit in first.writes), periods)
        self.assertEqual(tuple(first_files), periods)
        self.assertEqual(first_files, second_files)
        self.assertTrue(
            all(
                all(content is not None for content in files)
                for files in first_files.values()
            )
        )
        self.assertEqual(
            tuple(file.path for file in first.publication_files()),
            tuple(
                f"views/{period}/{name}"
                for period in periods
                for name in VIEW_FILE_NAMES
            ),
        )
