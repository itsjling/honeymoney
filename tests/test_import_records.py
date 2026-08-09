from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from honeymoney.import_records import (
    ImportRecordError,
    attempt_document,
    build_summary,
    initialize_record,
    load_attempts,
    next_attempt_number,
    read_transaction_snapshot,
    safe_source_label,
    transaction_snapshot_document,
    write_attempt,
    write_summary,
    write_transaction_snapshot,
)

SOURCE_ID = "src_" + "a" * 64
RECORD_ID = "rec_" + "b" * 64


def _report(
    number: int, *, outcome: str, digest: str | None = None
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "honeymoney_version": "0.2.0",
        "source_id": SOURCE_ID,
        "source_label": safe_source_label(SOURCE_ID, "csv"),
        "attempt_number": number,
        "requested_action": "import",
        "started_at": "2026-08-08T00:00:00Z",
        "finished_at": "2026-08-08T00:00:01Z",
        "outcome": outcome,
        "source_revision": "c" * 64,
        "parser_contract": "d" * 64,
        "counts": {"statement_transaction_count": 1},
        "warnings": [],
        "warning_count": 0,
        "omitted_warning_count": 0,
        "error_codes": [] if outcome == "success" else ["parse_failed"],
        "error_count": 0 if outcome == "success" else 1,
        "omitted_error_count": 0,
    }
    if outcome == "success":
        value.update(
            transactions_schema_version=1,
            transactions_digest=digest,
        )
    return value


class ImportRecordsTest(unittest.TestCase):
    def test_private_record_snapshot_attempt_and_summary_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "records"
            record = initialize_record(root, SOURCE_ID)
            columns = ["source_record_id", "description"]
            rows = [{"source_record_id": RECORD_ID, "description": "Synthetic"}]
            snapshot, digest = write_transaction_snapshot(record, columns, rows)
            report = _report(1, outcome="success", digest=digest)
            attempt = write_attempt(record, report)
            summary_path = write_summary(record, SOURCE_ID)

            self.assertEqual(load_attempts(record), [report])
            self.assertEqual(next_attempt_number(record), 2)
            self.assertEqual(
                json.loads(summary_path.read_text()),
                {
                    "schema_version": 1,
                    "source_id": SOURCE_ID,
                    "source_label": safe_source_label(SOURCE_ID, "csv"),
                    "ready": True,
                    "current_attempt_number": 1,
                    "statement_transaction_count": 1,
                },
            )
            for path in (root, record, record / "attempts"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            for path in (snapshot, attempt, summary_path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_initial_failure_is_visible_but_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = initialize_record(Path(temporary), SOURCE_ID)
            write_attempt(record, _report(1, outcome="failure"))
            summary = build_summary(record, SOURCE_ID)
            self.assertFalse(summary["ready"])
            self.assertIsNone(summary["current_attempt_number"])
            self.assertEqual(summary["statement_transaction_count"], 0)

    def test_attempt_is_canonical_bounded_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = initialize_record(Path(temporary), SOURCE_ID)
            failed = _report(1, outcome="failure")
            path = write_attempt(record, failed)
            before = path.read_bytes()
            write_attempt(record, dict(reversed(list(failed.items()))))
            self.assertEqual(path.read_bytes(), before)
            changed = dict(failed, source_label=safe_source_label(SOURCE_ID, "pdf"))
            with self.assertRaisesRegex(
                ImportRecordError, "attempt_immutable_conflict"
            ):
                write_attempt(record, changed)
            unsafe_label = dict(failed, source_label="private\nsource.csv")
            with self.assertRaisesRegex(ImportRecordError, "source_label_invalid"):
                attempt_document(unsafe_label)
            huge = dict(failed, warnings=["x" * 65536], warning_count=1)
            with self.assertRaisesRegex(ImportRecordError, "attempt_report_too_large"):
                attempt_document(huge)

    def test_history_rejects_gaps_noncanonical_bytes_and_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = initialize_record(Path(temporary), SOURCE_ID)
            report = _report(2, outcome="failure")
            path = record / "attempts" / "00000002.json"
            path.write_text(attempt_document(report))
            with self.assertRaisesRegex(ImportRecordError, "attempt_history_invalid"):
                load_attempts(record)
            path.unlink()
            bad = _report(1, outcome="failure")
            bad["schema_version"] = 2
            with self.assertRaisesRegex(
                ImportRecordError, "attempt_schema_unsupported"
            ):
                attempt_document(bad)

    def test_snapshot_requires_only_declared_fields_and_unique_record_ids(self) -> None:
        columns = ["source_record_id", "description"]
        row = {"source_record_id": RECORD_ID, "description": "Synthetic"}
        self.assertEqual(
            transaction_snapshot_document(columns, []),
            "source_record_id,description\n",
        )
        with self.assertRaisesRegex(ImportRecordError, "transaction_snapshot_invalid"):
            transaction_snapshot_document(columns, [row, row])
        with self.assertRaisesRegex(ImportRecordError, "transaction_snapshot_invalid"):
            transaction_snapshot_document(columns, [dict(row, extra="bad")])

    def test_snapshot_reader_requires_exact_header_and_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transactions.csv"
            path.write_text(
                transaction_snapshot_document(
                    ["source_record_id", "description"],
                    [{"source_record_id": RECORD_ID, "description": "Synthetic"}],
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                read_transaction_snapshot(path, ["source_record_id", "description"]),
                [{"source_record_id": RECORD_ID, "description": "Synthetic"}],
            )
            path.write_text(
                "description,source_record_id\nSynthetic," + RECORD_ID + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ImportRecordError, "transaction_snapshot_invalid"
            ):
                read_transaction_snapshot(path, ["source_record_id", "description"])

    def test_summary_fails_when_success_digest_disagrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = initialize_record(Path(temporary), SOURCE_ID)
            write_transaction_snapshot(record, ["source_record_id"], [])
            write_attempt(record, _report(1, outcome="success", digest="0" * 64))
            with self.assertRaisesRegex(
                ImportRecordError, "import_record_disagreement"
            ):
                build_summary(record, SOURCE_ID)

    def test_symlink_record_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            link = root / "records"
            os.symlink(outside, link)
            with self.assertRaises(OSError):
                initialize_record(link, SOURCE_ID)


if __name__ == "__main__":
    unittest.main()
