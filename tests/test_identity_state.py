from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.corrections import apply_correction_operation, ledger_output_documents
from honeymoney.csv_artifacts import csv_document
from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    IdentityError,
    extractor_contract_id,
    manifest_document,
    ownership_record,
    record_fingerprint,
    source_id,
    source_namespace_id,
    source_ownership,
    source_revision,
)
from honeymoney.identity_state import (
    LEGACY_CATEGORIZED_COLUMNS,
    identity_manifest_path,
    load_identity_state,
)
from honeymoney.persistence import persist_generation, recover_generation
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.schema import CATEGORIZED_COLUMNS


class IdentityStateTest(unittest.TestCase):
    def _row(self, transaction_id: str = "txn_0123456789abcdef") -> dict[str, str]:
        row = {column: "" for column in CATEGORIZED_COLUMNS}
        row.update(
            {
                "transaction_id": transaction_id,
                "date": "2026-06-18",
                "transaction_date": "2026-06-18",
                "account_id": "synthetic-checking",
                "account_type": "bank",
                "original_amount": "-12.00",
                "original_currency": "HKD",
                "posted_amount": "-12.00",
                "posted_currency": "HKD",
                "amount_hkd": "-12.00",
                "merchant": "SYNTHETIC SHOP",
                "original_description": "SYNTHETIC SHOP",
                "category": "Unknown",
                "flow_type": "unresolved",
                "owner": "Household",
                "confidence": "0.00",
                "needs_review": "true",
                "source_file": "synthetic.csv",
            }
        )
        return row

    def _v2_state(self) -> tuple[dict[str, str], dict[str, object]]:
        row = self._row()
        namespace = source_namespace_id("workspace", "synthetic.csv")
        source = source_id(namespace)
        revision = source_revision(b"synthetic statement\n")
        contract = extractor_contract_id(
            1,
            {"id": "synthetic", "csv": {"columns": {"date": "Date"}}},
        )
        fingerprint = record_fingerprint(row)
        record = ownership_record(
            source_id_value=source,
            fingerprint=fingerprint,
            origin=AllocationOrigin(revision, contract, AllocationLocator(1, (1,)), 1),
        )
        row.update(
            {
                "transaction_id": record["transaction_id"],
                "source_id": source,
                "source_namespace_id": namespace,
                "source_revision": revision,
                "source_record_id": record["source_record_id"],
            }
        )
        return row, {
            "schema_version": 1,
            "sources": [
                source_ownership(
                    source_id_value=source,
                    namespace_id=namespace,
                    revision=revision,
                    contract_id=contract,
                    records=[record],
                )
            ],
        }

    def _write_v2_state(self, path: Path) -> tuple[dict[str, str], str]:
        row, manifest = self._v2_state()
        document = manifest_document(manifest)
        path.write_text(csv_document(CATEGORIZED_COLUMNS, [row]), encoding="utf-8")
        identity_manifest_path(path).write_text(document, encoding="utf-8")
        return row, document

    def test_pristine_state_and_legacy_bootstrap_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            pristine = load_identity_state(path)
            self.assertEqual(pristine.rows, [])
            self.assertFalse(pristine.bootstrap_required)

            legacy = self._row()
            path.write_text(
                csv_document(
                    LEGACY_CATEGORIZED_COLUMNS,
                    [{key: legacy[key] for key in LEGACY_CATEGORIZED_COLUMNS}],
                ),
                encoding="utf-8",
            )
            migrated = load_identity_state(path)
            self.assertTrue(migrated.bootstrap_required)
            self.assertEqual(migrated.rows[0]["source_id"], "")
            self.assertEqual(migrated.manifest["sources"], [])

            path.write_text(
                csv_document(LEGACY_CATEGORIZED_COLUMNS, []), encoding="utf-8"
            )
            empty_legacy = load_identity_state(path)
            self.assertTrue(empty_legacy.bootstrap_required)
            self.assertEqual(empty_legacy.rows, [])

    def test_v2_or_partial_identity_header_without_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            path.write_text(csv_document(CATEGORIZED_COLUMNS, []), encoding="utf-8")
            with self.assertRaisesRegex(IdentityError, "identity_manifest_missing"):
                load_identity_state(path)

            path.write_text(
                csv_document(["transaction_id", "source_id", "date"], []),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IdentityError, "identity_manifest_missing"):
                load_identity_state(path)

    def test_manifest_without_ledger_and_row_disagreement_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            _, document = self._v2_state()
            identity_manifest_path(path).write_text(
                manifest_document(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(IdentityError, "identity_manifest_invalid"):
                load_identity_state(path)

            row, document = self._write_v2_state(path)
            row["source_revision"] = "rev_" + "0" * 64
            path.write_text(csv_document(CATEGORIZED_COLUMNS, [row]), encoding="utf-8")
            with self.assertRaisesRegex(IdentityError, "identity_manifest_invalid"):
                load_identity_state(path)
            self.assertTrue(document.endswith("\n"))

    def test_mutable_writes_preserve_manifest_bytes_and_publish_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "categorized.csv"
            row, document = self._write_v2_state(path)
            row["category"] = "Dining"
            documents = ledger_output_documents(path, [row])
            self.assertEqual(documents[identity_manifest_path(path)], document)

            corrections = root / "corrections.csv"
            corrections.write_text("transaction_id,category\n", encoding="utf-8")
            result = apply_correction_operation(
                {"corrections": str(corrections)},
                path,
                {row["transaction_id"]: {"category": "Dining"}},
            )
            self.assertEqual(result.applied_count, 1)
            self.assertEqual(
                identity_manifest_path(path).read_text(encoding="utf-8"), document
            )

    def test_generation_rollback_and_recovery_include_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            row, old_manifest = self._write_v2_state(path)
            row["category"] = "Dining"
            new_documents = ledger_output_documents(path, [row])

            import honeymoney.persistence as persistence

            original_replace = persistence._replace_from_retained

            def fail_at_ledger(entry: dict[str, object], source: str) -> None:
                if entry["target"] == str(path.resolve()) and source == "staged":
                    raise OSError("synthetic write failure")
                original_replace(entry, source)

            with patch.object(persistence, "_replace_from_retained", fail_at_ledger):
                with self.assertRaisesRegex(OSError, "synthetic write failure"):
                    persist_generation(path, new_documents)
            self.assertEqual(
                identity_manifest_path(path).read_text(encoding="utf-8"), old_manifest
            )

            with patch.object(
                persistence, "_finish_generation", side_effect=OSError("stop")
            ):
                with self.assertRaisesRegex(OSError, "stop"):
                    persist_generation(path, new_documents)
            recover_generation(path)
            self.assertEqual(
                identity_manifest_path(path).read_text(encoding="utf-8"),
                new_documents[identity_manifest_path(path)],
            )

    def test_ambiguous_legacy_correction_and_reconciliation_leave_rows_intact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "categorized.csv"
            first = self._row()
            second = self._row()
            second["account_id"] = "synthetic-savings"
            second["amount_hkd"] = "12.00"
            path.write_text(
                csv_document(
                    LEGACY_CATEGORIZED_COLUMNS,
                    [
                        {key: first[key] for key in LEGACY_CATEGORIZED_COLUMNS},
                        {key: second[key] for key in LEGACY_CATEGORIZED_COLUMNS},
                    ],
                ),
                encoding="utf-8",
            )
            corrections = path.parent / "corrections.csv"
            corrections.write_text("transaction_id,category\n", encoding="utf-8")
            with self.assertRaisesRegex(
                IdentityError, "identity_legacy_transaction_id_ambiguous"
            ):
                apply_correction_operation(
                    {"corrections": str(corrections)},
                    path,
                    {first["transaction_id"]: {"category": "Dining"}},
                )

            before = [dict(first), dict(second)]
            reconcile_ledger([first, second], {})
            self.assertEqual([first, second], before)
