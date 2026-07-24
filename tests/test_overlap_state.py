from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from honeymoney.corrections import ledger_output_documents
from honeymoney.csv_artifacts import csv_document
from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    extractor_contract_id,
    manifest_document,
    ownership_record,
    record_fingerprint,
    source_id,
    source_namespace_id,
    source_ownership,
    source_revision,
)
from honeymoney.identity_state import identity_manifest_path, load_identity_state
from honeymoney.overlap import (
    canonicalize_overlaps,
    overlap_manifest_path,
    source_occurrences_path,
)
from honeymoney.persistence import persist_generation
from honeymoney.schema import CATEGORIZED_COLUMNS, SOURCE_OCCURRENCE_COLUMNS


def _source_row(name: str, locator: str) -> tuple[dict[str, str], dict[str, object]]:
    row = {column: "" for column in SOURCE_OCCURRENCE_COLUMNS}
    row.update(
        {
            "date": "2026-05-04",
            "transaction_date": "2026-05-04",
            "account_id": "synthetic-card",
            "account_type": "credit_card",
            "original_amount": "-12.00",
            "original_currency": "HKD",
            "posted_amount": "-12.00",
            "posted_currency": "HKD",
            "amount_hkd": "-12.00",
            "merchant": "SYNTHETIC OVERLAP",
            "original_description": "SYNTHETIC OVERLAP",
            "category": "Dining",
            "flow_type": "expense",
            "flow_source": "correction",
            "owner": "Household",
            "confidence": "1.00",
            "needs_review": "false",
            "source_file": name,
            "source_row": "2",
        }
    )
    namespace = source_namespace_id("workspace", locator)
    source = source_id(namespace)
    revision = source_revision((name + "\n").encode())
    contract = extractor_contract_id(
        1, {"id": "synthetic", "csv": {"columns": {"date": "Date"}}}
    )
    fingerprint = record_fingerprint(row)
    owner = ownership_record(
        source_id_value=source,
        fingerprint=fingerprint,
        origin=AllocationOrigin(revision, contract, AllocationLocator(1, (2,)), 1),
    )
    row.update(
        {
            "transaction_id": owner["transaction_id"],
            "source_id": source,
            "source_namespace_id": namespace,
            "source_revision": revision,
            "source_record_id": owner["source_record_id"],
        }
    )
    manifest_source = source_ownership(
        source_id_value=source,
        namespace_id=namespace,
        revision=revision,
        contract_id=contract,
        records=[owner],
    )
    return row, manifest_source


class OverlapWorkspaceStateTest(unittest.TestCase):
    def _write_issue_31_state(self, path: Path) -> None:
        first, first_source = _source_row("a.csv", "a.csv")
        second, second_source = _source_row("b.csv", "b.csv")
        manifest = {
            "schema_version": 1,
            "sources": [first_source, second_source],
        }
        path.write_text(
            csv_document(SOURCE_OCCURRENCE_COLUMNS, [first, second]),
            encoding="utf-8",
        )
        identity_manifest_path(path).write_text(
            manifest_document(manifest), encoding="utf-8"
        )

    def test_exact_issue_31_state_migrates_in_memory_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)

            state = load_identity_state(path)

            self.assertTrue(state.canonical_migration_required)
            self.assertEqual(len(state.source_rows), 2)
            self.assertEqual(len(state.rows), 2)
            self.assertEqual(
                {row["transaction_id"] for row in state.rows},
                {row["transaction_id"] for row in state.source_rows},
            )
            self.assertRegex(
                state.overlap_manifest["namespace_key"], r"^ovns_[0-9a-f]{64}$"
            )
            self.assertFalse(source_occurrences_path(path).exists())
            self.assertFalse(overlap_manifest_path(path).exists())

    def test_complete_state_publishes_and_reloads_both_hidden_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            self._write_issue_31_state(path)
            migrated = load_identity_state(path)
            canonical = canonicalize_overlaps(
                migrated.source_rows, [], migrated.overlap_manifest
            )

            files = ledger_output_documents(
                path,
                canonical.rows,
                identity_manifest=migrated.manifest,
                source_occurrences=migrated.source_rows,
                overlap_manifest=canonical.manifest,
            )
            persist_generation(path, files)
            loaded = load_identity_state(path)

            self.assertFalse(loaded.canonical_migration_required)
            self.assertEqual(loaded.rows, canonical.rows)
            self.assertEqual(loaded.source_rows, migrated.source_rows)
            self.assertEqual(loaded.overlap_manifest, canonical.manifest)
            self.assertTrue(source_occurrences_path(path).exists())
            self.assertTrue(overlap_manifest_path(path).exists())
            self.assertEqual(
                list(loaded.rows[0]),
                CATEGORIZED_COLUMNS,
            )


if __name__ == "__main__":
    unittest.main()
